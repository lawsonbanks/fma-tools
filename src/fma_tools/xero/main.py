"""fma xero -- sign in once per organisation, then pull them all at a date you name.

    fma xero config --client-id <id>         once per Mac: which registered app this is
    fma xero auth [--key NSW]                once per organisation: a person clicks Allow
    fma xero accounts                        what is connected, and is the sign-in alive
    fma xero pull --as-at 2026-06-30 --all --out <folder>
    fma xero group --pull <folder> --as-at 2026-06-30 --out <file.xlsx>
    fma xero disconnect --org NSW            withdraw one organisation from this side

Read-only by construction: the scopes requested (oauth.SCOPES) contain nothing that can
change a ledger, and the only calls that are not GETs are the sign-in itself and the
removal of a connection.

`auth` is the one step in all of fma that needs a person. An agent runs the command;
the person sees Xero's own sign-in page in their browser. No password, code or token
ever passes through the agent, and none is printed.
"""

from __future__ import annotations

import re
import secrets
import sys
import time
import webbrowser
from datetime import datetime, timezone

from ..errors import EnvProblem, InputProblem, Refusal, ToolError
from . import group as group_mod
from . import oauth, pull as pull_mod, store, tenants
from .client import NETWORK_FIX, XeroClient
from .transport import default_transport

class _CodeUnspent(EnvProblem):
    """The code never reached Xero, so the same address can be given again while the
    code lasts. Any failure AFTER the exchange is not this: the code is spent."""


_KEY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,18}[A-Za-z0-9])?$")
_KEY_RULE = ("a key is 1 to 20 letters, digits, - or _, beginning and ending with a "
             "letter or digit")
_PENDING_MINUTES = 15


def add_arguments(p) -> None:
    sub = p.add_subparsers(dest="action", required=True)

    sp = sub.add_parser("config", help="record which registered Xero app this Mac uses")
    sp.add_argument("--client-id", help="the app's client id from developer.xero.com")
    sp.add_argument("--port", type=int, help=f"callback port (default {oauth.DEFAULT_PORT}); "
                                             "must match the app's redirect address")

    sp = sub.add_parser("auth", help="connect ONE organisation: a person clicks Allow")
    sp.add_argument("--expect-org", help="refuse unless the organisation granted has "
                                         "exactly this Xero name")
    sp.add_argument("--key", help="short key for file names, e.g. NSW")
    sp.add_argument("--paste", action="store_true",
                    help="print the link instead of listening; finish with --redirect")
    sp.add_argument("--redirect", help="the address the browser landed on after Allow "
                                       "(finishes a --paste)")
    sp.add_argument("--no-browser", action="store_true",
                    help="do not open a browser; the link is on stderr")
    sp.add_argument("--timeout", type=int, default=300,
                    help="seconds to wait for the click (default 300)")
    sp.add_argument("--without", action="append", metavar="SCOPE",
                    help="leave one permission out of the request (repeatable), for "
                         "when Xero rejects it for this app")

    sp = sub.add_parser("accounts", help="what is connected, and is each sign-in alive")
    sp.add_argument("--set-key", nargs=2, metavar=("ORG", "KEY"),
                    help="give an organisation its short key")

    sp = sub.add_parser("disconnect", help="remove one organisation's connection")
    sp.add_argument("--org", help="the organisation, by key or name")

    sp = sub.add_parser("pull", help="every named organisation's statements at one "
                                     "typed date")
    pull_mod.add_arguments(sp)

    sp = sub.add_parser("group", help="the organisations of one pull, side by side")
    group_mod.add_arguments(sp)


# -- config --------------------------------------------------------------------------

def _mask(client_id: str) -> str:
    return (client_id[:4] + "…" + client_id[-2:]) if len(client_id) > 8 else "…"


def _config(args) -> tuple[dict, list[str]]:
    current = store.load(store.APP)
    if args.client_id:
        cid = args.client_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{16,64}", cid):
            raise InputProblem("CLIENT_ID_INVALID",
                               "that does not look like a Xero client id (letters and "
                               "digits only); copy it from the app's page at "
                               "developer.xero.com")
        current["client_id"] = cid
    if args.port:
        if not 1024 <= args.port <= 65535:
            raise InputProblem("PORT_INVALID", "--port must be between 1024 and 65535")
        current["port"] = args.port
    if not current.get("client_id"):
        raise EnvProblem("XERO_NOT_CONFIGURED", "no Xero app is configured on this Mac",
                         fix=store.CONFIG_FIX)
    current.setdefault("port", oauth.DEFAULT_PORT)
    if args.client_id or args.port:
        store.save(store.APP, current)
    return ({"action": "config", "client_id": _mask(current["client_id"]),
             "port": current["port"],
             "redirect_address": oauth.redirect_uri(current["port"]),
             "folder": str(store.config_dir()),
             "note": "the redirect address must be registered with the app exactly as "
                     "shown"}, [])


# -- auth ----------------------------------------------------------------------------

def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _key_holder(key: str, rows: dict, own_tenant: str | None = None) -> dict | None:
    """The other organisation that already answers to `key`, if any."""
    wanted = tenants.file_key({"key": key}).casefold()
    for tid, r in rows.items():
        if tid != own_tenant and tenants.file_key(r).casefold() == wanted:
            return r
    return None


def _check_key(key: str | None, rows: dict, own_tenant: str | None = None) -> None:
    if key is None:
        return
    if not _KEY_RE.match(key):
        raise InputProblem("KEY_INVALID", _KEY_RULE)
    holder = _key_holder(key, rows, own_tenant)
    if holder is not None:
        raise Refusal("KEY_IN_USE", f"the key {key!r} already names {holder.get('name')!r}")


def _auth(args) -> tuple[dict, list[str]]:
    app = store.app()
    port = int(app.get("port") or oauth.DEFAULT_PORT)
    if args.key:
        # Checked before anyone is asked to click: after Allow it is too late to refuse
        # without leaving an organisation connected at Xero and unknown here.
        if not _KEY_RE.match(args.key):
            raise InputProblem("KEY_INVALID", _KEY_RULE)
        holder = _key_holder(args.key, tenants.registry())
        if holder is not None and (holder.get("name") or "").casefold() != \
                (args.expect_org or "").casefold():
            raise Refusal("KEY_IN_USE",
                          f"the key {args.key!r} already names {holder.get('name')!r}")

    dropped = set(args.without or [])
    unknown = sorted(dropped - set(oauth.SCOPES))
    if unknown:
        raise InputProblem("SCOPE_UNKNOWN",
                           f"--without names a permission this tool never asks for: "
                           f"{', '.join(unknown)}")
    if "offline_access" in dropped:
        raise Refusal("SCOPE_REQUIRED",
                      "offline_access is what lets the sign-in last past half an hour; "
                      "it cannot be left out")
    scopes = tuple(sc for sc in oauth.SCOPES if sc not in dropped)

    if args.redirect:
        pending = store.load(store.PENDING)
        if not pending.get("state"):
            raise Refusal("NO_PENDING_CONSENT",
                          "there is no consent waiting for an address; start one with: "
                          "fma xero auth --paste")
        if time.time() - float(pending.get("created_at", 0)) > _PENDING_MINUTES * 60:
            store.delete(store.PENDING)
            raise Refusal("PENDING_CONSENT_EXPIRED",
                          "that consent was started too long ago; start again with: "
                          "fma xero auth --paste")
        answer = oauth.parse_redirect(args.redirect)
        try:
            result = _complete(answer, pending["state"], pending["verifier"],
                               int(pending.get("port") or port), app["client_id"],
                               args.expect_org or pending.get("expect_org"),
                               args.key or pending.get("key"),
                               tuple(pending.get("scopes") or oauth.SCOPES))
        except _CodeUnspent:
            raise                               # the pending consent keeps waiting
        except ToolError:
            store.delete(store.PENDING)         # used, or can never be used
            raise
        store.delete(store.PENDING)
        return result

    verifier, challenge = oauth.new_pkce()
    state = secrets.token_urlsafe(24)
    url = oauth.consent_url(app["client_id"], port, state, challenge, scopes)

    if args.paste:
        store.save(store.PENDING, {"state": state, "verifier": verifier, "port": port,
                                   "expect_org": args.expect_org, "key": args.key,
                                   "scopes": list(scopes), "created_at": time.time()})
        _say("Open this link, sign in to Xero, pick ONE organisation and click Allow:")
        _say(url)
        return ({"action": "auth", "step": "link", "link": url,
                 "next": "after Allow the browser lands on an address that will not "
                         "load; copy that whole address and run: "
                         "fma xero auth --redirect '<the address>'",
                 "minutes_to_finish": 5}, [])

    try:
        listener = oauth.CallbackListener(port)
    except OSError as e:
        raise EnvProblem("XERO_PORT_BUSY", str(e),
                         fix="close whatever holds that port and run the command again")
    try:
        _say("Xero's sign-in page is opening in the browser. Sign in, pick ONE "
             "organisation, click Allow.")
        _say(f"If no page opened, open this link yourself:\n{url}")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:                       # no browser is not a failure here
                pass
        answer = listener.wait(max(5, args.timeout))
    finally:
        listener.close()
    if not answer:
        raise Refusal("CONSENT_TIMED_OUT",
                      f"nobody clicked Allow within {args.timeout} seconds; nothing was "
                      "connected. Run the command again when ready.")
    return _complete(answer, state, verifier, port, app["client_id"], args.expect_org,
                     args.key, scopes)


def _complete(answer: dict, state: str, verifier: str, port: int, client_id: str,
              expect_org: str | None, key: str | None,
              asked: tuple = oauth.SCOPES) -> tuple[dict, list[str]]:
    if answer.get("error"):
        why = answer.get("error_description") or answer["error"]
        if answer["error"] == "invalid_scope":
            raise Refusal("SCOPE_REJECTED",
                          f"Xero rejected a requested permission ({why}); nothing was "
                          "connected. Run again leaving the rejected one out with "
                          "--without <scope>, and report it: the list in this build "
                          "needs correcting.")
        raise Refusal("CONSENT_DENIED", f"Xero did not grant access ({why}); nothing "
                                        "was connected")
    if not answer.get("state") or answer.get("state") != state:
        raise Refusal("STATE_MISMATCH",
                      "the answer did not come from the consent this command started; "
                      "nothing was connected")
    if not answer.get("code"):
        raise Refusal("NO_CODE", "Xero's answer carried no code; nothing was connected")

    transport = default_transport()
    try:
        tok = oauth.exchange_code(transport, client_id, answer["code"], port, verifier)
    except oauth.TokenRejected as e:
        raise Refusal("CODE_REJECTED",
                      f"Xero would not accept the code ({e.reason}). A code works once "
                      "and for five minutes; run the command again.")
    except oauth.TokenUnavailable as e:
        raise _CodeUnspent("XERO_UNREACHABLE", f"could not reach Xero to finish the "
                                               f"sign-in ({e})", fix=NETWORK_FIX)

    access = oauth.claims(tok.get("access_token"))
    event = access.get("authentication_event_id")
    user_id = access.get("xero_userid") or access.get("sub")
    email = oauth.claims(tok.get("id_token")).get("email") or ""
    if not event or not user_id or not tok.get("refresh_token"):
        raise Refusal("SIGN_IN_INCOMPLETE",
                      "Xero's sign-in did not say which login or which consent it "
                      "belongs to, or carried no way to stay signed in; nothing was "
                      "saved")

    client = XeroClient(transport=transport)
    rows = tenants.registry()
    warnings: list[str] = []
    now = time.time()
    known = {tid: r for tid, r in rows.items() if r.get("user_id") == user_id}

    def save_sign_in() -> None:
        tokens = store.load(store.TOKENS)
        tokens.setdefault("users", {})[user_id] = {**oauth.stamp(tok, now), "email": email}
        store.save(store.TOKENS, tokens)

    def forget_unused_logins() -> None:
        """A login no organisation here relies on would lapse unnoticed and then fail
        every check with a fix that cannot cure it."""
        tokens = store.load(store.TOKENS)
        needed = {r.get("user_id") for r in tenants.registry().values()}
        stale = [u for u in (tokens.get("users") or {}) if u not in needed]
        for u in stale:
            tokens["users"].pop(u)
        if stale:
            store.save(store.TOKENS, tokens)

    def ask(auth_event_id: str | None = None) -> list[dict]:
        """Ask Xero what this login holds. The code is already spent by now, so a
        failure here must not waste the sign-in: for a login already held here it is
        saved first (it serves the organisations that login holds whatever else
        happened), and the message says what was and was not done."""
        try:
            return client.connections(user_id, auth_event_id=auth_event_id,
                                      access_token=tok["access_token"])
        except EnvProblem as e:
            why = "; ".join(p["message"] for p in e.problems)
            if known:
                with store.locked():
                    save_sign_in()
                raise EnvProblem(
                    "XERO_UNREACHABLE",
                    "the sign-in was saved, so the organisations this login already "
                    "holds here are served again; but Xero could not then be asked "
                    f"which organisation this consent granted ({why}). See what is "
                    "live, and add a missing one with: fma xero auth --expect-org "
                    "\"<its name>\"", fix="fma xero accounts")
            raise EnvProblem(
                "XERO_UNREACHABLE",
                "Xero accepted the sign-in but could not then be asked which "
                f"organisation it granted ({why}), so nothing was saved here. Run the "
                "command again: an organisation already connected is renewed, not "
                "connected twice", fix=store.AUTH_FIX)

    def undo(conns: list[dict]) -> str:
        """Remove connections this consent just made and this Mac is not keeping, so
        nothing is left connected at Xero that nobody here knows about. A connection
        this login already holds here is left alone. Says what Xero answered, not what
        was hoped."""
        fresh = [c for c in conns if c.get("id")
                 and (rows.get(str(c.get("tenantId"))) or {}).get("user_id") != user_id]
        if not fresh:
            return ""
        try:
            answers = [client._send("DELETE",
                                    f"https://api.xero.com/connections/{c['id']}",
                                    {"Authorization": f"Bearer {tok['access_token']}"})
                       for c in fresh]
        except ToolError:
            answers = []
        if answers and all(a.status in (200, 204, 404) for a in answers):
            return " What it connected has been disconnected again."
        return (" It is still connected at Xero; remove it there under Settings, "
                "Connected apps.")

    granted = ask(event)
    if not granted:
        # Xero named no organisation for this consent. Two honest readings: the person
        # signed in again for organisations already held here (the cure for a lapsed
        # sign-in), or Xero's filter did not point at the one just granted. What this
        # login holds at Xero decides which, and nothing is ever taken by guesswork.
        everything = ask()
        unregistered = [c for c in everything if str(c.get("tenantId")) not in rows]
        named = [c for c in unregistered if expect_org
                 and (c.get("tenantName") or "").casefold() == expect_org.casefold()]
        if len(named) == 1:
            granted = named
            warnings.append("Xero did not say which organisation this consent granted; "
                            f"{expect_org!r} was taken because it was asked for by name "
                            "and this login holds it")
        elif known:
            still = {str(c.get("tenantId")): c for c in everything}
            with store.locked():
                save_sign_in()
                current = tenants.registry()
                moved = False
                for t, c in still.items():      # a company connected again has a new id
                    r = current.get(t)
                    if r and r.get("user_id") == user_id and c.get("id") \
                            and r.get("connection_id") != c["id"]:
                        r["connection_id"], moved = c["id"], True
                if moved:
                    tenants.save_registry(current)
            renewed = sorted(tenants.key_of(r) for t, r in known.items() if t in still)
            gone = sorted(tenants.key_of(r) for t, r in known.items() if t not in still)
            if gone:
                warnings.append("no longer connected at Xero under this login: "
                                + ", ".join(gone))
            if unregistered:
                listed = ", ".join(repr(c.get("tenantName")) for c in unregistered)
                warnings.append(f"connected at Xero under this app but not held here: "
                                f"{listed}. To take one, run fma xero auth --expect-org "
                                "\"<its name>\"; to drop it, fma xero disconnect --org "
                                "\"<its name>\"")
            data = {"action": "auth", "step": "renewed", "organisations": renewed,
                    "authorised_by": email, "connected": len(rows),
                    "cap": tenants.FREE_TIER_CAP}
            not_applied = f" --key {key} was not applied." if key else ""
            if not renewed:
                raise Refusal(
                    "NOTHING_RENEWED",
                    "the sign-in was saved, but no organisation this login holds here "
                    f"is connected at Xero any more ({', '.join(gone)}). Connect one "
                    "again with: fma xero auth --expect-org \"<its name>\"."
                    + not_applied, data=data)
            if expect_org and expect_org.casefold() not in {
                    (r.get("name") or "").casefold() for t, r in known.items()
                    if t in still}:
                raise Refusal(
                    "CONSENT_WRONG_ORG",
                    f"asked for {expect_org!r}, which this consent did not connect. The "
                    f"sign-in was renewed for {', '.join(renewed)} and nothing else "
                    "changed." + not_applied, data=data)
            if key:
                warnings.append(f"--key {key} was not applied: no organisation was "
                                "newly connected")
            return data, warnings
        elif unregistered:
            listed = ", ".join(repr(c.get("tenantName")) for c in unregistered)
            raise Refusal("CONSENT_ORG_NOT_NAMED",
                          "Xero did not say which organisation this consent granted, so "
                          f"nothing was saved. This login holds {listed} under this app: "
                          "run the command again with --expect-org \"<its name>\" to "
                          "take one.")
    if len(granted) != 1:
        names = ", ".join(repr(c.get("tenantName")) for c in granted) or "none"
        raise Refusal("CONSENT_NOT_ONE_ORG",
                      f"this consent granted {len(granted)} organisations ({names}), "
                      "not exactly one, so nothing was saved here. Run the command "
                      "again and pick a single organisation." + undo(granted))
    conn = granted[0]
    tid, name = str(conn.get("tenantId") or ""), str(conn.get("tenantName") or "")
    if expect_org and name.casefold() != expect_org.casefold():
        raise Refusal("CONSENT_WRONG_ORG",
                      f"asked for {expect_org!r}; the organisation picked was {name!r}. "
                      f"Nothing was saved.{undo([conn])}")
    if not tid:
        raise Refusal("SIGN_IN_INCOMPLETE", "Xero named no organisation; nothing was saved")
    if key and _key_holder(key, rows, own_tenant=tid) is not None:
        warnings.append(f"the key {key!r} already names another organisation, so "
                        f"{name!r} was saved without one; give it its own with: "
                        f"fma xero accounts --set-key \"{name}\" <KEY>")
        key = None

    with store.locked():
        try:
            # The sign-in first: if the second write fails, an extra sign-in is
            # harmless, where a company pointing at a login with none is not.
            save_sign_in()
            rows = tenants.registry()
            previous = rows.get(tid) or {}
            rows[tid] = {
                "key": key or previous.get("key"), "name": name,
                "type": conn.get("tenantType") or "",
                "connection_id": conn.get("id") or "",
                "user_id": user_id, "authorised_by": email, "auth_event_id": event,
                "connected_at": conn.get("createdDateUtc") or "",
                "saved_at": datetime.fromtimestamp(now, timezone.utc)
                                    .isoformat(timespec="seconds")}
            tenants.save_registry(rows)
        except OSError as e:
            raise EnvProblem(
                "XERO_STORE_NOT_SAVED",
                f"Xero connected {name!r}, but that could not be saved here "
                f"({e.strerror or type(e).__name__}); it is still connected at Xero. "
                f"Fix the folder {store.config_dir()} and run: fma xero auth "
                f"--expect-org \"{name}\"", fix="fma doctor --fix")
        try:
            forget_unused_logins()
        except OSError:
            pass                    # an extra sign-in is ignored and swept next time
    if previous.get("user_id") and previous["user_id"] != user_id:
        warnings.append(f"{name!r} was held under another login "
                        f"({previous.get('authorised_by') or 'unnamed'}); that login's "
                        "own connection to it is still at Xero, and can be removed "
                        "there if it is no longer wanted")

    granted_scopes = set((tok.get("scope") or "").split())
    missing = [s for s in asked if s not in granted_scopes] if granted_scopes else []
    left_out = [s for s in oauth.SCOPES if s not in asked]
    if left_out:
        warnings.append("left out of this consent on request: " + ", ".join(left_out))
    if missing:
        warnings.append("Xero granted the sign-in without: " + ", ".join(missing)
                        + ". Reports that need them will refuse and say so.")
    if len(rows) >= tenants.FREE_TIER_CAP:
        warnings.append(f"{len(rows)} organisations are connected; Xero's free tier "
                        f"holds {tenants.FREE_TIER_CAP} per app")
    return ({"action": "auth", "step": "connected",
             "organisation": {"key": tenants.key_of(rows[tid]), "name": name,
                              "tenant_id": tid},
             "authorised_by": email, "connected": len(rows),
             "cap": tenants.FREE_TIER_CAP}, warnings)


# -- accounts ------------------------------------------------------------------------

def _accounts(args) -> tuple[dict, list[str]]:
    store.app()
    if args.set_key:
        wanted, key = args.set_key
        with store.locked():
            rows = tenants.registry()
            tid = tenants.find(wanted, rows)
            _check_key(key, rows, own_tenant=tid)
            rows[tid]["key"] = key
            tenants.save_registry(rows)
        return ({"action": "accounts", "step": "key",
                 "organisation": {"key": key, "name": rows[tid].get("name")}}, [])

    rows = tenants.registry()
    # Only the logins an organisation here still relies on: one nobody uses has no
    # business failing this check.
    needed = {r.get("user_id") for r in rows.values()}
    users = {u: v for u, v in (store.load(store.TOKENS).get("users") or {}).items()
             if u in needed}
    if not rows:
        raise EnvProblem("XERO_NOT_SIGNED_IN",
                         "no organisation is connected on this Mac yet", fix=store.AUTH_FIX)
    client = XeroClient()
    now = time.time()
    live: dict = {}                 # user id -> {tenant id: connection row}
    problems, warnings = [], []
    for uid in users:
        try:
            live[uid] = {str(c.get("tenantId")): c for c in client.connections(uid)}
        except EnvProblem as e:
            problems += e.problems
    users = {u: v for u, v in (store.load(store.TOKENS).get("users") or {}).items()
             if u in needed}                                  # refresh may have rotated
    listed = []
    for tid, r in sorted(rows.items(), key=lambda kv: tenants.key_of(kv[1]).casefold()):
        uid = r.get("user_id") or ""
        user = users.get(uid) or {}
        age = (now - float(user.get("refresh_issued_at", 0))) / 86400 if user else None
        if uid not in live:
            status = "sign-in not usable"
            if uid not in users:
                problems.append({"code": "XERO_NOT_SIGNED_IN",
                                 "message": f"{tenants.key_of(r)} ({r.get('name')}) has no "
                                            "sign-in on this Mac",
                                 "fix": f"{store.AUTH_FIX} --expect-org \"{r.get('name')}\""})
        elif tid not in live[uid]:
            status = "no longer connected at Xero"
            problems.append({"code": "XERO_CONNECTION_GONE",
                             "message": f"{tenants.key_of(r)} ({r.get('name')}) is no "
                                        "longer connected at Xero",
                             "fix": f"{store.AUTH_FIX} --expect-org \"{r.get('name')}\""})
        else:
            status = "live"
        listed.append({"key": tenants.key_of(r), "name": r.get("name"),
                       "status": status, "authorised_by": r.get("authorised_by") or "",
                       "sign_in_refreshed_days_ago": None if age is None else round(age, 1),
                       "tenant_id": tid})
    registered = set(rows)
    others = sorted({str(c.get("tenantName")) for conns in live.values()
                     for tid, c in conns.items() if tid not in registered})
    if others:
        warnings.append("connected at Xero under this app but not registered here "
                        f"(each counts toward the cap): {', '.join(others)}")
    at_xero = {tid for conns in live.values() for tid in conns}
    data = {"action": "accounts", "organisations": listed,
            "connected_at_xero": len(at_xero) if live else None,
            "cap": tenants.FREE_TIER_CAP, "unregistered_at_xero": others}
    if problems:
        raise EnvProblem("XERO_ATTENTION", f"{len(problems)} problem(s) found",
                         problems=problems, data=data)
    return data, warnings


# -- disconnect ----------------------------------------------------------------------

def _disconnect(args) -> tuple[dict, list[str]]:
    store.app()
    if not args.org:
        raise Refusal("ORG_REQUIRED", "say which organisation: --org <key>")
    rows = tenants.registry()
    client = XeroClient()
    try:
        tid = tenants.find(args.org, rows)
    except Refusal as unknown:
        if unknown.code != "ORG_UNKNOWN":
            raise
        # Not held here -- but it may be connected at Xero all the same (a consent
        # that was refused and could not be undone), holding one of the app's places.
        # Named in full, it can be let go from this side too.
        wanted = args.org.strip().casefold()
        strays = []
        for uid in {r.get("user_id") for r in rows.values() if r.get("user_id")}:
            try:
                strays += [(uid, c) for c in client.connections(uid)
                           if str(c.get("tenantId")) not in rows
                           and (c.get("tenantName") or "").casefold() == wanted]
            except EnvProblem:
                continue
        if len(strays) != 1 or not strays[0][1].get("id"):
            raise unknown
        uid, conn = strays[0]
        client.disconnect(uid, conn["id"])
        return ({"action": "disconnect",
                 "organisation": {"key": "(not held here)", "name": conn.get("tenantName")},
                 "connected": len(rows), "cap": tenants.FREE_TIER_CAP},
                ["that organisation was connected at Xero but not held on this Mac; its "
                 "connection has been removed"])
    row = rows[tid]
    warnings: list[str] = []
    if row.get("connection_id"):
        try:
            client.disconnect(row.get("user_id") or "", row["connection_id"])
        except EnvProblem as e:
            if e.code not in ("XERO_SIGN_IN_DEAD", "XERO_NOT_SIGNED_IN"):
                raise                     # an outage: keep the entry, try again later
            warnings.append("the sign-in is no longer usable, so Xero could not be told; "
                            "if the organisation still lists this app under Settings, "
                            "Connected apps, disconnect it there")
    with store.locked():
        rows = tenants.registry()
        rows.pop(tid, None)
        tenants.save_registry(rows)
        tokens = store.load(store.TOKENS)
        needed = {r.get("user_id") for r in rows.values()}
        stale = [u for u in (tokens.get("users") or {}) if u not in needed]
        for u in stale:
            tokens["users"].pop(u)
        if stale:
            store.save(store.TOKENS, tokens)
    return ({"action": "disconnect",
             "organisation": {"key": tenants.key_of(row), "name": row.get("name")},
             "connected": len(rows), "cap": tenants.FREE_TIER_CAP}, warnings)


# -- dispatch ------------------------------------------------------------------------

_ACTIONS = {"config": _config, "auth": _auth, "accounts": _accounts,
            "disconnect": _disconnect}


def _secrets_on_disk() -> list[str]:
    out = []
    try:
        for user in (store.load(store.TOKENS).get("users") or {}).values():
            out += [user.get("access_token"), user.get("refresh_token")]
        out.append(store.load(store.PENDING).get("verifier"))
    except Exception:
        pass
    return [s for s in out if s]


def run(args) -> tuple[dict, list[str]]:
    try:
        if args.action == "pull":
            data, warnings = pull_mod.run(args)
            return {"action": "pull", **data}, warnings
        if args.action == "group":
            data, warnings = group_mod.run(args)
            return {"action": "group", **data}, warnings
        return _ACTIONS[args.action](args)
    except ToolError:
        raise
    except Exception as e:
        # A bug must not become the way a token reaches a transcript.
        text = f"{type(e).__name__}: {e}"
        for s in _secrets_on_disk():
            text = text.replace(s, "[redacted]")
        raise ToolError("INTERNAL", text) from None


def summary(data: dict) -> str:
    action = data.get("action")
    if action == "pull":
        return pull_mod.summary(data)
    if action == "group":
        return group_mod.summary(data)
    if action == "config":
        return f"xero config: app {data['client_id']}, callback {data['redirect_address']}"
    if action == "auth":
        if data.get("step") == "link":
            return "xero auth: link printed; finish with --redirect"
        if data.get("step") == "renewed":
            return ("xero auth: sign-in renewed for "
                    f"{', '.join(data['organisations']) or 'no organisation'}")
        o = data["organisation"]
        return (f"xero auth: connected {o['key']} ({o['name']}); "
                f"{data['connected']} of {data['cap']} in use")
    if action == "accounts":
        if data.get("step") == "key":
            return f"xero accounts: {data['organisation']['name']} is now {data['organisation']['key']}"
        orgs = data.get("organisations", [])
        return (f"xero accounts: {sum(1 for o in orgs if o['status'] == 'live')} of "
                f"{len(orgs)} live; cap {data.get('cap')}")
    if action == "disconnect":
        return f"xero disconnect: removed {data['organisation']['key']}; {data['connected']} left"
    return "xero: done"

"""Talk to Xero: one transport, one paced GET, and a sign-in kept alive.

Three rules live here because nothing downstream could enforce them:

  * Every call names its organisation. A Xero sign-in can cover several, and the list
    Xero returns has no meaningful order, so "the first one" is never asked for.
  * The refresh token Xero hands back replaces the one just spent. It is on disk before
    the access token that came with it is used, or a crash in between strands the
    sign-in for good.
  * No token reaches an error message. Transport failures are reported by kind, and
    Xero's own error text is quoted only from API responses, never from the token
    endpoint.

Limits respected (Xero's published ones for an app on its free tier): 60 calls a minute
and a daily allowance per organisation, 5 in flight at once. This client is sequential,
paces itself under the minute limit, honours `Retry-After` on a 429, and reads the
allowance Xero reports back so a pull can say what it spent.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal

from ..errors import EnvProblem, InputProblem, Refusal
from . import oauth, store
from .transport import Response, TransportError, default_transport

API = "https://api.xero.com/api.xro/2.0"
CONNECTIONS = "https://api.xero.com/connections"

_MINUTE_LIMIT = 60
_MAX_429_RETRIES = 4
NETWORK_FIX = "check the network, then run the same command again"
SANDBOX_FIX = ("this session's network is not allowed to reach Xero. In Claude's settings "
               "(Capabilities, network egress) allow api.xero.com and identity.xero.com, or "
               "all domains; quit and reopen the app; then run the same command in a new "
               "session")


def network_fix(detail) -> str:
    """A proxy that refuses the tunnel is a sandbox's network rule, not an outage:
    running the same command again will not cure it, and the fix line must say what
    will."""
    return SANDBOX_FIX if "Tunnel connection failed" in str(detail) else NETWORK_FIX


def _json(body: bytes):
    return json.loads(body.decode("utf-8"), parse_float=Decimal)


def _xero_says(body: bytes) -> str:
    """Xero's own one-line explanation from an API error body, trimmed. Used only for
    API responses, whose bodies never carry a token."""
    try:
        d = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return ""
    if not isinstance(d, dict):
        return ""
    for key in ("Detail", "Message", "Title", "detail", "title"):
        if d.get(key):
            return str(d[key])[:200]
    return ""


@dataclass
class Spend:
    calls: int = 0
    day_remaining: int | None = None
    minute_remaining: int | None = None


@dataclass
class XeroClient:
    transport: object = None
    clock: object = None
    sleep: object = None
    spend: dict = field(default_factory=dict)       # tenant id -> Spend
    _recent: dict = field(default_factory=dict)     # tenant id -> [call times]

    def __post_init__(self):
        if self.transport is None:
            self.transport = default_transport()
        # looked up now, not at import, so a test can stand in for the clock
        self.clock = self.clock or time.time
        self.sleep = self.sleep or time.sleep

    # -- the sign-in -------------------------------------------------------------

    def access_token(self, user_id: str) -> str:
        """A usable access token for one Xero login, refreshed if it is about to
        lapse. The rotated refresh token is saved before this returns."""
        with store.locked():
            tokens = store.load(store.TOKENS)
            user = (tokens.get("users") or {}).get(user_id)
            if not user or not user.get("refresh_token"):
                raise EnvProblem("XERO_NOT_SIGNED_IN",
                                 "this Mac holds no Xero sign-in for that login",
                                 fix=store.AUTH_FIX)
            if float(user.get("access_expires_at", 0)) - 60 > self.clock():
                return user["access_token"]
            client_id = store.app()["client_id"]
            try:
                fresh = oauth.refresh(self.transport, client_id, user["refresh_token"])
            except oauth.TokenRejected as e:
                raise EnvProblem(
                    "XERO_SIGN_IN_DEAD",
                    f"Xero rejected the saved sign-in ({e.reason}); it must be "
                    "authorised again, once per organisation", fix=store.AUTH_FIX)
            except oauth.TokenUnavailable as e:
                raise EnvProblem("XERO_UNREACHABLE",
                                 f"could not reach Xero to refresh the sign-in ({e})",
                                 fix=network_fix(e))
            stamped = oauth.stamp(fresh, self.clock())
            if not stamped["refresh_token"]:
                # Xero always rotates; if an answer ever came without one, the token
                # just spent is the only one there is. Never overwrite it with nothing.
                stamped["refresh_token"] = user["refresh_token"]
            user.update(stamped)
            tokens["users"][user_id] = user
            # Before the new token is used. Xero has already retired the old one, so a
            # save that fails here is not an ordinary bug: say exactly what happened.
            for attempt in (1, 2):
                try:
                    store.save(store.TOKENS, tokens)
                    break
                except OSError as e:
                    if attempt == 2:
                        raise EnvProblem(
                            "XERO_SIGN_IN_NOT_SAVED",
                            "Xero renewed the sign-in but it could not be saved here "
                            f"({e.strerror or type(e).__name__}). Xero honours the saved "
                            "one for about half an hour more: fix the folder "
                            f"{store.config_dir()} and run the command again inside that "
                            "time, or authorise again afterwards",
                            fix=f"fma doctor --fix   # then, if it has lapsed: {store.AUTH_FIX}")
            return user["access_token"]

    # -- raw calls ---------------------------------------------------------------

    def _send(self, method: str, url: str, headers: dict) -> Response:
        try:
            return self.transport.request(method, url, headers, None, 30)
        except TransportError as e:
            raise EnvProblem("XERO_UNREACHABLE", f"could not reach Xero ({e})",
                             fix=network_fix(e))

    def _pace(self, tenant_id: str) -> None:
        now = self.clock()
        recent = [t for t in self._recent.get(tenant_id, []) if now - t < 60]
        if len(recent) >= _MINUTE_LIMIT - 1:
            self.sleep(60 - (now - recent[0]) + 0.5)
            now = self.clock()
            recent = [t for t in recent if now - t < 60]
        recent.append(now)
        self._recent[tenant_id] = recent

    def get_raw(self, user_id: str, tenant_id: str, path: str,
                params: dict | None = None) -> bytes:
        """GET one accounting resource for one named organisation; the response body
        exactly as Xero sent it."""
        if not tenant_id:
            raise Refusal("ORG_REQUIRED", "a Xero call was made without naming its "
                                          "organisation")
        url = f"{API}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        spend = self.spend.setdefault(tenant_id, Spend())
        refreshed_once = False
        for attempt in range(_MAX_429_RETRIES + 1):
            token = self.access_token(user_id)
            self._pace(tenant_id)
            r = self._send("GET", url, {"Authorization": f"Bearer {token}",
                                        "Xero-Tenant-Id": tenant_id,
                                        "Accept": "application/json"})
            spend.calls += 1
            for header, attr in (("x-daylimit-remaining", "day_remaining"),
                                 ("x-minlimit-remaining", "minute_remaining")):
                v = r.headers.get(header)
                if v is not None and str(v).strip().isdigit():
                    setattr(spend, attr, int(v))
            if r.status == 200:
                return r.body
            if r.status == 429:
                problem = r.headers.get("x-rate-limit-problem", "").lower()
                if "day" in problem:
                    raise Refusal(
                        "XERO_DAILY_LIMIT",
                        "Xero's daily call allowance for this organisation is spent; "
                        "it resets at midnight UTC. Nothing was written.")
                if attempt == _MAX_429_RETRIES:
                    break
                wait = r.headers.get("retry-after", "")
                self.sleep(float(wait) if wait.replace(".", "", 1).isdigit() else 5.0)
                continue
            if r.status == 401:
                challenge = r.headers.get("www-authenticate", "")
                if "insufficient_scope" in challenge:
                    raise Refusal(
                        "XERO_SCOPE_MISSING",
                        f"the sign-in was not granted the permission {path} needs. "
                        "Authorise again so Xero shows the full read-only list: "
                        f"{store.AUTH_FIX}")
                if not refreshed_once:
                    refreshed_once = True
                    self._expire(user_id)
                    continue
                raise EnvProblem("XERO_SIGN_IN_DEAD",
                                 "Xero no longer accepts the saved sign-in",
                                 fix=store.AUTH_FIX)
            if r.status == 403:
                raise Refusal(
                    "XERO_ACCESS_REMOVED",
                    "Xero refused this organisation: the login that authorised it no "
                    "longer has access, or the connection was removed in Xero. "
                    f"{_xero_says(r.body)}".strip())
            if r.status == 404:
                raise InputProblem("XERO_NOT_FOUND",
                                   f"Xero has no resource at {path}. {_xero_says(r.body)}"
                                   .strip())
            if r.status >= 500:
                raise EnvProblem("XERO_UNREACHABLE",
                                 f"Xero answered {r.status} for {path}",
                                 fix=NETWORK_FIX)
            raise InputProblem("XERO_REJECTED",
                               f"Xero answered {r.status} for {path}. "
                               f"{_xero_says(r.body)}".strip())
        raise EnvProblem("XERO_RATE_LIMITED",
                         "Xero kept answering 429 after several waits",
                         fix="wait a minute, then run the same command again")

    def get(self, user_id: str, tenant_id: str, path: str, params: dict | None = None):
        body = self.get_raw(user_id, tenant_id, path, params)
        try:
            return _json(body)
        except ValueError:
            raise InputProblem("XERO_NOT_JSON",
                               f"Xero's answer for {path} was not JSON")

    def _expire(self, user_id: str) -> None:
        with store.locked():
            tokens = store.load(store.TOKENS)
            user = (tokens.get("users") or {}).get(user_id)
            if user:
                user["access_expires_at"] = 0
                store.save(store.TOKENS, tokens)

    # -- connections -------------------------------------------------------------

    def connections(self, user_id: str, auth_event_id: str | None = None,
                    access_token: str | None = None) -> list[dict]:
        """The organisations a login has connected to this app. With an
        `auth_event_id`, only those granted by that one consent."""
        token = access_token or self.access_token(user_id)
        url = CONNECTIONS
        if auth_event_id:
            url += "?" + urllib.parse.urlencode({"authEventId": auth_event_id})
        r = self._send("GET", url, {"Authorization": f"Bearer {token}",
                                    "Accept": "application/json"})
        if r.status != 200:
            raise EnvProblem("XERO_UNREACHABLE",
                             f"Xero answered {r.status} when asked which organisations "
                             "are connected", fix=NETWORK_FIX)
        try:
            rows = _json(r.body)
        except ValueError:
            rows = None
        if not isinstance(rows, list):
            raise InputProblem("XERO_NOT_JSON", "Xero's list of connections was not a list")
        return rows

    def disconnect(self, user_id: str, connection_id: str) -> None:
        token = self.access_token(user_id)
        r = self._send("DELETE", f"{CONNECTIONS}/{urllib.parse.quote(connection_id)}",
                       {"Authorization": f"Bearer {token}"})
        if r.status not in (200, 204, 404):
            raise EnvProblem("XERO_UNREACHABLE",
                             f"Xero answered {r.status} when asked to remove the "
                             "connection", fix=NETWORK_FIX)

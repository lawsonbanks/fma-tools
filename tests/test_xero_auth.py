"""xero auth / accounts / disconnect: one organisation per consent, named, or nothing saved."""

import json
import socket
import stat
import urllib.parse
import urllib.request

import pytest

from fma_tools.xero import oauth, store, tenants
from xero_fakes import FakeXero, Org, jwt, signed_in

CLIENT_ID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def configured(xero, run_cli):
    port = _free_port()
    code, env = run_cli(["xero", "config", "--client-id", CLIENT_ID, "--port", str(port)])
    assert code == 0, env
    xero.port = port
    return xero


@pytest.fixture
def browser(monkeypatch, configured):
    """A person at a browser: follows the link, and Xero sends them back with a code.
    What they 'click' is set by the test through `answer`."""
    answer = {"code": configured.good_code, "state": None, "error": None, "seen": []}

    def open_(url):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        answer["seen"].append(url)
        configured.expected_challenge = q["code_challenge"]
        back = {"state": answer["state"] or q["state"]}
        if answer["error"]:
            back["error"] = answer["error"]
        else:
            back["code"] = answer["code"]
        with urllib.request.urlopen(q["redirect_uri"] + "?" + urllib.parse.urlencode(back),
                                    timeout=5) as r:
            assert r.status == 200
        return True
    monkeypatch.setattr("fma_tools.xero.main.webbrowser.open", open_)
    return answer


def _token_file() -> bytes | None:
    p = store.config_dir() / store.TOKENS
    return p.read_bytes() if p.exists() else None


# -- config --------------------------------------------------------------------------

def test_nothing_works_until_an_app_is_configured(xero, run_cli):
    for argv in (["xero", "auth"], ["xero", "accounts"], ["xero", "config"]):
        code, env = run_cli(argv)
        assert code == 3, argv
        assert env["problems"][0]["code"] == "XERO_NOT_CONFIGURED"
        assert env["problems"][0]["fix"].startswith("fma xero config --client-id")


def test_config_keeps_the_client_id_private_and_masked(xero, run_cli):
    code, env = run_cli(["xero", "config", "--client-id", CLIENT_ID])
    assert code == 0
    assert CLIENT_ID not in json.dumps(env)
    assert env["data"]["redirect_address"] == "http://localhost:8976/callback"
    d = store.config_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / store.APP).stat().st_mode) == 0o600
    assert store.load(store.APP)["client_id"] == CLIENT_ID


def test_config_refuses_something_that_is_not_a_client_id(xero, run_cli):
    code, env = run_cli(["xero", "config", "--client-id", "not a client id"])
    assert code == 2 and env["problems"][0]["code"] == "CLIENT_ID_INVALID"


# -- one consent, one organisation ---------------------------------------------------

def test_auth_end_to_end_through_the_real_callback(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--key", "A", "--timeout", "10"])
    assert code == 0, env["problems"]
    assert env["data"]["organisation"] == {"key": "A", "name": "Entity A Pty Ltd",
                                           "tenant_id": "t-a"}
    assert env["data"]["authorised_by"] == "adviser@example.test"
    assert env["data"]["connected"] == 1 and env["data"]["cap"] == 5
    # the link the person followed
    url = browser["seen"][0]
    assert "+" not in url, "a + in the scope list turns it into one unknown scope"
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
    assert q["client_id"] == CLIENT_ID and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == f"http://localhost:{configured.port}/callback"
    # what was kept, and how privately
    d = store.config_dir()
    for name in (store.TOKENS, store.TENANTS):
        assert stat.S_IMODE((d / name).stat().st_mode) == 0o600
    row = tenants.registry()["t-a"]
    assert row["key"] == "A" and row["user_id"] == "user-1"
    assert row["connection_id"] == "conn-user-1-t-a"
    # and none of it in the output
    for token in configured.issued:
        assert token not in json.dumps(env)


def test_a_second_consent_adds_and_never_replaces(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.add_org(Org("t-b", "Entity B Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    assert run_cli(["xero", "auth", "--key", "A", "--timeout", "10"])[0] == 0
    configured.will_grant("user-1", "adviser@example.test", ["t-b"])
    code, env = run_cli(["xero", "auth", "--key", "B", "--timeout", "10"])
    assert code == 0 and env["data"]["connected"] == 2
    assert sorted(tenants.key_of(r) for r in tenants.registry().values()) == ["A", "B"]


def test_a_consent_that_grants_two_organisations_saves_nothing(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.add_org(Org("t-b", "Entity B Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a", "t-b"])
    before = _token_file()
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_NOT_ONE_ORG"
    assert "2 organisations" in env["problems"][0]["message"]
    assert _token_file() == before and tenants.registry() == {}
    # and nothing is left connected at Xero that this Mac does not know about
    assert "disconnected again" in env["problems"][0]["message"]
    assert configured.users["user-1"]["connections"] == []


def test_a_consent_that_grants_none_saves_nothing(configured, browser, run_cli):
    configured.will_grant("user-1", "adviser@example.test", [])
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_NOT_ONE_ORG"
    assert _token_file() is None


def test_the_wrong_organisation_is_refused_and_disconnected_again(configured, browser, run_cli):
    configured.add_org(Org("t-demo", "Demo Company (AU)"))
    configured.will_grant("user-1", "adviser@example.test", ["t-demo"])
    code, env = run_cli(["xero", "auth", "--expect-org", "Entity A Pty Ltd",
                         "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_WRONG_ORG"
    assert "Demo Company (AU)" in env["problems"][0]["message"]
    assert "disconnected again" in env["problems"][0]["message"]
    assert configured.users["user-1"]["connections"] == []
    assert _token_file() is None and tenants.registry() == {}


def test_signing_in_again_renews_every_company_that_login_holds(configured, browser, run_cli):
    # The cure for a lapsed sign-in, exactly as the fix line prints it: `fma xero auth`.
    # If Xero keeps the original consent id on a connection, the new consent grants
    # nothing "new" -- and the new sign-in must still be kept, not thrown away.
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.add_org(Org("t-b", "Entity B Pty Ltd"))
    old = signed_in(configured, ["t-a", "t-b"], keys={"t-a": "A", "t-b": "B"}, expired=True)
    configured.refresh_tokens.pop(old["refresh_token"])            # it has lapsed
    assert run_cli(["xero", "accounts"])[0] == 3
    configured.will_grant("user-1", "adviser@example.test", [], event="event-renewal")
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 0, env["problems"]
    assert env["data"]["step"] == "renewed" and env["data"]["organisations"] == ["A", "B"]
    assert store.load(store.TOKENS)["users"]["user-1"]["refresh_token"] != old["refresh_token"]
    assert tenants.registry()["t-a"]["key"] == "A", "a renewal keeps the key"
    code, env = run_cli(["xero", "accounts"])
    assert code == 0 and [o["status"] for o in env["data"]["organisations"]] == ["live", "live"]


def _b_is_gone_at_xero(configured):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.add_org(Org("t-b", "Entity B Pty Ltd"))
    old = signed_in(configured, ["t-a", "t-b"], keys={"t-a": "A", "t-b": "B"})
    configured.users["user-1"]["connections"] = [
        c for c in configured.users["user-1"]["connections"] if c["tenantId"] != "t-b"]
    configured.will_grant("user-1", "adviser@example.test", [], event="event-renewal")
    return old


def test_a_renewal_says_which_companies_have_gone(configured, browser, run_cli):
    _b_is_gone_at_xero(configured)
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 0 and env["data"]["organisations"] == ["A"]
    assert any("no longer connected at Xero under this login: B" in w for w in env["warnings"])
    assert not any("not held here" in w for w in env["warnings"]), "A is held here"


def test_a_renewal_that_did_not_bring_back_the_company_asked_for_refuses(configured, browser, run_cli):
    # `--expect-org` means "refuse unless this is what was connected" on every path.
    # Exit 0 here would tell an agent following the fix line that B was back.
    old = _b_is_gone_at_xero(configured)
    code, env = run_cli(["xero", "auth", "--expect-org", "Entity B Pty Ltd", "--key", "B2",
                         "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_WRONG_ORG"
    message = env["problems"][0]["message"]
    assert "renewed for A" in message and "--key B2 was not applied" in message
    assert env["data"]["organisations"] == ["A"]
    # the sign-in itself was good and was kept: A works
    assert store.load(store.TOKENS)["users"]["user-1"]["refresh_token"] != old["refresh_token"]
    assert tenants.registry()["t-b"]["key"] == "B"


def test_a_renewal_with_nothing_left_to_renew_is_not_a_success(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"})
    configured.users["user-1"]["connections"] = []
    configured.will_grant("user-1", "adviser@example.test", [], event="event-renewal")
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "NOTHING_RENEWED"
    assert "(A)" in env["problems"][0]["message"]


def test_a_company_connected_again_takes_its_new_connection_id(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"})
    configured.users["user-1"]["connections"][0]["id"] = "conn-new"     # reconnected at Xero
    configured.will_grant("user-1", "adviser@example.test", [], event="event-renewal")
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 0 and env["data"]["step"] == "renewed"
    assert configured.users["user-1"]["connections"][0]["id"] == "conn-new"
    assert tenants.registry()["t-a"]["connection_id"] == "conn-new"
    assert run_cli(["xero", "disconnect", "--org", "A"])[0] == 0
    assert configured.users["user-1"]["connections"] == [], "disconnect reached the live one"


def test_a_failure_after_the_code_is_spent_does_not_keep_the_consent_waiting(configured, run_cli):
    # The code went to Xero and came back as a sign-in; then Xero could not be asked
    # what was granted. "Run the same command again" would be a lie: the code is gone.
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--paste", "--key", "A"])
    q = {k: v[0] for k, v in urllib.parse.parse_qs(
        urllib.parse.urlparse(env["data"]["link"]).query).items()}
    configured.expected_challenge = q["code_challenge"]
    landed = f"{q['redirect_uri']}?code={configured.good_code}&state={q['state']}"
    configured.connections_status = 503
    code, env = run_cli(["xero", "auth", "--redirect", landed])
    assert code == 3 and env["problems"][0]["code"] == "XERO_UNREACHABLE"
    assert "nothing was saved here" in env["problems"][0]["message"]
    assert env["problems"][0]["fix"] == "fma xero auth", "a new consent, not the same address"
    assert not (store.config_dir() / store.PENDING).exists()


def test_a_known_login_keeps_its_new_sign_in_even_if_xero_then_goes_quiet(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    old = signed_in(configured, ["t-a"], keys={"t-a": "A"}, expired=True)
    configured.refresh_tokens.pop(old["refresh_token"])                 # lapsed
    configured.will_grant("user-1", "adviser@example.test", [], event="event-renewal")
    configured.connections_status = 503
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 3 and "the sign-in was saved" in env["problems"][0]["message"]
    assert env["problems"][0]["fix"] == "fma xero accounts"
    configured.connections_status = None
    code, env = run_cli(["xero", "accounts"])
    assert code == 0 and env["data"]["organisations"][0]["status"] == "live"


@pytest.mark.parametrize("unwritable", [store.TOKENS, store.TENANTS])
def test_a_save_that_fails_at_the_last_step_says_what_is_and_is_not_saved(
        configured, browser, run_cli, monkeypatch, unwritable):
    # A works under login 1; login 2 re-authorises it; one of the two files cannot be
    # written. The sign-in goes first, so whichever write fails, A is never left
    # pointing at a login that has no sign-in.
    import os
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"})
    configured.will_grant("user-2", "other@example.test", ["t-a"])
    real = os.replace

    def no_replace(src, dst):
        if str(dst).endswith(unwritable):
            raise OSError(28, "No space left on device")
        return real(src, dst)
    with monkeypatch.context() as m:        # never monkeypatch.undo(): it would also
        m.setattr(os, "replace", no_replace)  # undo the guards that keep tests off
        code, env = run_cli(["xero", "auth", "--timeout", "10"])   # a real sign-in
    assert code == 3 and env["problems"][0]["code"] == "XERO_STORE_NOT_SAVED"
    assert "still connected at Xero" in env["problems"][0]["message"]
    assert tenants.registry()["t-a"]["user_id"] == "user-1"
    assert "user-1" in store.load(store.TOKENS)["users"]
    if unwritable == store.TOKENS:
        assert list(store.load(store.TOKENS)["users"]) == ["user-1"], "nothing changed"
    code, env = run_cli(["xero", "accounts"])
    assert code == 0 and env["data"]["organisations"][0]["status"] == "live"


def test_a_wrong_company_already_held_under_another_login_is_still_undone(configured, browser, run_cli):
    # Login 1 holds A here. Login 2 means to add C but picks A: login 2's new
    # connection to A is nobody's, and is removed; login 1's is untouched.
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"})
    configured.will_grant("user-2", "other@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--expect-org", "Entity C Pty Ltd", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_WRONG_ORG"
    assert "disconnected again" in env["problems"][0]["message"]
    assert configured.users["user-2"]["connections"] == []
    assert len(configured.users["user-1"]["connections"]) == 1
    assert tenants.registry()["t-a"]["user_id"] == "user-1"


def test_taking_a_company_over_says_the_earlier_logins_connection_remains(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"})
    configured.will_grant("user-2", "other@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 0
    assert any("held under another login (adviser@example.test)" in w for w in env["warnings"])


def test_an_answer_from_another_consent_is_refused(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    browser["state"] = "someone-elses-state"
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "STATE_MISMATCH"
    assert _token_file() is None


def test_declining_at_xero_is_a_refusal(configured, browser, run_cli):
    browser["error"] = "access_denied"
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_DENIED"


def test_a_rejected_scope_says_the_build_needs_correcting(configured, browser, run_cli):
    browser["error"] = "invalid_scope"
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "SCOPE_REJECTED"


def test_a_code_xero_will_not_take_is_a_refusal(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    browser["code"] = "stale-code"
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CODE_REJECTED"
    assert "five minutes" in env["problems"][0]["message"]


def test_nobody_clicking_is_a_refusal_not_a_hang(configured, run_cli, monkeypatch):
    monkeypatch.setattr("fma_tools.xero.main.webbrowser.open", lambda url: True)
    monkeypatch.setattr("fma_tools.xero.main.oauth.CallbackListener.wait",
                        lambda self, timeout: {})
    code, env = run_cli(["xero", "auth", "--timeout", "5"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_TIMED_OUT"


def test_a_port_in_use_is_exit_3(configured, run_cli):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", configured.port))
        holder.listen(1)
        code, env = run_cli(["xero", "auth", "--timeout", "5"])
    assert code == 3 and env["problems"][0]["code"] == "XERO_PORT_BUSY"
    assert env["problems"][0]["fix"]


def test_a_key_already_taken_refuses_before_anyone_is_asked_to_click(configured, run_cli, monkeypatch):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"})
    opened = []
    monkeypatch.setattr("fma_tools.xero.main.webbrowser.open", lambda url: opened.append(url))
    code, env = run_cli(["xero", "auth", "--key", "a", "--timeout", "5"])
    assert code == 1 and env["problems"][0]["code"] == "KEY_IN_USE"
    assert not opened


def test_the_paste_route_finishes_from_the_address_the_browser_landed_on(configured, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--paste", "--key", "A"])
    assert code == 0 and env["data"]["step"] == "link"
    link = env["data"]["link"]
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(link).query).items()}
    configured.expected_challenge = q["code_challenge"]
    landed = f"{q['redirect_uri']}?code={configured.good_code}&state={q['state']}"
    code, env = run_cli(["xero", "auth", "--redirect", landed])
    assert code == 0, env["problems"]
    assert env["data"]["organisation"]["key"] == "A"
    assert not (store.config_dir() / store.PENDING).exists(), "a verifier is used once"
    code, env = run_cli(["xero", "auth", "--redirect", landed])
    assert code == 1 and env["problems"][0]["code"] == "NO_PENDING_CONSENT"


# -- accounts ------------------------------------------------------------------------

def test_accounts_lists_what_is_live(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    xero.add_org(Org("t-b", "Entity B Pty Ltd"))
    signed_in(xero, ["t-a", "t-b"], keys={"t-a": "A", "t-b": "B"})
    code, env = run_cli(["xero", "accounts"])
    assert code == 0, env["problems"]
    assert [(o["key"], o["status"]) for o in env["data"]["organisations"]] == \
        [("A", "live"), ("B", "live")]
    assert env["data"]["connected_at_xero"] == 2 and env["data"]["cap"] == 5
    for token in xero.issued:
        assert token not in json.dumps(env)


def test_looking_at_accounts_is_itself_what_keeps_a_sign_in_alive(xero, run_cli):
    import time
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"}, now=time.time() - 50 * 86400)
    code, env = run_cli(["xero", "accounts"])
    # reaching Xero refreshed the sign-in, which is exactly what keeps it alive
    assert code == 0
    assert env["data"]["organisations"][0]["sign_in_refreshed_days_ago"] < 1


def test_accounts_says_when_a_company_is_gone_at_xero(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    xero.add_org(Org("t-b", "Entity B Pty Ltd"))
    signed_in(xero, ["t-a", "t-b"], keys={"t-a": "A", "t-b": "B"})
    xero.users["user-1"]["connections"] = [c for c in xero.users["user-1"]["connections"]
                                           if c["tenantId"] != "t-b"]
    code, env = run_cli(["xero", "accounts"])
    assert code == 3
    assert env["problems"][0]["code"] == "XERO_CONNECTION_GONE"
    assert 'fma xero auth --expect-org "Entity B Pty Ltd"' == env["problems"][0]["fix"]
    assert [o["status"] for o in env["data"]["organisations"]] == \
        ["live", "no longer connected at Xero"]


def test_accounts_names_connections_this_mac_does_not_know(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    xero.add_org(Org("t-x", "Demo Company (AU)"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    xero.users["user-1"]["connections"].append(
        {"id": "conn-x", "authEventId": "old", "tenantId": "t-x",
         "tenantType": "ORGANISATION", "tenantName": "Demo Company (AU)"})
    code, env = run_cli(["xero", "accounts"])
    assert code == 0
    assert env["data"]["unregistered_at_xero"] == ["Demo Company (AU)"]
    assert env["data"]["connected_at_xero"] == 2
    assert any("counts toward the cap" in w for w in env["warnings"])


def test_a_dead_sign_in_is_exit_3_with_the_fix(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    tok = signed_in(xero, ["t-a"], keys={"t-a": "A"}, expired=True)
    xero.refresh_tokens.pop(tok["refresh_token"])          # Xero has forgotten it
    code, env = run_cli(["xero", "accounts"])
    assert code == 3
    assert env["problems"][0]["code"] == "XERO_SIGN_IN_DEAD"
    assert env["problems"][0]["fix"] == "fma xero auth"
    assert env["data"]["organisations"][0]["status"] == "sign-in not usable"


def test_set_key_renames_and_refuses_a_clash(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    xero.add_org(Org("t-b", "Entity B Pty Ltd"))
    signed_in(xero, ["t-a", "t-b"], keys={"t-a": "A"})
    assert tenants.key_of(tenants.registry()["t-b"]) == "Entity_B_Pty_Ltd"
    code, env = run_cli(["xero", "accounts", "--set-key", "Entity B Pty Ltd", "B"])
    assert code == 0 and tenants.registry()["t-b"]["key"] == "B"
    code, env = run_cli(["xero", "accounts", "--set-key", "Entity B Pty Ltd", "A"])
    assert code == 1 and env["problems"][0]["code"] == "KEY_IN_USE"
    for bad in ("has space", "NSW_", ""):
        code, env = run_cli(["xero", "accounts", "--set-key", "Entity B Pty Ltd", bad])
        assert code == 2 and env["problems"][0]["code"] == "KEY_INVALID", bad
    # a leading dash never reaches the tool as a key on some Pythons (argparse takes it
    # for an option), so the rule itself is checked here
    from fma_tools.xero.main import _KEY_RE
    assert not _KEY_RE.match("-NSW") and not _KEY_RE.match("_NSW")
    assert _KEY_RE.match("N") and _KEY_RE.match("NSW-2") and _KEY_RE.match("a_b")
    assert not _KEY_RE.match("x" * 21) and _KEY_RE.match("x" * 20)
    # and a key that would make the same file name as another is the same key
    code, env = run_cli(["xero", "accounts", "--set-key", "Entity B Pty Ltd", "a"])
    assert code == 1 and env["problems"][0]["code"] == "KEY_IN_USE"


def test_disconnect_removes_the_connection_and_the_last_sign_in(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    xero.add_org(Org("t-b", "Entity B Pty Ltd"))
    signed_in(xero, ["t-a", "t-b"], keys={"t-a": "A", "t-b": "B"})
    code, env = run_cli(["xero", "disconnect", "--org", "A"])
    assert code == 0 and env["data"]["connected"] == 1
    assert [c["tenantId"] for c in xero.users["user-1"]["connections"]] == ["t-b"]
    assert "user-1" in store.load(store.TOKENS)["users"], "B still needs the sign-in"
    code, env = run_cli(["xero", "disconnect", "--org", "B"])
    assert code == 0 and env["data"]["connected"] == 0
    assert store.load(store.TOKENS)["users"] == {}
    code, env = run_cli(["xero", "disconnect", "--org", "B"])
    assert code == 1 and env["problems"][0]["code"] == "ORG_UNKNOWN"


# -- the pieces ----------------------------------------------------------------------

def test_pkce_matches_the_standards_own_example():
    # RFC 7636, appendix B
    assert oauth.challenge_for("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    verifier, challenge = oauth.new_pkce()
    assert 43 <= len(verifier) <= 128 and challenge == oauth.challenge_for(verifier)
    assert set(verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                                "0123456789-._~")
    assert len({oauth.new_pkce()[0] for _ in range(5)} | {verifier}) == 6, \
        "a verifier that repeats is a password anyone who saw one link holds"


def test_every_requested_scope_is_read_only():
    harmless = {"offline_access", "openid", "email"}
    for scope in oauth.SCOPES:
        assert scope in harmless or scope.endswith(".read"), scope
    url = oauth.consent_url("X" * 32, 8976, "s", "c")
    asked = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"][0].split()
    assert asked == list(oauth.SCOPES)
    assert "accounting.journals.read" not in asked, "not served on an affordable tier"


@pytest.mark.parametrize("text", [
    "http://localhost:8976/callback?code=abc&state=xyz",
    "'http://localhost:8976/callback?code=abc&state=xyz'",
    "code=abc&state=xyz",
])
def test_a_pasted_address_is_read_however_it_is_quoted(text):
    got = oauth.parse_redirect(text)
    assert got["code"] == "abc" and got["state"] == "xyz" and got["error"] is None


def test_the_listener_hears_only_the_callback_path():
    listener = oauth.CallbackListener(0)
    try:
        base = f"http://127.0.0.1:{listener.port}"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/favicon.ico", timeout=5)
        assert listener.wait(0.05) == {}
        urllib.request.urlopen(base + "/callback?code=one&state=s", timeout=5).read()
        urllib.request.urlopen(base + "/callback?code=two&state=s", timeout=5).read()
        assert listener.wait(1)["code"] == "one", "the first answer is the answer"
    finally:
        listener.close()


def test_token_failures_are_told_apart():
    from fma_tools.xero.transport import Response, TransportError

    class T:
        def __init__(self, answer):
            self.answer = answer

        def request(self, *a, **k):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
    with pytest.raises(oauth.TokenRejected) as e:
        oauth.refresh(T(Response(400, {}, b'{"error":"invalid_grant"}')), "c", "r")
    assert e.value.reason == "invalid_grant"
    with pytest.raises(oauth.TokenUnavailable):
        oauth.refresh(T(Response(503, {}, b"upstream sad")), "c", "r")
    with pytest.raises(oauth.TokenUnavailable):
        oauth.refresh(T(TransportError("URLError")), "c", "r")
    # a 200 without a token is not a sign-in
    with pytest.raises(oauth.TokenUnavailable):
        oauth.refresh(T(Response(200, {}, b"{}")), "c", "r")


def test_claims_are_read_and_garbage_is_survived():
    assert oauth.claims(jwt({"xero_userid": "u", "authentication_event_id": "e"})) == \
        {"xero_userid": "u", "authentication_event_id": "e"}
    for junk in (None, "", "not-a-jwt", "a.b.c", "a.!!!.c"):
        assert oauth.claims(junk) == {}


def test_two_fakes_do_not_share_state():
    assert FakeXero().orgs is not FakeXero().orgs


def test_a_permission_xero_rejects_can_be_left_out_without_a_new_build(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--without", "accounting.budgets.read",
                         "--timeout", "10"])
    assert code == 0, env["problems"]
    asked = urllib.parse.parse_qs(urllib.parse.urlparse(browser["seen"][0]).query)["scope"][0]
    assert "accounting.budgets.read" not in asked.split()
    assert "accounting.reports.trialbalance.read" in asked.split()
    assert any("left out of this consent" in w for w in env["warnings"])


def test_without_cannot_drop_what_keeps_the_sign_in_alive_or_invent_a_scope(configured, run_cli):
    code, env = run_cli(["xero", "auth", "--without", "offline_access", "--timeout", "5"])
    assert code == 1 and env["problems"][0]["code"] == "SCOPE_REQUIRED"
    code, env = run_cli(["xero", "auth", "--without", "accounting.transactions",
                         "--timeout", "5"])
    assert code == 2 and env["problems"][0]["code"] == "SCOPE_UNKNOWN"


def test_an_organisation_whose_sign_in_is_missing_is_exit_3_with_the_fix(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    xero.add_org(Org("t-b", "Entity B Pty Ltd"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    signed_in(xero, ["t-b"], keys={"t-b": "B"}, user_id="user-2", email="other@example.test")
    tokens = store.load(store.TOKENS)
    tokens["users"].pop("user-2")                      # that login's sign-in is gone
    store.save(store.TOKENS, tokens)
    code, env = run_cli(["xero", "accounts"])
    assert code == 3
    assert [p["code"] for p in env["problems"]] == ["XERO_NOT_SIGNED_IN"]
    assert env["problems"][0]["fix"] == 'fma xero auth --expect-org "Entity B Pty Ltd"'
    assert [(o["key"], o["status"]) for o in env["data"]["organisations"]] == \
        [("A", "live"), ("B", "sign-in not usable")]


def test_two_logins_each_reach_their_own_organisations(xero, run_cli, tmp_path):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    xero.add_org(Org("t-b", "Entity B Pty Ltd"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    signed_in(xero, ["t-b"], keys={"t-b": "B"}, user_id="user-2", email="other@example.test")
    code, env = run_cli(["xero", "pull", "--as-at", "2026-06-30", "--all", "--reports", "tb",
                         "--out", str(tmp_path / "p")])
    assert code == 0, env["problems"]
    record = json.loads((tmp_path / "p" / "PULL.json").read_text())
    assert [(o["key"], o["authorised_by"]) for o in record["organisations"]] == \
        [("A", "adviser@example.test"), ("B", "other@example.test")]


def test_a_dead_sign_in_does_not_trap_its_entry_here(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    tok = signed_in(xero, ["t-a"], keys={"t-a": "A"}, expired=True)
    xero.refresh_tokens.pop(tok["refresh_token"])
    code, env = run_cli(["xero", "disconnect", "--org", "A"])
    assert code == 0 and tenants.registry() == {}
    assert any("Connected apps" in w for w in env["warnings"])


def test_an_outage_does_not_forget_a_connection(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    xero.transport_down = True
    code, env = run_cli(["xero", "disconnect", "--org", "A"])
    assert code == 3 and env["problems"][0]["code"] == "XERO_UNREACHABLE"
    assert "t-a" in tenants.registry()


def test_an_undo_xero_refuses_is_not_reported_as_done(configured, browser, run_cli):
    configured.add_org(Org("t-demo", "Demo Company (AU)"))
    configured.will_grant("user-1", "adviser@example.test", ["t-demo"])
    configured.delete_status = 403
    code, env = run_cli(["xero", "auth", "--expect-org", "Entity A Pty Ltd",
                         "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_WRONG_ORG"
    assert "still connected at Xero" in env["problems"][0]["message"]
    assert "disconnected again" not in env["problems"][0]["message"]
    assert len(configured.users["user-1"]["connections"]) == 1


def test_a_login_no_company_uses_any_more_is_forgotten(configured, browser, run_cli):
    # Company A was authorised by one login and later by another. The first login's
    # sign-in would lapse unnoticed and then fail every check with a fix that cannot
    # cure it, so it is dropped the moment nothing relies on it.
    import time
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"}, now=time.time() - 70 * 86400)
    configured.will_grant("user-2", "other@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 0, env["problems"]
    assert list(store.load(store.TOKENS)["users"]) == ["user-2"]
    assert tenants.registry()["t-a"]["authorised_by"] == "other@example.test"
    assert tenants.registry()["t-a"]["key"] == "A"
    assert run_cli(["xero", "accounts"])[0] == 0
    code, env = run_cli(["doctor"])
    sign_in = {c["check"]: c for c in env["data"]["checks"]}["xero sign-in"]
    assert sign_in["status"] == "ok"


def test_a_leftover_login_does_not_fail_the_checks(xero, run_cli):
    # written by an earlier version, or by hand: a sign-in nothing here relies on
    import time
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    tokens = store.load(store.TOKENS)
    tokens["users"]["user-old"] = {"refresh_token": "long-dead", "access_token": "x",
                                   "access_expires_at": 0,
                                   "refresh_issued_at": time.time() - 400 * 86400}
    store.save(store.TOKENS, tokens)
    code, env = run_cli(["xero", "accounts"])
    assert code == 0, env["problems"]
    code, env = run_cli(["doctor"])
    assert {c["check"]: c for c in env["data"]["checks"]}["xero sign-in"]["status"] == "ok"
    assert run_cli(["xero", "disconnect", "--org", "A"])[0] == 0
    assert store.load(store.TOKENS)["users"] == {}, "and it goes when the last company goes"


def test_an_outage_while_finishing_a_pasted_consent_keeps_it_waiting(configured, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--paste", "--key", "A"])
    q = {k: v[0] for k, v in urllib.parse.parse_qs(
        urllib.parse.urlparse(env["data"]["link"]).query).items()}
    configured.expected_challenge = q["code_challenge"]
    landed = f"{q['redirect_uri']}?code={configured.good_code}&state={q['state']}"
    configured.transport_down = True
    code, env = run_cli(["xero", "auth", "--redirect", landed])
    assert code == 3 and env["problems"][0]["code"] == "XERO_UNREACHABLE"
    assert (store.config_dir() / store.PENDING).exists(), "the code was never spent"
    configured.transport_down = False
    code, env = run_cli(["xero", "auth", "--redirect", landed])       # the same command
    assert code == 0, env["problems"]
    assert env["data"]["organisation"]["key"] == "A"
    assert not (store.config_dir() / store.PENDING).exists()


def test_a_pasted_answer_that_can_never_work_ends_the_wait(configured, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--paste"])
    q = {k: v[0] for k, v in urllib.parse.parse_qs(
        urllib.parse.urlparse(env["data"]["link"]).query).items()}
    code, env = run_cli(["xero", "auth", "--redirect",
                         f"{q['redirect_uri']}?code=x&state=not-the-state"])
    assert code == 1 and env["problems"][0]["code"] == "STATE_MISMATCH"
    assert not (store.config_dir() / store.PENDING).exists()


def test_a_key_may_not_end_in_what_a_file_name_drops(configured, run_cli):
    code, env = run_cli(["xero", "auth", "--key", "NSW_", "--timeout", "5"])
    assert code == 2 and env["problems"][0]["code"] == "KEY_INVALID"


def test_one_login_taking_companies_over_never_costs_another_its_sign_in(configured, browser, run_cli, tmp_path):
    for t, n in (("t-a", "Entity A Pty Ltd"), ("t-b", "Entity B Pty Ltd"),
                 ("t-c", "Entity C Pty Ltd")):
        configured.add_org(Org(t, n))
    signed_in(configured, ["t-a", "t-b"], keys={"t-a": "A", "t-b": "B"})   # login 1: A and B
    configured.will_grant("user-2", "other@example.test", ["t-c"])         # login 2 adds C
    assert run_cli(["xero", "auth", "--key", "C", "--timeout", "10"])[0] == 0
    assert sorted(store.load(store.TOKENS)["users"]) == ["user-1", "user-2"]
    configured.will_grant("user-2", "other@example.test", ["t-b"])         # ...takes over B
    assert run_cli(["xero", "auth", "--timeout", "10"])[0] == 0
    assert sorted(store.load(store.TOKENS)["users"]) == ["user-1", "user-2"], \
        "A still relies on login 1"
    assert tenants.registry()["t-b"]["user_id"] == "user-2"
    assert tenants.registry()["t-b"]["key"] == "B", "the key outlives a change of login"
    configured.will_grant("user-2", "other@example.test", ["t-a"])         # ...and then A
    assert run_cli(["xero", "auth", "--timeout", "10"])[0] == 0
    assert sorted(store.load(store.TOKENS)["users"]) == ["user-2"]
    code, env = run_cli(["xero", "accounts"])
    assert code == 0 and [o["status"] for o in env["data"]["organisations"]] == ["live"] * 3
    code, env = run_cli(["xero", "pull", "--as-at", "2026-06-30", "--all", "--reports", "tb",
                         "--out", str(tmp_path / "p")])
    assert code == 0, env["problems"]


# If Xero's "which organisation did this consent grant" filter ever answers with
# nothing, the tool must neither guess nor become unable to connect anything.

def test_a_consent_xero_does_not_attribute_is_never_guessed(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.event_filter_blind = True
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_ORG_NOT_NAMED"
    assert "'Entity A Pty Ltd'" in env["problems"][0]["message"]
    assert "--expect-org" in env["problems"][0]["message"]
    assert _token_file() is None and tenants.registry() == {}


def test_naming_the_company_connects_it_when_xero_does_not_attribute(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.add_org(Org("t-b", "Entity B Pty Ltd"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"})
    configured.event_filter_blind = True
    configured.will_grant("user-1", "adviser@example.test", ["t-b"])
    code, env = run_cli(["xero", "auth", "--key", "B", "--expect-org", "entity b pty ltd",
                         "--timeout", "10"])
    assert code == 0, env["problems"]
    assert env["data"]["step"] == "connected" and env["data"]["organisation"]["key"] == "B"
    assert any("asked for by name" in w for w in env["warnings"])
    assert sorted(tenants.registry()) == ["t-a", "t-b"]


def test_a_renewal_says_what_else_that_login_holds_but_takes_none_of_it(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.add_org(Org("t-x", "Demo Company (AU)"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"})
    configured.event_filter_blind = True
    configured.will_grant("user-1", "adviser@example.test", ["t-x"])       # picked by mistake
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 0 and env["data"]["step"] == "renewed"
    assert sorted(tenants.registry()) == ["t-a"], "an unnamed organisation is not taken"
    assert any("'Demo Company (AU)'" in w and "not held here" in w for w in env["warnings"])


def test_a_stray_connection_can_be_let_go_from_this_side(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    xero.add_org(Org("t-x", "Demo Company (AU)"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    xero.users["user-1"]["connections"].append(
        {"id": "conn-x", "authEventId": "old", "tenantId": "t-x",
         "tenantType": "ORGANISATION", "tenantName": "Demo Company (AU)"})
    assert run_cli(["xero", "accounts"])[1]["data"]["connected_at_xero"] == 2
    code, env = run_cli(["xero", "disconnect", "--org", "Demo"])        # part of a name
    assert code == 1 and env["problems"][0]["code"] == "ORG_UNKNOWN"
    code, env = run_cli(["xero", "disconnect", "--org", "demo company (au)"])
    assert code == 0, env["problems"]
    assert any("not held on this Mac" in w for w in env["warnings"])
    assert [c["tenantId"] for c in xero.users["user-1"]["connections"]] == ["t-a"]
    assert sorted(tenants.registry()) == ["t-a"]
    code, env = run_cli(["xero", "accounts"])
    assert code == 0 and env["data"]["connected_at_xero"] == 1


# -- what a mutation pass found unpinned ----------------------------------------------

def _paste(configured, run_cli, *extra):
    """Start a pasted consent; return the query of the link and the address it lands on."""
    code, env = run_cli(["xero", "auth", "--paste", *extra])
    assert code == 0, env["problems"]
    q = {k: v[0] for k, v in urllib.parse.parse_qs(
        urllib.parse.urlparse(env["data"]["link"]).query).items()}
    configured.expected_challenge = q["code_challenge"]
    return q, f"{q['redirect_uri']}?code={configured.good_code}&state={q['state']}"


def test_config_refuses_a_port_no_app_could_register(xero, run_cli):
    code, env = run_cli(["xero", "config", "--client-id", CLIENT_ID, "--port", "80"])
    assert code == 2 and env["problems"][0]["code"] == "PORT_INVALID"
    assert store.load(store.APP) == {}, "nothing is half-saved"


def test_accounts_with_nothing_connected_is_exit_3_with_the_fix(configured, run_cli):
    code, env = run_cli(["xero", "accounts"])
    assert code == 3 and env["problems"][0]["code"] == "XERO_NOT_SIGNED_IN"
    assert env["problems"][0]["fix"] == "fma xero auth"
    assert not configured.log, "there is nobody to ask Xero as"


def test_disconnect_says_which_organisation_or_refuses(configured, run_cli):
    code, env = run_cli(["xero", "disconnect"])
    assert code == 1 and env["problems"][0]["code"] == "ORG_REQUIRED"


def test_a_removal_xero_refuses_does_not_forget_the_connection(xero, run_cli):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    xero.delete_status = 500
    code, env = run_cli(["xero", "disconnect", "--org", "A"])
    assert code == 3 and env["problems"][0]["code"] == "XERO_UNREACHABLE"
    assert "t-a" in tenants.registry() and len(xero.users["user-1"]["connections"]) == 1


def test_a_stray_name_two_logins_both_hold_is_not_guessed(xero, run_cli):
    for t, n in (("t-a", "Entity A Pty Ltd"), ("t-b", "Entity B Pty Ltd"),
                 ("t-x", "Demo Company (AU)"), ("t-y", "Demo Company (AU)")):
        xero.add_org(Org(t, n))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    signed_in(xero, ["t-b"], keys={"t-b": "B"}, user_id="user-2", email="other@example.test")
    for uid, tid in (("user-1", "t-x"), ("user-2", "t-y")):
        xero.users[uid]["connections"].append(
            {"id": f"conn-{tid}", "authEventId": "old", "tenantId": tid,
             "tenantType": "ORGANISATION", "tenantName": "Demo Company (AU)"})
    code, env = run_cli(["xero", "disconnect", "--org", "Demo Company (AU)"])
    assert code == 1 and env["problems"][0]["code"] == "ORG_UNKNOWN"
    assert [len(xero.users[u]["connections"]) for u in ("user-1", "user-2")] == [2, 2]


def test_the_wrong_company_that_is_already_held_here_is_left_connected(configured, browser, run_cli):
    # Login 1 holds A here and means to add B, but picks A again at Xero. Refusing is
    # right; "undoing" would cut the connection A's pulls run on.
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"})
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    code, env = run_cli(["xero", "auth", "--expect-org", "Entity B Pty Ltd", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_WRONG_ORG"
    assert "disconnected again" not in env["problems"][0]["message"]
    assert [c["tenantId"] for c in configured.users["user-1"]["connections"]] == ["t-a"]
    code, env = run_cli(["xero", "accounts"])
    assert code == 0 and env["data"]["organisations"][0]["status"] == "live"


def test_a_login_that_holds_nothing_here_renews_nothing_of_anothers(configured, browser, run_cli):
    # A is held here under login 1. Login 2 can see A at Xero too, signs in, and Xero
    # names nothing as granted. A's row is login 1's: this is not a renewal of it.
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(configured, ["t-a"], keys={"t-a": "A"})
    configured.users["user-2"] = {"email": "other@example.test", "connections": [
        {"id": "conn-user-2-t-a", "authEventId": "old", "tenantId": "t-a",
         "tenantType": "ORGANISATION", "tenantName": "Entity A Pty Ltd"}]}
    configured.event_filter_blind = True
    configured.will_grant("user-2", "other@example.test", [], event="event-x")
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_NOT_ONE_ORG"
    assert list(store.load(store.TOKENS)["users"]) == ["user-1"]
    assert tenants.registry()["t-a"]["user_id"] == "user-1"


def test_a_name_that_matches_nothing_takes_nothing_and_removes_nothing(configured, browser, run_cli):
    # Xero does not say what was granted, the login holds one company under this app,
    # and the name asked for is another: that company is not taken, and not removed.
    configured.add_org(Org("t-c", "Entity C Pty Ltd"))
    configured.event_filter_blind = True
    configured.will_grant("user-1", "adviser@example.test", ["t-c"])
    code, env = run_cli(["xero", "auth", "--expect-org", "Entity D Pty Ltd", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_ORG_NOT_NAMED"
    assert "'Entity C Pty Ltd'" in env["problems"][0]["message"]
    assert len(configured.users["user-1"]["connections"]) == 1
    assert _token_file() is None and tenants.registry() == {}


def test_a_sign_in_that_could_not_be_kept_alive_is_not_saved(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    configured.drop_from_token = {"refresh_token"}
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "SIGN_IN_INCOMPLETE"
    assert _token_file() is None and tenants.registry() == {}


def test_a_connection_that_names_no_organisation_is_not_saved(configured, browser, run_cli, monkeypatch):
    from fma_tools.xero.client import XeroClient
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    monkeypatch.setattr(XeroClient, "connections",
                        lambda self, *a, **k: [{"id": "c-1", "tenantName": "Entity A Pty Ltd"}])
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 1 and env["problems"][0]["code"] == "SIGN_IN_INCOMPLETE"
    assert _token_file() is None and tenants.registry() == {}


def test_a_permission_xero_left_out_of_the_sign_in_is_said(configured, browser, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    configured.grant_without = {"accounting.reports.aged.read"}
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 0, env["problems"]
    assert any("granted the sign-in without: accounting.reports.aged.read" in w
               for w in env["warnings"])


def test_the_fifth_company_says_the_free_tier_is_full(configured, browser, run_cli):
    ids = [f"t-{i}" for i in range(5)]
    for i, t in enumerate(ids):
        configured.add_org(Org(t, f"Entity {i} Pty Ltd"))
    signed_in(configured, ids[:3])
    configured.will_grant("user-1", "adviser@example.test", [ids[3]])
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 0 and env["data"]["connected"] == 4
    assert not any("per app" in w for w in env["warnings"])
    configured.will_grant("user-1", "adviser@example.test", [ids[4]])
    code, env = run_cli(["xero", "auth", "--timeout", "10"])
    assert code == 0 and env["data"]["connected"] == 5
    assert any("5 organisations are connected" in w and "holds 5 per app" in w
               for w in env["warnings"])


def test_a_pasted_answer_with_no_code_is_refused(configured, run_cli):
    q, _ = _paste(configured, run_cli)
    code, env = run_cli(["xero", "auth", "--redirect", f"{q['redirect_uri']}?state={q['state']}"])
    assert code == 1 and env["problems"][0]["code"] == "NO_CODE"


def test_a_pasted_consent_left_too_long_is_ended(configured, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.will_grant("user-1", "adviser@example.test", ["t-a"])
    _, landed = _paste(configured, run_cli)
    pending = store.load(store.PENDING)
    pending["created_at"] -= 16 * 60
    store.save(store.PENDING, pending)
    code, env = run_cli(["xero", "auth", "--redirect", landed])
    assert code == 1 and env["problems"][0]["code"] == "PENDING_CONSENT_EXPIRED"
    assert not (store.config_dir() / store.PENDING).exists()
    assert tenants.registry() == {}


def test_the_paste_route_remembers_which_company_was_asked_for(configured, run_cli):
    configured.add_org(Org("t-demo", "Demo Company (AU)"))
    configured.will_grant("user-1", "adviser@example.test", ["t-demo"])
    _, landed = _paste(configured, run_cli, "--expect-org", "Entity A Pty Ltd")
    code, env = run_cli(["xero", "auth", "--redirect", landed])
    assert code == 1 and env["problems"][0]["code"] == "CONSENT_WRONG_ORG"
    assert tenants.registry() == {} and configured.users["user-1"]["connections"] == []


def test_a_key_taken_while_a_pasted_consent_waited_is_not_shared(configured, run_cli):
    configured.add_org(Org("t-a", "Entity A Pty Ltd"))
    configured.add_org(Org("t-b", "Entity B Pty Ltd"))
    signed_in(configured, ["t-a"])                           # A is here, with no key yet
    configured.will_grant("user-1", "adviser@example.test", ["t-b"])
    _, landed = _paste(configured, run_cli, "--key", "NSW")
    assert run_cli(["xero", "accounts", "--set-key", "Entity A Pty Ltd", "NSW"])[0] == 0
    code, env = run_cli(["xero", "auth", "--redirect", landed])
    assert code == 0, env["problems"]
    assert any("already names another organisation" in w for w in env["warnings"])
    assert tenants.registry()["t-a"]["key"] == "NSW" and tenants.registry()["t-b"]["key"] is None

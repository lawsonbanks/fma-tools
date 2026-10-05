"""xero client: the sign-in is kept alive without ever being lost or shown."""

import json
import os
import stat
import time

import pytest

from fma_tools.errors import EnvProblem, InputProblem, Refusal
from fma_tools.xero import store
from fma_tools.xero.client import XeroClient
from xero_fakes import Org, signed_in

AS_AT = "2026-06-30"


@pytest.fixture
def one(xero):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    return xero


def _saved_refresh_token() -> str:
    return store.load(store.TOKENS)["users"]["user-1"]["refresh_token"]


def test_the_new_refresh_token_is_on_disk_before_it_is_used(one):
    old = signed_in(one, ["t-a"], expired=True)["refresh_token"]
    seen = []
    # at the moment Xero is asked for a report, what does the disk hold?
    one.before_api = lambda path, tenant: seen.append(_saved_refresh_token())
    XeroClient().get("user-1", "t-a", "Organisation")
    assert seen and seen[0] != old, "the rotated token was not saved before use"
    assert seen[0] in one.refresh_tokens, "what is on disk is the one Xero will accept"
    assert old not in one.refresh_tokens, "Xero spent the old one"
    p = store.config_dir() / store.TOKENS
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_a_token_that_is_still_good_is_not_spent(one):
    tok = signed_in(one, ["t-a"])
    XeroClient().get("user-1", "t-a", "Organisation")
    assert _saved_refresh_token() == tok["refresh_token"]
    assert not [u for _, u, _ in one.log if "connect/token" in u]


def test_a_renewal_that_cannot_be_saved_says_exactly_that(one, monkeypatch):
    # Xero has already retired the token on disk by the time the save fails, so this is
    # not "a bug, exit 4": the person has about half an hour and needs to be told.
    signed_in(one, ["t-a"], expired=True)
    path = store.config_dir() / store.TOKENS
    before = path.read_bytes()
    tries = []

    def no_replace(src, dst):
        tries.append(dst)
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", no_replace)
    with pytest.raises(EnvProblem) as e:
        XeroClient().get("user-1", "t-a", "Organisation")
    assert e.value.code == "XERO_SIGN_IN_NOT_SAVED" and e.value.exit_code == 3
    assert "renewed the sign-in but it could not be saved" in str(e.value)
    assert "half an hour" in str(e.value) and e.value.fix
    assert len(tries) == 2, "one retry, then say so"
    assert path.read_bytes() == before, "the file is whole, though what it holds is spent"
    assert not list(store.config_dir().glob(".*.tmp")), "no half-written file is left"
    assert not one.api_calls(), "the unsaved token must not have been used"
    for token in one.issued:
        assert token not in str(e.value)


def test_a_sign_in_xero_has_ended_is_exit_3_with_the_fix(one):
    tok = signed_in(one, ["t-a"], expired=True)
    one.refresh_tokens.pop(tok["refresh_token"])
    before = (store.config_dir() / store.TOKENS).read_bytes()
    with pytest.raises(EnvProblem) as e:
        XeroClient().get("user-1", "t-a", "Organisation")
    assert e.value.code == "XERO_SIGN_IN_DEAD" and e.value.fix == "fma xero auth"
    assert "invalid_grant" in str(e.value)
    assert (store.config_dir() / store.TOKENS).read_bytes() == before


def test_a_network_blip_is_not_a_verdict_on_the_sign_in(one):
    signed_in(one, ["t-a"], expired=True)
    before = (store.config_dir() / store.TOKENS).read_bytes()
    one.transport_down = True
    with pytest.raises(EnvProblem) as e:
        XeroClient().get("user-1", "t-a", "Organisation")
    assert e.value.code == "XERO_UNREACHABLE"
    assert "auth" not in (e.value.fix or ""), "an outage is not cured by signing in again"
    assert (store.config_dir() / store.TOKENS).read_bytes() == before
    one.transport_down = False
    XeroClient().get("user-1", "t-a", "Organisation")          # and it recovers


def test_a_429_waits_as_long_as_xero_says(one):
    signed_in(one, ["t-a"])
    one.rate_limit = [{"retry-after": "7", "x-rate-limit-problem": "minute"}]
    slept = []
    c = XeroClient(sleep=slept.append)
    c.get("user-1", "t-a", "Organisation")
    assert slept == [7.0]
    assert c.spend["t-a"].calls == 2


def test_a_spent_daily_allowance_is_a_refusal_not_a_retry(one):
    signed_in(one, ["t-a"])
    one.rate_limit = [{"retry-after": "3600", "x-rate-limit-problem": "day"}]
    slept = []
    with pytest.raises(Refusal) as e:
        XeroClient(sleep=slept.append).get("user-1", "t-a", "Organisation")
    assert e.value.code == "XERO_DAILY_LIMIT" and slept == []


def test_endless_429s_end_in_exit_3(one):
    signed_in(one, ["t-a"])
    one.rate_limit = [{"retry-after": "1"}] * 10
    with pytest.raises(EnvProblem) as e:
        XeroClient(sleep=lambda s: None).get("user-1", "t-a", "Organisation")
    assert e.value.code == "XERO_RATE_LIMITED"


def test_calls_are_paced_under_sixty_a_minute(one):
    signed_in(one, ["t-a"])
    one.day_remaining = None
    now, slept = [1000.0], []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
    c = XeroClient(clock=lambda: now[0], sleep=sleep)
    # keep the saved access token "fresh" on this pretend clock
    tokens = store.load(store.TOKENS)
    tokens["users"]["user-1"]["access_expires_at"] = now[0] + 10_000
    store.save(store.TOKENS, tokens)
    for _ in range(70):
        c.get("user-1", "t-a", "Organisation")
        now[0] += 0.01
    assert slept, "seventy calls in under a second must have been held back"
    assert sum(slept) >= 59


def test_the_allowance_xero_reports_is_read(one):
    signed_in(one, ["t-a"])
    one.day_remaining = 500
    c = XeroClient()
    c.get("user-1", "t-a", "Organisation")
    assert c.spend["t-a"].day_remaining == 499 and c.spend["t-a"].minute_remaining == 59


def test_a_call_that_names_no_organisation_is_refused(one):
    signed_in(one, ["t-a"])
    with pytest.raises(Refusal) as e:
        XeroClient().get("user-1", "", "Organisation")
    assert e.value.code == "ORG_REQUIRED" and not one.api_calls()


def test_an_organisation_this_login_cannot_reach_is_refused(one):
    signed_in(one, ["t-a"])
    one.add_org(Org("t-z", "Entity Z Pty Ltd"))
    with pytest.raises(Refusal) as e:
        XeroClient().get("user-1", "t-z", "Organisation")
    assert e.value.code == "XERO_ACCESS_REMOVED"


def test_an_access_token_xero_rejects_is_refreshed_once(one):
    tok = signed_in(one, ["t-a"])
    one.access.pop(tok["access_token"])              # Xero no longer honours it
    XeroClient().get("user-1", "t-a", "Organisation")
    assert _saved_refresh_token() != tok["refresh_token"]


def test_no_login_means_exit_3_with_the_fix(one):
    store.save(store.APP, {"client_id": "A" * 32, "port": 8976})
    with pytest.raises(EnvProblem) as e:
        XeroClient().get("nobody", "t-a", "Organisation")
    assert e.value.code == "XERO_NOT_SIGNED_IN" and e.value.fix == "fma xero auth"


def test_a_corrupt_store_is_never_read_as_an_empty_one(one, run_cli):
    signed_in(one, ["t-a"], keys={"t-a": "A"})
    (store.config_dir() / store.TOKENS).write_text("{ not json")
    code, env = run_cli(["xero", "accounts"])
    assert code == 2 and env["problems"][0]["code"] == "XERO_STORE_UNREADABLE"
    assert "Do not delete it blind" in env["problems"][0]["message"]


def test_a_bug_cannot_carry_a_token_into_the_envelope(one, run_cli, monkeypatch):
    tok = signed_in(one, ["t-a"], keys={"t-a": "A"})
    from fma_tools.xero import pull

    def boom(args):
        raise RuntimeError(f"unexpected: {tok['refresh_token']} and {tok['access_token']}")
    monkeypatch.setattr(pull, "run", boom)
    code, env = run_cli(["xero", "pull", "--as-at", AS_AT, "--all", "--out", "/tmp/x"])
    assert code == 4 and env["problems"][0]["code"] == "INTERNAL"
    text = json.dumps(env)
    assert tok["refresh_token"] not in text and tok["access_token"] not in text
    assert "[redacted]" in text


def test_writes_are_whole_files(one):
    signed_in(one, ["t-a"])
    d = store.config_dir()
    assert sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) == \
        ["app.json", "tenants.json", "tokens.json"]
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    json.loads((d / "tokens.json").read_text())


def test_clock_and_sleep_default_to_the_real_ones(one):
    signed_in(one, ["t-a"])
    c = XeroClient()
    assert c.clock is time.time and c.sleep is time.sleep


def test_the_real_transport_names_itself_and_verifies_with_certifi(monkeypatch):
    import urllib.request
    from fma_tools.xero import transport
    seen = {}

    class Reply:
        status = 200
        headers = {"X-DayLimit-Remaining": "999"}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["agent"] = req.get_header("User-agent")
        seen["tenant"] = req.get_header("Xero-tenant-id")
        seen["verifies"] = context is not None and context.verify_mode.name == "CERT_REQUIRED"
        return Reply()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r = transport.UrllibTransport().request("GET", "https://example.test/x",
                                            {"Xero-Tenant-Id": "t-a"})
    assert seen["agent"].startswith("fma-tools/") and seen["tenant"] == "t-a"
    assert seen["verifies"]
    assert r.status == 200 and r.headers == {"x-daylimit-remaining": "999"}


def test_a_response_cut_off_mid_body_is_a_failed_connection_not_a_bug(monkeypatch):
    import http.client
    import urllib.request
    from fma_tools.xero import transport

    for failure in (http.client.IncompleteRead(b"half"), http.client.BadStatusLine("")):
        def broken(req, timeout=None, context=None, failure=failure):
            raise failure
        monkeypatch.setattr(urllib.request, "urlopen", broken)
        with pytest.raises(transport.TransportError) as e:
            transport.UrllibTransport().request("GET", "https://example.test/x", {})
        assert str(e.value) == type(failure).__name__


def test_a_transport_failure_carries_its_kind_and_nothing_else(monkeypatch):
    import urllib.error
    import urllib.request
    from fma_tools.xero import transport

    def down(req, timeout=None, context=None):
        raise urllib.error.URLError("refused: https://example.test/x?code=SECRET-CODE")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(transport.TransportError) as e:
        transport.UrllibTransport().request("GET", "https://example.test/x?code=SECRET-CODE", {})
    assert str(e.value) == "URLError"


@pytest.mark.parametrize("status, body, kind, code", [
    (503, b"upstream", EnvProblem, "XERO_UNREACHABLE"),
    (500, b'{"Title":"An error occurred"}', EnvProblem, "XERO_UNREACHABLE"),
    (400, b'{"Type":"ValidationException","Message":"The date range is too long"}',
     None, "XERO_REJECTED"),
])
def test_xeros_other_answers_map_to_the_contract(one, status, body, kind, code):
    from fma_tools.errors import InputProblem
    signed_in(one, ["t-a"])
    one.api_status = (status, body)
    with pytest.raises(kind or InputProblem) as e:
        XeroClient().get("user-1", "t-a", "Reports/ProfitAndLoss")
    assert e.value.code == code
    if status == 400:
        assert "The date range is too long" in str(e.value), "Xero's own reason is passed on"


def test_a_resource_xero_does_not_have_is_exit_2(one):
    from fma_tools.errors import InputProblem
    signed_in(one, ["t-a"])
    with pytest.raises(InputProblem) as e:
        XeroClient().get("user-1", "t-a", "Reports/NoSuchReport")
    assert e.value.code == "XERO_NOT_FOUND" and e.value.exit_code == 2


def test_an_answer_that_is_not_json_is_exit_2(one):
    from fma_tools.errors import InputProblem
    signed_in(one, ["t-a"])
    one.api_status = (200, b"<Response><Id>xml, because Accept was ignored</Id></Response>")
    with pytest.raises(InputProblem) as e:
        XeroClient().get("user-1", "t-a", "Organisation")
    assert e.value.code == "XERO_NOT_JSON"


def test_a_refresh_answer_without_a_new_token_never_blanks_the_old_one(one, monkeypatch):
    from fma_tools.xero import oauth
    old = signed_in(one, ["t-a"], expired=True)["refresh_token"]
    real = oauth.refresh

    def without_rotation(transport, client_id, refresh_token):
        answer = real(transport, client_id, refresh_token)
        answer.pop("refresh_token")
        return answer
    monkeypatch.setattr(oauth, "refresh", without_rotation)
    XeroClient().get("user-1", "t-a", "Organisation")
    assert _saved_refresh_token() == old


def test_the_guards_survive_a_test_that_undoes_its_patches(monkeypatch):
    # The floor under the per-test guards: even with every monkeypatch undone, the
    # sign-in folder is a throwaway and the transport refuses.
    from pathlib import Path
    from fma_tools.xero import transport
    with monkeypatch.context() as m:
        m.delenv("FMA_CONFIG_DIR", raising=False)
    monkeypatch.undo()
    try:
        assert "fma-config-session-" in str(store.config_dir())
        assert Path.home() / ".config" not in store.config_dir().parents
        with pytest.raises(AssertionError):
            transport.default_transport()
        import webbrowser
        assert webbrowser.open.__name__ == "_no_browser", "and no real browser either"
    finally:
        pass


def test_a_body_cut_off_while_it_is_being_read_is_a_failed_connection(monkeypatch):
    import http.client
    import urllib.error
    import urllib.request
    from fma_tools.xero import transport

    class CutOff:
        status = 200
        headers = {}

        def read(self):
            raise http.client.IncompleteRead(b"half")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: CutOff())
    with pytest.raises(transport.TransportError) as e:
        transport.UrllibTransport().request("GET", "https://example.test/x", {})
    assert str(e.value) == "IncompleteRead"

    class Unreadable(urllib.error.HTTPError):
        def read(self, *a):
            raise http.client.IncompleteRead(b"")

    def refused(*a, **k):
        raise Unreadable("https://example.test/x", 503, "Service Unavailable",
                         {"Retry-After": "3"}, None)
    monkeypatch.setattr(urllib.request, "urlopen", refused)
    r = transport.UrllibTransport().request("GET", "https://example.test/x", {})
    assert (r.status, r.body, r.headers) == (503, b"", {"retry-after": "3"})


def test_a_token_answer_with_an_unreadable_lifetime_does_not_lose_the_sign_in(one, monkeypatch):
    from fma_tools.xero import oauth
    signed_in(one, ["t-a"], expired=True)
    real = oauth.refresh

    def strange(transport, client_id, refresh_token):
        answer = real(transport, client_id, refresh_token)
        answer["expires_in"] = None
        return answer
    monkeypatch.setattr(oauth, "refresh", strange)
    XeroClient().get("user-1", "t-a", "Organisation")
    assert _saved_refresh_token() in one.refresh_tokens, "the new sign-in was saved"


def test_a_store_file_that_is_not_an_object_is_never_read_as_empty(one):
    signed_in(one, ["t-a"])
    (store.config_dir() / store.TOKENS).write_text("[]")
    with pytest.raises(InputProblem) as e:
        store.load(store.TOKENS)
    assert e.value.code == "XERO_STORE_UNREADABLE"


def test_the_lock_is_a_real_one(one):
    # Two processes refreshing at once would each spend the same token, and Xero keeps
    # only the last answer. A second holder must be kept out, not waved through.
    import fcntl
    with store.locked():
        fd = os.open(store.config_dir() / ".lock", os.O_WRONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    fd = os.open(store.config_dir() / ".lock", os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)        # free again once released
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_a_sign_in_is_stamped_with_when_it_lapses():
    from fma_tools.xero import oauth
    kept = oauth.stamp({"access_token": "a", "refresh_token": "r", "expires_in": 1800,
                        "scope": "x y"}, 1_000.0)
    assert kept == {"access_token": "a", "refresh_token": "r", "access_expires_at": 2_800.0,
                    "refresh_issued_at": 1_000.0, "scope": "x y"}


def test_an_answer_about_connections_that_is_not_a_list_is_exit_2(one, run_cli):
    signed_in(one, ["t-a"], keys={"t-a": "A"})
    one.connections_status = 200                    # a 200 that carries {} and no list
    code, env = run_cli(["xero", "accounts"])
    assert code == 2 and env["problems"][0]["code"] == "XERO_NOT_JSON"

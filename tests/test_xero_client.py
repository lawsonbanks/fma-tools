"""xero client: the sign-in is kept alive without ever being lost or shown."""

import json
import os
import stat
import time

import pytest

from fma_tools.errors import EnvProblem, Refusal
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


def test_a_save_that_fails_leaves_the_old_sign_in_whole(one, monkeypatch):
    signed_in(one, ["t-a"], expired=True)
    path = store.config_dir() / store.TOKENS
    before = path.read_bytes()

    def no_replace(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", no_replace)
    with pytest.raises(OSError):
        XeroClient().get("user-1", "t-a", "Organisation")
    assert path.read_bytes() == before
    assert not list(store.config_dir().glob(".*.tmp")), "no half-written file is left"
    assert not one.api_calls(), "the unsaved token must not have been used"


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

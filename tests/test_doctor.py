"""doctor: every FAIL carries exactly one copy-pasteable fix line; exit 0/3."""


def test_doctor_runs_and_every_fail_carries_a_fix(run_cli):
    code, env = run_cli(["doctor"])
    checks = env["data"]["checks"]
    assert checks, "doctor ran no checks"
    for c in checks:
        assert c["status"] in ("ok", "FAIL")
        if c["status"] == "FAIL":
            assert c["fix"], f"FAIL without a fix line: {c}"
    if any(c["status"] == "FAIL" for c in checks):
        assert code == 3
        assert env["status"] == "error"
        for p in env["problems"]:
            assert p.get("fix"), f"problem without a fix line: {p}"
    else:
        assert code == 0


def test_forced_failure_is_exit_3_with_fix(run_cli, monkeypatch):
    from fma_tools import doctor
    def broken():
        raise RuntimeError("Python too old (forced)")
    monkeypatch.setattr(doctor, "_check_python", broken)
    code, env = run_cli(["doctor"])
    assert code == 3
    fails = [c for c in env["data"]["checks"] if c["status"] == "FAIL"]
    assert any("forced" in c["detail"] for c in fails)
    assert all(c["fix"] for c in fails)


def test_dir_check_ok_and_missing(run_cli, tmp_path):
    code, env = run_cli(["doctor", "--dir", str(tmp_path)])
    by_name = {c["check"]: c for c in env["data"]["checks"]}
    assert by_name[f"directory {tmp_path}"]["status"] == "ok"

    code, env = run_cli(["doctor", "--dir", str(tmp_path / "nope")])
    assert code == 3
    fails = [c for c in env["data"]["checks"] if c["status"] == "FAIL"]
    assert any("not a directory" in c["detail"] for c in fails)


def _by_name(env):
    return {c["check"]: c for c in env["data"]["checks"]}


def test_all_five_subcommands_are_wired(run_cli):
    code, env = run_cli(["doctor"])
    wired = _by_name(env)["all five subcommands wired"]
    assert wired["status"] == "ok" and "xero" in wired["detail"]


def test_a_mac_that_never_uses_xero_has_nothing_to_fail(run_cli):
    code, env = run_cli(["doctor"])
    xero = _by_name(env)["xero"]
    assert xero["status"] == "ok" and "not configured" in xero["detail"]
    assert not [c for c in env["data"]["checks"] if c["check"].startswith("xero ")]


def test_a_configured_mac_without_a_sign_in_is_still_healthy(run_cli):
    assert run_cli(["xero", "config", "--client-id", "A" * 32])[0] == 0
    code, env = run_cli(["doctor"])
    checks = _by_name(env)
    assert checks["xero folder is private"]["status"] == "ok"
    assert checks["xero app"]["status"] == "ok"
    assert "no organisation connected yet" in checks["xero sign-in"]["detail"]


def test_a_sign_in_file_others_can_read_fails_and_fix_repairs_it(run_cli):
    import os
    import stat
    from fma_tools.xero import store
    assert run_cli(["xero", "config", "--client-id", "A" * 32])[0] == 0
    app = store.config_dir() / store.APP
    os.chmod(app, 0o644)
    code, env = run_cli(["doctor"])
    private = _by_name(env)["xero folder is private"]
    assert code == 3 and private["status"] == "FAIL"
    assert "644" in private["detail"] and private["fix"].startswith("chmod 700")
    code, env = run_cli(["doctor", "--fix"])
    assert _by_name(env)["xero folder is private"]["status"] == "ok"
    assert stat.S_IMODE(app.stat().st_mode) == 0o600
    # and the folder itself, which is what keeps the file names from other accounts
    os.chmod(store.config_dir(), 0o755)
    code, env = run_cli(["doctor"])
    private = _by_name(env)["xero folder is private"]
    assert code == 3 and private["status"] == "FAIL" and "755, want 700" in private["detail"]
    run_cli(["doctor", "--fix"])
    assert stat.S_IMODE(store.config_dir().stat().st_mode) == 0o700


def test_a_lapsed_sign_in_fails_with_the_auth_fix(xero, run_cli):
    import time
    from xero_fakes import Org, signed_in
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"}, now=time.time() - 61 * 86400)
    code, env = run_cli(["doctor"])
    sign_in = _by_name(env)["xero sign-in"]
    assert code == 3 and sign_in["status"] == "FAIL"
    assert "61 days ago" in sign_in["detail"] and sign_in["fix"] == "fma xero auth"


def test_a_sign_in_near_its_lapse_says_so_without_failing(xero, run_cli):
    import time
    from xero_fakes import Org, signed_in
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"}, now=time.time() - 50 * 86400)
    code, env = run_cli(["doctor"])
    sign_in = _by_name(env)["xero sign-in"]
    assert sign_in["status"] == "ok" and "lapses it at 60" in sign_in["detail"]
    assert not xero.log, "the plain doctor never touches the network"


def test_doctor_xero_asks_xero(xero, run_cli):
    from xero_fakes import Org, signed_in
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    code, env = run_cli(["doctor", "--xero"])
    live = _by_name(env)["xero live: connections"]
    assert live["status"] == "ok" and "1 connection" in live["detail"]

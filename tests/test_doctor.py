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

"""The envelope contract: valid JSON on stdout on every exit path, uniform exit codes."""

import json


def test_internal_error_is_exit_4_with_valid_json(build_xlsx, run_cli, monkeypatch):
    from fma_tools.read_ledger import main as rl
    def boom(args):
        raise ValueError("synthetic bug")
    monkeypatch.setattr(rl, "run", boom)
    path = build_xlsx({"A1": 1.0})
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 4
    assert env["status"] == "error"
    assert env["problems"][0]["code"] == "INTERNAL"
    assert "synthetic bug" in env["problems"][0]["message"]


def test_envelope_shape_on_success(build_xlsx, run_cli):
    path = build_xlsx({"A1": 1.0})
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    assert set(env) == {"tool", "version", "status", "data", "problems", "warnings"}
    assert env["tool"] == "read-ledger"
    assert env["status"] == "ok"
    assert env["problems"] == []


def test_version_flag(capsys):
    from fma_tools import __version__
    from fma_tools.cli import main
    import pytest
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_human_summary_on_stderr(build_xlsx, capsys):
    from fma_tools.cli import main
    path = build_xlsx({"A1": 1.0})
    code = main(["read-ledger", str(path)])
    captured = capsys.readouterr()
    json.loads(captured.out)                      # stdout is the machine channel
    assert "[read-ledger]" in captured.err        # stderr is the human channel

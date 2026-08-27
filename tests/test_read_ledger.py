"""read-ledger: the trap, the grammar's refusals (each proven RED), and the contract."""

from datetime import datetime

import openpyxl
import pytest

from conftest import cell, sheet, xeroify


# ── the trap ─────────────────────────────────────────────────────────────────

def test_trap_reproduced_then_evaluated(build_xlsx, run_cli):
    path = build_xlsx({"B2": 100.5, "B3": 200.25, "B4": 50.25,
                       "B5": "=SUM(B2:B4)", "B6": "=(B5-B2)"})
    xeroify(path)
    # First prove the fixture carries the real trap: the naive cold read gets 0. Not
    # None. Zero -- the value every downstream numeric guard happily accepts.
    naive = openpyxl.load_workbook(path, data_only=True).active
    assert naive["B5"].value == 0
    assert naive["B6"].value == 0
    # Now the tool returns the true figures.
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    assert cell(env, "B5") == pytest.approx(351.0)
    assert cell(env, "B6") == pytest.approx(250.5)
    assert sheet(env)["formula_count"] == 2
    refs = {f["ref"]: f for f in sheet(env)["formula_cells"]}
    assert refs["Sheet1!B5"]["formula"] == "=SUM(B2:B4)"
    assert refs["Sheet1!B5"]["value"] == pytest.approx(351.0)


def test_comma_list_sum_and_row_range(build_xlsx, run_cli):
    path = build_xlsx({"D2": 10.0, "D3": 999.0, "D4": 32.5,
                       "D6": "=SUM(D2,D4)",          # grand-total comma form
                       "A8": 1.0, "B8": 2.0, "C8": 3.0,
                       "E8": "=SUM(A8:C8)"})         # row range (aged Total column)
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    assert cell(env, "D6") == pytest.approx(42.5)
    assert cell(env, "E8") == pytest.approx(6.0)


# ── refusals, each RED ───────────────────────────────────────────────────────

def _refuses(run_cli, path, *needles):
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 1
    assert env["status"] == "refuse"
    assert env["problems"][0]["code"] == "FORMULA_UNEVALUATED"
    msg = env["problems"][0]["message"]
    for n in needles:
        assert n in msg, f"{n!r} not in {msg!r}"
    return msg


def test_unknown_function_refuses(build_xlsx, run_cli):
    path = build_xlsx({"A1": "=VLOOKUP(B1,C1:D9,2)"})
    _refuses(run_cli, path, "Sheet1!A1", "refusing")


def test_rectangle_sum_refuses(build_xlsx, run_cli):
    path = build_xlsx({"A2": 1, "B2": 2, "A3": 3, "B3": 4, "C5": "=SUM(A2:B3)"})
    _refuses(run_cli, path, "spans both rows and columns")


def test_cycle_refuses(build_xlsx, run_cli):
    path = build_xlsx({"A1": "=B1", "B1": "=A1"})
    _refuses(run_cli, path, "nesting past", "cycle")


def test_text_operand_refuses(build_xlsx, run_cli):
    path = build_xlsx({"A1": "Total Trading Income", "A2": "=A1+1"})
    _refuses(run_cli, path, "holds text", "#VALUE!")


def test_division_by_zero_refuses(build_xlsx, run_cli):
    path = build_xlsx({"A1": "=1/0"})
    _refuses(run_cli, path, "#DIV/0!")


def test_cross_sheet_reference_refuses(build_xlsx, run_cli):
    path = build_xlsx({"A1": "=Other!B2"}, extra_sheets={"Other": {"B2": 5}})
    code, env = run_cli(["read-ledger", str(path), "--sheet", "Sheet1"])
    assert code == 1
    assert env["problems"][0]["code"] == "FORMULA_UNEVALUATED"


# ── Excel semantics deliberately kept ────────────────────────────────────────

def test_blank_reference_is_zero(build_xlsx, run_cli):
    path = build_xlsx({"A2": "=Z99+5"})
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    assert cell(env, "A2") == pytest.approx(5.0)


def test_sum_skips_text_and_blank(build_xlsx, run_cli):
    path = build_xlsx({"B1": "Opening balance", "B2": 1.5, "B4": 2.5,
                       "B6": "=SUM(B1:B5)"})
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    assert cell(env, "B6") == pytest.approx(4.0)


# ── serialisation ────────────────────────────────────────────────────────────

def test_empty_cell_is_null_never_empty_string(build_xlsx, run_cli):
    path = build_xlsx({"A1": "x", "C1": "y"})
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    assert cell(env, "B1") is None
    assert "" not in sheet(env)["rows"][0]


def test_date_cell_is_iso_string(build_xlsx, run_cli):
    path = build_xlsx({"A1": datetime(2026, 6, 30)})
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    assert cell(env, "A1") == "2026-06-30"


# ── metadata + the date gate ─────────────────────────────────────────────────

# Xero's real header order: report title first, entity second, date line third
# (measured on live exports).
_HEADER = {"A1": "Aged Receivables Detail", "A2": "Fixture Pty Ltd",
           "A3": "As at 30 June 2026",
           "A5": "Invoice Number", "B5": "Due Date", "C5": "Total",
           "A6": "INV-0001", "B6": datetime(2026, 5, 1), "C6": 11.0}


def test_header_metadata_extracted(build_xlsx, run_cli):
    path = build_xlsx(_HEADER)
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    md = env["data"]["metadata"]
    assert md["entity"] == "Fixture Pty Ltd"
    assert md["report_title"] == "Aged Receivables Detail"
    assert md["report_date"] == "2026-06-30"
    assert md["report_date_raw"] == "As at 30 June 2026"


def test_period_header_extracted(build_xlsx, run_cli):
    path = build_xlsx({"A1": "Profit and Loss", "A2": "Fixture Pty Ltd",
                       "A3": "For the month ended 31 July 2026", "A5": "Account",
                       "B5": "Jul 2026", "A6": "Sales", "B6": 22.0})
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    assert env["data"]["metadata"]["report_period"] == {"start": None, "end": "2026-07-31"}


def test_expect_date_pass(build_xlsx, run_cli):
    path = build_xlsx(_HEADER)
    code, env = run_cli(["read-ledger", str(path), "--expect-date", "2026-06-30"])
    assert code == 0


def test_expect_date_mismatch_refuses_quoting_both(build_xlsx, run_cli):
    # The Debtors defect: Xero defaulted the date field, and nothing downstream
    # detected it. This is the detection.
    path = build_xlsx(_HEADER)
    code, env = run_cli(["read-ledger", str(path), "--expect-date", "2026-07-31"])
    assert code == 1
    assert env["problems"][0]["code"] == "DATE_MISMATCH"
    msg = env["problems"][0]["message"]
    assert "2026-06-30" in msg and "2026-07-31" in msg
    assert "As at 30 June 2026" in msg


def test_expect_date_with_unreadable_header_refuses(build_xlsx, run_cli):
    path = build_xlsx({"A1": 1.0})
    code, env = run_cli(["read-ledger", str(path), "--expect-date", "2026-06-30"])
    assert code == 1
    assert env["problems"][0]["code"] == "DATE_MISMATCH"


def test_no_date_without_flag_is_a_warning(build_xlsx, run_cli):
    path = build_xlsx({"A1": 1.0})
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    assert any("no report date" in w for w in env["warnings"])
    assert any("no live formulas" in w for w in env["warnings"])


# ── input problems (exit 2) ──────────────────────────────────────────────────

def test_missing_file(run_cli, tmp_path):
    code, env = run_cli(["read-ledger", str(tmp_path / "absent.xlsx")])
    assert code == 2
    assert env["problems"][0]["code"] == "CANNOT_OPEN"


def test_csv_refused_as_unsupported(run_cli, tmp_path):
    p = tmp_path / "export.csv"
    p.write_text("a,b\n1,2\n")
    code, env = run_cli(["read-ledger", str(p)])
    assert code == 2


def test_zero_byte_file_named_cloud_only(run_cli, tmp_path):
    p = tmp_path / "dehydrated.xlsx"
    p.touch()
    code, env = run_cli(["read-ledger", str(p)])
    assert code == 2
    assert env["problems"][0]["code"] == "CLOUD_ONLY_FILE"
    assert "cloud-only" in env["problems"][0]["message"]
    assert "not an empty file" in env["problems"][0]["message"]


def test_unknown_sheet(build_xlsx, run_cli):
    path = build_xlsx({"A1": 1.0})
    code, env = run_cli(["read-ledger", str(path), "--sheet", "Nope"])
    assert code == 2
    assert env["problems"][0]["code"] == "SHEET_NOT_FOUND"
    assert "Sheet1" in env["problems"][0]["message"]


# ── multi-sheet + --out ──────────────────────────────────────────────────────

def test_all_sheets_read_by_default(build_xlsx, run_cli):
    path = build_xlsx({"A1": 1.0}, extra_sheets={"Second": {"A1": "=2+3"}})
    code, env = run_cli(["read-ledger", str(path)])
    assert code == 0
    assert [s["name"] for s in env["data"]["sheets"]] == ["Sheet1", "Second"]
    assert cell(env, "A1", "Second") == pytest.approx(5.0)


def test_out_writes_rows_to_file(build_xlsx, run_cli, tmp_path):
    import json
    path = build_xlsx({"A1": "=1+1"})
    out = tmp_path / "rows.json"
    code, env = run_cli(["read-ledger", str(path), "--out", str(out)])
    assert code == 0
    assert env["data"]["rows_file"] == str(out)
    assert "rows" not in env["data"]["sheets"][0]          # not inlined
    doc = json.loads(out.read_text())
    assert doc["sheets"][0]["rows"][0][0] == 2.0

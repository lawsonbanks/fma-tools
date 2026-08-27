"""debtors: the four-way gate, the offsetting-swap catch, the second date trip wire."""

import json
from datetime import date

import pytest

from fma_tools.reconcile.debtors import band
from reconcile_fixtures import bands_of, detail_row, make_detail_doc

AS_AT = "2026-08-11"

# (invoice number, due date, amount, true band at AS_AT)
_INVOICES = [
    ("INV-01", "2026-08-20", 900.0, "current"),
    ("INV-02", "2026-08-01", 500.0, "lt1"),
    ("INV-03", "2026-07-05", 400.0, "m1"),
    ("INV-04", "2026-06-02", 300.0, "m2"),
    ("INV-05", "2026-05-03", 250.0, "m3"),
    ("INV-06", "2026-01-10", 150.0, "older"),
    ("INV-07", "2024-11-01", 100.0, "older"),
    ("", "2026-07-20", 60.0, "lt1"),       # an unnumbered overpayment line
]


def _rows():
    return [detail_row(n, due, amt, b) for n, due, amt, b in _INVOICES]


def _data(tmp_path, detail_path, **overrides):
    total = round(sum(a for _, _, a, _ in _INVOICES), 2)
    entry = {"as_at": AS_AT, "detail_source": detail_path,
             "agent_bands": bands_of([(a, b) for _, _, a, b in _INVOICES]),
             "summary_total": total, "ar_control_bs": total}
    entry.update(overrides.pop("entry", {}))
    doc = {"dates": [entry], **overrides}
    p = tmp_path / "debtors_data.json"
    p.write_text(json.dumps(doc))
    return str(p)


@pytest.fixture
def run_debtors(run_cli):
    def _run(data_path):
        return run_cli(["reconcile", "debtors", "--data", data_path])
    return _run


def test_consistent_book_ties(run_debtors, tmp_path):
    detail = make_detail_doc(tmp_path, AS_AT, _rows())
    code, env = run_debtors(_data(tmp_path, detail))
    assert code == 0, [p["message"] for p in env["problems"]][:6]
    line = next(c for c in env["data"]["checks"] if c["id"] == f"{AS_AT}.line_by_line")
    assert line["passed"]


def test_wrong_summary_total_is_red(run_debtors, tmp_path):
    detail = make_detail_doc(tmp_path, AS_AT, _rows())
    code, env = run_debtors(_data(tmp_path, detail, entry={"summary_total": 9999.0}))
    assert code == 1
    assert any(b["id"] == f"{AS_AT}.summary_total" for b in env["data"]["breaks"])


def test_offsetting_band_swap_caught_line_by_line(run_debtors, tmp_path):
    """Two equal amounts in swapped band columns: every TOTAL still ties, only the
    line-by-line check can catch it — the check two offsetting errors cannot pass."""
    rows = _rows()
    swapped = [detail_row("INV-03", "2026-07-05", 400.0, "m2"),   # truly m1
               detail_row("INV-08", "2026-06-02", 400.0, "m1")]   # truly m2
    rows = [r for r in rows if r[0] not in ("INV-03", "INV-04")] + swapped
    detail = make_detail_doc(tmp_path, AS_AT, rows)
    true_bands = [(a, b) for n, d_, a, b in _INVOICES if n not in ("INV-03", "INV-04")]
    true_bands += [(400.0, "m1"), (400.0, "m2")]
    total = round(sum(a for a, _ in true_bands), 2)
    data = _data(tmp_path, detail,
                 entry={"agent_bands": bands_of(true_bands), "summary_total": total,
                        "ar_control_bs": total})
    code, env = run_debtors(data)
    assert code == 1
    breaks = {b["id"]: b for b in env["data"]["breaks"]}
    assert f"{AS_AT}.line_by_line" in breaks
    assert "INV-03" in breaks[f"{AS_AT}.line_by_line"]["detail"]
    # and the totals genuinely tied — the swap was invisible to (a) and (b)
    assert f"{AS_AT}.summary_total" not in breaks
    assert not any(k.startswith(f"{AS_AT}.band.") for k in breaks)


def test_agent_bands_checked_not_trusted(run_debtors, tmp_path):
    detail = make_detail_doc(tmp_path, AS_AT, _rows())
    bands = bands_of([(a, b) for _, _, a, b in _INVOICES])
    bands["m1"], bands["m2"] = bands["m2"], bands["m1"]      # agent got these backwards
    code, env = run_debtors(_data(tmp_path, detail, entry={"agent_bands": bands}))
    assert code == 1
    assert any(b["id"] == f"{AS_AT}.agent_band.m1" for b in env["data"]["breaks"])


def test_ar_control_gap_red_then_disclosed_green(run_debtors, tmp_path):
    detail = make_detail_doc(tmp_path, AS_AT, _rows())
    code, env = run_debtors(_data(tmp_path, detail,
                                  entry={"ar_control_bs": 2560.0}))   # book is 2660
    assert code == 1
    assert any(b["id"] == f"{AS_AT}.ar_control" for b in env["data"]["breaks"])

    code, env = run_debtors(_data(
        tmp_path, detail, entry={"ar_control_bs": 2560.0},
        disclosed_differences=[{"check": f"{AS_AT}.ar_control", "amount": 100.0,
                                "note": "unapplied credit notes per client ledger"}]))
    assert code == 0, [p["message"] for p in env["problems"]][:4]
    assert env["data"]["disclosures_applied"]


def test_wrong_header_date_is_the_second_trip_wire(run_debtors, tmp_path):
    detail = make_detail_doc(tmp_path, "2026-08-31", _rows())    # Xero's default date
    data = _data(tmp_path, detail)
    code, env = run_debtors(data)
    assert code == 1
    brk = next(b for b in env["data"]["breaks"] if b["id"] == f"{AS_AT}.header_date")
    assert "2026-08-31" in brk["detail"]


def test_dateless_rows_counted_never_silently_dropped(run_debtors, tmp_path):
    rows = _rows() + [["NOTE-ROW", None, None, None,
                       0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 55.0]]
    detail = make_detail_doc(tmp_path, AS_AT, rows)
    code, env = run_debtors(_data(tmp_path, detail))
    assert code == 0, [p["message"] for p in env["problems"]][:4]
    assert any("date-less" in w and "55" in w for w in env["warnings"])


def test_control_ledger_opens_and_closes_on_the_book(run_debtors, tmp_path):
    detail = make_detail_doc(tmp_path, AS_AT, _rows())
    total = round(sum(a for _, _, a, _ in _INVOICES), 2)
    data = _data(tmp_path, detail,
                 control_ledger={"from": AS_AT, "to": AS_AT,
                                 "opening": total, "closing": total + 7.0})
    code, env = run_debtors(data)
    assert code == 1
    assert any(b["id"] == "control_ledger.closing" for b in env["data"]["breaks"])


def test_five_band_agent_data_is_schema_refusal(run_debtors, tmp_path):
    detail = make_detail_doc(tmp_path, AS_AT, _rows())
    bands = bands_of([(a, b) for _, _, a, b in _INVOICES])
    del bands["lt1"]                                     # the FY26 five-band collapse
    code, env = run_debtors(_data(tmp_path, detail, entry={"agent_bands": bands}))
    assert code == 2
    assert env["problems"][0]["code"] == "CONTRACT_INVALID"


# ── the banding convention, pinned ───────────────────────────────────────────

@pytest.mark.parametrize("due,as_at,expected", [
    ("2026-08-11", "2026-08-11", "current"),   # due today is not overdue
    ("2026-08-12", "2026-08-11", "current"),
    ("2026-08-10", "2026-08-11", "lt1"),
    ("2026-07-11", "2026-08-11", "m1"),        # exactly one calendar month
    ("2026-07-12", "2026-08-11", "lt1"),       # one day short of a month
    ("2026-01-31", "2026-02-28", "m1"),        # month-end clamp: Jan 31 + 1m = Feb 28
    ("2026-04-11", "2026-08-11", "older"),     # four months
    ("2024-01-01", "2026-08-11", "older"),
])
def test_band_convention(due, as_at, expected):
    assert band(date.fromisoformat(due), date.fromisoformat(as_at)) == expected

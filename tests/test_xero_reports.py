"""xero reports: the shapes Xero documents, walked without changing a figure.

The payloads here are built to the structure of the examples in Xero's published API
specification (three title lines, a Header row, Sections of Rows and one SummaryRow,
untitled sections holding a lone total) with invented names and figures."""

import json
from datetime import date
from decimal import Decimal

import pytest

from fma_tools.errors import InputProblem, Refusal
from fma_tools.xero import layout, reports


def _cells(*values, account=None):
    out = [{"Value": v} for v in values]
    if account:
        for c in out:
            c["Attributes"] = [{"Value": account, "Id": "account"}]
    return out


def _balance_sheet(net_assets="185448.30", titles=None) -> bytes:
    """Three month-end columns, as `periods=2&timeframe=MONTH` returns them. Net assets
    and total equity differ by a cent in the first column, as they do in Xero's own
    documented example."""
    return json.dumps({"Reports": [{
        "ReportName": "Balance Sheet", "ReportType": "BalanceSheet",
        "ReportTitles": titles or ["Balance Sheet", "Entity A Pty Ltd", "As at 30 April 2019"],
        "ReportDate": "12 April 2019",
        "Rows": [
            {"RowType": "Header", "Cells": _cells("", "30 Apr 2019", "31 Mar 2019", "28 Feb 2019")},
            {"RowType": "Section", "Title": "Assets", "Rows": []},
            {"RowType": "Section", "Title": "Bank", "Rows": [
                {"RowType": "Row", "Cells": _cells("Savings", "-1850.00", "-1850.00", "0.00", account="a-1")},
                {"RowType": "Row", "Cells": _cells("Cheque", "2146.37", "2020.00", "0.00", account="a-2")},
                {"RowType": "SummaryRow", "Cells": _cells("Total Bank", "296.37", "170.00", "0.00")}]},
            {"RowType": "Section", "Title": "", "Rows": [
                {"RowType": "SummaryRow", "Cells": _cells("Total Assets", "296.37", "170.00", "0.00")}]},
            {"RowType": "Section", "Title": "", "Rows": [
                {"RowType": "Row", "Cells": _cells("Net Assets", net_assets, "170.00", "0.00")}]},
            {"RowType": "Section", "Title": "Equity", "Rows": [
                {"RowType": "Row", "Cells": _cells("Current Year Earnings", "114.62", "100.00", "0.00",
                                                   account="00000000-0000-0000-0000-000000000000")},
                {"RowType": "Row", "Cells": _cells("Retained Earnings", "185333.67", "70.00", "0.00", account="a-9")},
                {"RowType": "SummaryRow", "Cells": _cells("Total Equity", "185448.29", "170.00", "0.00")}]},
        ]}]}).encode()


def test_the_documented_balance_sheet_shape_is_walked_verbatim():
    rep = reports.parse(_balance_sheet())
    assert rep.name == "Balance Sheet" and rep.entity == "Entity A Pty Ltd"
    assert rep.header == ["", "30 Apr 2019", "31 Mar 2019", "28 Feb 2019"]
    assert [s.title for s in rep.sections] == ["Assets", "Bank", "", "", "Equity"]
    cye = rep.find("current year earnings")
    assert cye.values == [Decimal("114.62"), Decimal("100.00"), Decimal("0.00")]
    assert rep.find("Total Bank").kind == "summary" and rep.find("Net Assets").kind == "row"
    assert all(isinstance(v, Decimal) for s in rep.sections for ln in s.lines for v in ln.values)


def test_the_run_date_is_never_taken_for_the_as_at_date():
    rep = reports.parse(_balance_sheet())               # ReportDate says 12 April
    reports.prove_as_at(rep, date(2019, 4, 30), "bs")
    with pytest.raises(Refusal) as e:
        reports.prove_as_at(rep, date(2019, 4, 12), "bs")
    assert e.value.code == "DATE_MISMATCH"


def test_every_period_column_is_footed():
    ties = reports.section_ties(reports.parse(_balance_sheet()), "bs")
    assert [t.status for t in ties] == ["ok"] * 6           # Bank x3, Equity x3
    assert "Total Bank (31 Mar 2019)" in ties[1].name


def test_a_cent_between_net_assets_and_equity_is_rounding_and_a_dollar_is_a_break():
    assert reports.balance_sheet_tie(reports.parse(_balance_sheet()), "bs").status == "rounding"
    tie = reports.balance_sheet_tie(reports.parse(_balance_sheet(net_assets="185449.30")), "bs")
    assert tie.status == "break" and "apart by 1.01" in tie.detail


def test_a_tie_that_cannot_be_run_says_so_rather_than_passing():
    rep = reports.parse(_balance_sheet())
    rep.sections = [s for s in rep.sections if s.title != "Equity"]
    assert reports.balance_sheet_tie(rep, "bs").status == "not_checked"
    tb = reports.Report(["Trial Balance", "Entity A Pty Ltd", "As at 30 April 2019"],
                        ["Account", "Dr", "Cr"], [])
    assert [t.status for t in reports.trial_balance_ties(tb, "tb")] == ["not_checked"] * 2


def test_the_workbook_keeps_every_column_xero_returned(tmp_path):
    rep = reports.parse(_balance_sheet())
    grid = layout.grid_for(rep, reports.as_at_line(date(2019, 4, 30)))
    assert grid[:5] == [["Balance Sheet"], ["Entity A Pty Ltd"], ["As at 30 April 2019"], [],
                        ["Account", "30 Apr 2019", "31 Mar 2019", "28 Feb 2019"]]
    assert ["Cheque", Decimal("2146.37"), Decimal("2020.00"), Decimal("0.00")] in grid
    info = layout.write_workbook(tmp_path / "bs.xlsx", [(rep.name, grid)], {"date": "2019-04-30"})
    assert info["report_date"] == "2019-04-30"


def test_an_answer_that_is_not_a_report_is_exit_2():
    for junk in (b"<html>", b"{}", b'{"Reports": []}', b"[]"):
        with pytest.raises(InputProblem):
            reports.parse(junk)


def test_an_organisation_without_a_year_end_cannot_have_ranges_derived():
    body = json.dumps({"Organisations": [{"Name": "Entity A Pty Ltd"}]}).encode()
    with pytest.raises(Refusal) as e:
        reports.parse_organisation(body)
    assert e.value.code == "FY_END_UNKNOWN"


def test_a_chart_without_class_or_status_is_still_read():
    body = json.dumps({"Accounts": [{"AccountID": "a-1", "Code": "091", "Name": "Savings",
                                     "Type": "BANK", "TaxType": "NONE"}]}).encode()
    assert reports.parse_accounts(body) == [{"id": "a-1", "code": "091", "name": "Savings",
                                             "type": "BANK", "class": "", "status": "",
                                             "tax_type": "NONE"}]


def test_side_by_side_keeps_each_dates_own_figure():
    now = reports.parse(_balance_sheet())
    then = reports.parse(_balance_sheet(titles=["Balance Sheet", "Entity A Pty Ltd",
                                                "As at 31 March 2019"]))
    then.header[1] = "31 Mar 2019"
    then.sections[1].lines.insert(1, reports.Line("row", "Term Deposit", [Decimal("5.00")], "a-3"))
    both = reports.side_by_side(now, then)
    assert both.header == ["", "30 Apr 2019", "31 Mar 2019"]
    bank = next(s for s in both.sections if s.title == "Bank")
    assert [(ln.label, ln.values) for ln in bank.lines] == [
        ("Savings", [Decimal("-1850.00"), Decimal("-1850.00")]),
        ("Cheque", [Decimal("2146.37"), Decimal("2146.37")]),
        ("Term Deposit", [None, Decimal("5.00")]),
        ("Total Bank", [Decimal("296.37"), Decimal("296.37")])]
    assert [s.title for s in both.sections] == [s.title for s in now.sections]


def test_a_footing_that_could_not_run_is_said_not_skipped():
    rep = reports.parse(_balance_sheet())
    bank = next(s for s in rep.sections if s.title == "Bank")
    bank.lines[0].values[1] = "n/a"                         # text in the March column
    ties = {t.name: t.status for t in reports.section_ties(rep, "bs")}
    assert ties["bs: Total Bank (30 Apr 2019)"] == "ok"
    assert ties["bs: Total Bank (31 Mar 2019)"] == "not_checked"
    bank.lines.append(reports.Line("summary", "Total Bank (again)", [Decimal("1")] * 3))
    again = [t for t in reports.section_ties(rep, "bs") if t.name == "bs: Bank"]
    assert again and again[0].status == "not_checked" and "more than one total" in again[0].detail

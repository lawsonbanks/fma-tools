"""Ask Xero for a report and understand what came back -- without changing a figure.

Xero answers every report in one shape: a list of title lines, then rows that are a
Header, or a Section holding Rows and at most one SummaryRow. This module walks that
tree into plain sections and lines, keeps every label and total exactly as Xero wrote
it, and proves three things before a caller is allowed to keep the result:

  * The dates are the dates asked for. Xero echoes them in the title lines; the echo is
    parsed and compared. (`ReportDate` in the response is the day the report ran, not
    the date it is as at, and is never used.)
  * The report foots: each section's total is the sum of the lines above it.
  * The statements agree with each other where Xero computes the same figure twice:
    trial balance debits and credits, net assets and equity, and the year-to-date
    profit against Current Year Earnings on the balance sheet.

Amounts are Decimal from the moment they are read. A float never touches money here.
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from ..errors import InputProblem, Refusal

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")

# A total may differ from the sum of its lines by a few cents when Xero rounds each
# line of a converted balance on its own. Past this, it is a break.
_ROUNDING = Decimal("0.05")
_CENT = Decimal("0.01")


# -- dates ---------------------------------------------------------------------------

def parse_iso(text: str, what: str) -> date:
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        raise InputProblem("DATE_INVALID", f"{what} must be a date as YYYY-MM-DD, "
                                           f"got {text!r}")


def long_date(d: date) -> str:
    """30 September 2026 -- from a fixed table, so it reads the same on any locale."""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def as_at_line(d: date) -> str:
    return f"As at {long_date(d)}"


def range_line(a: date, b: date) -> str:
    return f"{long_date(a)} to {long_date(b)}"


_DATE_IN_TEXT = re.compile(
    r"(\d{1,2})\s+(" + "|".join(m[:3] for m in MONTHS) + r")[a-z]*\.?,?\s+(\d{4})",
    re.IGNORECASE)


def dates_in(text: str) -> list[date]:
    out = []
    for day, mon, year in _DATE_IN_TEXT.findall(text or ""):
        month = [m[:3].lower() for m in MONTHS].index(mon[:3].lower()) + 1
        try:
            out.append(date(int(year), month, int(day)))
        except ValueError:
            pass
    return out


def month_start(d: date) -> date:
    return d.replace(day=1)


def financial_year(as_at: date, end_month: int, end_day: int) -> tuple[date, date]:
    """(first day, last day) of the financial year containing `as_at`, from the
    organisation's own year end. Never assumed: a group can hold a 30 June company and
    a 31 December one."""
    def year_end(year: int) -> date:
        return date(year, end_month, min(end_day, calendar.monthrange(year, end_month)[1]))
    end = year_end(as_at.year)
    if as_at > end:
        end = year_end(as_at.year + 1)
    start = year_end(end.year - 1) + timedelta(days=1)
    return start, end


# -- the report tree -----------------------------------------------------------------

@dataclass
class Line:
    kind: str                       # "row" | "summary"
    label: str
    values: list                    # Decimal | str | None, one per value column
    account_id: str | None = None


@dataclass
class Section:
    title: str
    lines: list[Line] = field(default_factory=list)


@dataclass
class Report:
    titles: list[str]
    header: list[str]
    sections: list[Section]

    @property
    def name(self) -> str:
        return self.titles[0] if self.titles else ""

    @property
    def entity(self) -> str:
        return self.titles[1] if len(self.titles) > 1 else ""

    def find(self, label: str) -> Line | None:
        want = label.casefold()
        for s in self.sections:
            for ln in s.lines:
                if ln.label.strip().casefold() == want:
                    return ln
        return None


def to_number(text):
    """Xero's cell text as a Decimal; an empty cell as None; anything else untouched."""
    if text is None:
        return None
    s = str(text).strip()
    if s == "":
        return None
    neg = s.startswith("(") and s.endswith(")")
    core = s.strip("()").replace(",", "")
    try:
        d = Decimal(core)
    except InvalidOperation:
        return s
    if not d.is_finite():
        return s
    return -d if neg else d


def _cells(row: dict) -> list[dict]:
    return [c or {} for c in (row.get("Cells") or [])]


def _line(row: dict, kind: str) -> Line:
    cells = _cells(row)
    label = str(cells[0].get("Value", "")) if cells else ""
    account_id = None
    for c in cells:
        for a in c.get("Attributes") or []:
            if (a or {}).get("Id") == "account" and a.get("Value"):
                account_id = str(a["Value"])
                break
        if account_id:
            break
    return Line(kind, label, [to_number(c.get("Value")) for c in cells[1:]], account_id)


def parse(body: bytes) -> Report:
    try:
        payload = json.loads(body.decode("utf-8"))
        rep = payload["Reports"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise InputProblem("XERO_NOT_A_REPORT", "Xero's answer did not carry a report")
    header: list[str] = []
    sections: list[Section] = []
    loose: Section | None = None
    for row in rep.get("Rows") or []:
        kind = row.get("RowType")
        if kind == "Header":
            header = [str(c.get("Value", "")) for c in _cells(row)]
            loose = None
        elif kind == "Section":
            sec = Section(str(row.get("Title") or ""))
            for inner in row.get("Rows") or []:
                ik = inner.get("RowType")
                if ik in ("Row", "SummaryRow"):
                    sec.lines.append(_line(inner, "summary" if ik == "SummaryRow" else "row"))
            sections.append(sec)
            loose = None
        elif kind in ("Row", "SummaryRow"):
            if loose is None:
                loose = Section("")
                sections.append(loose)
            loose.lines.append(_line(row, "summary" if kind == "SummaryRow" else "row"))
    return Report([str(t) for t in rep.get("ReportTitles") or []], header, sections)


# -- proof of dates ------------------------------------------------------------------

def _echo(report: Report) -> tuple[str, list[date]]:
    """The title line that carries the dates, and the dates in it. Xero puts the
    report name first and the organisation second; the dates follow."""
    for line in report.titles[2:] or report.titles:
        found = dates_in(line)
        if found:
            return line, found
    return (report.titles[-1] if report.titles else ""), []


def prove_as_at(report: Report, as_at: date, what: str) -> None:
    line, found = _echo(report)
    if not found:
        raise Refusal("DATE_NOT_ECHOED",
                      f"{what}: Xero's answer names no date (title line {line!r}), so "
                      f"it cannot be proved to be as at {as_at.isoformat()}")
    if found != [as_at]:
        raise Refusal("DATE_MISMATCH",
                      f"{what}: asked for {as_at.isoformat()}, Xero's title line says "
                      f"{line!r}")


def prove_range(report: Report, start: date, end: date, what: str) -> bool:
    """True when both ends were echoed; False when Xero named only the end ("For the
    month ended ..."), which proves the end and leaves the start as asked."""
    line, found = _echo(report)
    if not found:
        raise Refusal("DATE_NOT_ECHOED",
                      f"{what}: Xero's answer names no date (title line {line!r}), so "
                      f"the period {start.isoformat()} to {end.isoformat()} cannot be "
                      "proved")
    if found == [start, end]:
        return True
    if found == [end]:
        return False
    raise Refusal("DATE_MISMATCH",
                  f"{what}: asked for {start.isoformat()} to {end.isoformat()}, Xero's "
                  f"title line says {line!r}")


# -- ties ----------------------------------------------------------------------------

@dataclass
class Tie:
    name: str
    status: str            # "ok" | "rounding" | "break" | "not_checked"
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _money(d: Decimal) -> str:
    return f"{d:,.2f}"


def _compare(name: str, a, b, a_what: str, b_what: str) -> Tie:
    if not isinstance(a, Decimal) or not isinstance(b, Decimal):
        return Tie(name, "not_checked", f"{a_what} or {b_what} is not a number in "
                                        "Xero's answer")
    diff = a - b
    if diff == 0:
        return Tie(name, "ok", f"{a_what} {_money(a)} = {b_what} {_money(b)}")
    status = "rounding" if abs(diff) <= _ROUNDING else "break"
    return Tie(name, status, f"{a_what} {_money(a)} against {b_what} {_money(b)}, "
                             f"apart by {_money(diff)}")


def section_ties(report: Report, what: str) -> list[Tie]:
    """Every section that has lines and one total: the total is the sum of the lines."""
    out = []
    for sec in report.sections:
        rows = [ln for ln in sec.lines if ln.kind == "row"]
        totals = [ln for ln in sec.lines if ln.kind == "summary"]
        if not rows or not totals:
            continue                    # nothing to foot: lines with no total, or a lone total
        if len(totals) > 1:
            out.append(Tie(f"{what}: {sec.title or totals[0].label}", "not_checked",
                           "the section carries more than one total, so which lines each "
                           "one sums is not known"))
            continue
        total = totals[0]
        for j, tv in enumerate(total.values):
            col = [ln.values[j] for ln in rows if j < len(ln.values)]
            col_name = report.header[j + 1] if j + 1 < len(report.header) else f"column {j + 1}"
            name = f"{what}: {total.label} ({col_name})"
            if tv is None and all(v is None for v in col):
                continue                # an empty column foots to an empty total
            if any(isinstance(v, str) for v in col) or not isinstance(tv, Decimal):
                # said, never skipped in silence: a tie that did not run is not a pass
                out.append(Tie(name, "not_checked", "a cell in this column holds text or "
                                                    "nothing where a number belongs"))
                continue
            s = sum((v for v in col if isinstance(v, Decimal)), Decimal(0))
            out.append(_compare(name, s, tv, "sum of lines", "Xero's total"))
    return out


def _column(report: Report, name: str) -> int | None:
    for i, h in enumerate(report.header[1:]):
        if h.strip().casefold() == name.casefold():
            return i
    return None


def trial_balance_ties(report: Report, what: str) -> list[Tie]:
    """Debits equal credits, for the period and for the year to date; and Xero's own
    Total line, which sits in a section of its own, is the sum of every account."""
    out = []
    rows = [ln for s in report.sections for ln in s.lines if ln.kind == "row"]
    total = report.find("Total")

    def column_sum(i: int) -> Decimal:
        return sum((ln.values[i] for ln in rows
                    if i < len(ln.values) and isinstance(ln.values[i], Decimal)), Decimal(0))

    for debit, credit in (("Debit", "Credit"), ("YTD Debit", "YTD Credit")):
        i, j = _column(report, debit), _column(report, credit)
        name = f"{what}: {debit} = {credit}"
        if i is None or j is None:
            out.append(Tie(name, "not_checked",
                           f"Xero's trial balance has no {debit!r}/{credit!r} columns "
                           f"(it has {report.header!r})"))
            continue
        out.append(_compare(name, column_sum(i), column_sum(j), debit, credit))
        if total is not None and total.kind == "summary":
            for k, col in ((i, debit), (j, credit)):
                if k < len(total.values) and isinstance(total.values[k], Decimal):
                    out.append(_compare(f"{what}: Total ({col})", column_sum(k),
                                        total.values[k], "sum of accounts", "Xero's total"))
    return out


def _first_value(line: Line | None):
    if line is None or not line.values:
        return None
    return line.values[0]


def balance_sheet_tie(report: Report, what: str) -> Tie:
    na, eq = report.find("Net Assets"), report.find("Total Equity")
    name = f"{what}: Net Assets = Total Equity"
    if na is None or eq is None:
        return Tie(name, "not_checked", "Xero's balance sheet has no line called "
                                        "'Net Assets' or 'Total Equity'")
    return _compare(name, _first_value(na), _first_value(eq), "Net Assets", "Total Equity")


def earnings_tie(pl_year_to_date: Report, balance_sheet: Report, what: str) -> Tie:
    """The one figure Xero computes twice, independently: profit for the financial
    year to date, and Current Year Earnings on the balance sheet at the same date. A
    gap means the pull asked for the wrong range or basis."""
    np_, cye = pl_year_to_date.find("Net Profit"), balance_sheet.find("Current Year Earnings")
    name = f"{what}: year-to-date Net Profit = Current Year Earnings"
    if np_ is None or cye is None:
        return Tie(name, "not_checked", "no 'Net Profit' line on the P&L or no 'Current "
                                        "Year Earnings' line on the balance sheet")
    return _compare(name, _first_value(np_), _first_value(cye), "Net Profit",
                    "Current Year Earnings")


# -- two dates side by side ----------------------------------------------------------

def _line_key(sec: Section, ln: Line):
    return ("id", ln.account_id) if ln.account_id else ("label", sec.title, ln.label, ln.kind)


def _section_key(sec: Section):
    if sec.title:
        return ("title", sec.title)
    return ("untitled", sec.lines[0].label if sec.lines else "")


def side_by_side(primary: Report, other: Report) -> Report:
    """One report with a column per date, each column Xero's own figure at that date.

    Xero leaves an account off a balance sheet when its balance is nil, so the two
    dates do not list the same accounts. Lines are matched by account id (by label for
    totals), and a line present at only one date keeps an empty cell at the other --
    empty, never zero: the report did not say zero."""
    merged = [Section(s.title, [Line(ln.kind, ln.label, [_first_value(ln), None],
                                     ln.account_id) for ln in s.lines])
              for s in primary.sections]
    by_section = {}
    for s in merged:
        by_section.setdefault(_section_key(s), s)
    last_index = -1
    for osec in other.sections:
        target = by_section.get(_section_key(osec))
        if target is None:
            target = Section(osec.title)
            last_index += 1
            merged.insert(last_index, target)
            by_section[_section_key(osec)] = target
        else:
            last_index = merged.index(target)
        index = {_line_key(target, ln): ln for ln in target.lines}
        for oln in osec.lines:
            hit = index.get(_line_key(osec, oln))
            if hit is not None:
                hit.values[1] = _first_value(oln)
                continue
            new = Line(oln.kind, oln.label, [None, _first_value(oln)], oln.account_id)
            pos = next((i for i, ln in enumerate(target.lines) if ln.kind == "summary"),
                       len(target.lines))
            if oln.kind == "summary":
                pos = len(target.lines)
            target.lines.insert(pos, new)
    header = [primary.header[0] if primary.header else "",
              primary.header[1] if len(primary.header) > 1 else "",
              other.header[1] if len(other.header) > 1 else ""]
    return Report(list(primary.titles), header, merged)


# -- the calls -----------------------------------------------------------------------
# standardLayout=true: a layout someone saved on screen regroups accounts, and the ties
# above lean on Xero's standard lines (Net Assets, Net Profit, Current Year Earnings).

def trial_balance(c, user: str, tenant: str, as_at: date) -> bytes:
    return c.get_raw(user, tenant, "Reports/TrialBalance", {"date": as_at.isoformat()})


def balance_sheet(c, user: str, tenant: str, as_at: date) -> bytes:
    return c.get_raw(user, tenant, "Reports/BalanceSheet",
                     {"date": as_at.isoformat(), "standardLayout": "true"})


def profit_and_loss(c, user: str, tenant: str, start: date, end: date) -> bytes:
    return c.get_raw(user, tenant, "Reports/ProfitAndLoss",
                     {"fromDate": start.isoformat(), "toDate": end.isoformat(),
                      "standardLayout": "true"})


def bank_summary(c, user: str, tenant: str, start: date, end: date) -> bytes:
    return c.get_raw(user, tenant, "Reports/BankSummary",
                     {"fromDate": start.isoformat(), "toDate": end.isoformat()})


def accounts(c, user: str, tenant: str) -> bytes:
    return c.get_raw(user, tenant, "Accounts")


def organisation(c, user: str, tenant: str) -> bytes:
    return c.get_raw(user, tenant, "Organisation")


def parse_organisation(body: bytes) -> dict:
    try:
        org = json.loads(body.decode("utf-8"))["Organisations"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise InputProblem("XERO_NOT_AN_ORGANISATION",
                           "Xero's answer did not carry an organisation")
    try:
        end_month, end_day = int(org["FinancialYearEndMonth"]), int(org["FinancialYearEndDay"])
        if not (1 <= end_month <= 12 and 1 <= end_day <= 31):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise Refusal("FY_END_UNKNOWN",
                      "Xero did not say when this organisation's financial year ends, "
                      "so no year-to-date range can be derived")
    return {"name": org.get("Name") or "", "legal_name": org.get("LegalName") or "",
            "base_currency": org.get("BaseCurrency") or "",
            "fy_end_month": end_month, "fy_end_day": end_day,
            "sales_tax_basis": org.get("SalesTaxBasis") or "",
            "timezone": org.get("Timezone") or "",
            "is_demo": bool(org.get("IsDemoCompany"))}


def parse_accounts(body: bytes) -> list[dict]:
    try:
        rows = json.loads(body.decode("utf-8"))["Accounts"]
    except (ValueError, KeyError, TypeError):
        raise InputProblem("XERO_NOT_ACCOUNTS", "Xero's answer did not carry a chart "
                                                "of accounts")
    return [{"id": str(a.get("AccountID") or ""), "code": str(a.get("Code") or ""),
             "name": str(a.get("Name") or ""), "type": str(a.get("Type") or ""),
             "class": str(a.get("Class") or ""), "status": str(a.get("Status") or ""),
             "tax_type": str(a.get("TaxType") or "")} for a in rows or []]

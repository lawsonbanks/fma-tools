"""Debtors four-way gate, at every pinned date — with the agent's banding CHECKED.

The tool ingests the read-ledger JSON of each Aged Receivables Detail export directly
(the agent never transcribes 420 invoice rows — transcription is the risk these tools
exist to remove), recomputes every invoice's band itself from Due Date to the as-at
date, and then proves:

  (a) the recomputed banded total equals the Summary total, to the cent;
  (b) the six recomputed bands equal Xero's own band columns, and the agent's bands;
  (c) line by line, every invoice sits in the band Xero's own row already carries —
      the mismatch count must be zero. This is the check two offsetting errors
      cannot pass, which is why it exists;
  (d) the banded total equals the receivables control on the Balance Sheet
      (disclosed differences allowed, named), and the control ledger opens and
      closes on the aged book.

If any of the four breaks: stop. Do not chart it, do not plug it, do not adjust a
figure to make it tie.
"""

from __future__ import annotations

import calendar
import json
from datetime import date
from pathlib import Path

from ..errors import InputProblem
from .lib import CheckResult, Disclosures, fact, tie

# Xero's six standard ageing bands, in report order. "Older" is the 4+ month band.
BUCKETS = ["current", "lt1", "m1", "m2", "m3", "older"]

# Header label (normalised) -> our column key. Columns are located by label, never
# by position.
_HEADER_KEYS = {
    "invoice date": "inv_date", "due date": "due_date",
    "invoice number": "inv_number", "invoice reference": "reference",
    "current": "current", "< 1 month": "lt1", "1 month": "m1",
    "2 months": "m2", "3 months": "m3", "older": "older", "total": "total",
}


def _norm(s) -> str:
    return " ".join(str(s).lower().split())


def _add_months(d: date, months: int) -> date:
    """EDATE semantics: same day `months` on, clamped to the month's end."""
    y, m = divmod(d.year * 12 + (d.month - 1) + months, 12)
    day = min(d.day, calendar.monthrange(y, m + 1)[1])
    return date(y, m + 1, day)


def band(due: date, as_at: date) -> str:
    """Whole calendar months overdue, Due Date to the as-at date, into Xero's six
    bands. Not yet due (including due today) is Current. The month count is EDATE
    arithmetic: one month overdue means a full calendar month has passed, month-end
    clamped. Checks (b) and (c) tie this convention to Xero's own columns on every
    run, so a convention drift is loud on day one."""
    if due >= as_at:
        return "current"
    months = (as_at.year - due.year) * 12 + (as_at.month - due.month)
    if months > 0 and _add_months(due, months) > as_at:
        months -= 1
    if months < 1:
        return "lt1"
    if months == 1:
        return "m1"
    if months == 2:
        return "m2"
    if months == 3:
        return "m3"
    return "older"


def _parse_iso(s, what: str) -> date:
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        raise InputProblem("CONTRACT_INVALID", f"{what} is not an ISO date: {s!r}")


def load_detail_doc(path_str: str) -> dict:
    p = Path(path_str)
    if not p.exists():
        raise InputProblem("CANNOT_OPEN", f"detail_source {p} does not exist — pass "
                           "the JSON written by `fma read-ledger --out`")
    def _refuse_constant(s):
        raise InputProblem("CONTRACT_INVALID",
                           f"{s} in {p.name} is not a number a data document can carry")
    try:
        return json.loads(p.read_text(), parse_constant=_refuse_constant)
    except (OSError, json.JSONDecodeError) as e:
        raise InputProblem("CANNOT_OPEN", f"cannot read detail_source {p}: {e}")


def parse_detail(doc: dict) -> tuple[list[dict], list[dict], dict]:
    """(invoices, dateless_rows, meta) out of a read-ledger JSON document.

    An invoice line has a due date and a total; it does NOT need a document number
    (Xero issues overpayment/prepayment lines with the number cell empty, by
    design — requiring one silently dropped three lines and $27,060 off a live
    book). Rows with no date are headings and subtotals, not invoices — excluded,
    but counted and totalled, never silently dropped."""
    sheets = doc.get("sheets") or []
    if not sheets or "rows" not in sheets[0]:
        raise InputProblem("CONTRACT_INVALID",
                           "detail_source carries no row data — write it with "
                           "`fma read-ledger <detail.xlsx> --out <path>`")
    rows = sheets[0]["rows"]
    hdr_idx, colmap = None, {}
    for i, row in enumerate(rows):
        norms = [_norm(c) for c in row]
        if "invoice number" in norms and "invoice date" in norms:
            for j, n in enumerate(norms):
                key = _HEADER_KEYS.get(n)
                if key and key not in colmap:
                    colmap[key] = j
            hdr_idx = i
            break
    if hdr_idx is None or "total" not in colmap or "due_date" not in colmap:
        raise InputProblem("CONTRACT_INVALID",
                           "no 'Invoice Number' / 'Invoice Date' header row found — "
                           "is this the read-ledger JSON of a Xero Aged Receivables "
                           "Detail export?")

    def get(row, key):
        j = colmap.get(key)
        return row[j] if (j is not None and j < len(row)) else None

    invoices, dateless = [], []
    for row in rows[hdr_idx + 1:]:
        cells = [c for c in row if c is not None and str(c).strip()]
        if not cells:
            continue
        c0 = str(row[0]).strip() if row and row[0] is not None else ""
        if c0.lower().startswith("total"):
            continue                      # a subtotal or the grand total row
        total = get(row, "total")
        due = get(row, "due_date")
        if not isinstance(total, (int, float)):
            continue                      # a customer group header
        if due is None or str(due).strip() == "":
            dateless.append({"row": row, "amount": float(total)})
            continue
        invoices.append({
            "inv_number": str(get(row, "inv_number") or "").strip(),
            "due_date": str(due)[:10],
            "amount": round(float(total), 2),
            "buckets": {b: float(get(row, b)) if isinstance(get(row, b), (int, float))
                        else 0.0 for b in BUCKETS},
        })
    return invoices, dateless, doc.get("metadata", {})


def _xero_band_of(inv: dict) -> str | None:
    """The band Xero's own row carries: the column holding the invoice's amount.
    None when the row does not place its amount in exactly one band."""
    holders = [b for b in BUCKETS if abs(inv["buckets"][b] - inv["amount"]) <= 0.005
               and inv["buckets"][b] != 0]
    if len(holders) == 1:
        return holders[0]
    nonzero = [b for b in BUCKETS if inv["buckets"][b] != 0]
    return nonzero[0] if len(nonzero) == 1 else None


def checks(data: dict, disc: Disclosures) -> tuple[list[CheckResult], list[str]]:
    out: list[CheckResult] = []
    warnings: list[str] = []
    totals_by_date: dict[str, float] = {}

    for entry in data["dates"]:
        as_at_s = entry["as_at"]
        as_at = _parse_iso(as_at_s, f"dates[{as_at_s}].as_at")
        doc = load_detail_doc(entry["detail_source"])
        invoices, dateless, meta = parse_detail(doc)

        # 0. the second trip wire behind read-ledger --expect-date: the export this
        # data claims to be at as_at really is
        header_date = meta.get("report_date")
        out.append(fact(f"{as_at_s}.header_date",
                        f"{as_at_s}: detail export header carries the as-at date",
                        header_date == as_at_s,
                        f"header says {header_date!r}"))

        if dateless:
            warnings.append(f"{as_at_s}: {len(dateless)} date-less row(s) excluded "
                            f"from banding, totalling {sum(r['amount'] for r in dateless):,.2f} "
                            "— headings and subtotals, not invoices")

        # recompute every band ourselves; the agent's banding is checked, not trusted
        recomputed = {b: 0.0 for b in BUCKETS}
        mismatches = []
        for inv in invoices:
            due = _parse_iso(inv["due_date"], "invoice due_date")
            ours = band(due, as_at)
            recomputed[ours] = round(recomputed[ours] + inv["amount"], 2)
            xeros = _xero_band_of(inv)
            if xeros is not None and xeros != ours:
                mismatches.append(f"{inv['inv_number'] or '(unnumbered)'} due "
                                  f"{inv['due_date']} {inv['amount']:,.2f}: "
                                  f"ours {ours}, export {xeros}")
        banded_total = round(sum(recomputed.values()), 2)
        totals_by_date[as_at_s] = banded_total

        # (a) banded total = Summary total, to the cent
        out.append(tie(f"{as_at_s}.summary_total",
                       f"{as_at_s}: banded total = Aged Summary total",
                       banded_total, entry["summary_total"]))
        # (b) the six bands, against the export's own columns and the agent's bands
        export_bands = {b: round(sum(i["buckets"][b] for i in invoices), 2) for b in BUCKETS}
        for b in BUCKETS:
            out.append(tie(f"{as_at_s}.band.{b}",
                           f"{as_at_s}: recomputed {b} = export's own column",
                           recomputed[b], export_bands[b]))
            out.append(tie(f"{as_at_s}.agent_band.{b}",
                           f"{as_at_s}: agent's {b} = recomputed",
                           entry["agent_bands"][b], recomputed[b]))
            if "summary_bands" in entry:
                out.append(tie(f"{as_at_s}.summary_band.{b}",
                               f"{as_at_s}: summary {b} = recomputed",
                               entry["summary_bands"][b], recomputed[b]))
        # (c) line by line — the check two offsetting errors cannot pass
        out.append(fact(f"{as_at_s}.line_by_line",
                        f"{as_at_s}: every invoice sits in the band the export's own "
                        "row carries (mismatch count must be zero)",
                        not mismatches,
                        f"{len(mismatches)} mismatch(es): " + "; ".join(mismatches[:8])
                        if mismatches else f"{len(invoices)} invoices agree"))
        # (d) the AR control on the Balance Sheet (disclosures allowed, named)
        out.append(tie(f"{as_at_s}.ar_control",
                       f"{as_at_s}: banded total = AR control on the Balance Sheet",
                       banded_total, entry["ar_control_bs"],
                       disclosed=disc.for_check(f"{as_at_s}.ar_control")))

    # (d) continued: the control ledger opens and closes on the aged book
    ledger = data.get("control_ledger")
    if ledger:
        for end, key in (("from", "opening"), ("to", "closing")):
            d = ledger[end]
            if d not in totals_by_date:
                raise InputProblem("CONTRACT_INVALID",
                                   f"control_ledger.{end} {d!r} is not one of the "
                                   f"dates in this run: {sorted(totals_by_date)}")
            out.append(tie(f"control_ledger.{key}",
                           f"control ledger {key} = aged book at {d}",
                           ledger[key], totals_by_date[d],
                           disclosed=disc.for_check(f"control_ledger.{key}")))
    return out, warnings

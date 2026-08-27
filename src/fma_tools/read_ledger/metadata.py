"""Read the header block a Xero export carries above its column headers.

Xero writes the entity name, the report title and a date line ("As at 30 June 2026",
"For the month ended 31 July 2026", "1 July 2025 to 30 June 2026") into the top rows.
Surfacing the date is load-bearing: Xero defaults the report date field to the end of
the current month, not to the date asked for, and a wrong as-at date in an export is
invisible downstream unless something reads the header back.
"""

from __future__ import annotations

import re
from datetime import date, datetime

_DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%d %B, %Y", "%d %b, %Y")

_ENDED_RE = re.compile(r"for the .* ended\s*:?\s*(.+)$", re.IGNORECASE)
_RANGE_RE = re.compile(r"^(.*?)\s+(?:to|-|–)\s+(.*)$")


def parse_date_text(s) -> date | None:
    """A date out of a Xero header fragment, or None. Ported format list."""
    s = str(s).strip()
    low = s.lower()
    for pre in ("as at", "as of"):
        if low.startswith(pre):
            s = s[len(pre):]
    s = s.strip().lstrip(":").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s[:24], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _row_text(row) -> str:
    return " ".join(str(c).strip() for c in row if c is not None and str(c).strip())


def _looks_like_header_row(row) -> bool:
    """The column-header row ends the header block: Xero marks it with 'Account' in its
    first populated cell on statements, and any row with 3+ populated cells is tabular."""
    cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
    if not cells:
        return False
    if cells[0].lower() == "account":
        return True
    return len(cells) >= 3


def extract(grid: list[list]) -> dict:
    meta = {"entity": None, "report_title": None, "report_date_raw": None,
            "report_date": None, "report_period": None}
    plain: list[str] = []
    for row in grid[:10]:
        text = _row_text(row)
        if not text:
            continue
        if _looks_like_header_row(row):
            break
        low = text.lower()
        if low.startswith(("as at", "as of")):
            meta["report_date_raw"] = text
            d = parse_date_text(text)
            meta["report_date"] = d.isoformat() if d else None
            continue
        m = _ENDED_RE.search(text)
        if m:
            meta["report_date_raw"] = text
            d = parse_date_text(m.group(1))
            if d:
                meta["report_period"] = {"start": None, "end": d.isoformat()}
            continue
        rng = _RANGE_RE.match(text)
        if rng:
            d0, d1 = parse_date_text(rng.group(1)), parse_date_text(rng.group(2))
            if d1:
                meta["report_date_raw"] = text
                meta["report_period"] = {"start": d0.isoformat() if d0 else None,
                                         "end": d1.isoformat()}
                continue
        plain.append(text)
    # Xero writes the report title first and the entity second (measured on live
    # NSW balance-sheet and P&L exports, 2026-08-13 pull).
    if plain:
        meta["report_title"] = plain[0]
    if len(plain) > 1:
        meta["entity"] = plain[1]
    return meta

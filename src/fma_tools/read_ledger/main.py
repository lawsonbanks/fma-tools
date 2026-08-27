"""fma read-ledger -- read a Xero or Excel export safely, or refuse.

The one job: hand the agent the true grid. Formulas are evaluated by this tool (never
trusted to a cached value, which Xero writes as zero), the header date is surfaced so it
can be checked against the date that was asked for, and anything unreadable is a loud
refusal -- never a silent nil.

Boundary: no mapping to contract fields, no opinion about which report this is. The
agent reads the grid and fills the contract; this tool proves the grid is real.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..errors import InputProblem, Refusal
from . import loader, metadata
from .formula import FormulaProblem, SheetResolver, coord, to_json_value


def add_arguments(p) -> None:
    p.add_argument("file", help="absolute path to the .xlsx export")
    p.add_argument("--sheet", help="read one named sheet (default: all sheets)")
    p.add_argument("--expect-date",
                   help="refuse unless the export header carries exactly this date "
                        "(YYYY-MM-DD). Xero defaults the report date field; this is "
                        "the detection nothing downstream has.")
    p.add_argument("--out", help="write the full rows JSON to this absolute path "
                                 "instead of inlining it in stdout")


def _resolve_sheet(sg: loader.SheetGrid) -> dict:
    resolver = SheetResolver(sg.grid, sheet_name=sg.name,
                             formula_mask=sg.formula_mask)
    rows, formula_cells = [], []
    for r in range(len(sg.grid)):
        vals = []
        for c in range(len(sg.grid[r])):
            raw = sg.grid[r][c]
            try:
                v = resolver.value_at(r, c)
            except FormulaProblem as e:
                raise Refusal("FORMULA_UNEVALUATED", str(e))
            if resolver.is_formula_at(r, c):
                formula_cells.append({"ref": f"{sg.name}!{coord(r, c)}",
                                      "formula": str(raw), "value": to_json_value(v)})
            vals.append(to_json_value(v))
        rows.append(vals)
    return {"name": sg.name, "n_rows": len(rows),
            "n_cols": max((len(r) for r in rows), default=0),
            "formula_count": len(formula_cells), "formula_cells": formula_cells,
            "merged_cells": sg.merged_count, "rows": rows}


def run(args) -> tuple[dict, list[str]]:
    path = Path(args.file).expanduser().resolve()
    grids = loader.load(path, args.sheet)
    warnings: list[str] = []

    sheets = [_resolve_sheet(sg) for sg in grids]
    meta = metadata.extract(grids[0].grid)

    for s in sheets:
        if s["formula_count"] == 0:
            warnings.append(
                f"sheet {s['name']!r} carries no live formulas -- either the format "
                "changed or the file was opened and saved in Excel; the values are "
                "real but verify the provenance")
        if s["merged_cells"]:
            warnings.append(f"sheet {s['name']!r} has {s['merged_cells']} merged "
                            "cell ranges; values sit in the top-left cell of each")

    # The date gate covers EVERY sheet: a wrong-dated second sheet is exactly the
    # fault nothing downstream detects.
    per_sheet_meta = [(sg.name, metadata.extract(sg.grid)) for sg in grids]
    sheet_dates = []          # (sheet, date, raw line)
    for name, m in per_sheet_meta:
        for d in (m.get("report_date"), (m.get("report_period") or {}).get("end")):
            if d:
                sheet_dates.append((name, d, m.get("report_date_raw")))
    header_dates = [d for _, d, _ in sheet_dates]
    if args.expect_date:
        if not header_dates:
            raise Refusal("DATE_MISMATCH",
                          f"--expect-date {args.expect_date} was given but no date "
                          f"could be read from any sheet header of {path.name} "
                          f"(raw header line: {meta.get('report_date_raw')!r}). "
                          "Nothing downstream detects a wrong-dated export; re-pull "
                          "with the date field set explicitly.")
        wrong = [(n, d, raw) for n, d, raw in sheet_dates if d != args.expect_date]
        if wrong:
            n, d, raw = wrong[0]
            raise Refusal("DATE_MISMATCH",
                          f"sheet {n!r} header says {d} (raw line: {raw!r}) but "
                          f"{args.expect_date} was requested. Xero defaults the report "
                          "date field to the end of the current month -- re-export "
                          "with the date set explicitly.")
    elif not header_dates:
        warnings.append("no report date could be read from the export header; "
                        "check the as-at date by eye or re-pull")

    data = {"source_file": str(path), "metadata": meta}
    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        try:
            out_path.write_text(json.dumps({"source_file": str(path),
                                            "metadata": meta, "sheets": sheets},
                                           indent=1))
        except OSError as e:
            raise InputProblem("CANNOT_OPEN",
                               f"cannot write --out {out_path} ({e}) -- if the "
                               "target folder lives in OneDrive it may be cloud-only; "
                               "open it in Finder first")
        data["rows_file"] = str(out_path)
        data["sheets"] = [{k: s[k] for k in
                           ("name", "n_rows", "n_cols", "formula_count", "merged_cells")}
                          for s in sheets]
    else:
        data["sheets"] = sheets
    return data, warnings


def summary(data: dict) -> str:
    n_sheets = len(data.get("sheets", []))
    formulas = sum(s.get("formula_count", 0) for s in data.get("sheets", []))
    d = data.get("metadata", {}).get("report_date") or \
        (data.get("metadata", {}).get("report_period") or {}).get("end") or "no date read"
    return (f"read-ledger: {Path(data['source_file']).name} -- {n_sheets} sheet(s), "
            f"{formulas} formula(s) evaluated, report date {d}")

"""fma xero pull -- every named organisation's statements at one typed date, or nothing.

The order is the point:

  1. Refuse without `--as-at`. There is no default date anywhere in this tool. Xero's
     own screens default a report to the end of the current month, and that default has
     put a wrong date into a pack more than once.
  2. Reach every organisation first (one small call each). A sign-in that has lost one
     company fails here, before a single file exists.
  3. Fetch everything, prove every date against Xero's own title line, run every tie.
     Collect ALL the breaks; one run tells you everything wrong.
  4. Only then write: each workbook read back through read-ledger's loader before it is
     kept, raw responses beside them, and the record of the pull last. If anything
     fails, every file this run made is removed. A folder holds a whole pull or none.

What the pull does not contain is stated in its own record, every time, because a
reader will otherwise assume a complete set: Xero's API gives no general ledger detail
on an affordable tier and no ageing columns at all.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import __version__
from ..errors import InputProblem, Refusal
from . import layout, reports, tenants
from .client import XeroClient

KINDS = ("tb", "bs", "pl", "bank", "coa")

NOT_BY_API = [
    "Account Transactions (general ledger detail): Xero serves it only from an endpoint "
    "reserved for its highest developer tier. Export it by hand from Xero.",
    "Aged Receivables and Aged Payables with Xero's own ageing columns: they exist only "
    "in the on-screen report. Export them by hand from Xero.",
]


def add_arguments(p) -> None:
    p.add_argument("--as-at", help="the as-at date, YYYY-MM-DD. Required: there is no "
                                   "default date")
    p.add_argument("--compare", help="an earlier date to set beside the as-at balance "
                                     "sheet, YYYY-MM-DD")
    p.add_argument("--org", action="append", help="an organisation by key or name "
                                                  "(repeatable)")
    p.add_argument("--all", action="store_true", help="every connected organisation")
    p.add_argument("--prefix", default="", help="leads every file name, e.g. the client")
    p.add_argument("--out", help="absolute path of an empty or new folder for this pull")
    p.add_argument("--reports", default=",".join(KINDS),
                   help=f"which to pull, comma-separated from {','.join(KINDS)} "
                        "(default: all)")
    p.add_argument("--front-matter", action="append", metavar="KEY=VALUE",
                   help="one more line for the head of PULL.md, for a drive whose files "
                        "must carry fields this tool does not know (repeatable)")


def _safe(text: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text.strip())
    return out.strip("_")


_OWN_FIELDS = ("type", "title", "status", "description", "tags")


def _front_matter(items: list[str] | None) -> dict:
    """KEY=VALUE pairs for the head of PULL.md. Plain single-line values only, and
    never one of the fields the record writes for itself."""
    out = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if (not sep or not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in _OWN_FIELDS
                or key in out or not value or len(value) > 120
                or any(ch in value for ch in "\r\n")):
            raise InputProblem("FRONT_MATTER_INVALID",
                               f"--front-matter takes key=value on one line, a lower-case "
                               f"key that is not one of {', '.join(_OWN_FIELDS)}, once "
                               f"each; got {item!r}")
        out[key] = value
    return out


def _kinds(text: str) -> list[str]:
    asked = [k.strip().lower() for k in (text or "").split(",") if k.strip()]
    bad = [k for k in asked if k not in KINDS]
    if bad or not asked:
        raise InputProblem("REPORTS_INVALID",
                           f"--reports takes any of {', '.join(KINDS)}; got {text!r}")
    return [k for k in KINDS if k in asked]


def _out_dir(text: str | None) -> Path:
    if not text:
        raise InputProblem("OUT_REQUIRED", "--out is required: the absolute path of an "
                                           "empty or new folder for this pull")
    out = Path(text).expanduser()
    if not out.is_absolute():
        raise InputProblem("PATH_NOT_ABSOLUTE", f"--out {text!r} is not an absolute path")
    if out.exists():
        if not out.is_dir():
            raise InputProblem("OUT_NOT_A_FOLDER", f"{out} exists and is not a folder")
        if any(p for p in out.iterdir() if p.name != ".DS_Store"):
            raise Refusal("OUT_NOT_EMPTY",
                          f"{out} already holds files. A pull lands in a folder of its "
                          "own so two pulls can never be mistaken for one.")
    elif not out.parent.is_dir():
        raise InputProblem("OUT_PARENT_MISSING",
                           f"{out.parent} does not exist -- if it lives in OneDrive it "
                           "may be cloud-only; open it in Finder first")
    probe = out if out.exists() else out.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        raise InputProblem("CANNOT_WRITE",
                           f"{probe} cannot be written to; nothing was asked of Xero")
    return out


def _ranges(as_at: date, fy_start: date, prior: tuple[date, date]) -> list[tuple[str, date, date]]:
    """The three P&L periods a pack reads, from the one typed date and the
    organisation's own year: the month to date, the year to date, the prior year."""
    wanted = [("month to date", reports.month_start(as_at), as_at),
              ("year to date", fy_start, as_at),
              ("prior year", prior[0], prior[1])]
    seen, out = set(), []
    for label, a, b in wanted:
        if (a, b) not in seen:
            seen.add((a, b))
            out.append((label, a, b))
    return out


def _calls_needed(kinds: list[str], compare: bool) -> int:
    return (("tb" in kinds) + ("bs" in kinds) * (2 if compare else 1)
            + ("pl" in kinds) * 3 + ("bank" in kinds) + ("coa" in kinds))


def run(args) -> tuple[dict, list[str]]:
    if not args.as_at:
        raise Refusal("AS_AT_REQUIRED",
                      "no --as-at date was given. This tool never chooses a date: say "
                      "which day the figures are as at, as YYYY-MM-DD.")
    as_at = reports.parse_iso(args.as_at, "--as-at")
    compare = reports.parse_iso(args.compare, "--compare") if args.compare else None
    if compare and compare >= as_at:
        raise Refusal("COMPARE_NOT_EARLIER",
                      f"--compare {compare} is not before --as-at {as_at}")
    kinds = _kinds(args.reports)
    extra_head = _front_matter(getattr(args, "front_matter", None))
    out = _out_dir(args.out)
    prefix = _safe(args.prefix)
    orgs = tenants.resolve(args.org, args.all)

    client = XeroClient()
    warnings: list[str] = []
    if compare and "bs" not in kinds:
        warnings.append("--compare sets a second date beside the balance sheet, and "
                        "this pull leaves the balance sheet out")

    # 2. reach every organisation before anything else
    plans = []
    for tid, row in orgs:
        user = row.get("user_id") or ""
        org_raw = reports.organisation(client, user, tid)
        org = reports.parse_organisation(org_raw)
        if not layout.name_reads_as_itself(org["name"]):
            raise Refusal(
                "ORG_NAME_READS_AS_A_DATE",
                f"{tenants.key_of(row)}: the organisation's Xero name, {org['name']!r}, "
                "reads as a date line to read-ledger, so none of its workbooks could be "
                "proved after writing. Nothing was written. Pull the others by --org, "
                "and export this one by hand.")
        spend = client.spend[tid]
        needed = _calls_needed(kinds, bool(compare))
        if spend.day_remaining is not None and spend.day_remaining < needed:
            raise Refusal(
                "XERO_DAILY_LIMIT",
                f"{tenants.key_of(row)}: this pull needs {needed} more calls and Xero "
                f"reports {spend.day_remaining} left today for that organisation. "
                "Nothing was written; the allowance resets at midnight UTC.")
        plans.append((tid, row, user, org, org_raw))

    # 3. fetch, prove, tie -- nothing is written yet
    staged, org_records, all_ties = [], [], []
    for tid, row, user, org, org_raw in plans:
        key = tenants.file_key(row)
        stem = f"{prefix}_{key}" if prefix else key
        fy_start, fy_end = reports.financial_year(as_at, org["fy_end_month"],
                                                  org["fy_end_day"])
        prior = reports.financial_year(fy_start - timedelta(days=1), org["fy_end_month"],
                                       org["fy_end_day"])
        files, ties = [], []
        raws = [(f"{stem}_Organisation.json", org_raw)]
        bs_rep = ytd_rep = None

        if "tb" in kinds:
            raw = reports.trial_balance(client, user, tid, as_at)
            rep = reports.parse(raw)
            what = f"{key} Trial Balance"
            reports.prove_as_at(rep, as_at, what)
            ties += reports.section_ties(rep, what) + reports.trial_balance_ties(rep, what)
            raw_name = f"{stem}_TrialBalance_{as_at}.json"
            raws.append((raw_name, raw))
            files.append({"kind": "trial_balance", "raw": raw_name,
                          "name": f"{stem}_Trial_Balance_as_at_{as_at}.xlsx",
                          "sheets": [(rep.name, layout.grid_for(rep, reports.as_at_line(as_at)))],
                          "expect": {"date": as_at.isoformat()},
                          "dates": {"as_at": as_at.isoformat()}})

        if "bs" in kinds:
            raw = reports.balance_sheet(client, user, tid, as_at)
            bs_rep = reports.parse(raw)
            what = f"{key} Balance Sheet"
            reports.prove_as_at(bs_rep, as_at, what)
            ties += reports.section_ties(bs_rep, what)
            ties.append(reports.balance_sheet_tie(bs_rep, what))
            raw_name = f"{stem}_BalanceSheet_{as_at}.json"
            raws.append((raw_name, raw))
            shown, name = bs_rep, f"{stem}_Balance_Sheet_as_at_{as_at}.xlsx"
            dates = {"as_at": as_at.isoformat()}
            raw_names = [raw_name]
            if compare:
                raw2 = reports.balance_sheet(client, user, tid, compare)
                rep2 = reports.parse(raw2)
                what2 = f"{key} Balance Sheet ({compare})"
                reports.prove_as_at(rep2, compare, what2)
                ties += reports.section_ties(rep2, what2)
                ties.append(reports.balance_sheet_tie(rep2, what2))
                raw2_name = f"{stem}_BalanceSheet_{compare}.json"
                raws.append((raw2_name, raw2))
                raw_names.append(raw2_name)
                shown = reports.side_by_side(bs_rep, rep2)
                name = f"{stem}_Balance_Sheet_as_at_{as_at}_vs_{compare}.xlsx"
                dates["compare"] = compare.isoformat()
            files.append({"kind": "balance_sheet", "raw": raw_names, "name": name,
                          "sheets": [(shown.name, layout.grid_for(shown, reports.as_at_line(as_at)))],
                          "expect": {"date": as_at.isoformat()}, "dates": dates})

        if "pl" in kinds:
            for label, a, b in _ranges(as_at, fy_start, prior):
                raw = reports.profit_and_loss(client, user, tid, a, b)
                rep = reports.parse(raw)
                what = f"{key} Profit and Loss ({label})"
                both = reports.prove_range(rep, a, b, what)
                if not both:
                    warnings.append(f"{what}: Xero's title names only the end of the "
                                    f"period; the start {a} is as asked, not as echoed")
                ties += reports.section_ties(rep, what)
                # by its dates, not its label: in the first month of a financial year
                # the month to date IS the year to date, and is fetched once
                if (a, b) == (fy_start, as_at):
                    ytd_rep = rep
                raw_name = f"{stem}_ProfitAndLoss_{a}_{b}.json"
                raws.append((raw_name, raw))
                files.append({"kind": "profit_and_loss", "period": label, "raw": raw_name,
                              "name": f"{stem}_Profit_and_Loss_{a}_to_{b}.xlsx",
                              "sheets": [(rep.name, layout.grid_for(rep, reports.range_line(a, b)))],
                              "expect": {"start": a.isoformat(), "end": b.isoformat()},
                              "dates": {"start": a.isoformat(), "end": b.isoformat(),
                                        "start_echoed": both}})

        if bs_rep is not None and ytd_rep is not None:
            ties.append(reports.earnings_tie(ytd_rep, bs_rep, key))

        if "bank" in kinds:
            a, b = reports.month_start(as_at), as_at
            raw = reports.bank_summary(client, user, tid, a, b)
            rep = reports.parse(raw)
            what = f"{key} Bank Summary"
            both = reports.prove_range(rep, a, b, what)
            if not both:
                warnings.append(f"{what}: Xero's title names only the end of the period")
            ties += reports.section_ties(rep, what)
            raw_name = f"{stem}_BankSummary_{a}_{b}.json"
            raws.append((raw_name, raw))
            files.append({"kind": "bank_summary", "raw": raw_name,
                          "name": f"{stem}_Bank_Summary_{a}_to_{b}.xlsx",
                          "sheets": [(rep.name, layout.grid_for(rep, reports.range_line(a, b)))],
                          "expect": {"start": a.isoformat(), "end": b.isoformat()},
                          "dates": {"start": a.isoformat(), "end": b.isoformat(),
                                    "start_echoed": both}})

        if "coa" in kinds:
            raw = reports.accounts(client, user, tid)
            chart = reports.parse_accounts(raw)
            raw_name = f"{stem}_Accounts.json"
            raws.append((raw_name, raw))
            body = [[a["code"], a["name"], a["type"], a["class"], a["tax_type"], a["status"]]
                    for a in sorted(chart, key=lambda a: (a["code"] == "", a["code"], a["name"]))]
            grid = layout.table_grid(
                "Chart of Accounts", org["name"],
                "The chart at the moment of the pull, not at a report date",
                ["Code", "Name", "Type", "Class", "Tax type", "Status"], body)
            files.append({"kind": "chart_of_accounts", "raw": raw_name,
                          "name": f"{stem}_Chart_of_Accounts.xlsx",
                          "sheets": [("Chart of Accounts", grid)], "expect": None,
                          "dates": {}})

        for t in ties:
            if t.status == "rounding":
                warnings.append(f"{t.name}: {t.detail} (within rounding)")
            elif t.status == "not_checked":
                warnings.append(f"{t.name}: not checked -- {t.detail}")
        all_ties += ties
        spend = client.spend[tid]
        org_records.append({
            "key": tenants.key_of(row), "tenant_id": tid, "name": org["name"],
            "legal_name": org["legal_name"], "base_currency": org["base_currency"],
            "sales_tax_basis": org["sales_tax_basis"], "is_demo": org["is_demo"],
            "financial_year": {"start": fy_start.isoformat(), "end": fy_end.isoformat()},
            "authorised_by": row.get("authorised_by") or "",
            "ties": [t.as_dict() for t in ties],
            "calls": spend.calls, "day_remaining": spend.day_remaining, "files": []})
        staged.append((files, raws))

    breaks = [t for t in all_ties if t.status == "break"]
    if breaks:
        raise Refusal(
            "TIES_BROKEN", f"{len(breaks)} tie(s) broke; nothing was written",
            problems=[{"code": "TIE_BROKEN", "message": f"{t.name}: {t.detail}"}
                      for t in breaks],
            data={"as_at": as_at.isoformat(), "written": []})

    # 4. write, all or nothing
    made_out = not out.exists()
    raw_dir = out / "raw"
    written: list[Path] = []
    try:
        try:
            out.mkdir(exist_ok=True)
            raw_dir.mkdir(exist_ok=True)
        except OSError as e:
            raise InputProblem("CANNOT_WRITE",
                               f"cannot create {raw_dir} ({e.strerror or e}) -- if the "
                               "folder lives in OneDrive it may be cloud-only; open it "
                               "in Finder first")
        for (files, raws), record in zip(staged, org_records):
            raw_hashes = {}
            for raw_name, blob in raws:
                p = raw_dir / raw_name
                raw_hashes[raw_name] = layout.write_bytes(p, blob)
                written.append(p)
            record["raw"] = [{"file": f"raw/{n}", "sha256": h} for n, h in raw_hashes.items()]
            for f in files:
                p = out / f["name"]
                info = layout.write_workbook(p, f["sheets"], f["expect"])
                written.append(p)
                entry = {"file": info["file"], "kind": f["kind"], "sha256": info["sha256"],
                         "rows": info["rows"], **f["dates"]}
                if f.get("period"):
                    entry["period"] = f["period"]
                raw_list = f["raw"] if isinstance(f["raw"], list) else [f["raw"]]
                entry["raw"] = [f"raw/{n}" for n in raw_list]
                record["files"].append(entry)
        pulled_at = datetime.now(timezone.utc).replace(microsecond=0)
        record_doc = {
            "schema": 1, "tool": "fma xero pull", "version": __version__,
            "pulled_at_utc": pulled_at.isoformat().replace("+00:00", "Z"),
            "as_at": as_at.isoformat(),
            "compare": compare.isoformat() if compare else None,
            "basis": "accrual", "layout": "Xero's standard layout",
            "organisations": org_records, "not_available_by_api": NOT_BY_API,
            "warnings": warnings, "front_matter": extra_head}
        blob = json.dumps(record_doc, indent=1).encode()
        layout.write_bytes(out / "PULL.json", blob)
        written.append(out / "PULL.json")
        layout.write_bytes(out / "PULL.md", _markdown(record_doc).encode())
        written.append(out / "PULL.md")
    except BaseException:
        for p in written:
            with contextlib.suppress(OSError):
                p.unlink()
        with contextlib.suppress(OSError):
            raw_dir.rmdir()
        if made_out:
            with contextlib.suppress(OSError):
                out.rmdir()
        raise

    data = {"as_at": as_at.isoformat(), "compare": compare.isoformat() if compare else None,
            "out": str(out), "record": str(out / "PULL.json"),
            "organisations": [{"key": r["key"], "name": r["name"],
                               "files": [f["file"] for f in r["files"]],
                               "calls": r["calls"], "day_remaining": r["day_remaining"],
                               "ties": {s: sum(1 for t in r["ties"] if t["status"] == s)
                                        for s in ("ok", "rounding", "not_checked")}}
                              for r in org_records],
            "not_available_by_api": NOT_BY_API}
    return data, warnings


def _markdown(doc: dict) -> str:
    lines = ["---", "type: record",
             f"title: Xero pull as at {doc['as_at']}",
             *[f"{k}: {v}" for k, v in (doc.get("front_matter") or {}).items()],
             "status: final",
             f"description: What was pulled from Xero as at {doc['as_at']}, when, under "
             "whose authority, and what the pull does not contain.",
             "tags: [xero, pull]", "---", "",
             f"# Xero pull as at {reports.long_date(date.fromisoformat(doc['as_at']))}", "",
             f"Pulled {doc['pulled_at_utc']} by `fma xero` {doc['version']}. Read-only. "
             f"Accrual basis, {doc['layout']}. Every date below was typed for this pull "
             "or derived from it and the organisation's own financial year; none is a "
             "default.", ""]
    if doc.get("compare"):
        lines += [f"Balance sheets carry a second column as at {doc['compare']}.", ""]
    for org in doc["organisations"]:
        lines += [f"## {org['key']} — {org['legal_name'] or org['name']}", "",
                  f"- Xero name: {org['name']}",
                  f"- Authorised by: {org['authorised_by'] or 'not recorded'}",
                  f"- Financial year: {org['financial_year']['start']} to "
                  f"{org['financial_year']['end']}; base currency {org['base_currency']}",
                  f"- Calls spent: {org['calls']}"
                  + (f"; Xero reports {org['day_remaining']} left today"
                     if org["day_remaining"] is not None else ""), "",
                  "| File | What | Dates | sha256 |", "| --- | --- | --- | --- |"]
        for f in org["files"]:
            if f.get("as_at"):
                dates = f"as at {f['as_at']}" + (f" vs {f['compare']}" if f.get("compare") else "")
            elif f.get("start"):
                dates = f"{f['start']} to {f['end']}"
            else:
                dates = "at the moment of the pull"
            what = f["kind"].replace("_", " ") + (f" ({f['period']})" if f.get("period") else "")
            lines.append(f"| `{f['file']}` | {what} | {dates} | `{f['sha256'][:16]}…` |")
        agreed = sum(1 for t in org["ties"] if t["status"] == "ok")
        others = [t for t in org["ties"] if t["status"] != "ok"]
        lines += ["", f"Ties: {agreed} of {len(org['ties'])} agree to the cent"
                      + ("." if not others else "; the rest:"), ""]
        lines += [f"- {t['status'].replace('_', ' ')}: {t['name']} — {t['detail']}"
                  for t in others]
        if others:
            lines.append("")
    lines += ["## Not in this pull, and why", ""]
    lines += [f"- {n}" for n in doc["not_available_by_api"]]
    if doc.get("warnings"):
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in doc["warnings"]]
    lines += ["", "The responses exactly as Xero sent them are in `raw/`; `PULL.json` is "
              "this record in full, with whole hashes.", ""]
    return "\n".join(lines)


def summary(data: dict) -> str:
    orgs = data.get("organisations", [])
    n = sum(len(o["files"]) for o in orgs)
    return (f"xero pull: {n} file(s) for {len(orgs)} organisation(s) as at "
            f"{data.get('as_at')} in {data.get('out')}")

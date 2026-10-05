"""fma xero group -- the organisations of one pull, side by side, and nothing claimed
that was not done.

What the sheet is: each organisation's trial balance at one date, lined up by account
code, with a column that is their plain sum. What it is not, printed on its face: it is
not consolidated (nothing is eliminated between the organisations) and it is not a set
of statutory group accounts. The Eliminations column is there, and empty, because the
work that fills it is a person's judgement about which balances are with each other.

Charts of accounts differ between companies of one group more often than anyone
expects. This tool never decides that two accounts are "the same":

  * Two accounts share a line only when they carry the same account CODE, or codes a
    mapping file says belong together. Never because their names match.
  * An account with no code (usual for a bank account in Xero) stands on a line of its
    own, under its own organisation. Its name is not searched for something that looks
    like a code: "Term Deposit (800)" is not account 800.
  * Every difference between the charts is listed on a second sheet for a person to
    read.

It works from the raw responses a pull kept -- the trial balance joined to the chart
of accounts by Xero's account id -- so nothing is re-read from a rendered sheet, and it
needs both to have been pulled. It uses no network.
"""

from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from ..errors import InputProblem, Refusal
from . import layout, reports

_CLASS_ORDER = {"ASSET": 0, "LIABILITY": 1, "EQUITY": 2, "REVENUE": 3, "EXPENSE": 4}
# The trial balance groups its lines under these headings; they stand in for an
# account's class when the chart does not say.
_SECTION_CLASS = {"ASSETS": "ASSET", "LIABILITIES": "LIABILITY", "EQUITY": "EQUITY",
                  "REVENUE": "REVENUE", "EXPENSES": "EXPENSE"}
_YES = {"y", "yes", "true", "1"}
_OUT_BY = Decimal("0.05")


def add_arguments(p) -> None:
    p.add_argument("--pull", help="absolute path of the pull folder (holds PULL.json)")
    p.add_argument("--as-at", help="the pull's as-at date, YYYY-MM-DD; refuses if the "
                                   "pull says otherwise")
    p.add_argument("--out", help="absolute path of the .xlsx to write")
    p.add_argument("--replace", action="store_true",
                   help="write over an existing --out (which may hold eliminations "
                        "someone typed in)")
    p.add_argument("--group-name", default="Group", help="what to call the group on "
                                                         "the sheet")
    p.add_argument("--mapping", help="absolute path of a CSV with columns entity, code, "
                                     "group_code[, group_name][, intercompany]")


def _abs(text: str | None, flag: str) -> Path:
    if not text:
        raise InputProblem("ARGUMENT_REQUIRED", f"{flag} is required")
    p = Path(text).expanduser()
    if not p.is_absolute():
        raise InputProblem("PATH_NOT_ABSOLUTE", f"{flag} {text!r} is not an absolute path")
    return p


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise InputProblem("CANNOT_OPEN",
                           f"cannot read {path} ({e.strerror or e}) -- if it lives in "
                           "OneDrive it may be cloud-only; open it in Finder first")


def _load_mapping(path: Path | None, keys: set[str]) -> dict:
    """(entity key, code) -> {"group_code", "group_name", "intercompany", "line"}."""
    if path is None:
        return {}
    try:
        text = _read(path).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InputProblem("MAPPING_INVALID",
                           f"{path.name} is not UTF-8 text; in Excel, save it as "
                           "'CSV UTF-8 (Comma delimited)'")
    rows = list(csv.DictReader(text.splitlines()))
    have = {(h or "").strip().lower() for h in (rows[0].keys() if rows else []) if h}
    if not rows or not {"entity", "code", "group_code"} <= have:
        raise InputProblem("MAPPING_INVALID",
                           f"{path.name} needs the columns entity, code, group_code "
                           "(group_name and intercompany are optional)")
    out, folded = {}, {k.casefold(): k for k in keys}
    for n, raw in enumerate(rows, start=2):
        if None in raw:
            raise InputProblem("MAPPING_INVALID",
                               f"{path.name} line {n} has more cells than the header")
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not r.get("entity") and not r.get("code"):
            continue
        entity = folded.get(r.get("entity", "").casefold())
        if entity is None:
            raise Refusal("MAPPING_UNKNOWN_ENTITY",
                          f"{path.name} line {n} names {r.get('entity')!r}, which is not "
                          f"in this pull ({', '.join(sorted(keys))})")
        if not r.get("code") or not r.get("group_code"):
            raise InputProblem("MAPPING_INVALID",
                               f"{path.name} line {n} has no code or no group_code")
        if (entity, r["code"]) in out:
            raise Refusal("MAPPING_DUPLICATE", f"{path.name} maps {entity} {r['code']} twice")
        out[(entity, r["code"])] = {
            "group_code": r["group_code"], "group_name": r.get("group_name", ""),
            "intercompany": r.get("intercompany", "").lower() in _YES, "line": n}
    return out


def _entity(pull: Path, record: dict) -> dict:
    """One organisation: its year-to-date balance by account, and its chart."""
    key = record["key"]
    hashes = {r["file"]: r["sha256"] for r in record.get("raw", [])}

    def raw(relative: str) -> bytes:
        blob = _read(pull / relative)
        want = hashes.get(relative)
        if want and hashlib.sha256(blob).hexdigest() != want:
            raise Refusal("PULL_CHANGED",
                          f"{relative} is not the file this pull recorded; pull again "
                          "rather than build a group sheet on changed inputs")
        return blob

    tb_file = next((f for f in record["files"] if f["kind"] == "trial_balance"), None)
    chart_file = next((f for f in record["files"] if f["kind"] == "chart_of_accounts"), None)
    if tb_file is None or chart_file is None:
        missing = "trial balance" if tb_file is None else "chart of accounts"
        raise Refusal("PULL_INCOMPLETE",
                      f"the pull holds no {missing} for {key}. A group sheet needs both: "
                      "the chart is the only place an account's code can be read from. "
                      "Pull again with tb and coa included.")
    chart = reports.parse_accounts(raw(chart_file["raw"][0]))
    by_id = {a["id"]: a for a in chart if a["id"]}

    rep = reports.parse(raw(tb_file["raw"][0]))
    cols = {h.strip().casefold(): i for i, h in enumerate(rep.header[1:])}
    if "ytd debit" not in cols or "ytd credit" not in cols:
        raise Refusal("TRIAL_BALANCE_SHAPE",
                      f"{key}: the trial balance has no 'YTD Debit'/'YTD Credit' columns "
                      f"(it has {rep.header!r}), so no balance can be read from it")

    def amount(ln, i) -> Decimal:
        v = ln.values[i] if i < len(ln.values) else None
        if isinstance(v, str):
            raise Refusal("TRIAL_BALANCE_SHAPE",
                          f"{key}: {ln.label!r} carries {v!r} where a number belongs")
        return v or Decimal(0)

    balances, total = [], Decimal(0)
    for n, (sec, ln) in enumerate((s, ln) for s in rep.sections for ln in s.lines):
        if ln.kind != "row":
            continue
        balance = amount(ln, cols["ytd debit"]) - amount(ln, cols["ytd credit"])
        known = by_id.get(ln.account_id or "")
        balances.append({
            # The chart's code, even when it is empty. The label is never mined for one.
            "code": known["code"] if known else "",
            "name": (known["name"] if known and known["name"] else ln.label.strip()),
            "own": ln.account_id or f"line-{n}",
            "class": ((known or {}).get("class") or "").upper()
                     or _SECTION_CLASS.get(sec.title.strip().upper(), sec.title.upper()),
            "type": (known or {}).get("type") or "", "balance": balance})
        total += balance
    if abs(total) > _OUT_BY:
        raise Refusal("TRIAL_BALANCE_OUT",
                      f"{key}: year-to-date debits and credits differ by {total:,.2f}; "
                      "an out-of-balance trial balance is not added to anything")
    return {"key": key, "label": record.get("legal_name") or record.get("name") or key,
            "name": record.get("name") or "", "currency": record.get("base_currency") or "",
            "fy_end": (record.get("financial_year") or {}).get("end", ""),
            "authorised_by": record.get("authorised_by") or "",
            "tb_file": tb_file["file"], "tb_sha256": tb_file["sha256"],
            "balances": balances,
            "codes": {a["code"] for a in chart if a["code"]},
            "chart": [{"code": a["code"], "name": a["name"]} for a in chart
                      if a["status"].upper() in ("", "ACTIVE")]}


def _differences(entities: list[dict], mapping: dict) -> list[list]:
    """What a person has to look at before these charts can be treated as one."""
    keys = {e["key"] for e in entities}
    codes: dict = {}          # group code -> {entity key: account name}
    names: dict = {}          # folded name -> [display name, {group codes}]
    out = []
    for e in entities:
        for a in e["chart"]:
            m = mapping.get((e["key"], a["code"]))
            code = m["group_code"] if m else a["code"]
            if not code:
                continue
            codes.setdefault(code, {}).setdefault(e["key"], a["name"])
            entry = names.setdefault(a["name"].strip().casefold(), [a["name"].strip(), set()])
            entry[1].add(code)
        for a in e["balances"]:
            if not a["code"]:
                out.append(["No code: not lined up", None, a["name"],
                            f"in {e['key']}: the account has no code, so it stands on a "
                            "line of its own"])
    if len(keys) > 1:
        for code in sorted(codes):
            held = codes[code]
            missing = sorted(keys - set(held))
            if missing:
                out.append(["In some organisations only", code, next(iter(held.values())),
                            f"in {', '.join(sorted(held))}; not in {', '.join(missing)}"])
            if len({n.strip().casefold() for n in held.values()}) > 1:
                out.append(["One code, different names", code, sorted(held.values())[0],
                            "; ".join(f"{k}: {v}" for k, v in sorted(held.items()))])
    for folded in sorted(names):
        display, held = names[folded]
        if len(held) > 1:
            out.append(["One name, different codes", ", ".join(sorted(held)), display,
                        "the same account name sits under more than one code"])
    return out


def run(args) -> tuple[dict, list[str]]:
    pull = _abs(args.pull, "--pull")
    out = _abs(args.out, "--out")
    if out.suffix.lower() != ".xlsx":
        raise InputProblem("OUT_NOT_XLSX", f"--out must end in .xlsx, got {out.name}")
    if out.exists() and not args.replace:
        raise Refusal("OUT_EXISTS",
                      f"{out.name} already exists and may hold eliminations someone "
                      "typed in. Give another name, or pass --replace to write over it.")
    if not args.as_at:
        raise Refusal("AS_AT_REQUIRED",
                      "no --as-at date was given. Say which date the group sheet is as "
                      "at; it is checked against the pull's own record.")
    as_at = reports.parse_iso(args.as_at, "--as-at")
    record_path = pull / "PULL.json"
    if not record_path.exists():
        raise Refusal("PULL_RECORD_MISSING",
                      f"{pull} holds no PULL.json; a group sheet is built only from a "
                      "pull this tool recorded")
    try:
        doc = json.loads(_read(record_path).decode("utf-8"))
    except ValueError:
        raise InputProblem("CANNOT_OPEN", f"{record_path} is not valid JSON")
    if doc.get("as_at") != as_at.isoformat():
        raise Refusal("DATE_MISMATCH",
                      f"the pull is as at {doc.get('as_at')}, not {as_at.isoformat()}")

    entities = [_entity(pull, r) for r in doc.get("organisations") or []]
    if not entities:
        raise Refusal("PULL_EMPTY", "the pull records no organisations")
    warnings: list[str] = []
    currencies = sorted({e["currency"] for e in entities})
    if len(currencies) > 1:
        raise Refusal("CURRENCIES_DIFFER",
                      "these organisations keep their books in different currencies "
                      f"({', '.join(currencies)}); their balances cannot be added")
    year_ends = sorted({e["fy_end"][5:] for e in entities if e["fy_end"]})
    statement = (f"{args.group_name}: management aggregation of {len(entities)} "
                 f"organisation{'s' if len(entities) != 1 else ''}, no eliminations, "
                 "not statutory accounts")
    if len(year_ends) > 1:
        warnings.append("the organisations have different financial-year ends, so the "
                        "profit and loss lines cover different periods; the balance "
                        "sheet lines are all as at the same date")
        statement += "; financial-year ends differ, so profit and loss lines cover " \
                     "different periods"

    by_key = {e["key"]: e for e in entities}
    mapping = _load_mapping(_abs(args.mapping, "--mapping") if args.mapping else None,
                            set(by_key))
    # A mapping line that matches no account is a typo waiting to be believed.
    unmatched = [(m["line"], ek, code) for (ek, code), m in mapping.items()
                 if code not in by_key[ek]["codes"]]
    if unmatched:
        raise Refusal(
            "MAPPING_UNMATCHED",
            f"{len(unmatched)} mapping line(s) name a code the organisation's chart does "
            "not have; nothing was written",
            problems=[{"code": "MAPPING_UNMATCHED",
                       "message": f"line {n}: {ek} has no account coded {code!r}"}
                      for n, ek, code in sorted(unmatched)])

    rows: dict = {}
    applied = set()
    for e in entities:
        for a in e["balances"]:
            m = mapping.get((e["key"], a["code"])) if a["code"] else None
            if m:
                applied.add((e["key"], a["code"]))
            code = m["group_code"] if m else a["code"]
            # no code: a line of its own, under its own organisation
            rk = ("code", code) if code else ("own", e["key"], a["own"])
            row = rows.setdefault(rk, {"code": code,
                                       "name": (m or {}).get("group_name") or a["name"],
                                       "class": a["class"], "type": a["type"],
                                       "values": {}, "intercompany": False})
            row["values"][e["key"]] = row["values"].get(e["key"], Decimal(0)) + a["balance"]
            row["intercompany"] = row["intercompany"] or bool(m and m["intercompany"])
    ordered = sorted(rows.values(),
                     key=lambda r: (_CLASS_ORDER.get(r["class"], 9), r["code"] == "",
                                    r["code"], r["name"].casefold()))
    differences = _differences(entities, mapping)

    header = ["Code", "Account", "Class", "Type", *[e["label"] for e in entities],
              "Group (sum)", "Eliminations", "Intercompany"]
    body, totals = [], {e["key"]: Decimal(0) for e in entities}
    for r in ordered:
        vals = [r["values"].get(e["key"]) for e in entities]
        for e, v in zip(entities, vals):
            totals[e["key"]] += v or Decimal(0)
        body.append([r["code"] or None, r["name"], r["class"] or None, r["type"] or None,
                     *vals, sum((v for v in vals if v is not None), Decimal(0)), None,
                     "yes" if r["intercompany"] else None])
    body += [[], [None, "Total (debits less credits)", None, None,
                  *[totals[e["key"]] for e in entities],
                  sum(totals.values(), Decimal(0)), None, None]]
    date_line = reports.as_at_line(as_at)
    group_grid = [["Group trial balance"], [statement], [date_line], [], header, *body]
    diff_grid = [["Chart differences"], [statement], [date_line], [],
                 ["Difference", "Code", "Account", "Detail"],
                 *(differences or [[None, None, "None found",
                                    "every code is in every organisation under one name"]])]
    about_grid = layout.table_grid(
        "About this sheet", statement,
        "Built from the trial balances a pull recorded; year-to-date debits less credits",
        ["Key", "Legal entity", "Xero name", "Financial year ends", "Base currency",
         "Authorised by", "Trial balance file", "sha256"],
        [[e["key"], e["label"], e["name"], e["fy_end"], e["currency"], e["authorised_by"],
          e["tb_file"], e["tb_sha256"]] for e in entities])

    info = layout.write_workbook(out, [("Group", group_grid),
                                       ("Chart differences", diff_grid),
                                       ("About", about_grid)],
                                 {"date": as_at.isoformat()}, layout.GROUP_CREATOR)
    data = {"out": str(out), "as_at": as_at.isoformat(), "sha256": info["sha256"],
            "organisations": [{"key": e["key"], "entity": e["label"]} for e in entities],
            "accounts": len(ordered), "differences": len(differences),
            "mapping_lines": len(mapping), "mapping_lines_with_a_balance": len(applied),
            "eliminations": "none; the column is empty"}
    return data, warnings


def summary(data: dict) -> str:
    return (f"xero group: {data.get('accounts')} account line(s) across "
            f"{len(data.get('organisations', []))} organisation(s) as at "
            f"{data.get('as_at')}, {data.get('differences')} chart difference(s) listed")

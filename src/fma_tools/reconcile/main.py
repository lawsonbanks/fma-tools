"""fma reconcile -- run a pack's arithmetic ties and refuse if one breaks.

Every check runs, every break aggregates, one report -- so a single run tells you
everything wrong. Exit 1 lists every broken tie; a broken tie stops the pack. Never
adjust a figure to make it tie. A break in the source's own records passes only as a
disclosed difference: named against its check, carrying a note, echoed in the output
so the pack can print the finding.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..errors import InputProblem, Refusal
from . import assertions, debtors, month_end, year_end
from .lib import Disclosures, gather, load_schema, validate_schema


def add_arguments(p) -> None:
    p.add_argument("mode", choices=["month_end", "debtors", "year_end", "assert"],
                   help="which tie set runs")
    p.add_argument("--contract", help="month_end: absolute path to the contract JSON")
    p.add_argument("--display", help="month_end: the whole-dollar display copy to "
                                     "verify against the contract (round-once)")
    p.add_argument("--data", help="debtors / year_end / assert: the data document")
    p.add_argument("--ties", help="assert: the ties spec JSON")


def _load(path_str: str | None, what: str) -> dict | list:
    if not path_str:
        raise InputProblem("CONTRACT_INVALID", f"{what} is required for this mode")
    p = Path(path_str).expanduser().resolve()
    if not p.exists():
        raise InputProblem("CANNOT_OPEN", f"no file at {p}")
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise InputProblem("CANNOT_OPEN", f"cannot read {p}: {e}")


def run(args) -> tuple[dict, list[str]]:
    warnings: list[str] = []

    if args.mode == "month_end":
        c = _load(args.contract, "--contract")
        validate_schema(c, load_schema("month_end_contract.schema.json"), "contract")
        disc = Disclosures(c.get("disclosed_differences"),
                           non_disclosable=month_end.NON_DISCLOSABLE)
        results = month_end.checks(c, disc)
        disc.assert_all_consumed()
        if args.display:
            d = _load(args.display, "--display")
            validate_schema(d, load_schema("month_end_contract.schema.json"),
                            "display copy")
            results += month_end.display_checks(c, d)
    elif args.mode == "debtors":
        data = _load(args.data, "--data")
        validate_schema(data, load_schema("debtors_data.schema.json"), "debtors data")
        disc = Disclosures(data.get("disclosed_differences"))
        results, warnings = debtors.checks(data, disc)
        disc.assert_all_consumed()
    elif args.mode == "year_end":
        data = _load(args.data, "--data")
        validate_schema(data, load_schema("year_end_data.schema.json"), "year-end data")
        disc = Disclosures(data.get("disclosed_differences"))
        results = year_end.checks(data, disc)
        disc.assert_all_consumed()
    else:  # assert
        data = _load(args.data, "--data")
        ties_spec = _load(args.ties, "--ties")
        validate_schema(ties_spec, load_schema("ties.schema.json"), "ties spec")
        disc = Disclosures(data.get("disclosed_differences") if isinstance(data, dict)
                           else None)
        results = assertions.checks(data, ties_spec, disc)
        disc.assert_all_consumed()

    checks_json, breaks = gather(results)
    data_out = {"mode": args.mode, "n_checks": len(checks_json),
                "checks": checks_json, "breaks": breaks,
                "disclosures_applied": disc.applied()}
    if breaks:
        raise Refusal(
            "TIE_BROKEN", f"{len(breaks)} tie(s) broke", data=data_out,
            problems=[{"code": "TIE_BROKEN",
                       "message": b["name"] + (f" — {b['detail']}" if b.get("detail")
                                               else "")
                                  + (f" (left {b['left']}, right {b['right']}, "
                                     f"difference {b['difference']})"
                                     if "left" in b else "")}
                      for b in breaks])
    return data_out, warnings


def summary(data: dict) -> str:
    n = data.get("n_checks", 0)
    disc = data.get("disclosures_applied", [])
    tail = f", {len(disc)} disclosed difference(s) applied" if disc else ""
    return f"reconcile {data.get('mode')}: {n} checks, all tie{tail}"

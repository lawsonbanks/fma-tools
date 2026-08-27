"""Shared machinery for the reconcile modes: tolerance, check results, disclosures.

Ported from the engine this replaces, with its institutional memory kept in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import InputProblem

# Real Xero data is to the cent, but every subtotal here is a sum of already-rounded
# floats, so the two sides of a tie differ by IEEE-754 drift (~1e-9) that an exact !=
# reports as a break. Compare to HALF A CENT: that ignores the representation noise
# (measured max drift on the live June VIC pack was 7e-10) while catching any genuine
# break, which is dollars, never 1e-9. A dollar-level gap is a real mapping bug — fix
# the pull/mapping/contract, NEVER widen this tolerance to hide it.
CENT = 0.005

# "To the dollar" ties (prior-year P&L against the lodged statutory accounts).
DOLLAR = 0.5


@dataclass
class CheckResult:
    id: str
    name: str
    passed: bool
    left: float | None = None
    right: float | None = None
    tolerance: float | None = None
    disclosed: float = 0.0
    detail: str | None = None

    def to_json(self) -> dict:
        d = {"id": self.id, "name": self.name, "passed": self.passed}
        if self.left is not None:
            d.update(left=self.left, right=self.right, tolerance=self.tolerance,
                     difference=round((self.left or 0) - ((self.right or 0) + self.disclosed), 6))
        if self.disclosed:
            d["disclosed_difference"] = self.disclosed
        if self.detail:
            d["detail"] = self.detail
        return d


def tie(check_id: str, name: str, left: float, right: float, *,
        tol: float = CENT, disclosed: float = 0.0, detail: str | None = None) -> CheckResult:
    """left == right + disclosed, within tol. The disclosure is a named, shown item
    (unposted/unallocated, a client-records gap) — never a silent plug. A recorded
    disclosure that does not actually close the gap still fails."""
    passed = abs(left - (right + disclosed)) <= tol
    return CheckResult(check_id, name, passed, left=float(left), right=float(right),
                       tolerance=tol, disclosed=disclosed, detail=detail)


def fact(check_id: str, name: str, passed: bool, detail: str | None = None) -> CheckResult:
    """A non-arithmetic check (a date agrees, a basis matches, a count is zero)."""
    return CheckResult(check_id, name, passed, detail=detail)


class Disclosures:
    """disclosed_differences: [{check, amount, note}] — how a break in the SOURCE'S
    OWN records passes as a named finding while a break WE caused still refuses.

    Every disclosure must name the check it closes, carry a non-empty note, and be
    consumed by a check that exists; anything else is an input problem, not a pass.
    """

    def __init__(self, items: list[dict] | None, *, non_disclosable: set[str] = frozenset()):
        self._by_check: dict[str, float] = {}
        self._notes: dict[str, list[str]] = {}
        self._used: set[str] = set()
        for d in (items or []):
            check = d.get("check", "")
            note = str(d.get("note", "")).strip()
            if not note:
                raise InputProblem(
                    "CONTRACT_INVALID",
                    f"disclosed difference against {check!r} carries no note — a "
                    "difference with no name is a plug, and a plug never passes")
            if check in non_disclosable:
                raise InputProblem(
                    "CONTRACT_INVALID",
                    f"the {check!r} tie cannot be disclosed away: if it breaks, stop "
                    "and find the cause. Never adjust the pack to make it tie.")
            self._by_check[check] = self._by_check.get(check, 0.0) + float(d["amount"])
            self._notes.setdefault(check, []).append(note)

    def for_check(self, check_id: str) -> float:
        self._used.add(check_id)
        return self._by_check.get(check_id, 0.0)

    def applied(self) -> list[dict]:
        return [{"check": k, "amount": v, "notes": self._notes[k]}
                for k, v in self._by_check.items() if k in self._used]

    def assert_all_consumed(self) -> None:
        unknown = [k for k in self._by_check if k not in self._used]
        if unknown:
            raise InputProblem(
                "CONTRACT_INVALID",
                f"disclosed differences name checks that do not exist: {unknown} — "
                "a typo here would silently skip the tie it meant to disclose")


def validate_schema(doc: dict, schema: dict, what: str) -> None:
    """Refuse a malformed input with JSON pointers, never a KeyError traceback."""
    import jsonschema
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        msgs = []
        for e in errors[:12]:
            pointer = "/" + "/".join(str(p) for p in e.absolute_path)
            msgs.append(f"{pointer or '/'}: {e.message}")
        more = f" (+{len(errors) - 12} more)" if len(errors) > 12 else ""
        raise InputProblem("CONTRACT_INVALID",
                           f"{what} fails its schema: " + "; ".join(msgs) + more)


def load_schema(name: str) -> dict:
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parent.parent / "schemas" / name).read_text())


def gather(checks: list[CheckResult]) -> tuple[list[dict], list[dict]]:
    """(checks_json, breaks_json) — every check reported, every break aggregated so
    one run tells you everything wrong."""
    checks_json = [c.to_json() for c in checks]
    breaks = [c.to_json() for c in checks if not c.passed]
    return checks_json, breaks

"""assert mode — one-off ties as signed sums of field paths. Deliberately not an
expression language: the tool performs the arithmetic, the agent only NAMES what must
equal what, and the named ties print in the output for a human to review. No
functions, no conditionals, no operators beyond a leading minus on a path.
"""

from __future__ import annotations

from ..errors import InputProblem
from .lib import CENT, CheckResult, Disclosures, tie


def _resolve(doc: dict, path: str) -> float:
    sign = 1.0
    if path.startswith("-"):
        sign, path = -1.0, path[1:]
    v = doc
    for part in path.split("."):
        if isinstance(v, list):
            try:
                v = v[int(part)]
                continue
            except (ValueError, IndexError):
                raise InputProblem("CONTRACT_INVALID",
                                   f"path {path!r}: index {part!r} not in the list")
        if not isinstance(v, dict) or part not in v:
            raise InputProblem("CONTRACT_INVALID",
                               f"path {path!r} does not resolve in the data document "
                               f"(missing {part!r})")
        v = v[part]
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise InputProblem("CONTRACT_INVALID",
                           f"path {path!r} resolves to {v!r}, not a number")
    return sign * float(v)


def checks(data: dict, ties_spec: list[dict], disc: Disclosures) -> list[CheckResult]:
    out = []
    for i, t in enumerate(ties_spec):
        name = t.get("name") or f"tie[{i}]"
        cid = f"assert.{name}"
        left = sum(_resolve(data, p) for p in t["left"])
        right = sum(_resolve(data, p) for p in t["right"])
        out.append(tie(cid, name, left, right,
                       tol=float(t.get("tolerance", CENT)),
                       disclosed=disc.for_check(cid),
                       detail=f"left = {' + '.join(t['left'])}; "
                              f"right = {' + '.join(t['right'])}"))
    return out

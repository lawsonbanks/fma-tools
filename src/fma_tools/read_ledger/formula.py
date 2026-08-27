"""Evaluate the tiny formula grammar Xero writes into .xlsx exports, refusing anything else.

WHY THIS EXISTS, AND WHAT IT PREVENTS (carried from the engine this replaces).

Xero writes every subtotal as a formula -- `=SUM(B8:B12)`, `=(B13 - B19)` -- and caches
zero or nothing. `openpyxl(data_only=True)` returns the cached value, so on a file that
has never been opened in Excel it returns **0** for Total Trading Income, Gross Profit,
Total Operating Expenses and Net Profit. Not blank. Zero. Every downstream numeric guard
passes, every arithmetic works, and the pack prints a fully-formed set of figures that
are all wrong in the same direction.

So this module resolves the formulas itself and REFUSES on any cell it cannot resolve.
The grammar is deliberately tiny -- cell references, SUM over a single-row or
single-column range or a comma list, `+ - * /`, parentheses and numeric literals --
because a Xero export contains nothing else, and a wider evaluator would start guessing
instead of refusing.

Three refusals here are deliberately stricter than Excel-compatible silence:
  - a formula referencing a TEXT cell refuses (Excel shows #VALUE!; the old engine
    silently substituted 0.0)
  - division by zero refuses (Excel shows #DIV/0!; the old engine returned a blank)
  - a SUM range spanning both rows and columns refuses (it would double-count any
    subtotal inside the block)
A blank cell referenced in arithmetic is 0.0 -- that IS Excel semantics, and sparse
statement grids are normal. SUM skips non-numeric cells (label rows sit inside ranges).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time

_MAX_DEPTH = 24


class FormulaProblem(RuntimeError):
    """A cell holds a formula this module will not evaluate. Refuse; never assume nil."""


_TOKEN_RE = re.compile(r"""
    (?P<ref>\$?[A-Za-z]{1,3}\$?\d+)
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<sum>SUM\b)
  | (?P<op>[-+*/(),:])
  | (?P<ws>\s+)
""", re.VERBOSE | re.IGNORECASE)

_REF_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def col_letters(index: int) -> str:
    out, n = "", index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def coord(r: int, c: int) -> str:
    return f"{col_letters(c)}{r + 1}"


class _Token:
    __slots__ = ("kind", "text", "pos")

    def __init__(self, kind, text, pos):
        self.kind, self.text, self.pos = kind, text, pos


def _tokenize(expr: str, origin: str) -> list[_Token]:
    out, pos = [], 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise FormulaProblem(
                f"{origin}: cannot read formula {expr!r} at offset {pos} "
                f"({expr[pos:pos + 12]!r}); refusing to treat it as nil")
        pos = m.end()
        if m.lastgroup != "ws":
            out.append(_Token(m.lastgroup, m.group(), m.start()))
    return out


class _Parser:
    """Precedence-climbing parser that evaluates as it parses. No eval() anywhere."""

    def __init__(self, expr: str, resolver: "SheetResolver", origin: str):
        self.expr = expr
        self.origin = origin
        self.resolver = resolver
        self.toks = _tokenize(expr, origin)
        self.i = 0

    def _refuse(self, why: str) -> FormulaProblem:
        return FormulaProblem(f"{self.origin}: {why} in formula {self.expr!r}; "
                              "refusing to treat it as nil")

    def _peek(self) -> _Token | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _take_op(self, text: str) -> None:
        t = self._peek()
        if t is None or t.kind != "op" or t.text != text:
            got = f"{t.text!r} at offset {t.pos}" if t else "end of formula"
            raise self._refuse(f"expected {text!r}, got {got}")
        self.i += 1

    def parse(self) -> float:
        v = self._expr()
        t = self._peek()
        if t is not None:
            raise self._refuse(f"unexpected {t.text!r} at offset {t.pos}")
        return v

    def _expr(self) -> float:
        v = self._term()
        while (t := self._peek()) and t.kind == "op" and t.text in "+-":
            self.i += 1
            rhs = self._term()
            v = v + rhs if t.text == "+" else v - rhs
        return v

    def _term(self) -> float:
        v = self._factor()
        while (t := self._peek()) and t.kind == "op" and t.text in "*/":
            self.i += 1
            rhs = self._factor()
            if t.text == "/":
                if rhs == 0:
                    raise self._refuse("division by zero (Excel would show #DIV/0!)")
                v = v / rhs
            else:
                v = v * rhs
        return v

    def _factor(self) -> float:
        t = self._peek()
        if t and t.kind == "op" and t.text in "+-":
            self.i += 1
            v = self._factor()
            return v if t.text == "+" else -v
        return self._primary()

    def _primary(self) -> float:
        t = self._peek()
        if t is None:
            raise self._refuse("formula ends where a value was expected")
        if t.kind == "num":
            self.i += 1
            return float(t.text)
        if t.kind == "ref":
            self.i += 1
            return self._ref_value(t.text)
        if t.kind == "sum":
            self.i += 1
            self._take_op("(")
            total = self._sum_args()
            self._take_op(")")
            return total
        if t.kind == "op" and t.text == "(":
            self.i += 1
            v = self._expr()
            self._take_op(")")
            return v
        raise self._refuse(f"unexpected {t.text!r} at offset {t.pos}")

    def _ref_value(self, ref_text: str) -> float:
        m = _REF_RE.match(ref_text)
        v = self.resolver.cell(m.group(1), m.group(2))
        if v is None:
            return 0.0          # a blank cell in arithmetic is 0 -- Excel semantics
        if isinstance(v, bool):
            return float(v)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            raise self._refuse(
                f"reference {ref_text.upper()} holds text {v!r} (Excel would show #VALUE!)")
        raise self._refuse(
            f"reference {ref_text.upper()} holds a {type(v).__name__} value {v!r}, "
            "which this grammar does not do arithmetic on")

    def _sum_args(self) -> float:
        """(REF | REF:REF) (',' (REF | REF:REF))*  -- Xero writes a section subtotal as
        `SUM(B8:B12)` but a report's grand total as a comma list of the subtotal cells
        (79 of them on an aged payables export), because the rows between are individual
        invoices that must not be counted twice. Both forms are one operation."""
        total = 0.0
        while True:
            t = self._peek()
            if t is None or t.kind != "ref":
                got = f"{t.text!r}" if t else "end of formula"
                raise self._refuse(f"SUM argument is {got}, not a cell or range")
            self.i += 1
            nxt = self._peek()
            if nxt and nxt.kind == "op" and nxt.text == ":":
                self.i += 1
                end = self._peek()
                if end is None or end.kind != "ref":
                    raise self._refuse("SUM range is missing its end cell")
                self.i += 1
                total += self._range_sum(t.text, end.text)
            else:
                m = _REF_RE.match(t.text)
                v = self.resolver.cell(m.group(1), m.group(2))
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    total += float(v)
            t = self._peek()
            if t and t.kind == "op" and t.text == ",":
                self.i += 1
                continue
            return total

    def _range_sum(self, start: str, end: str) -> float:
        m0, m1 = _REF_RE.match(start), _REF_RE.match(end)
        c0, r0 = col_index(m0.group(1)), int(m0.group(2))
        c1, r1 = col_index(m1.group(1)), int(m1.group(2))
        # A column range is a section subtotal down a statement; a ROW range is an aged
        # report's Total column summing its band columns across. Both are ordinary. A
        # rectangle spanning both is not, and would double-count anything a subtotal in
        # it already carries -- so that one refuses.
        if c0 != c1 and r0 != r1:
            raise self._refuse(
                f"SUM over {start.upper()}:{end.upper()} spans both rows and columns; "
                "a subtotal that covers a block risks counting a subtotal inside it twice")
        total = 0.0
        for r in range(min(r0, r1) - 1, max(r0, r1)):
            for c in range(min(c0, c1), max(c0, c1) + 1):
                v = self.resolver.value_at(r, c)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    total += float(v)
        return total


class SheetResolver:
    """The raw grid plus a resolver, so a formula can reach any cell including another
    formula. Depth is tracked rather than assumed acyclic: a circular reference in a
    client file must surface as a refusal, not a recursion crash."""

    def __init__(self, grid: list[list], sheet_name: str = ""):
        self.grid = grid
        self.sheet_name = sheet_name
        self._cache: dict[tuple[int, int], object] = {}
        self._depth = 0

    def cell(self, letters: str, rownum: str | int):
        r, c = int(rownum) - 1, col_index(letters)
        if r < 0 or r >= len(self.grid):
            return 0.0
        if c < 0 or c >= len(self.grid[r]):
            return 0.0
        return self.value_at(r, c)

    def value_at(self, r: int, c: int):
        if r < 0 or r >= len(self.grid) or c < 0 or c >= len(self.grid[r]):
            return None
        key = (r, c)
        if key in self._cache:
            return self._cache[key]
        raw = self.grid[r][c]
        if isinstance(raw, str) and raw.startswith("="):
            origin = (f"{self.sheet_name}!{coord(r, c)}" if self.sheet_name
                      else coord(r, c))
            if self._depth > _MAX_DEPTH:
                raise FormulaProblem(
                    f"{origin}: formula nesting past {_MAX_DEPTH} levels: {raw!r} "
                    "-- refusing rather than unwinding a possible cycle")
            self._depth += 1
            try:
                out = _Parser(raw[1:], self, origin).parse()
            finally:
                self._depth -= 1
        elif isinstance(raw, bool):
            out = raw
        elif isinstance(raw, (int, float)):
            out = float(raw)
        else:
            out = raw           # None, text, date -- passed through untouched
        self._cache[key] = out
        return out


def is_formula(raw) -> bool:
    return isinstance(raw, str) and raw.startswith("=")


def to_json_value(v):
    """A resolved cell value as its honest JSON form. Empty is null, never "" -- the
    old engine's ""-padding protected ITS string parsers; null is the true value for a
    JSON-reading agent. Dates are ISO-8601 strings."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, datetime):
        if v.hour == v.minute == v.second == 0 and v.microsecond == 0:
            return v.date().isoformat()
        return v.isoformat()
    if isinstance(v, (date, time)):
        return v.isoformat()
    return str(v)

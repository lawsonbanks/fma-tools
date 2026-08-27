"""The three failure classes every fma tool maps onto, and their exit codes.

Exit codes are uniform across subcommands:
  0  pass
  1  REFUSE — the gate did its job (broken tie, unevaluable formula, failed render gate)
  2  cannot read input (missing/corrupt/cloud-only file, schema violation)
  3  environment missing (message carries the doctor fix line)
  4  internal bug — never a finding
Finer distinctions live in problem codes, not exit codes.
"""

from __future__ import annotations


class ToolError(Exception):
    exit_code = 4
    status = "error"

    def __init__(self, code: str, message: str, fix: str | None = None,
                 problems: list[dict] | None = None, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.fix = fix
        self.data = data
        if problems is not None:
            self.problems = problems
        else:
            p = {"code": code, "message": message}
            if fix:
                p["fix"] = fix
            self.problems = [p]


class Refusal(ToolError):
    """The answer is no: the condition this tool exists to catch is present."""
    exit_code = 1
    status = "refuse"


class InputProblem(ToolError):
    """The check could not even run: the input is missing, unreadable or malformed."""
    exit_code = 2


class EnvProblem(ToolError):
    """A dependency is missing. The message is the doctor fix line."""
    exit_code = 3

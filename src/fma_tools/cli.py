"""The fma entry point. One command, four subcommands, one output contract.

Every invocation prints a JSON envelope to stdout --
  {"tool", "version", "status": "ok"|"refuse"|"error", "data", "problems", "warnings"}
-- and a one-line human summary to stderr. Exit codes are uniform (errors.py): 0 pass,
1 refuse, 2 cannot read input, 3 environment missing, 4 internal bug.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .errors import EnvProblem, ToolError


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fma",
                                description="FMA delivery gates: read an export safely, "
                                            "run the ties, render the artifact.")
    p.add_argument("--version", action="version", version=f"fma-tools {__version__}")
    sub = p.add_subparsers(dest="tool", required=True)

    from .read_ledger import main as read_ledger_main
    sp = sub.add_parser("read-ledger",
                        help="read a Xero/Excel export safely; refuse what cannot be proven")
    read_ledger_main.add_arguments(sp)
    sp.set_defaults(run=read_ledger_main.run, summary=read_ledger_main.summary)

    from . import doctor
    sp = sub.add_parser("doctor", help="say exactly what is missing and how to install it")
    doctor.add_arguments(sp)
    sp.set_defaults(run=doctor.run, summary=doctor.summary)

    # imported unconditionally: a guard that silently deletes a subcommand turns a
    # broken install into an argparse "invalid choice" with no fix line
    from .reconcile import main as reconcile_main
    sp = sub.add_parser("reconcile",
                        help="run a pack's arithmetic ties; refuse if one breaks")
    reconcile_main.add_arguments(sp)
    sp.set_defaults(run=reconcile_main.run, summary=reconcile_main.summary)

    from .render import main as render_main
    sp = sub.add_parser("render", help="turn finished HTML into PDF, DOCX or PPTX")
    render_main.add_arguments(sp)
    sp.set_defaults(run=render_main.run, summary=render_main.summary)

    return p


def main(argv=None) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as e:
        if e.code in (0, None):          # --help / --version: human output is the point
            raise
        # a usage error must still honour the contract: valid JSON on stdout
        envelope = {"tool": "fma", "version": __version__, "status": "error",
                    "data": {}, "warnings": [],
                    "problems": [{"code": "USAGE",
                                  "message": "invalid arguments -- see the usage "
                                             "text on stderr, or `fma --help`"}]}
        print(json.dumps(envelope, indent=1))
        return 2
    envelope = {"tool": args.tool, "version": __version__, "status": "ok",
                "data": {}, "problems": [], "warnings": []}
    exit_code = 0
    try:
        data, warnings = args.run(args)
        envelope["data"] = data
        envelope["warnings"] = warnings
        human = args.summary(data)
    except ToolError as e:
        envelope["status"] = e.status
        envelope["problems"] = e.problems
        if e.data is not None:
            envelope["data"] = e.data
        exit_code = e.exit_code
        human = "; ".join(p["message"] for p in e.problems)
    except ModuleNotFoundError as e:
        err = EnvProblem("ENV_MISSING", f"missing dependency {e.name!r} -- run `fma doctor` "
                                        "and follow its fix lines")
        envelope["status"] = err.status
        envelope["problems"] = err.problems
        exit_code = err.exit_code
        human = err.problems[0]["message"]
    except Exception as e:  # a bug, never a finding
        envelope["status"] = "error"
        envelope["problems"] = [{"code": "INTERNAL",
                                 "message": f"{type(e).__name__}: {e}"}]
        exit_code = 4
        human = f"internal error: {type(e).__name__}: {e}"

    print(json.dumps(envelope, indent=1))
    print(f"[{envelope['tool']}] {human}", file=sys.stderr)
    for w in envelope["warnings"]:
        print(f"[{envelope['tool']}] warning: {w}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

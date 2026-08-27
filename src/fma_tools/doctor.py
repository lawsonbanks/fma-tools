"""fma doctor -- say exactly what is missing and how to install it.

One line per check on stderr; the JSON twin rides the standard envelope on stdout.
Every FAIL carries exactly one copy-pasteable fix command. `--fix` runs the fixes that
are reachable from inside this venv (today: installing Playwright's chromium); anything
else is named with the exact external command. Exit 0 healthy, 3 problems found.

Wayne never sees this: his agent runs doctor, runs the fix lines, re-runs doctor until
it exits 0, and relays "tools healthy".
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .errors import EnvProblem

_REINSTALL = ("uv tool install --force --python 3.12 "
              "git+https://github.com/lawsonbanks/fma-tools")

_IMPORTS = [
    # (module, pip name, what depends on it). `pymupdf` not `fitz`: the fitz alias
    # prints a deprecation warning to STDOUT, which corrupts the JSON channel.
    ("openpyxl", "openpyxl", "read-ledger"),
    ("jsonschema", "jsonschema", "reconcile input validation"),
    ("playwright", "playwright", "render's browser"),
    ("docx", "python-docx", "render --docx"),
    ("pptx", "python-pptx", "render --pptx"),
    ("pymupdf", "pymupdf", "render's PDF read-back gates"),
]


def add_arguments(p) -> None:
    p.add_argument("--fix", action="store_true",
                   help="run the fixes reachable from this venv (chromium install)")
    p.add_argument("--dir", help="also check this absolute path is a writable directory")
    p.add_argument("--deep", action="store_true",
                   help="end-to-end proof: launch chromium, render a one-page smoke "
                        "PDF, read it back (~3s)")


def _check_self() -> str:
    exe = shutil.which("fma")
    return f"fma-tools at {exe or sys.executable} (module {Path(__file__).parent})"


def _check_python() -> str:
    if sys.version_info < (3, 12):
        raise RuntimeError(f"Python {sys.version.split()[0]} < 3.12")
    return f"Python {sys.version.split()[0]}"


def _check_import(mod: str, pip_name: str) -> str:
    if importlib.util.find_spec(mod) is None:
        raise RuntimeError(f"{pip_name} is not importable")
    importlib.import_module(mod)
    try:
        ver = importlib.metadata.version(pip_name)
    except importlib.metadata.PackageNotFoundError:
        ver = "?"
    return f"{pip_name} {ver}"


def _chromium_path() -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return p.chromium.executable_path


def _check_chromium() -> str:
    path = _chromium_path()
    if not path or not os.path.exists(path):
        raise RuntimeError("Chromium browser not installed for Playwright")
    return f"Chromium at {path}"


def _check_dir(d: str) -> str:
    p = Path(d).expanduser()
    if not p.is_dir():
        raise RuntimeError(f"{p} is not a directory -- if it lives in OneDrive it may "
                           "be cloud-only; open it in Finder first")
    try:
        probe = p / ".fma_doctor_probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise RuntimeError(f"{p} is not writable ({e})")
    return f"{p} exists and is writable"


def _check_deep() -> str:
    import pymupdf
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "smoke.html"
        pdf = Path(td) / "smoke.pdf"
        html.write_text("<!doctype html><style>@page{size:A4;margin:0}</style>"
                        "<p>fma doctor smoke render</p>")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(html.as_uri())
            page.pdf(path=str(pdf), prefer_css_page_size=True)
            browser.close()
        doc = pymupdf.open(str(pdf))
        n, text = doc.page_count, doc[0].get_text()
        doc.close()
        if n != 1 or "smoke render" not in text:
            raise RuntimeError(f"smoke PDF wrong ({n} pages, text {text!r})")
    return "chromium launched, PDF rendered and read back"


def _fix_chromium() -> str:
    r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"playwright install chromium failed: {r.stderr.strip()[-400:]}")
    return "installed chromium"


def run(args) -> tuple[dict, list[str]]:
    checks = []

    def do(name: str, probe, fix: str | None, fixer=None):
        try:
            detail = probe()
            checks.append({"check": name, "status": "ok", "detail": detail, "fix": None})
        except Exception as e:
            if args.fix and fixer is not None:
                try:
                    fixer()
                    detail = probe()
                    checks.append({"check": name, "status": "ok",
                                   "detail": f"{detail} (fixed this run)", "fix": None})
                    return
                except Exception as e2:
                    e = e2
            checks.append({"check": name, "status": "FAIL", "detail": str(e), "fix": fix})

    do("fma install", _check_self, _REINSTALL)
    do("python >= 3.12", _check_python, _REINSTALL)
    for mod, pip_name, why in _IMPORTS:
        do(f"{pip_name} ({why})", lambda m=mod, n=pip_name: _check_import(m, n), _REINSTALL)
    if importlib.util.find_spec("playwright") is not None:
        do("playwright chromium", _check_chromium,
           f"{sys.executable} -m playwright install chromium", _fix_chromium)
    if args.dir:
        do(f"directory {args.dir}", lambda: _check_dir(args.dir),
           "open the folder in Finder so OneDrive hydrates it, or pick a real path")
    if args.deep:
        do("deep: smoke render + read-back", _check_deep, _REINSTALL)

    failed = [c for c in checks if c["status"] == "FAIL"]
    data = {"checks": checks, "problems_found": len(failed)}

    print(f"fma doctor -- {len(checks)} checks", file=sys.stderr)
    for c in checks:
        mark = "ok  " if c["status"] == "ok" else "FAIL"
        print(f"  {mark}  {c['check']}: {c['detail']}", file=sys.stderr)
        if c["status"] == "FAIL" and c["fix"]:
            print(f"        fix: {c['fix']}", file=sys.stderr)

    if failed:
        raise EnvProblem(
            "ENV_MISSING", f"{len(failed)} problem(s) found", data=data,
            problems=[{"code": "ENV_MISSING",
                       "message": f"{c['check']}: {c['detail']}",
                       **({"fix": c["fix"]} if c["fix"] else {})} for c in failed])
    return data, []


def summary(data: dict) -> str:
    n = len(data.get("checks", []))
    return f"doctor: {n} checks, all ok"

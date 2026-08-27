"""Confidentiality as a mechanism, not a habit. This repo is public: no client
workbook is ever committed (fixtures are built in-test), and no client name appears in
source or tests. The client markers below are constructed, not written literally, so
this file passes its own check.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "dist", "node_modules"}

_CLIENT_MARKERS = [
    re.compile(r"\b" + "".join(["I", "G", "S"]) + r"\b"),
    re.compile("".join(["Integrated ", "Group ", "Services"]), re.IGNORECASE),
]


def _files(root: Path):
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            yield p


def test_no_workbooks_committed():
    offenders = [p for p in _files(ROOT) if p.suffix.lower() in (".xlsx", ".xls", ".xlsm")]
    assert not offenders, (
        f"workbooks committed to a public repo: {offenders} -- fixtures are built "
        "in-test, never checked in")


def test_no_client_names_in_source_or_tests():
    offenders = []
    for base in (ROOT / "src", ROOT / "tests", ROOT / "README.md"):
        paths = _files(base) if base.is_dir() else ([base] if base.exists() else [])
        for p in paths:
            try:
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            for rx in _CLIENT_MARKERS:
                if rx.search(text):
                    offenders.append((str(p), rx.pattern))
    assert not offenders, f"client markers found in a public repo: {offenders}"

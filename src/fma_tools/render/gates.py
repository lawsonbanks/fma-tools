"""Read the finished artifact back and gate on the bytes that will ship.

Never on a preview, never on a re-render, never on the in-memory object: what ships
is the file. PyMuPDF does the PDF read-back (a pure wheel -- no poppler binaries for
doctor to chase across machines).
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

_DASHES = {"—": "an em dash (U+2014)", "–": "an en dash (U+2013)",
           "―": "a horizontal bar (U+2015)"}

# covers -3,000, -$3,000 and the letter-prefixed currency forms -A$3,000 / -US$3,000
_BARE_MINUS = re.compile(r"(?<![\w)])-\s?(?:A\$|NZ\$|US\$)?[\d$€£][\d,.]*")


def pdf_pages(path: Path) -> list[dict]:
    """[{width_pt, height_pt, text}] per page, off the rendered bytes."""
    import pymupdf
    doc = pymupdf.open(str(path))
    out = [{"width_pt": p.rect.width, "height_pt": p.rect.height,
            "text": p.get_text()} for p in doc]
    doc.close()
    return out


def gate_pdf(pages: list[dict], width_pt: float, height_pt: float,
             expect_pages: int | None, allow_textless: set[int]) -> list[dict]:
    gates = []
    bad = [f"page {i}: {p['width_pt']:.1f}x{p['height_pt']:.1f}pt"
           for i, p in enumerate(pages, 1)
           if abs(p["width_pt"] - width_pt) > 1 or abs(p["height_pt"] - height_pt) > 1]
    gates.append({"name": "page-size", "status": "fail" if bad else "pass",
                  "detail": "; ".join(bad) if bad else
                            f"{width_pt:.1f}x{height_pt:.1f}pt on all {len(pages)} page(s)"})
    textless = [i for i, p in enumerate(pages, 1)
                if not p["text"].strip() and i not in allow_textless]
    gates.append({"name": "pdf-text-layer",
                  "status": "fail" if textless else "pass",
                  "detail": (f"pages {textless} yield no extracted text -- pictures "
                             "of pages, not pages" if textless else
                             "every page carries a text layer")})
    if expect_pages is not None:
        ok = len(pages) == expect_pages
        gates.append({"name": "page-count", "status": "pass" if ok else "fail",
                      "detail": f"{len(pages)} page(s), expected {expect_pages}"})
    return gates


def lint_text(texts: list[str], where: str, allow_dashes: bool) -> dict:
    """FMA house style over the shipped text: no em/en dash, no bare minus as a
    negative -- parentheses for negatives, U+2212 for a nil."""
    problems = []
    joined = "\n".join(texts)
    for ch, name in _DASHES.items():
        if ch in joined:
            n = joined.count(ch)
            problems.append(f"{n} {name} in the {where}")
    minus = _BARE_MINUS.findall(joined)
    if minus:
        problems.append(f"bare minus as a negative in the {where}: "
                        f"{minus[:5]} -- parentheses bound a negative, U+2212 marks a nil")
    if allow_dashes:
        return {"name": f"dash-lint ({where})", "status": "pass",
                "detail": "skipped by --allow-dashes"
                          + (f"; would have failed: {problems}" if problems else "")}
    return {"name": f"dash-lint ({where})",
            "status": "fail" if problems else "pass",
            "detail": "; ".join(problems) if problems else "clean"}


def gate_pptx_runs(saved: list[list[str]], expected: list[str]) -> list[dict]:
    """The Wayne gate, no opt-out: every slide carries real text runs, and the run
    multiset equals what the browser painted. An all-picture deck fails here --
    the artifact that shipped twice can never ship again."""
    gates = []
    empty = [i for i, runs in enumerate(saved, 1) if not runs]
    gates.append({"name": "pptx-editable-runs",
                  "status": "fail" if empty else "pass",
                  "detail": (f"slides {empty} carry no editable text runs: pictures "
                             "of slides, the exact fault this gate exists to stop"
                             if empty else
                             f"every slide editable; min "
                             f"{min((len(r) for r in saved), default=0)} run(s)/slide")})
    got = [r for runs in saved for r in runs]
    want_c, got_c = Counter(_norm(t) for t in expected), Counter(_norm(t) for t in got)
    missing = list((want_c - got_c).elements())
    extra = list((got_c - want_c).elements())
    gates.append({"name": "pptx-text-parity",
                  "status": "fail" if (missing or extra) else "pass",
                  "detail": (f"missing from the pptx: {missing[:6]}; not on the page: "
                             f"{extra[:6]}" if (missing or extra) else
                             f"{len(got)} runs match the browser exactly")})
    return gates


def gate_docx(saved_texts: list[str], saved_pictures: int,
              expected_texts: list[str], expected_pictures: int) -> list[dict]:
    gates = []
    want_c = Counter(_norm(t) for t in expected_texts)
    got_c = Counter(_norm(t) for t in saved_texts)
    missing = list((want_c - got_c).elements())
    extra = list((got_c - want_c).elements())
    gates.append({"name": "docx-text-parity",
                  "status": "fail" if (missing or extra) else "pass",
                  "detail": (f"missing from the docx: {missing[:6]}; unexpected: "
                             f"{extra[:6]}" if (missing or extra) else
                             f"{len(saved_texts)} blocks match the source exactly")})
    ok = saved_pictures == expected_pictures
    gates.append({"name": "docx-graphics-parity",
                  "status": "pass" if ok else "fail",
                  "detail": (f"{saved_pictures} picture(s) as expected" if ok else
                             f"{saved_pictures} picture(s) in the docx, "
                             f"{expected_pictures} chart/image node(s) in the HTML "
                             "-- the retype-loses-the-graphics fault, mechanised")})
    return gates


def gate_network(blocked: list[str], allow: bool) -> dict:
    if not blocked:
        return {"name": "network-sealed", "status": "pass",
                "detail": "no external requests"}
    detail = (f"{len(blocked)} external request(s) aborted: "
              + "; ".join(sorted(set(blocked))[:5])
              + " -- embed assets as data: URIs so the document renders identically "
                "on every machine")
    if allow:
        return {"name": "network-sealed", "status": "pass",
                "detail": "DOWNGRADED by --unsafe-allow-network; " + detail}
    return {"name": "network-sealed", "status": "fail", "detail": detail}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

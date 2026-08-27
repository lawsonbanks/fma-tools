"""Classify the input HTML and read its declared geometry. Static, before any browser.

Two kinds, and everything downstream keys off the split:
  DOCUMENT -- no `section.slide`; must declare `@page { size: ... }`; outputs pdf/docx.
  DECK     -- one or more `<section class="slide">` at 1280x720 CSS px; outputs pdf/pptx.

A document with no `@page` size refuses outright: Chrome defaults to US Letter, and an
Australian advisory once nearly put board papers on American paper because a build
emitted no @page rule and nothing caught it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import Refusal

DECK_W, DECK_H = 1280, 720            # CSS px
PT_PER_PX = 0.75

_PAGE_KEYWORDS = {
    "a4": (595.28, 841.89), "a3": (841.89, 1190.55),
    "a5": (419.53, 595.28), "letter": (612.0, 792.0), "legal": (612.0, 1008.0),
}
_UNIT_PT = {"pt": 1.0, "px": 0.75, "in": 72.0, "mm": 72.0 / 25.4, "cm": 72.0 / 2.54}


@dataclass
class Geometry:
    kind: str                 # "document" | "deck"
    width_pt: float
    height_pt: float
    css_size: str             # the CSS the browser gets, e.g. "A4" or "1280px 720px"


def classify(html: str) -> str:
    return "deck" if re.search(r"<section[^>]*\bclass=\"[^\"]*\bslide\b", html) else "document"


def _parse_page_size(spec: str) -> tuple[float, float] | None:
    parts = spec.strip().lower().split()
    if not parts:
        return None
    if parts[0] in _PAGE_KEYWORDS:
        w, h = _PAGE_KEYWORDS[parts[0]]
        if len(parts) > 1 and parts[1] == "landscape":
            w, h = h, w
        return (w, h)
    if len(parts) == 2:
        dims = []
        for p in parts:
            m = re.fullmatch(r"([\d.]+)(pt|px|in|mm|cm)", p)
            if not m:
                return None
            dims.append(float(m.group(1)) * _UNIT_PT[m.group(2)])
        return (dims[0], dims[1])
    return None


def geometry(html: str) -> Geometry:
    kind = classify(html)
    if kind == "deck":
        return Geometry("deck", DECK_W * PT_PER_PX, DECK_H * PT_PER_PX,
                        f"{DECK_W}px {DECK_H}px")
    m = re.search(r"@page[^{}]*\{([^{}]*)\}", html)
    size = None
    if m:
        sm = re.search(r"size\s*:\s*([^;}]+)", m.group(1))
        if sm:
            size = _parse_page_size(sm.group(1))
    if size is None:
        raise Refusal(
            "GEOMETRY_UNDECLARED",
            "the document declares no @page size. Chrome then defaults to US Letter "
            "-- board papers on American paper, and nothing downstream catches it. "
            "Add `@page { size: A4; margin: 18mm }` (or the size this document "
            "actually is) to the stylesheet.")
    sm = re.search(r"size\s*:\s*([^;}]+)", m.group(1))
    return Geometry("document", size[0], size[1], sm.group(1).strip())


def author_warnings(html: str) -> list[str]:
    """Print-CSS knowledge as author-facing warnings, never gates."""
    out = []
    if re.search(r"@media\s+screen", html):
        out.append("the HTML carries an @media screen block; Chrome's print pass "
                   "renders @media print and drops screen styles -- check nothing "
                   "load-bearing lives there")
    if "box-shadow" in html:
        out.append("box-shadow rasterises into square tiles in a print PDF; prefer "
                   "borders for print output")
    for m in re.finditer(r"(?:src|href)\s*=\s*[\"'](https?://[^\"']+)", html):
        out.append(f"external reference {m.group(1)[:80]} -- the render browser is "
                   "network-sealed, so this will fail the network gate; embed it as "
                   "a data: URI")
    return out

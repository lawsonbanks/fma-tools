"""fma render -- turn finished HTML into PDF, DOCX or PPTX, gate on the shipped
bytes, and leave NOTHING behind on a failure.

Run discipline (ported whole from the engine this replaces): unlink every requested
output before rendering, so a file existing proves this run produced it; render; read
each artifact back; gate; and on ANY failure unlink every artifact the run created. A
refused pptx never leaves a passing pdf on disk -- a half-shipped pack is exactly what
a rushed operator sends.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from ..errors import InputProblem, Refusal
from . import contract, docx_emit, gates as G, pptx_emit
from .browser import Session


def add_arguments(p) -> None:
    p.add_argument("file", help="absolute path to the finished HTML")
    p.add_argument("--pdf", help="write the PDF here")
    p.add_argument("--docx", help="write the Word file here (documents only)")
    p.add_argument("--pptx", help="write the PowerPoint here (decks only)")
    p.add_argument("--pptx-font", default="Arial",
                   help=f"deck face; one of {', '.join(pptx_emit.SAFE_FONTS)} "
                        "(must be installed on the machine that OPENS the file)")
    p.add_argument("--expect-pages", type=int,
                   help="assert the PDF page count (a deck asserts its slide count "
                        "automatically)")
    p.add_argument("--allow-dashes", action="store_true",
                   help="skip the FMA dash lint (em/en dash, bare minus)")
    p.add_argument("--allow-textless-pdf-pages", default="",
                   help="comma-separated page numbers allowed to carry no text "
                        "(a deliberate full-bleed divider)")
    p.add_argument("--unsafe-allow-network", action="store_true",
                   help="downgrade the network-seal gate to a warning (debugging only)")
    p.add_argument("--emit-text", action="store_true",
                   help="include the per-page extracted PDF text in the JSON, for "
                        "the adversarial figure check")


def run(args) -> tuple[dict, list[str]]:
    html_path = Path(args.file).expanduser().resolve()
    if not html_path.exists():
        raise InputProblem("CANNOT_OPEN", f"no file at {html_path}")
    html = html_path.read_text(encoding="utf-8", errors="replace")

    outputs = {k: Path(v).expanduser().resolve()
               for k, v in (("pdf", args.pdf), ("docx", args.docx),
                            ("pptx", args.pptx)) if v}
    if not outputs:
        raise InputProblem("CONTRACT_INVALID",
                           "nothing to produce -- pass at least one of --pdf, "
                           "--docx, --pptx")
    geo = contract.geometry(html)
    if geo.kind == "deck" and "docx" in outputs:
        raise Refusal("VOCAB_REFUSED", "this HTML is a deck (section.slide); a deck "
                      "renders to --pdf and --pptx, not Word")
    if geo.kind == "document" and "pptx" in outputs:
        raise Refusal("VOCAB_REFUSED", "this HTML is a document (no section.slide); "
                      "it renders to --pdf and --docx, not PowerPoint")
    if "pptx" in outputs and args.pptx_font not in pptx_emit.SAFE_FONTS:
        raise InputProblem("CONTRACT_INVALID",
                           f"--pptx-font {args.pptx_font!r} is not in the safe set "
                           f"{pptx_emit.SAFE_FONTS} -- PowerPoint substitutes an "
                           "uninstalled face and every line re-wraps")

    warnings = contract.author_warnings(html)
    allow_textless = {int(x) for x in args.allow_textless_pdf_pages.split(",") if x}
    expect_pages = args.expect_pages
    if geo.kind == "deck" and expect_pages is None:
        expect_pages = len(re.findall(
            r"<section[^>]*\bclass=\"[^\"]*\bslide\b", html))

    # freshness: a file existing must prove this run produced it
    for p in outputs.values():
        p.unlink(missing_ok=True)

    needs_browser = ("pdf" in outputs or "pptx" in outputs
                     or ("docx" in outputs and docx_emit.count_chart_nodes(html) > 0))
    session = None
    workdir = Path(tempfile.mkdtemp(prefix="fma_render_"))
    written: list[Path] = []
    gates: list[dict] = []
    data: dict = {"input": str(html_path), "kind": geo.kind,
                  "geometry": {"width_pt": round(geo.width_pt, 2),
                               "height_pt": round(geo.height_pt, 2)},
                  "produced": []}
    try:
        try:
            if needs_browser:
                session = Session()
                session.load(html_path)
                if geo.kind == "deck":
                    session.inject_deck_print_css()

            pdf_pages_data = None
            if "pdf" in outputs:
                session.pdf(outputs["pdf"], geo.css_size, geo.width_pt, geo.height_pt)
                written.append(outputs["pdf"])
                pdf_pages_data = G.pdf_pages(outputs["pdf"])
                data["produced"].append({"format": "pdf", "path": str(outputs["pdf"]),
                                         "pages": len(pdf_pages_data),
                                         "bytes": outputs["pdf"].stat().st_size})

            docx_ledger = None
            if "docx" in outputs:
                shots = []
                if docx_emit.count_chart_nodes(html) > 0:
                    # top-level chart nodes only: a chart nested inside another
                    # marked node is part of ITS pixels, and numbering it would
                    # shift every later screenshot off its slot in the parser
                    session.page.evaluate(
                        "() => { const SEL = 'svg, canvas, [data-render=\"image\"]';"
                        " [...document.querySelectorAll(SEL)]"
                        ".filter(el => !el.parentElement.closest(SEL))"
                        ".forEach((el, i) => "
                        "el.setAttribute('data-fma-img', String(i))); }")
                    shots = session.screenshot_marked(workdir)
                docx_ledger = docx_emit.emit(html, geo, shots, outputs["docx"])
                written.append(outputs["docx"])
                data["produced"].append({"format": "docx",
                                         "path": str(outputs["docx"]),
                                         "pictures": docx_ledger["expected_pictures"],
                                         "bytes": outputs["docx"].stat().st_size})

            slides_m = None
            if "pptx" in outputs:
                # measured AFTER the PDF: the font override re-lays the page out
                # under the face PowerPoint will really use
                slides_m = pptx_emit.measure(session, args.pptx_font)
                shots = session.screenshot_marked(workdir)
                n = pptx_emit.build(slides_m, shots, outputs["pptx"], args.pptx_font)
                written.append(outputs["pptx"])
                data["produced"].append({"format": "pptx",
                                         "path": str(outputs["pptx"]), "slides": n,
                                         "font": args.pptx_font,
                                         "bytes": outputs["pptx"].stat().st_size})

            # ---- gates, on the shipped bytes -------------------------------
            if session is not None:
                gates.append(G.gate_network(session.blocked_urls,
                                            args.unsafe_allow_network))
            if pdf_pages_data is not None:
                gates += G.gate_pdf(pdf_pages_data, geo.width_pt, geo.height_pt,
                                    expect_pages, allow_textless)
                gates.append(G.lint_text([p["text"] for p in pdf_pages_data],
                                         "PDF text layer", args.allow_dashes))
                if args.emit_text:
                    data["text"] = [p["text"] for p in pdf_pages_data]
            if "pptx" in outputs:
                oob = [msg for sm in slides_m for msg in sm.get("out_of_bounds", [])]
                gates.append({"name": "slide-bounds",
                              "status": "fail" if oob else "pass",
                              "detail": "; ".join(oob[:6]) if oob else
                                        "nothing paints outside its slide box"})
                saved = pptx_emit.saved_runs(outputs["pptx"])
                gates += G.gate_pptx_runs(saved, pptx_emit.expected_runs(slides_m))
                gates.append(G.lint_text([r for runs in saved for r in runs],
                                         "PowerPoint text runs", args.allow_dashes))
                min_runs = min((len(r) for r in saved), default=0)
                for p in data["produced"]:
                    if p["format"] == "pptx":
                        p["text_runs"] = sum(len(r) for r in saved)
                        p["min_runs_per_slide"] = min_runs
            if docx_ledger is not None:
                saved_texts, saved_pics = docx_emit.saved_texts_and_pictures(
                    outputs["docx"])
                gates += G.gate_docx(saved_texts, saved_pics,
                                     docx_ledger["expected_texts"],
                                     docx_ledger["expected_pictures"])
                gates.append(G.lint_text(saved_texts, "Word text",
                                         args.allow_dashes))

            data["gates"] = gates
            failed = [g for g in gates if g["status"] == "fail"]
            if failed:
                raise Refusal(
                    "GATE_FAILED", f"{len(failed)} gate(s) failed", data=data,
                    problems=[{"code": "GATE_FAILED",
                               "message": f"{g['name']}: {g['detail']}"}
                              for g in failed])
        except BaseException as e:
            removed = []
            for p in written:
                p.unlink(missing_ok=True)
                removed.append(str(p))
            if isinstance(e, Refusal) and e.data is not None:
                e.data["removed"] = removed
                e.data.setdefault("gates", gates)
            raise
    finally:
        if session is not None:
            session.close()
        shutil.rmtree(workdir, ignore_errors=True)
    return data, warnings


def summary(data: dict) -> str:
    parts = [f"{p['format']} ({p.get('pages') or p.get('slides') or ''}p)".replace(" ()p", "")
             for p in data.get("produced", [])]
    n_gates = len(data.get("gates", []))
    return (f"render {data.get('kind')}: " + ", ".join(parts)
            + f" -- {n_gates} gates green")

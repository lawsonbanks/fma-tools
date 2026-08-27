"""render: both kinds end to end, every gate proven RED, cleanup proven."""

import json

import pytest

DOC_CSS = "@page { size: A4; margin: 18mm } body { font-family: Georgia, serif }"

DOC_MIN = f"""<!doctype html><html><head><style>{DOC_CSS}</style></head><body>
<h1>Fixture Finance Pack</h1>
<p>The month closed with cash of $1,234 and receivables of ($567).</p>
<h2 class="annex" data-badge="Annex A">Profit and loss</h2>
<table><colgroup><col style="width:60%"><col style="width:40%"></colgroup>
<thead><tr><th>Line</th><th class="num">Amount</th></tr></thead>
<tbody><tr><td>Revenue</td><td class="num">20,000</td></tr>
<tr class="total"><td>Total</td><td class="num">20,000</td></tr></tbody></table>
<div class="callout" data-label="DECISIONS FOR YOU"><p>Rule on the tax basis.</p></div>
<div class="kpi-grid"><div class="kpi"><div class="k">DSO</div><div class="v">41</div>
<div class="u">days</div></div></div>
<ul><li>One thing</li><li>Another thing</li></ul>
<svg width="200" height="60" xmlns="http://www.w3.org/2000/svg">
<rect x="0" y="10" width="150" height="30" fill="#0D2B4E"/></svg>
</body></html>"""


def _deck(slides_html: str, extra_css: str = "") -> str:
    return f"""<!doctype html><html><head><style>
    body {{ margin: 0; font-family: Arial, sans-serif }}
    section.slide {{ width: 1280px; height: 720px; position: relative;
                     overflow: hidden; box-sizing: border-box; padding: 40px }}
    {extra_css}</style></head><body>{slides_html}</body></html>"""


DECK_MIN = _deck("""
<section class="slide"><h1>68.2% of a $5.79m book is past due</h1>
<p>The headline figure is $1,988,929 in Current.</p>
<table><thead><tr><th>Band</th><th>Amount</th></tr></thead>
<tbody><tr><td>Current</td><td>1,988,929</td></tr></tbody></table></section>
<section class="slide"><h2>Cash position</h2>
<p>Closing bank $4,500 against opening $3,000.</p>
<svg width="300" height="80" xmlns="http://www.w3.org/2000/svg">
<rect x="0" y="20" width="200" height="40" fill="#333"/></svg></section>
""")


@pytest.fixture
def render(run_cli, tmp_path):
    def _render(html: str, *flags, name="in.html"):
        p = tmp_path / name
        p.write_text(html)
        argv = ["render", str(p)]
        for f in flags:
            if f == "--pdf":
                argv += ["--pdf", str(tmp_path / "out.pdf")]
            elif f == "--docx":
                argv += ["--docx", str(tmp_path / "out.docx")]
            elif f == "--pptx":
                argv += ["--pptx", str(tmp_path / "out.pptx")]
            else:
                argv.append(f)
        return run_cli(argv)
    return _render


# ── green paths ──────────────────────────────────────────────────────────────

def test_document_pdf_and_docx(render, tmp_path):
    code, env = render(DOC_MIN, "--pdf", "--docx")
    assert code == 0, env["problems"]
    formats = {p["format"] for p in env["data"]["produced"]}
    assert formats == {"pdf", "docx"}
    assert all(g["status"] == "pass" for g in env["data"]["gates"])
    by_name = {g["name"]: g for g in env["data"]["gates"]}
    assert "docx-text-parity" in by_name and "docx-graphics-parity" in by_name
    assert (tmp_path / "out.pdf").exists() and (tmp_path / "out.docx").exists()


def test_deck_pdf_and_pptx_headline_editable(render, tmp_path):
    code, env = render(DECK_MIN, "--pdf", "--pptx")
    assert code == 0, env["problems"]
    pptx = next(p for p in env["data"]["produced"] if p["format"] == "pptx")
    assert pptx["slides"] == 2
    assert pptx["min_runs_per_slide"] >= 1
    # the Wayne check, on the shipped bytes: the headline number is editable text
    from fma_tools.render.pptx_emit import saved_runs
    runs = [r for slide in saved_runs(tmp_path / "out.pptx") for r in slide]
    assert any("$5.79m" in r for r in runs)
    # and the PDF carries one page per slide
    pdf = next(p for p in env["data"]["produced"] if p["format"] == "pdf")
    assert pdf["pages"] == 2


def test_emit_text_returns_page_text(render):
    code, env = render(DOC_MIN, "--pdf", "--emit-text")
    assert code == 0
    assert any("Fixture Finance Pack" in t for t in env["data"]["text"])


# ── refusals, each RED ───────────────────────────────────────────────────────

def test_no_atpage_refuses_naming_the_letter_trap(render):
    code, env = render("<html><body><p>hi</p></body></html>", "--pdf")
    assert code == 1
    assert env["problems"][0]["code"] == "GEOMETRY_UNDECLARED"
    assert "Letter" in env["problems"][0]["message"]


def test_untaught_element_refuses_with_line_and_fix(render):
    html = DOC_MIN.replace("<ul>", "<aside class=\"timeline\">x</aside><ul>")
    code, env = render(html, "--docx")
    assert code == 1
    msg = env["problems"][0]["message"]
    assert env["problems"][0]["code"] == "VOCAB_REFUSED"
    assert "aside" in msg and "line" in msg and "taught blocks" in msg


def test_table_without_colgroup_refuses(render):
    html = DOC_MIN.replace("<colgroup><col style=\"width:60%\">"
                           "<col style=\"width:40%\"></colgroup>", "")
    code, env = render(html, "--docx")
    assert code == 1
    assert "colgroup" in env["problems"][0]["message"]


def test_kind_format_mismatch_refuses(render):
    code, env = render(DECK_MIN, "--docx")
    assert code == 1 and "deck" in env["problems"][0]["message"]
    code, env = render(DOC_MIN, "--pptx")
    assert code == 1 and "document" in env["problems"][0]["message"]


def test_unsafe_font_refuses(render):
    code, env = render(DECK_MIN, "--pptx", "--pptx-font", "Lato")
    assert code == 2
    assert "safe set" in env["problems"][0]["message"]


# ── gates, each RED, and the cleanup discipline ──────────────────────────────

def test_external_reference_fails_the_network_gate(render, tmp_path):
    html = DOC_MIN.replace("<p>", "<p><img src=\"https://example.com/x.png\">", 1)
    code, env = render(html, "--pdf")
    assert code == 1
    assert any("network-sealed" in p["message"] for p in env["problems"])
    # cleanup: the failed run leaves nothing behind
    assert not (tmp_path / "out.pdf").exists()
    assert str(tmp_path / "out.pdf") in env["data"]["removed"]

    code, env = render(html, "--pdf", "--unsafe-allow-network")
    assert code == 0
    assert any("DOWNGRADED" in g["detail"] for g in env["data"]["gates"])


def test_dash_lint_red_then_allowed(render):
    html = DOC_MIN.replace("cash of $1,234", "cash — the figure -3,000")
    code, env = render(html, "--pdf")
    assert code == 1
    assert any("em dash" in p["message"] for p in env["problems"])
    assert any("bare minus" in p["message"] for p in env["problems"])
    code, env = render(html, "--pdf", "--allow-dashes")
    assert code == 0


def test_image_only_slide_fails_the_wayne_gate_and_unlinks_the_pdf(render, tmp_path):
    deck = _deck("""<section class="slide">
      <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
           style="width:1200px;height:640px"></section>""")
    code, env = render(deck, "--pdf", "--pptx")
    assert code == 1
    assert any("no editable text runs" in p["message"] for p in env["problems"])
    # the 13/14 Aug artifact: a passing pdf must NOT survive beside the refused pptx
    assert not (tmp_path / "out.pdf").exists()
    assert not (tmp_path / "out.pptx").exists()
    assert str(tmp_path / "out.pdf") in env["data"]["removed"]


def test_table_that_fits_chrome_but_not_powerpoint_refuses(render):
    rows = "".join(f"<tr><td>Row {i}</td><td>{i},000</td></tr>" for i in range(24))
    deck = _deck(f"""<section class="slide"><h2>Register</h2>
      <table style="position:absolute; top:120px; left:40px">
      <thead><tr><th>Item</th><th>Amount</th></tr></thead>
      <tbody>{rows}</tbody></table></section>""",
      extra_css="td, th { height: 24px; font-size: 14px }")
    code, env = render(deck, "--pptx")
    assert code == 1
    assert any("will not fit PowerPoint" in p["message"] for p in env["problems"])
    assert any("do NOT raise the growth factor" in p["message"]
               for p in env["problems"])


def test_wrong_slide_size_refuses(render):
    deck = DECK_MIN.replace("width: 1280px; height: 720px",
                            "width: 1000px; height: 700px")
    code, env = render(deck, "--pptx")
    assert code == 1
    assert any("1280x720" in p["message"] for p in env["problems"])


def test_overflowing_slide_fails_bounds_gate(render):
    deck = _deck("""<section class="slide"><h2>Over the edge</h2>
      <p style="position:absolute; top:700px; height:60px">runs off the bottom</p>
      </section>""")
    code, env = render(deck, "--pptx")
    assert code == 1
    assert any("slide-bounds" in p["message"] or "outside the slide box"
               in p["message"] for p in env["problems"])


def test_expect_pages_assertion(render):
    code, env = render(DOC_MIN, "--pdf", "--expect-pages", "9")
    assert code == 1
    assert any("expected 9" in p["message"] for p in env["problems"])


# ── review-found holes, each proven closed ───────────────────────────────────

def test_colspan_refuses(render):
    html = DOC_MIN.replace('<td>Revenue</td><td class="num">20,000</td>',
                           '<td colspan="2">Revenue 20,000</td>')
    code, env = render(html, "--docx")
    assert code == 1
    assert "colspan" in env["problems"][0]["message"]


def test_letter_prefixed_currency_bare_minus_caught(render):
    html = DOC_MIN.replace("cash of $1,234", "a movement of -A$3,000")
    code, env = render(html, "--pdf")
    assert code == 1
    assert any("bare minus" in p["message"] for p in env["problems"])


def test_file_subresource_fails_the_seal(render, tmp_path):
    (tmp_path / "local.png").write_bytes(
        __import__("base64").b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="))
    html = DOC_MIN.replace("<p>", "<p><img src=\"local.png\" width=\"1\">", 1)
    code, env = render(html, "--pdf")
    assert code == 1
    assert any("network-sealed" in p["message"] for p in env["problems"])


def test_text_beside_embedded_chart_refuses_not_drops(render):
    deck = _deck("""<section class="slide"><h2>Title</h2>
      <p>label text beside the chart
        <svg width="100" height="40" xmlns="http://www.w3.org/2000/svg">
        <rect width="80" height="30" fill="#333"/></svg></p></section>""")
    code, env = render(deck, "--pptx")
    assert code == 1
    assert any("silently dropped" in p["message"] for p in env["problems"])


def test_usage_error_still_emits_json(run_cli):
    code, env = run_cli(["reconcile", "no_such_mode", "--data", "/tmp/x.json"])
    assert code == 2
    assert env["status"] == "error"
    assert env["problems"][0]["code"] == "USAGE"

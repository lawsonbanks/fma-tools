"""One sealed Chromium session per render run.

Playwright, exclusively: it ships its own pinned browser, which is what kills the
system-Chrome path hardcode class, and the Chrome CLI's --print-to-pdf has no
page-size argument so it could never produce a 960x540 deck.

The browser is network-sealed: every http(s) request is aborted and recorded, so a
non-self-contained HTML fails loudly and identically on every machine, instead of
rendering with blank glyphs on one Mac and real ones on another. The document itself
arrives over file://, and data: URIs never touch the network.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import EnvProblem
from .contract import DECK_H, DECK_W


class Session:
    def __init__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError:
            raise EnvProblem("BROWSER_MISSING",
                             "playwright is not installed -- run `fma doctor` and "
                             "follow its fix lines")
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch()
        except Exception as e:
            self._pw.stop()
            raise EnvProblem("BROWSER_MISSING",
                             f"chromium failed to launch ({str(e).splitlines()[0]}) "
                             "-- run `fma doctor --fix`")
        self.blocked_urls: list[str] = []
        self.page = None

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def load(self, html_path: Path, *, seal: bool = True) -> None:
        ctx = self._browser.new_context(
            viewport={"width": DECK_W, "height": DECK_H}, device_scale_factor=2)
        if seal:
            def _block(route):
                url = route.request.url
                if route.request.resource_type == "document":
                    route.continue_()      # the HTML itself, over file://
                elif url.startswith(("http://", "https://", "file://")):
                    # a file:// SUBRESOURCE renders on this Mac and silently
                    # breaks on another -- self-contained means data: URIs only
                    self.blocked_urls.append(url)
                    route.abort()
                else:
                    route.continue_()
            ctx.route("**/*", _block)
        self.page = ctx.new_page()
        self.page.goto(html_path.resolve().as_uri(), wait_until="networkidle")

    def pdf(self, out_path: Path, css_size: str, width_pt: float, height_pt: float) -> None:
        self.page.emulate_media(media="print")
        # Playwright's pdf() takes px/in/cm/mm, not pt: hand it inches
        self.page.pdf(path=str(out_path),
                      width=f"{width_pt / 72:.4f}in", height=f"{height_pt / 72:.4f}in",
                      print_background=True, prefer_css_page_size=True,
                      margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})

    def inject_deck_print_css(self) -> None:
        """Mechanical print geometry for a deck, injected rather than authored."""
        self.page.add_style_tag(content=(
            f"@page {{ size: {DECK_W}px {DECK_H}px; margin: 0; }}"
            "section.slide { page-break-after: always; break-after: page; }"
            "html, body { margin: 0; }"))

    def override_font(self, family: str) -> None:
        """Re-measure under the face PowerPoint will actually use, so every box is
        sized for the real glyphs, not Chrome's webfont."""
        self.page.add_style_tag(
            content=f"* {{ font-family: {family}, sans-serif !important; }}")
        self.page.wait_for_timeout(250)

    def screenshot_marked(self, workdir: Path, attr: str = "data-fma-img") -> list[str]:
        """Element screenshots for every node the measure pass marked, in order."""
        out = []
        n = self.page.locator(f"[{attr}]").count()
        for i in range(n):
            loc = self.page.locator(f"[{attr}='{i}']")
            path = workdir / f"img{i}.png"
            loc.screenshot(path=str(path))
            out.append(str(path))
        return out

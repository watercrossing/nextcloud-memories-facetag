"""Driving Memories' viewer and this app's overlay with Playwright.

Shared by the real-Memories suite (tests/ci/ui/) and the stand-in harness (tests/ui-harness/):
everything here only assumes what both provide — `globalThis._m.viewer` with its PhotoSwipe
instance, and the overlay's own classes.
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field

from playwright.sync_api import CDPSession, Page

API = "/apps/memories_facetag/api"


def fold(s: str) -> str:
    """As the picker's filter folds names: no accents, lower case."""
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c)).lower()


@dataclass
class Call:
    method: str
    path: str  # after .../api, e.g. /file/123/locate
    body: str | None


@dataclass
class Overlay:
    """This app's overlay on a page, and the API calls it made."""

    page: Page
    calls: list[Call] = field(default_factory=list)
    finished: list[Call] = field(default_factory=list)
    js_errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        def record(into):
            def f(req):
                if API in req.url:
                    into.append(Call(req.method, req.url.split(API, 1)[1].split("?")[0], req.post_data))
            return f
        self.page.on("request", record(self.calls))
        self.page.on("requestfinished", record(self.finished))

    # ---------------------------------------------------------------- state

    def current_fileid(self) -> int | None:
        return self.page.evaluate("() => globalThis._m.viewer.photoswipe?.currSlide?.data?.photo?.fileid ?? null")

    def is_on(self) -> bool:
        return self.page.locator(".facetag-layer").count() > 0

    def button_active(self) -> bool:
        b = self.page.locator(".memories-viewer .top-bar .facetag-button")
        return b.count() == 1 and "active" in (b.get_attribute("class") or "") and b.get_attribute("aria-pressed") == "true"

    def stored(self) -> str | None:
        return self.page.evaluate("() => localStorage.getItem('memories_facetag:on')")

    def labels(self) -> list[str]:
        return sorted(self.page.locator(".facetag-label").all_text_contents())

    def picker_open(self) -> bool:
        return self.page.locator(".facetag-picker").count() > 0

    def saves(self) -> list[Call]:
        """Calls that change something."""
        return [c for c in self.calls if c.method in ("POST", "PUT", "DELETE") and not c.path.endswith("/locate")]

    # ------------------------------------------------------------ geometry

    def image_rect(self) -> dict:
        """The image's on-screen box: the slide's content element (Memories' zoom wrapper is zero high)."""
        return self.page.evaluate("""() => {
            const s = globalThis._m.viewer.photoswipe.currSlide
            const r = s.content.element.getBoundingClientRect()
            return {x: r.x, y: r.y, width: r.width, height: r.height}
        }""")

    def natural_aspect(self) -> float:
        """Width / height of the image as loaded (any preview size has the photo's aspect)."""
        return self.page.evaluate("""() => {
            const s = globalThis._m.viewer.photoswipe.currSlide
            return s.width / s.height
        }""")

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        r = self.image_rect()
        return r["x"] + x * r["width"], r["y"] + y * r["height"]

    def tags(self) -> list[dict]:
        """Every tag drawn: its detection id, label and on-screen box."""
        return self.page.evaluate("""() => [...document.querySelectorAll('.facetag-tag')].map(t => {
            const r = t.querySelector('.facetag-box').getBoundingClientRect()
            return {id: Number(t.dataset.id), label: t.querySelector('.facetag-label').textContent,
                    x: r.x, y: r.y, width: r.width, height: r.height}
        })""")

    def settle(self, timeout: float = 10) -> None:
        """Wait until the image stops moving (PhotoSwipe's transitions, the full-size image coming in)."""
        last, same, end = None, 0, time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                r = self.image_rect()
            except Exception:
                r = None
            key = r and tuple(round(v) for v in r.values())
            same = same + 1 if key and key == last else 0
            if same >= 3 and r["width"] > 1 and r["height"] > 1:
                return
            last = key
            self.page.wait_for_timeout(100)
        raise AssertionError("the image never settled")

    # -------------------------------------------------------------- actions

    def hide_hint(self) -> None:
        self.page.evaluate("() => document.querySelector('.facetag-hint')?.remove()")

    def wait_tags(self, fileid: int | None = None, timeout: float = 15) -> None:
        """Wait until the overlay shows the faces of `fileid` (default: the current photo)."""
        fid = fileid or self.current_fileid()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(c.method == "GET" and c.path == f"/file/{fid}" for c in self.finished):
                ok = self.page.evaluate("""(fid) => {
                    const l = document.querySelector('.facetag-layer')
                    return !!l && globalThis._m.viewer.photoswipe.currSlide.data.photo.fileid === fid
                }""", fid)
                if ok:
                    self.page.wait_for_timeout(200)  # the response is in; give rendering a moment
                    return
            self.page.wait_for_timeout(100)
        raise AssertionError(f"tags of file {fid} never loaded; calls: {self.calls[-5:]}")

    def wait_picker(self, timeout: float = 30) -> None:
        """Wait for the picker, and for locate/suggestions to have answered."""
        self.page.wait_for_selector(".facetag-picker", timeout=timeout * 1000)
        self.page.wait_for_function("""() => { const p = document.querySelector('.facetag-picker')
            return !p || !/Looking for a face|Finding suggestions/.test(p.textContent) }""", timeout=timeout * 1000)

    def items(self) -> list[dict]:
        return self.page.evaluate("""() => [...document.querySelectorAll('.facetag-picker .facetag-item')].map(n => ({
            text: n.textContent, suggested: n.classList.contains('suggested'), new: n.classList.contains('new')}))""")

    def element_at(self, x: float, y: float) -> str:
        """What is on top at a screen point, as `tag.class1.class2` (with its closest overlay part)."""
        return self.page.evaluate("""([x, y]) => {
            const e = document.elementFromPoint(x, y)
            if (!e) return ''
            const part = e.closest('.facetag-item, .facetag-remove, .facetag-picker, .facetag-tag, .facetag-square')
            return e.tagName.toLowerCase() + (part ? ' in ' + part.className : '')
        }""", [x, y])


def swipe(cdp: CDPSession, x0: float, x1: float, y: float, steps: int = 10) -> None:
    """A real one-finger swipe, through Chrome's touch input (Playwright has no swipe gesture)."""
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x0, "y": y}]})
    for i in range(1, steps + 1):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x0 + (x1 - x0) * i / steps, "y": y}]})
        time.sleep(0.016)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

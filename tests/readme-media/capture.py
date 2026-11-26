#!/usr/bin/env python3
"""Screenshots and the short animation in README.md, taken from the real Memories in the test stack.

    NC_PORT=18080 tests/ci/stack.sh up && NC_PORT=18080 tests/ci/stack.sh dataset
    NC_PORT=18080 uv run --no-project --with playwright python tests/readme-media/capture.py [--reset]

Needs a freshly loaded stack (Recognize has run, the scan has not, nothing tagged by hand), the
stack's Nextcloud published on 127.0.0.1:NC_PORT, Playwright's Chromium, and ffmpeg with libwebp.
It changes the stack (enables/disables the app, runs the scan, tags faces); --reset undoes an
earlier run's scan and tags first, so it can run again on the same stack. Writes into docs/media/:

- before.webp  stock Memories (this app disabled): a photo with four people, the sidebar's
               People section naming one
- after.webp   the same photo with this app and its scan: tagging mode, Recognize's named face,
               and "Who is this?" boxes — one of them a face Recognize missed and the scan found
- picker.webp  tapping that face: the name picker, the likeliest people first (★)
- tagging.webp a few seconds of tagging a face neither Recognize nor the scan found: zoom in,
               tap, the picker finds the face and suggests who it is, pick the name

The photos are from Wikimedia Commons (credits in tests/data/ATTRIBUTION.md); the README credits
the two used here.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci"))
import stackctl as s  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/media"
BASE = f"http://127.0.0.1:{os.environ.get('NC_PORT', '18080')}"
PASSWORD = "readme-media-throwaway-" + base64.b32encode(os.urandom(10)).decode()
VIEWPORT = {"width": 1280, "height": 800}

# Manifest files (stable names) of the two photos, and who in them is acted on
BEFORE_AFTER = "groups/G7Biarritz_48616290793_d0b933b8.jpg"      # Recognize names 1 of 4; misses Macron
BEFORE_AFTER_PERSON = "Emmanuel Macron"                            # ...whom the scan then finds
ANIMATION = "groups/State_Dinner_The_Official_State_Visit_of_France_26832278157_23fcfeb1.jpg"
ANIMATION_PERSON = "Emmanuel Macron"                               # missed by Recognize and the scan
FPS = 20


def photo(name: str) -> tuple[int, dict]:
    ids = s.file_ids()
    p = next(p for p in s.manifest()["photos"] if p["file"] == name)
    return ids[name], p


def person(p: dict, name: str) -> dict:
    return next(x for x in p["people"] if x["name"] == name)


def login(page: Page) -> None:
    page.goto(BASE + "/login")
    page.fill("#user", s.TEST_USER)
    page.fill("#password", PASSWORD)
    page.keyboard.press("Enter")
    page.wait_for_url("**/apps/**")


def open_photo(page: Page, fid: int, folder: str) -> None:
    """Open a photo in Memories' viewer, by its URL (the folder view only renders what is scrolled to)."""
    day = s.scalar(f"SELECT dayid FROM oc_memories WHERE fileid = {fid}")
    page.goto("about:blank")  # a hash change alone doesn't make Memories open another photo
    page.goto(f"{BASE}/apps/memories/folders/Photos/{folder}#v/{day}/{fid}")
    page.wait_for_selector(".memories-viewer .top-bar")
    page.wait_for_function("() => document.querySelector('.memories-viewer .pswp__img:not(.pswp__img--placeholder)')")
    time.sleep(1.5)  # full-size image in, transitions settled


def tagging_on(page: Page) -> None:
    page.locator(".facetag-button").click()
    page.wait_for_selector(".facetag-layer")
    page.evaluate("() => document.querySelector('.facetag-hint')?.remove()")
    page.wait_for_function("() => document.querySelectorAll('.facetag-tag').length > 0")
    time.sleep(0.5)


def image_rect(page: Page) -> dict:
    return page.evaluate("""() => {
        const s = globalThis._m.viewer.photoswipe.currSlide
        const r = s.content.element.getBoundingClientRect()
        return {x: r.x, y: r.y, width: r.width, height: r.height}
    }""")


def on_screen(page: Page, p: dict) -> tuple[float, float]:
    r = image_rect(page)
    return r["x"] + p["x"] * r["width"], r["y"] + p["y"] * r["height"]


def sidebar(page: Page) -> None:
    page.locator(".memories-viewer .top-bar [aria-label='Info']").first.click()
    page.wait_for_selector("text=People", timeout=15000)
    time.sleep(2)  # face thumbnails


def screenshot(page: Page, name: str) -> None:
    """A still, as lossy WebP: these are regenerated, and every version stays in git history."""
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".png") as png:
        page.screenshot(path=png.name)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", png.name, "-c:v", "libwebp",
                        "-quality", "85", str(OUT / name)], check=True)
    print(f"wrote docs/media/{name} ({(OUT / name).stat().st_size >> 10} KB)", file=sys.stderr)


# ------------------------------------------------------------------ animation

# A drawn fingertip: headless screenshots show no pointer
FINGER_JS = """([x, y, pressed]) => {
    let d = document.getElementById('readme-finger')
    if (!d) {
        d = document.createElement('div')
        d.id = 'readme-finger'
        document.body.appendChild(d)
    }
    const r = pressed ? 15 : 19
    d.style.cssText = `position:fixed;left:${x - r}px;top:${y - r}px;width:${2 * r}px;height:${2 * r}px;
        border-radius:50%;background:rgba(255,255,255,${pressed ? .75 : .45});border:2px solid rgba(255,255,255,.95);
        box-shadow:0 0 0 1px rgba(0,0,0,.35),0 2px 8px rgba(0,0,0,.4);z-index:2147483647;pointer-events:none`
}"""


class Recorder:
    """Frames taken one by one, each with how long it shows: scripted motion in even steps,
    waiting (face detection) in real time, so the clip shows how fast it really is."""

    def __init__(self, page: Page):
        self.page = page
        self.frames: list[tuple[bytes, float]] = []

    def snap(self, seconds: float = 1 / FPS) -> None:
        self.frames.append((self.page.screenshot(type="png"), seconds))

    def wait_until(self, js: str, timeout: float = 30) -> None:
        """Keep taking frames, timed as they happen, until js is true."""
        t = time.monotonic()
        last = t
        while True:
            done = self.page.evaluate(js)
            png = self.page.screenshot(type="png")
            now = time.monotonic()
            self.frames.append((png, now - last))
            last = now
            if done:
                return
            if now - t > timeout:
                sys.exit("timed out waiting for " + js)

    def write_webp(self, target: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # concat demuxer: each frame for its own duration
            lines = []
            for i, (png, d) in enumerate(self.frames):
                (Path(tmp) / f"f{i:04d}.png").write_bytes(png)
                lines += [f"file 'f{i:04d}.png'", f"duration {d:.3f}"]
            lines.append(f"file 'f{len(self.frames) - 1:04d}.png'")
            (Path(tmp) / "list.txt").write_text("\n".join(lines) + "\n")
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-i", f"{tmp}/list.txt",
                            "-vf", f"fps={FPS},scale=960:-1:flags=lanczos", "-c:v", "libwebp_anim",
                            "-quality", "75", "-loop", "0", str(target)], check=True)
        total = sum(d for _, d in self.frames)
        print(f"wrote docs/media/{target.name} ({total:.1f} s, {target.stat().st_size >> 10} KB)", file=sys.stderr)


def ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def animation(page: Page) -> None:
    fid, p = photo(ANIMATION)
    who = person(p, ANIMATION_PERSON)
    open_photo(page, fid, ANIMATION.split("/")[0])
    tagging_on(page)
    page.mouse.move(5, VIEWPORT["height"] - 5)  # out of the way: hovering a row would select it
    rec = Recorder(page)
    rec.snap(0.6)

    # Zoom in towards the face, in even steps (PhotoSwipe's own transition is not captured)
    x0, y0 = on_screen(page, who)
    start = page.evaluate("() => globalThis._m.viewer.photoswipe.currSlide.currZoomLevel")
    steps = 15
    for k in range(1, steps + 1):
        level = start * (1 + 1.6 * ease(k / steps))
        page.evaluate("([l, x, y]) => globalThis._m.viewer.photoswipe.zoomTo(l, {x, y}, 0)", [level, x0, y0])
        rec.snap()
    rec.snap(0.2)

    # The finger comes down on the face
    x, y = on_screen(page, who)
    page.evaluate(FINGER_JS, [x, y, False])
    rec.snap(0.2)
    page.evaluate(FINGER_JS, [x, y, True])
    page.mouse.click(x, y)
    page.mouse.move(5, VIEWPORT["height"] - 5)
    page.evaluate(FINGER_JS, [x, y, False])
    rec.wait_until("() => !!document.querySelector('.facetag-picker') && "
                   "!document.querySelector('.facetag-picker').textContent.includes('Looking for a face')")
    rec.snap(0.5)

    # ...and moves to the first suggestion, which must be the right person
    first = page.locator(".facetag-item").first
    if ANIMATION_PERSON not in first.inner_text():
        page.screenshot(path=str(OUT / "failed-animation.png"))
        sys.exit(f"see docs/media/failed-animation.png: first suggestion is {first.inner_text()!r}, not {ANIMATION_PERSON}")
    box = first.bounding_box()
    tx, ty = box["x"] + min(90, box["width"] / 3), box["y"] + box["height"] / 2
    for k in range(1, 7):
        e = ease(k / 6)
        page.evaluate(FINGER_JS, [x + (tx - x) * e, y + (ty - y) * e, False])
        rec.snap()
    page.evaluate(FINGER_JS, [tx, ty, True])
    rec.snap(0.1)
    page.mouse.click(tx, ty)
    page.mouse.move(5, VIEWPORT["height"] - 5)
    page.evaluate(FINGER_JS, [tx, ty, False])
    rec.wait_until("() => !document.querySelector('.facetag-picker')")
    page.evaluate("() => document.getElementById('readme-finger')?.remove()")
    rec.snap(1.4)
    rec.write_webp(OUT / "tagging.webp")


# ------------------------------------------------------------------ main

def main() -> None:
    if not shutil.which("ffmpeg"):
        sys.exit("needs ffmpeg (with libwebp)")
    if "--reset" in sys.argv:
        # Undo an earlier capture: the scan's faces and the hand-placed tags
        s.occ("memories_facetag:scan", "--undo")
        s.execute("DELETE FROM oc_recognize_face_detections WHERE id IN (SELECT detection_id FROM oc_facetag_manual)")
        s.execute("DELETE FROM oc_facetag_manual")
    if int(s.scalar("SELECT count(*) FROM oc_facetag_scanned")) or int(s.scalar("SELECT count(*) FROM oc_facetag_manual")):
        sys.exit("needs a freshly loaded stack (or --reset): the scan has run or faces were tagged here already")
    r = s.run(["exec", "-T", "-u", "www-data", "-e", f"OC_PASS={PASSWORD}", "nextcloud",
               "php", "occ", "user:resetpassword", "--password-from-env", s.TEST_USER])
    fid, p = photo(BEFORE_AFTER)
    folder = BEFORE_AFTER.split("/")[0]

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        login(page)

        # 1. Stock Memories
        s.occ("app:disable", "memories_facetag")
        time.sleep(4)  # the web server caches app config for 3 s
        open_photo(page, fid, folder)
        sidebar(page)
        screenshot(page, "before.webp")

        # 2. The animation, on a photo the scan finds nothing new in
        s.occ("app:enable", "memories_facetag")
        time.sleep(4)  # the web server caches app config for 3 s
        animation(page)

        # 3. After the scan: tagging mode on the first photo, then the picker on the face it found
        out = s.occ("memories_facetag:scan", "--settle-hours", "0", "--no-cluster").stdout
        print(out.strip().splitlines()[-1], file=sys.stderr)
        open_photo(page, fid, folder)
        if not page.locator(".facetag-layer").count():
            tagging_on(page)
        page.evaluate("() => document.querySelector('.facetag-hint')?.remove()")
        time.sleep(0.5)
        screenshot(page, "after.webp")

        x, y = on_screen(page, person(p, BEFORE_AFTER_PERSON))
        page.mouse.click(x, y)
        page.wait_for_selector(".facetag-picker")
        page.wait_for_function("() => !document.querySelector('.facetag-picker')?.textContent.includes('Finding suggestions')", timeout=30000)
        time.sleep(0.5)
        screenshot(page, "picker.webp")
        browser.close()


if __name__ == "__main__":
    main()

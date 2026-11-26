"""The stand-in viewer: PhotoSwipe set up the way Memories does it, with the app's API mocked.

For fast local iteration on js/facetag.js without the stack: a run takes seconds.

    uv run --no-project --with pytest --with playwright --with pillow python -m pytest tests/ui-harness

PhotoSwipe 5.4.4 (the build Memories 11 ships, MIT) comes from npm into FACETAG_CACHE/ui-harness,
checked against its sha256; the photo is drawn here (synthetic: four grey ovals on 8192×5464).
It catches the viewer bugs the stand-in copies from Memories (see README.md), not Memories'
own changes — tests/ci/ui/ is what CI runs against the real thing.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tests/ci/ui"))
from viewer import Overlay  # noqa: E402

CACHE = Path(os.environ.get("FACETAG_CACHE", ROOT / ".cache")) / "ui-harness"
PSWP_URL = "https://registry.npmjs.org/photoswipe/-/photoswipe-5.4.4.tgz"
PSWP_SHA256 = "4615d56b1f4f8e40f70c9f433a97803e5131d7166285b467ff63d26e42ef397b"
W, H = 8192, 5464
# The photo's faces (relative boxes), the same on every slide
OVALS = [(1500, 2000), (3000, 1800), (4500, 2100), (6000, 1900)]
FACE = {"width": 500 / W, "height": 650 / H}
PEOPLE = [{"id": 10, "name": "Alice", "count": 5}, {"id": 11, "name": "Bob", "count": 3},
          {"id": 12, "name": "Zoë", "count": 2}]


def face(i: int) -> dict:
    x, y = OVALS[i]
    return {"x": x / W, "y": y / H, **FACE}


def assets() -> dict[str, Path]:
    CACHE.mkdir(parents=True, exist_ok=True)
    esm, css, jpg = CACHE / "photoswipe.esm.js", CACHE / "photoswipe.css", CACHE / "photo.jpg"
    if not (esm.exists() and css.exists()):
        data = urllib.request.urlopen(PSWP_URL, timeout=60).read()
        if hashlib.sha256(data).hexdigest() != PSWP_SHA256:
            raise RuntimeError(f"{PSWP_URL}: sha256 mismatch")
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            esm.write_bytes(t.extractfile("package/dist/photoswipe.esm.js").read())
            css.write_bytes(t.extractfile("package/dist/photoswipe.css").read())
    if not jpg.exists():
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (W, H), (70, 90, 110))
        d = ImageDraw.Draw(im)
        for x, y in OVALS:
            d.ellipse((x, y, x + 500, y + 650), fill=(205, 170, 150), outline=(40, 30, 30), width=12)
        im.save(jpg, quality=80)
    return {"/": HERE / "site/index.html", "/photoswipe.esm.js": esm, "/pswp.css": css, "/photo.jpg": jpg,
            "/facetag.js": ROOT / "js/facetag.js", "/facetag.css": ROOT / "css/facetag.css"}


class MockApi:
    """The app's API, in memory. Slide 1 has Alice (named) and an unnamed face; slide 2 has Bob;
    slide 3 none. locate finds an oval within the tapped box and suggests Bob."""

    def __init__(self):
        self.next_id = 100
        self.faces = {1: [{**face(0), "id": 1, "clusterId": 10, "name": "Alice", "manual": False},
                          {**face(2), "id": 2, "clusterId": None, "name": "", "manual": False}],
                      2: [{**face(1), "id": 3, "clusterId": 11, "name": "Bob", "manual": False}],
                      3: []}
        self.people = [dict(p) for p in PEOPLE]
        self.delay = 0.0

    def detection(self, did: int) -> tuple[int, dict]:
        return next((fid, d) for fid, ds in self.faces.items() for d in ds if d["id"] == did)

    def who(self, body: dict) -> tuple[int | None, str]:
        if "clusterId" in body:
            return body["clusterId"], next(p["name"] for p in self.people if p["id"] == body["clusterId"])
        p = next((p for p in self.people if p["name"].lower() == body["name"].lower()), None)
        if not p:
            p = {"id": max(x["id"] for x in self.people) + 1, "name": body["name"], "count": 0}
            self.people.append(p)
        return p["id"], p["name"]

    def __call__(self, method: str, path: str, body: dict | None) -> dict:
        parts = path.strip("/").split("/")
        if parts[0] == "file":
            fid = int(parts[1])
            if len(parts) == 2:
                return {"detections": self.faces.get(fid, []), "people": self.people}
            if parts[2] == "locate":
                cx, cy = body["x"] + body["width"] / 2, body["y"] + body["height"] / 2
                for i in range(len(OVALS)):
                    f = face(i)
                    if abs(f["x"] + f["width"] / 2 - cx) < f["width"] and abs(f["y"] + f["height"] / 2 - cy) < f["height"]:
                        return {"face": f, "token": f"t{fid}-{i}", "suggestions": [{"id": 11, "name": "Bob", "distance": 0.3, "score": 0.3}]}
                return {"face": None, "token": None, "suggestions": []}
            if parts[2] == "tags":
                cid, name = self.who(body)
                self.next_id += 1
                d = {k: body[k] for k in ("x", "y", "width", "height")}
                self.faces.setdefault(fid, []).append({**d, "id": self.next_id, "clusterId": cid, "name": name, "manual": True})
                return {"id": self.next_id, "detections": self.faces[fid]}
        if parts[0] == "detection":
            fid, d = self.detection(int(parts[1]))
            if len(parts) == 3 and parts[2] == "suggest":
                return {"suggestions": [{"id": 12, "name": "Zoë", "distance": 0.35, "score": 0.35}]}
            if method == "PUT":
                d["clusterId"], d["name"] = self.who(body)
            elif method == "DELETE":
                self.faces[fid].remove(d)
            return {"detections": self.faces[fid]}
        raise ValueError(f"no mock for {method} {path}")


@pytest.fixture(scope="session")
def files():
    return assets()


@pytest.fixture(scope="session")
def pw():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(pw):
    b = pw.chromium.launch()
    yield b
    b.close()


def _open(browser, files, options):
    ctx = browser.new_context(**options)
    page = ctx.new_page()
    api = MockApi()

    def handle(route):
        path = route.request.url.split("h.test", 1)[1].split("?")[0]
        if path in files:
            ct = {".html": "text/html", ".css": "text/css", ".js": "application/javascript", ".jpg": "image/jpeg"}[files[path].suffix]
            return route.fulfill(body=files[path].read_bytes(), content_type=ct)
        if "/api/" in path:
            if api.delay:
                page.wait_for_timeout(api.delay * 1000)
            body = json.loads(route.request.post_data) if route.request.post_data else None
            return route.fulfill(json=api(route.request.method, path.split("/api", 1)[1], body))
        return route.fulfill(status=404)

    page.route("http://h.test/**", handle)
    ov = Overlay(page)
    ov.api = api
    page.on("pageerror", lambda e: ov.js_errors.append(str(e)))
    page.goto("http://h.test/")
    page.wait_for_function("window.__ready")
    ov.settle()
    return ctx, ov


@pytest.fixture
def desktop(browser, files):
    ctx, ov = _open(browser, files, {"viewport": {"width": 1600, "height": 900}})
    yield ov
    ctx.close()


@pytest.fixture
def phone(pw, browser, files):
    ctx, ov = _open(browser, files, pw.devices["Pixel 7"])
    yield ov
    ctx.close()

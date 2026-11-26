"""Fixtures for the UI tests: Playwright (Chromium) against the real Memories in the test stack.

    NC_PORT=18080 tests/ci/stack.sh up && NC_PORT=18080 tests/ci/stack.sh dataset
    NC_PORT=18080 tests/ci/stack.sh ui          # or after `stack.sh test`, on the same stack

Each test gets a fresh browser context (logged in as the test user; tagging mode not yet
remembered), and every face row it adds or changes is put back afterwards, so tests don't depend
on each other. On a failure the page's screenshot goes to UI_ARTIFACTS (default ui-artifacts/),
with a Playwright trace if UI_TRACE=retain-on-failure (CI sets it; `on` keeps every trace).
"""

from __future__ import annotations

import base64
import os
import re
import sys
from pathlib import Path

import pytest
from playwright.sync_api import Browser, sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import stackctl as s  # noqa: E402
from viewer import Overlay  # noqa: E402

BASE = f"http://127.0.0.1:{os.environ.get('NC_PORT', '18080')}"
PASSWORD = "ui-tests-throwaway-" + base64.b32encode(os.urandom(10)).decode()
DESKTOP = {"viewport": {"width": 1280, "height": 800}}
ARTIFACTS = Path(os.environ.get("UI_ARTIFACTS", "ui-artifacts"))
TRACE = os.environ.get("UI_TRACE", "off")  # off | retain-on-failure | on


def pytest_collection_modifyitems(items):
    items.sort(key=lambda i: i.fspath.basename)  # cheap checks first


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    if rep.when == "call":
        item.failed_call = rep.failed
    return rep


# ------------------------------------------------------------------ stack

@pytest.fixture(scope="session")
def stack():
    """The stack as the UI tests need it: the app on, no scan results, nothing tagged by hand."""
    if not os.environ.get("NC_PORT"):
        pytest.exit("NC_PORT is not set: the stack must publish Nextcloud for a browser (see CLAUDE.md)", 2)
    s.occ("app:enable", "memories_facetag")
    s.occ("memories_facetag:scan", "--undo")
    s.execute("DELETE FROM oc_recognize_face_detections WHERE id IN (SELECT detection_id FROM oc_facetag_manual)")
    s.execute("DELETE FROM oc_facetag_manual")
    s.run(["exec", "-T", "-u", "www-data", "-e", f"OC_PASS={PASSWORD}", "nextcloud",
           "php", "occ", "user:resetpassword", "--password-from-env", s.TEST_USER])


def _snapshot() -> tuple[dict, set]:
    rows = s.sql(f"SELECT id, cluster_id, threshold FROM oc_recognize_face_detections WHERE user_id = '{s.TEST_USER}'")
    clusters = {int(r["id"]) for r in s.sql(f"SELECT id FROM oc_recognize_face_clusters WHERE user_id = '{s.TEST_USER}'")}
    return {int(r["id"]): (r["cluster_id"], r["threshold"]) for r in rows}, clusters


@pytest.fixture(autouse=True)
def restore_faces(stack):
    """Put back every face row and person a test added or changed."""
    before, clusters = _snapshot()
    yield
    after, clusters_after = _snapshot()
    added = [i for i in after if i not in before]
    if added:
        ids = ",".join(map(str, added))
        s.execute(f"DELETE FROM oc_facetag_manual WHERE detection_id IN ({ids})")
        s.execute(f"DELETE FROM oc_recognize_face_detections WHERE id IN ({ids})")
    for i, (cluster, threshold) in before.items():
        if after.get(i) != (cluster, threshold):
            c = "NULL" if cluster is None else int(cluster)
            s.execute(f"UPDATE oc_recognize_face_detections SET cluster_id = {c}, threshold = {float(threshold)} WHERE id = {i}")
    new = clusters_after - clusters
    if new:
        s.execute(f"DELETE FROM oc_recognize_face_clusters WHERE id IN ({','.join(map(str, new))})")


# --------------------------------------------------------------- browser

@pytest.fixture(scope="session")
def pw():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(pw):
    b = pw.chromium.launch()
    yield b
    b.close()


@pytest.fixture(scope="session")
def logged_in(stack, browser: Browser) -> dict:
    """Cookies of a logged-in session, for every test's fresh context."""
    ctx = browser.new_context(**DESKTOP)
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill("#user", s.TEST_USER)
    page.fill("#password", PASSWORD)
    page.keyboard.press("Enter")
    page.wait_for_url("**/apps/**")
    state = ctx.storage_state()
    ctx.close()
    return state


def _context(request, browser: Browser, logged_in: dict, options: dict):
    ctx = browser.new_context(**options, storage_state=logged_in)
    ctx.set_default_timeout(20_000)
    if TRACE != "off":
        ctx.tracing.start(screenshots=True, snapshots=True)
    page = ctx.new_page()
    ov = Overlay(page)
    page.on("pageerror", lambda e: ov.js_errors.append(str(e)))
    yield ov
    failed = getattr(request.node, "failed_call", True)
    name = re.sub(r"[^\w.-]+", "_", request.node.nodeid.split("::", 1)[-1])
    if failed:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(ARTIFACTS / f"{name}.png"))
        except Exception as e:  # a crashed page still gets its trace
            print(f"no screenshot: {e}")
    if TRACE == "on" or (TRACE == "retain-on-failure" and failed):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        ctx.tracing.stop(path=str(ARTIFACTS / f"{name}.trace.zip"))
    elif TRACE != "off":
        ctx.tracing.stop()
    ctx.close()


@pytest.fixture
def desktop(request, browser, logged_in) -> Overlay:
    yield from _context(request, browser, logged_in, DESKTOP)


@pytest.fixture
def phone(request, pw, browser, logged_in) -> Overlay:
    """Pixel 7: touch, mobile layout."""
    yield from _context(request, browser, logged_in, pw.devices["Pixel 7"])


@pytest.fixture
def phone_landscape(request, pw, browser, logged_in) -> Overlay:
    yield from _context(request, browser, logged_in, pw.devices["Pixel 7 landscape"])


# ------------------------------------------------------------ photo set

@pytest.fixture(scope="session")
def photos(stack) -> dict[int, dict]:
    """Nextcloud file id → manifest entry."""
    ids = s.file_ids()
    return {ids[p["file"]]: p for p in s.manifest()["photos"]}


@pytest.fixture(scope="session")
def people(stack) -> dict[str, int]:
    """Ground-truth person → cluster id (the largest, if several have the name). Other named
    clusters (the server tests leave an empty one behind) are left out."""
    rows = s.sql(f"""SELECT c.id, c.title, count(d.id) AS n FROM oc_recognize_face_clusters c
                     LEFT JOIN oc_recognize_face_detections d ON d.cluster_id = c.id
                     WHERE c.user_id = '{s.TEST_USER}' AND c.title <> '' GROUP BY c.id, c.title ORDER BY n""")
    identities = set(s.manifest()["identities"].values())
    return {r["title"]: int(r["id"]) for r in rows if r["title"] in identities}


def detections(file_id: int) -> list[dict]:
    return s.sql(f"""SELECT d.id, d.x, d.y, d.width, d.height, d.cluster_id, d.threshold, d.face_vector,
                            c.title, m.source, m.found_face
                     FROM oc_recognize_face_detections d
                     LEFT JOIN oc_recognize_face_clusters c ON c.id = d.cluster_id
                     LEFT JOIN oc_facetag_manual m ON m.detection_id = d.id
                     WHERE d.user_id = '{s.TEST_USER}' AND d.file_id = {file_id} ORDER BY d.id""")


@pytest.fixture(scope="session")
def missed(photos, people) -> list[tuple[int, dict]]:
    """Ground-truth faces of named people that Recognize did not find, biggest first: (file id, person)."""
    out = []
    for fid, photo in photos.items():
        dets = detections(fid)
        out += [(fid, p) for p in photo["people"] if p["name"] in people and not any(s.matches(p, d) for d in dets)]
    return sorted(out, key=lambda x: -x[1]["size"])


# --------------------------------------------------------------- viewer

def open_photo(ov: Overlay, fid: int, photos: dict[int, dict]) -> None:
    """Open a photo in Memories' viewer by its URL."""
    page = ov.page
    folder = photos[fid]["file"].rsplit("/", 1)[0]
    day = s.scalar(f"SELECT dayid FROM oc_memories WHERE fileid = {fid}")
    page.goto("about:blank")  # a hash change alone doesn't make Memories open another photo
    page.goto(f"{BASE}/apps/memories/folders/{s.PHOTOS}/{folder}#v/{day}/{fid}")
    page.wait_for_selector(".memories-viewer .top-bar .facetag-button")
    page.wait_for_function(f"() => globalThis._m?.viewer?.photoswipe?.currSlide?.data?.photo?.fileid === {fid}")
    page.wait_for_selector(".memories-viewer .pswp__img:not(.pswp__img--placeholder)")
    ov.settle()


def tagging_on(ov: Overlay, tap: bool = False) -> None:
    """Switch tagging on with the button (or, if it is on already — it is remembered — just wait for the tags)."""
    page = ov.page
    if ov.is_on():
        ov.hide_hint()
        ov.wait_tags()
        return
    button = page.locator(".memories-viewer .top-bar .facetag-button")
    button.tap() if tap else button.click()
    page.wait_for_selector(".facetag-layer")
    ov.hide_hint()
    ov.wait_tags()

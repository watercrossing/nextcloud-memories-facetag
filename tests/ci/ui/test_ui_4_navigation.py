"""Moving between photos while tagging: arrows, arrow keys, a real touch swipe on a phone.

After each move the new photo's tags must be drawn, and a tap must locate on the new photo — the
bugs this caught were Memories' `currentPhoto` lagging behind a swipe.
"""

import pytest

import stackctl as s
from conftest import detections, open_photo, tagging_on
from viewer import click_control, swipe


@pytest.fixture
def start(photos):
    """A group photo in the middle of its folder as the viewer orders it, by date taken, so it has
    neighbours both sides (file ids follow the order the scan met the files, which varies)."""
    groups = [fid for fid, p in photos.items() if p["kind"] == "group"]
    rows = s.sql(f"SELECT fileid FROM oc_memories WHERE fileid IN ({','.join(map(str, groups))}) "
                 "ORDER BY datetaken DESC, fileid DESC")
    order = [r["fileid"] for r in rows]
    assert len(order) == len(groups), f"{len(groups)} group photos, {len(order)} in Memories' index"
    return order[len(order) // 2]


def assert_shows(ov, fid: int) -> None:
    """The overlay shows exactly the faces of `fid`, named as in the database."""
    ov.wait_tags(fid)
    want = sorted(d["title"] or "Who is this?" for d in detections(fid))
    assert ov.labels() == want, f"photo {fid}: tags {ov.labels()}, its faces {want}"


def tap_locates_here(ov, fid: int, tap) -> None:
    """A tap on an untagged spot of the photo opens the picker and locates on *this* photo."""
    r = ov.image_rect()
    tags = ov.tags()
    for fx, fy in ((0.5, 0.85), (0.15, 0.85), (0.85, 0.85), (0.5, 0.5), (0.15, 0.15)):
        x, y = r["x"] + fx * r["width"], r["y"] + fy * r["height"]
        if not any(t["x"] - 60 <= x <= t["x"] + t["width"] + 60 and t["y"] - 60 <= y <= t["y"] + t["height"] + 60 for t in tags):
            break
    n = len(ov.calls)
    tap(x, y)
    ov.wait_picker()
    locates = [c.path for c in ov.calls[n:] if c.path.endswith("/locate")]
    assert locates == [f"/file/{fid}/locate"], f"tap on photo {fid} located on {locates}"
    ov.page.keyboard.press("Escape")
    assert not ov.picker_open() and ov.is_on()


def test_arrows_and_arrow_keys(desktop, start, photos):
    ov, page = desktop, desktop.page
    open_photo(ov, start, photos)
    tagging_on(ov)
    assert_shows(ov, start)

    click_control(page, page.locator(".pswp__button--arrow--next"))
    nxt = ov.wait_moved(start)
    assert ov.is_on()
    assert_shows(ov, nxt)
    tap_locates_here(ov, nxt, page.mouse.click)

    page.keyboard.press("ArrowRight")
    third = ov.wait_moved(nxt)
    assert third != start
    assert_shows(ov, third)

    page.keyboard.press("ArrowLeft")
    assert ov.wait_moved(third) == nxt
    assert_shows(ov, nxt)
    tap_locates_here(ov, nxt, page.mouse.click)

    click_control(page, page.locator(".pswp__button--arrow--prev"))
    assert ov.wait_moved(nxt) == start
    assert_shows(ov, start)
    assert not ov.saves() and not ov.js_errors, (ov.saves(), ov.js_errors)


def test_touch_swipe_on_a_phone(phone, start, photos):
    ov, page = phone, phone.page
    cdp = page.context.new_cdp_session(page)
    open_photo(ov, start, photos)
    tagging_on(ov, tap=True)
    assert_shows(ov, start)
    vw = page.viewport_size["width"]
    r = ov.image_rect()
    y = r["y"] + r["height"] / 2

    swipe(cdp, vw * 0.85, vw * 0.1, y)
    nxt = ov.wait_moved(start)  # times out if the swipe did nothing
    assert ov.is_on() and not ov.picker_open(), "a swipe is not a tap"
    assert_shows(ov, nxt)
    tap_locates_here(ov, nxt, page.touchscreen.tap)

    # Swipe starting on a tag: PhotoSwipe still gets it
    tags = ov.tags()
    if tags:
        t = max(tags, key=lambda t: t["width"])
        swipe(cdp, t["x"] + t["width"] / 2, t["x"] + t["width"] / 2 + vw * 0.75, t["y"] + t["height"] / 2)
    else:
        swipe(cdp, vw * 0.1, vw * 0.85, y)
    assert ov.wait_moved(nxt) == start, "swiping back (starting on a face) should return to the first photo"
    assert not ov.picker_open()
    assert_shows(ov, start)
    tap_locates_here(ov, start, page.touchscreen.tap)
    assert not ov.saves() and not ov.js_errors, (ov.saves(), ov.js_errors)

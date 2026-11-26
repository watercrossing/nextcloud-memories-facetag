"""Touch screens: the click a browser sends after a tap lands on whatever is under the finger by then
— often the picker that tap just opened. It must not pick a name or press "Remove"."""

import stackctl as s
from conftest import detections, open_photo, tagging_on


def test_ghost_click_after_a_tap_picks_nothing(phone_landscape, missed, photos):
    """Landscape phone: the picker opens over the finger (no room below the face, so it flips up)."""
    ov, page = phone_landscape, phone_landscape.page
    hits = []
    for fid, person in missed[:6]:
        open_photo(ov, fid, photos)
        tagging_on(ov, tap=True)
        r = ov.image_rect()
        # low on the photo, so the picker has to open above the face — over the finger
        x, y = r["x"] + person["x"] * r["width"], r["y"] + min(0.8, person["y"] + 0.25) * r["height"]
        if "facetag" in ov.element_at(x, y):
            continue
        n = len(ov.calls)
        page.touchscreen.tap(x, y)
        ov.wait_picker()
        under = ov.element_at(x, y)
        assert ov.picker_open(), "the tap should leave the picker open"
        assert not ov.saves(), f"the click after the tap saved something: {ov.saves()}"
        if "facetag-item" in under or "facetag-picker" in under:
            hits.append(under)
        page.keyboard.press("Escape")
    assert hits, "the picker never opened under the finger: the test proved nothing (layout changed?)"

    # A deliberate tap on a name does pick it
    fid, person = missed[0]
    open_photo(ov, fid, photos)
    tagging_on(ov, tap=True)
    page.touchscreen.tap(*ov.to_screen(person["x"], person["y"]))
    ov.wait_picker()
    page.locator(".facetag-item").first.tap()
    page.wait_for_selector(".facetag-picker", state="detached")
    assert len(ov.saves()) == 1 and ov.saves()[0].path == f"/file/{fid}/tags"
    assert not ov.js_errors, ov.js_errors


def test_tapping_a_tag_opens_it_and_removes_nothing(phone, photos):
    ov, page = phone, phone.page
    fid, d = next((fid, d) for fid, p in sorted(photos.items()) if p["kind"] == "portrait"
                  for d in detections(fid) if d["title"] and s.identity_of(p, d) == d["title"])
    open_photo(ov, fid, photos)
    tagging_on(ov, tap=True)
    t = next(t for t in ov.tags() if t["id"] == d["id"])
    x, y = t["x"] + t["width"] / 2, t["y"] + t["height"] / 2
    page.touchscreen.tap(x, y)
    ov.wait_picker()
    page.wait_for_timeout(500)
    assert ov.picker_open() and d["title"] in page.locator(".facetag-current").inner_text()
    assert not ov.saves(), f"tapping a tag changed it: {ov.saves()}"
    row = next(x for x in detections(fid) if x["id"] == d["id"])
    assert row["cluster_id"] == d["cluster_id"]

    # "Remove" works when tapped on purpose
    page.locator(".facetag-remove").tap()
    page.wait_for_selector(".facetag-picker", state="detached")
    assert [c.method for c in ov.saves()] == ["DELETE"]
    assert not ov.js_errors, ov.js_errors

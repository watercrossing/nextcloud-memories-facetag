"""js/facetag.js in the stand-in viewer (seconds per run; see conftest.py). The real-Memories suite
in tests/ci/ui/ covers the same ground against the real thing."""

from conftest import face
from viewer import swipe


def on(ov, tap=False):
    b = ov.page.locator(".facetag-button")
    b.tap() if tap else b.click()
    ov.page.wait_for_selector(".facetag-layer")
    ov.hide_hint()
    ov.wait_tags()


def centre(ov, f):
    return ov.to_screen(f["x"] + f["width"] / 2, f["y"] + f["height"] / 2)


def test_tags_on_the_faces(desktop):
    ov = desktop
    on(ov)
    r = ov.image_rect()
    assert r["height"] > 100, "measured the zero-high zoom wrapper?"
    for t in ov.tags():
        f = next(d for d in ov.api.faces[1] if d["id"] == t["id"])
        assert abs(t["x"] - (r["x"] + f["x"] * r["width"])) <= 2 and abs(t["y"] - (r["y"] + f["y"] * r["height"])) <= 2
    assert ov.labels() == ["Alice", "Who is this?"]


def test_tap_locate_pick(desktop):
    ov, page = desktop, desktop.page
    on(ov)
    page.mouse.click(*centre(ov, face(3)))
    ov.wait_picker()
    assert page.locator(".facetag-square.found").count() == 1
    assert ov.items()[0] == {"text": "Bob", "suggested": True, "new": False}
    page.locator(".facetag-item").first.click()
    page.wait_for_selector(".facetag-picker", state="detached")
    assert [c.path for c in ov.saves()] == ["/file/1/tags"]
    assert "Bob" in ov.labels()


def test_tap_a_tag_opens_it_and_wheel_zoom_over_it_works(desktop):
    ov, page = desktop, desktop.page
    on(ov)
    x, y = centre(ov, face(0))
    page.mouse.move(x, y)
    assert page.locator(".facetag-tag.hover").count() == 1, "the tag under the mouse is highlighted"
    page.mouse.click(x, y)
    ov.wait_picker()
    assert "Alice" in page.locator(".facetag-current").inner_text()
    page.keyboard.press("Escape")
    w = ov.image_rect()["width"]
    page.mouse.move(x, y)
    for _ in range(4):
        page.mouse.wheel(0, -300)
        page.wait_for_timeout(50)
    ov.settle()
    assert ov.image_rect()["width"] > 1.3 * w, "a wheel over a tag should zoom"
    t = next(t for t in ov.tags() if t["id"] == 1)
    fx, fy = centre(ov, face(0))
    assert abs(t["x"] + t["width"] / 2 - fx) <= 2 and abs(t["y"] + t["height"] / 2 - fy) <= 2, "tag follows the zoom"


def test_next_photo_typing_and_escape(desktop):
    ov, page = desktop, desktop.page
    on(ov)
    page.keyboard.press("ArrowRight")
    ov.settle()
    ov.wait_tags(2)
    assert ov.labels() == ["Bob"], "the next photo's tags (Memories' currentPhoto lags here)"
    page.mouse.click(*ov.to_screen(0.6, 0.8))
    ov.wait_picker()
    assert [c.path for c in ov.calls if c.path.endswith("/locate")] == ["/file/2/locate"]
    page.keyboard.type("Zed")
    page.wait_for_timeout(300)
    assert page.locator(".facetag-search input").input_value() == "Zed", "focus trap took the name box's focus"
    page.keyboard.press("Escape")
    assert ov.is_on() and not ov.picker_open()
    page.keyboard.press("Escape")
    assert not ov.is_on() and ov.stored() is None


def test_sticky_across_reload(desktop):
    ov, page = desktop, desktop.page
    on(ov)
    page.reload()
    page.wait_for_function("window.__ready")
    page.wait_for_selector(".facetag-layer")
    page.locator(".facetag-button").click()
    page.reload()
    page.wait_for_function("window.__ready")
    page.wait_for_timeout(500)
    assert not ov.is_on()


def test_phone_swipe_from_a_face_then_tap(phone):
    ov, page = phone, phone.page
    cdp = page.context.new_cdp_session(page)
    on(ov, tap=True)
    x, y = centre(ov, face(0))  # start the swipe on Alice's tag
    swipe(cdp, x + 150, x - 150, y)
    ov.settle()
    assert ov.current_fileid() == 2 and not ov.picker_open()
    ov.wait_tags(2)
    assert ov.labels() == ["Bob"]
    page.touchscreen.tap(*ov.to_screen(0.5, 0.8))
    ov.wait_picker()
    assert [c.path for c in ov.calls if c.path.endswith("/locate")] == ["/file/2/locate"]
    page.keyboard.type("Zed")
    assert page.locator(".facetag-search input").input_value() == "Zed"


def test_phone_ghost_click_and_tag_tap(phone):
    ov, page = phone, phone.page
    on(ov, tap=True)
    ov.api.delay = 0.3
    vw, vh = page.viewport_size["width"], page.viewport_size["height"]
    for x, y in ((vw * 0.5, vh * 0.55), (vw * 0.3, vh * 0.45)):
        page.touchscreen.tap(x, y)
        ov.wait_picker()
        assert not ov.saves()
        page.keyboard.press("Escape")
    page.touchscreen.tap(*centre(ov, face(0)))
    ov.wait_picker()
    page.wait_for_timeout(400)
    assert ov.picker_open() and not ov.saves(), "tapping a tag must not remove or change it"
    page.locator(".facetag-remove").tap()
    page.wait_for_selector(".facetag-picker", state="detached")
    assert [c.method + " " + c.path for c in ov.saves()] == ["DELETE /detection/1"]
    assert not ov.js_errors, ov.js_errors

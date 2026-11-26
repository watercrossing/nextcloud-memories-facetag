"""The tagging button and tagging mode: on/off, remembered across the viewer closing and a reload, Esc."""

import pytest

import stackctl as s
from conftest import BASE, open_photo, tagging_on
from viewer import click_control


@pytest.fixture
def group(photos) -> int:
    """The group photo where Recognize found the most faces."""
    groups = [fid for fid, p in photos.items() if p["kind"] == "group"]
    rows = s.sql(f"""SELECT file_id, count(*) AS n FROM oc_recognize_face_detections
                     WHERE user_id = '{s.TEST_USER}' AND file_id IN ({",".join(map(str, groups))})
                     GROUP BY file_id ORDER BY n DESC, file_id LIMIT 1""")
    return int(rows[0]["file_id"])


def test_button_toggles_tagging(desktop, group, photos):
    ov, page = desktop, desktop.page
    open_photo(ov, group, photos)
    button = page.locator(".memories-viewer .top-bar .facetag-button")
    assert button.is_visible() and button.get_attribute("aria-pressed") == "false"
    assert not ov.is_on()

    tagging_on(ov)
    assert ov.button_active() and ov.stored() == "1"
    assert page.locator(".facetag-tag").count() > 0, "Recognize's faces should be drawn"
    assert page.locator(".memories-viewer.facetag-on").count() == 1

    click_control(page, button)
    assert not ov.is_on() and not ov.button_active() and ov.stored() is None
    assert page.locator(".facetag-tag").count() == 0
    assert not ov.js_errors, ov.js_errors


def test_tagging_stays_on_across_closing_the_viewer_and_a_reload(desktop, group, photos):
    ov, page = desktop, desktop.page
    open_photo(ov, group, photos)
    tagging_on(ov)

    # Close the viewer with Memories' own Back button: the overlay goes with it
    click_control(page, page.locator(".memories-viewer .top-bar-left [aria-label='Back'], .memories-viewer [aria-label='Back']").first)
    page.wait_for_function("() => !globalThis._m.viewer.isOpen")
    assert not ov.is_on()

    # Reopen a photo from the folder: tagging is back on by itself
    open_photo(ov, group, photos)
    page.wait_for_selector(".facetag-layer")
    assert ov.button_active()
    ov.wait_tags()
    assert page.locator(".facetag-tag").count() > 0

    page.reload()
    page.wait_for_selector(".facetag-layer")
    assert ov.button_active()
    assert not ov.js_errors, ov.js_errors


def test_reopening_from_the_timeline_keeps_tagging_on(desktop, group, photos):
    """Close the viewer and open a photo by clicking it in the folder view (no page load in between)."""
    ov, page = desktop, desktop.page
    open_photo(ov, group, photos)
    tagging_on(ov)
    click_control(page, page.locator(".memories-viewer [aria-label='Back']").first)
    page.wait_for_function("() => !globalThis._m.viewer.isOpen")
    page.wait_for_selector(".img-outer")
    page.locator(".img-outer").first.click()  # a photo in the folder view
    page.wait_for_function("() => globalThis._m.viewer.isOpen")
    page.wait_for_selector(".facetag-layer")
    assert ov.button_active()


def test_escape_switches_tagging_off(desktop, group, photos):
    ov, page = desktop, desktop.page
    open_photo(ov, group, photos)
    tagging_on(ov)
    page.keyboard.press("Escape")
    assert not ov.is_on() and ov.stored() is None
    assert page.evaluate("() => globalThis._m.viewer.isOpen"), "Esc switches tagging off; the viewer stays"
    page.reload()
    page.wait_for_selector(".memories-viewer .top-bar .facetag-button")
    page.wait_for_timeout(1000)
    assert not ov.is_on(), "switched off stays off"
    assert BASE in page.url

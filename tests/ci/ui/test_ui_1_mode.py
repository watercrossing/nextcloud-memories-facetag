"""The tagging button and tagging mode: on/off, remembered across the viewer closing and a reload, Esc."""

import pytest

from conftest import BASE, open_photo, tagging_on


@pytest.fixture
def group(photos) -> int:
    """A group photo with several faces Recognize found."""
    return next(fid for fid, p in sorted(photos.items()) if p["file"].startswith("groups/G7Biarritz"))


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

    button.click()
    assert not ov.is_on() and not ov.button_active() and ov.stored() is None
    assert page.locator(".facetag-tag").count() == 0
    assert not ov.js_errors, ov.js_errors


def test_tagging_stays_on_across_closing_the_viewer_and_a_reload(desktop, group, photos):
    ov, page = desktop, desktop.page
    open_photo(ov, group, photos)
    tagging_on(ov)

    # Close the viewer with Memories' own Back button: the overlay goes with it
    page.locator(".memories-viewer .top-bar-left [aria-label='Back'], .memories-viewer [aria-label='Back']").first.click()
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
    page.locator(".memories-viewer [aria-label='Back']").first.click()
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

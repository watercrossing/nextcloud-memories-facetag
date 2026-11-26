"""The name picker: tap a face Recognize missed, pick a suggestion or type a name, change and remove tags."""

import json

import pytest

import stackctl as s
from conftest import detections, open_photo, tagging_on
from viewer import fold


def tap_missed_face(ov, fid: int, person: dict, photos) -> None:
    """Open the photo, tagging on, click the middle of the person's face, wait for the picker."""
    open_photo(ov, fid, photos)
    tagging_on(ov)
    x, y = ov.to_screen(person["x"], person["y"])
    assert "facetag" not in ov.element_at(x, y), "the face should be untagged"
    ov.page.mouse.click(x, y)
    ov.wait_picker()


@pytest.fixture
def missed_face(missed):
    """A big face Recognize missed (file id, ground-truth person)."""
    return missed[0]


def test_tap_a_missed_face_finds_it_and_suggests_the_person(desktop, missed, photos, people):
    ov, page = desktop, desktop.page
    tried = []
    # The biggest few faces Recognize missed: each one found and the right person first
    for fid, person in missed[:3]:
        tap_missed_face(ov, fid, person, photos)
        located = [c for c in ov.calls if c.path == f"/file/{fid}/locate"]
        assert located, f"no locate call for file {fid}: {ov.calls[-4:]}"
        found = page.locator(".facetag-square.found").count() == 1
        items = ov.items()
        tried.append((person["name"], found, items[:2]))
        assert found, f"{person['name']}: no face found where clicked; picker shows {items[:3]}"
        assert items and items[0]["suggested"] and items[0]["text"] == person["name"], \
            f"{person['name']} should be suggested first (★): {items[:4]}"
        # The square snapped onto the found face, which is the person's
        sq = page.locator(".facetag-square").bounding_box()
        r = ov.image_rect()
        box = {"x": (sq["x"] - r["x"]) / r["width"], "y": (sq["y"] - r["y"]) / r["height"],
               "width": sq["width"] / r["width"], "height": sq["height"] / r["height"]}
        assert s.matches(person, box), f"found square {box} is not on {person['name']}'s face"

        before = {d["id"] for d in detections(fid)}
        page.locator(".facetag-item").first.click()
        page.wait_for_selector(".facetag-picker", state="detached")
        new = [d for d in detections(fid) if d["id"] not in before]
        assert len(new) == 1, f"one new face row expected, got {new}"
        d = new[0]
        assert d["cluster_id"] == people[person["name"]] and d["source"] == "manual" and d["found_face"] is True
        assert any(v != 0 for v in json.loads(d["face_vector"])), "a located face keeps its own descriptor"
        assert s.matches(person, d)
        assert person["name"] in ov.labels()
    assert not ov.js_errors, ov.js_errors


def test_typing_filters_and_creates_a_person(desktop, missed_face, photos, people):
    ov, page = desktop, desktop.page
    fid, person = missed_face
    tap_missed_face(ov, fid, person, photos)
    box = page.locator(".facetag-search input")
    assert page.evaluate("() => document.activeElement?.matches('.facetag-search input')"), "the name box has focus"

    # Filter to existing people, accents and case folded
    # someone else, preferably with an accent in the name, typed without it
    others = sorted((n for n in people if n != person["name"]), key=lambda n: fold(n) == n.lower())
    other = others[0]
    query = fold(other)[:4]
    page.keyboard.type(query)
    page.wait_for_timeout(300)
    items = ov.items()
    assert items and all(query in fold(i["text"]) for i in items if not i["new"]), items
    assert other in [i["text"] for i in items]
    # PhotoSwipe's focus trap must not have taken focus back
    page.wait_for_timeout(1000)
    assert page.evaluate("() => document.activeElement?.matches('.facetag-search input')"), "focus lost while typing"
    assert box.input_value() == query

    # A new name
    box.fill("")
    page.keyboard.type("Ada Lovelace-Test")
    page.wait_for_timeout(300)
    items = ov.items()
    assert items[-1]["new"] and "Ada Lovelace-Test" in items[-1]["text"], items
    assert box.input_value() == "Ada Lovelace-Test", "every key reached the name box"
    # arrow keys move the selection inside the picker, not the photo
    page.keyboard.press("ArrowDown")
    assert ov.current_fileid() == fid
    page.locator(".facetag-item.new").click()
    page.wait_for_selector(".facetag-picker", state="detached")
    cluster = s.sql(f"SELECT id FROM oc_recognize_face_clusters WHERE user_id = '{s.TEST_USER}' AND title = 'Ada Lovelace-Test'")
    assert len(cluster) == 1
    d = [d for d in detections(fid) if d["cluster_id"] == cluster[0]["id"]]
    assert len(d) == 1 and d[0]["source"] == "manual"
    assert "Ada Lovelace-Test" in ov.labels()
    assert not ov.js_errors, ov.js_errors


def test_enter_picks_the_selected_name(desktop, missed_face, photos, people):
    ov, page = desktop, desktop.page
    fid, person = missed_face
    tap_missed_face(ov, fid, person, photos)
    page.keyboard.type(person["name"][:5])
    page.wait_for_timeout(200)
    page.keyboard.press("Enter")
    page.wait_for_selector(".facetag-picker", state="detached")
    assert any(d["cluster_id"] == people[person["name"]] and d["source"] == "manual" for d in detections(fid))


def test_change_then_remove_an_existing_tag(desktop, photos, people):
    ov, page = desktop, desktop.page
    # A face Recognize found and named, in a portrait
    fid, d, who = next((fid, d, d["title"]) for fid, p in sorted(photos.items()) if p["kind"] == "portrait"
                       for d in detections(fid) if d["title"] and s.identity_of(p, d) == d["title"])
    open_photo(ov, fid, photos)
    tagging_on(ov)
    tag = next(t for t in ov.tags() if t["id"] == d["id"])
    page.mouse.click(tag["x"] + tag["width"] / 2, tag["y"] + tag["height"] / 2)
    ov.wait_picker()
    assert who in page.locator(".facetag-current").inner_text()
    assert page.locator(".facetag-remove").inner_text() == "Remove tag"
    assert who not in [i["text"] for i in ov.items()], "the current person is not offered again"

    other = next(n for n in people if n != who)
    page.keyboard.type(other)
    page.wait_for_timeout(200)
    page.locator(".facetag-item", has_text=other).first.click()
    page.wait_for_selector(".facetag-picker", state="detached")
    row = next(x for x in detections(fid) if x["id"] == d["id"])
    assert row["cluster_id"] == people[other] and row["source"] is None
    assert other in ov.labels()

    # Remove: a face Recognize found leaves its person and stays as an unnamed face
    tag = next(t for t in ov.tags() if t["id"] == d["id"])
    page.mouse.click(tag["x"] + tag["width"] / 2, tag["y"] + tag["height"] / 2)
    ov.wait_picker()
    page.locator(".facetag-remove").click()
    page.wait_for_selector(".facetag-picker", state="detached")
    row = next(x for x in detections(fid) if x["id"] == d["id"])
    assert row["cluster_id"] is None and float(row["threshold"]) > 0
    assert "Who is this?" in ov.labels()
    assert not ov.js_errors, ov.js_errors


def test_escape_closes_the_picker_first(desktop, missed_face, photos):
    ov, page = desktop, desktop.page
    fid, person = missed_face
    tap_missed_face(ov, fid, person, photos)
    page.keyboard.press("Escape")
    assert not ov.picker_open() and ov.is_on(), "first Esc closes the picker, tagging stays on"
    page.keyboard.press("Escape")
    assert not ov.is_on()
    assert not ov.saves()

"""Tags land on the faces: on-screen boxes against the photo set's ground truth, also after zooming."""

import pytest

import stackctl as s
from conftest import detections, open_photo, tagging_on


def groups_with_faces(photos, n: int) -> list[int]:
    """Group photos where Recognize named the most ground-truth faces."""
    scored = []
    for fid, p in photos.items():
        if p["kind"] != "group":
            continue
        named = [d for d in detections(fid) if d["title"] and s.identity_of(p, d) == d["title"]]
        scored.append((len(named), fid))
    return [fid for _, fid in sorted(scored, reverse=True)[:n]]


def check_tags(ov, fid: int, photo: dict) -> int:
    """Every drawn tag is on its detection's box, and named tags are on the right person's face.
    Returns how many tags were checked against the ground truth."""
    rect = ov.image_rect()
    assert rect["height"] > 50 and rect["width"] > 50, f"image box {rect}: measured the wrong element?"
    aspect = rect["width"] / rect["height"]
    assert abs(aspect / ov.natural_aspect() - 1) < 0.02, f"image box {rect} has not the photo's aspect"
    dets = {d["id"]: d for d in detections(fid)}
    tags = ov.tags()
    assert {t["id"] for t in tags} == set(dets), "every face of the photo should be drawn, and nothing else"
    checked = 0
    for t in tags:
        d = dets[t["id"]]
        # exactly where the detection says, in the measured image box
        for k, off in (("x", "x"), ("y", "y")):
            want = rect[off] + float(d[k]) * rect["width" if k == "x" else "height"]
            assert abs(t[k] - want) <= 2, f"tag {t} not at its detection {d['x']},{d['y']}"
        assert abs(t["width"] - float(d["width"]) * rect["width"]) <= 2
        # ...and that is the person's face in the ground truth
        who = s.identity_of(photo, d)
        if who and d["title"]:
            truth = next(p for p in photo["people"] if p["name"] == who)
            cx, cy = t["x"] + t["width"] / 2, t["y"] + t["height"] / 2
            tx, ty = rect["x"] + truth["x"] * rect["width"], rect["y"] + truth["y"] * rect["height"]
            reach = 0.6 * max(truth["size"] * rect["width"], t["width"], t["height"])
            assert ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5 <= reach, f"{who}'s tag is not on their face"
            assert t["label"] == d["title"] == who, f"tag on {who}'s face says {t['label']!r}"
            checked += 1
    return checked


@pytest.mark.parametrize("device", ["desktop", "phone"])
def test_tags_land_on_the_faces(device, request, photos):
    ov = request.getfixturevalue(device)
    checked = 0
    for fid in groups_with_faces(photos, 3):
        open_photo(ov, fid, photos)
        tagging_on(ov, tap=device == "phone")
        checked += check_tags(ov, fid, photos[fid])
    assert checked >= 6, f"only {checked} named faces checked"
    assert not ov.js_errors, ov.js_errors


def test_tags_follow_the_image_when_zooming(desktop, photos):
    ov, page = desktop, desktop.page
    for fid in groups_with_faces(photos, 6):
        open_photo(ov, fid, photos)
        # PhotoSwipe zooms up to the size Memories loads; some photos are no bigger than the screen
        if page.evaluate("() => { const z = globalThis._m.viewer.photoswipe.currSlide.zoomLevels; return z.max / z.fit }") >= 2:
            break
    else:
        pytest.fail("no group photo PhotoSwipe can zoom into 2×")
    tagging_on(ov)
    before = ov.image_rect()
    # Wheel zoom (Memories turns on PhotoSwipe's wheelToZoom), centred on a face
    d = next(d for d in detections(fid) if d["title"])
    x, y = ov.to_screen(float(d["x"]) + float(d["width"]) / 2, float(d["y"]) + float(d["height"]) / 2)
    page.mouse.move(x, y)
    for _ in range(4):
        page.mouse.wheel(0, -300)
        page.wait_for_timeout(60)
    ov.settle()
    after = ov.image_rect()
    assert after["width"] > 1.3 * before["width"], f"did not zoom: {before} → {after}"
    check_tags(ov, fid, photos[fid])

    # Pan by dragging, then zoom back out: still on the faces
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 120, y - 60, steps=8)
    page.mouse.up()
    ov.settle()
    assert ov.image_rect()["x"] != after["x"], "did not pan"
    check_tags(ov, fid, photos[fid])
    assert not ov.picker_open(), "a drag is not a tap"

    page.mouse.wheel(0, 1200)
    ov.settle()
    check_tags(ov, fid, photos[fid])

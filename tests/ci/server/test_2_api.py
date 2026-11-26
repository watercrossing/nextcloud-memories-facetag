"""The HTTP API, as the test user with an app password, checked against the database after each call."""

import json

import pytest

import stackctl as s
from conftest import box_around, detections

NEW_PERSON = "Test Person CI"


@pytest.fixture(scope="module")
def recognized(photos, people):
    """(file id, person, detection) for faces Recognize found and name_clusters.py named, in portraits."""
    out = []
    for fid, photo in photos.items():
        if photo["kind"] != "portrait":
            continue
        p = photo["people"][0]
        for d in detections(fid):
            if s.matches(p, d) and d["cluster_id"] == people.get(p["name"]) and d["source"] is None:
                out.append((fid, p, d))
    assert len(out) >= 20, f"only {len(out)} named Recognize faces in portraits"
    return out


@pytest.fixture(scope="module")
def located(api, missed, people):
    """locate() on every ground-truth face Recognize missed (of people who are named)."""
    results = []
    for fid, p in missed:
        if p["name"] not in people:
            continue
        r = api("POST", f"/file/{fid}/locate", box_around(p))
        assert r.status == 200, r.body
        results.append((fid, p, r.json()))
    return results


def test_file_lists_detections_and_people(api, recognized, people):
    fid, p, d = recognized[0]
    r = api("GET", f"/file/{fid}")
    assert r.status == 200, r.body
    body = r.json()
    mine = [x for x in body["detections"] if x["id"] == d["id"]]
    assert mine and mine[0]["name"] == p["name"] and mine[0]["clusterId"] == people[p["name"]] and not mine[0]["manual"]
    assert set(people) <= {x["name"] for x in body["people"]}


def test_errors(api, other_api, recognized):
    fid, _, d = recognized[0]
    assert api("GET", "/file/999999999").status == 404
    assert api("POST", f"/file/{fid}/locate", {"x": 0.5, "y": 0.5, "width": 0, "height": 0.1}).status == 400
    assert api("POST", f"/file/{fid}/tags", {"x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}).status == 400  # nobody named
    # Another user sees neither alice's photos nor her faces
    assert other_api("GET", f"/file/{fid}").status == 404
    assert other_api("PUT", f"/detection/{d['id']}", {"name": "x"}).status == 404
    assert other_api("DELETE", f"/detection/{d['id']}").status == 404


def test_locate_finds_missed_faces_and_suggests_the_right_person(located):
    assert len(located) >= 5, f"only {len(located)} missed ground-truth faces to locate"
    found = [x for x in located if x[2]["face"]]
    top1 = [x for x in found if x[2]["suggestions"] and x[2]["suggestions"][0]["name"] == x[1]["name"]]
    top5 = [x for x in found if x[1]["name"] in [sg["name"] for sg in x[2]["suggestions"]]]
    print(f"\nlocate on {len(located)} faces Recognize missed: face found {len(found)}, "
          f"right person first {len(top1)}, in the top 5 {len(top5)}")
    for fid, p, r in located:
        print(f"  {fid} {p['name']:22} face={'yes' if r['face'] else 'no ':3} "
              f"suggested={[(x['name'], x['distance']) for x in r['suggestions'][:3]]}")
    # On the pinned versions all were found, 58 of 63 named right first, all in the top 5 (Nov 2026)
    assert len(found) >= 0.9 * len(located)
    assert len(top5) >= 0.9 * len(found)
    assert len(top1) >= 0.85 * len(found)


def test_tag_a_located_face(api, located, people):
    fid, p, r = next(x for x in located if x[2]["face"] and x[2]["suggestions"] and x[2]["suggestions"][0]["name"] == x[1]["name"])
    before = {d["id"] for d in detections(fid)}
    box = box_around(p, p["size"] * 1.5)  # a sloppy tap; the located face's box should win
    t = api("POST", f"/file/{fid}/tags", {**box, "token": r["token"], "clusterId": people[p["name"]]})
    assert t.status == 200, t.body
    new = [d for d in detections(fid) if d["id"] not in before]
    assert len(new) == 1 and new[0]["id"] == t.json()["id"]
    d = new[0]
    assert d["cluster_id"] == people[p["name"]] and d["source"] == "manual" and d["found_face"] is True
    assert any(v != 0 for v in json.loads(d["face_vector"])), "a located face should keep its own descriptor"
    assert abs(d["x"] - r["face"]["x"]) < 1e-6 and abs(d["width"] - r["face"]["width"]) < 1e-6, "box should be the located face's"
    listed = [x for x in t.json()["detections"] if x["id"] == d["id"]]
    assert listed and listed[0]["name"] == p["name"] and listed[0]["manual"] is True


def test_tag_a_new_person_without_a_face(api, recognized):
    fid = recognized[1][0]
    t = api("POST", f"/file/{fid}/tags", {"x": 0.0, "y": 0.9, "width": 0.08, "height": 0.08, "name": NEW_PERSON})
    assert t.status == 200, t.body
    tid = t.json()["id"]
    cluster = s.sql(f"SELECT id, title FROM oc_recognize_face_clusters WHERE user_id = '{s.TEST_USER}' AND title = '{NEW_PERSON}'")
    assert len(cluster) == 1
    d = next(d for d in detections(fid) if d["id"] == tid)
    assert d["cluster_id"] == cluster[0]["id"] and d["source"] == "manual" and d["found_face"] is False
    assert not any(json.loads(d["face_vector"])), "no face, brand-new person: a zero vector"
    # The same name in other case is the same person
    again = api("PUT", f"/detection/{tid}", {"name": NEW_PERSON.upper()})
    assert again.status == 200
    assert s.scalar(f"SELECT count(*) FROM oc_recognize_face_clusters WHERE user_id = '{s.TEST_USER}' AND lower(title) = lower('{NEW_PERSON}')") == 1


def test_suggest_for_a_recognized_face(api, recognized):
    right = 0
    for fid, p, d in recognized[:10]:
        r = api("GET", f"/detection/{d['id']}/suggest")
        assert r.status == 200, r.body
        sg = r.json()["suggestions"]
        right += bool(sg) and sg[0]["name"] == p["name"]
    assert right >= 8, f"right person first for only {right} of 10 named faces"


def test_reassign_then_remove_a_recognized_face(api, recognized, people):
    fid, p, d = recognized[2]
    other = next(n for n in people if n != p["name"])
    r = api("PUT", f"/detection/{d['id']}", {"clusterId": people[other]})
    assert r.status == 200, r.body
    row = next(x for x in detections(fid) if x["id"] == d["id"])
    assert row["cluster_id"] == people[other] and row["threshold"] == 0 and row["source"] is None

    # Removing a face Recognize found takes it out of its person, Recognize's way, and keeps it
    r = api("DELETE", f"/detection/{d['id']}")
    assert r.status == 200, r.body
    row = next((x for x in detections(fid) if x["id"] == d["id"]), None)
    assert row is not None, "a face Recognize found is not deleted"
    assert row["cluster_id"] is None and row["threshold"] > 0 and row["source"] is None

    # Put it back for the tests after this
    assert api("PUT", f"/detection/{d['id']}", {"clusterId": people[p['name']]}).status == 200


def test_remove_a_manual_tag(api, recognized):
    fid = recognized[1][0]
    tid = next(d["id"] for d in detections(fid) if d["source"] == "manual")
    r = api("DELETE", f"/detection/{tid}")
    assert r.status == 200, r.body
    assert not any(d["id"] == tid for d in detections(fid))
    assert s.scalar(f"SELECT count(*) FROM oc_facetag_manual WHERE detection_id = {tid}") == 0

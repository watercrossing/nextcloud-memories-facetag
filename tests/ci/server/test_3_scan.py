"""The library scan via occ: settle time, dry run, a real run, re-run, --recheck, --undo."""

import json

import pytest

import stackctl as s
from conftest import counts, scan


@pytest.fixture(scope="module")
def before():
    c = counts()
    assert c["scan"] == c["scanknn"] == c["scanned"] == 0, f"scan rows before the scan tests: {dict(c)}"
    return c


@pytest.fixture(scope="module")
def dry(before):
    stats, out = scan("--settle-hours", "0", "--dry-run", "-v")
    assert stats.get("exit") == 0 and stats, out
    return stats


@pytest.fixture(scope="module")
def real(dry):
    stats, out = scan("--settle-hours", "0", "-v")
    assert stats.get("exit") == 0 and stats, out
    print(out[-2000:])
    return stats


def test_freshly_indexed_photos_wait_for_recognize(before):
    # Everything was indexed minutes ago: the default 24 h settle time leaves it all to Recognize
    stats, out = scan()
    assert stats["photos"] == 0, out


def test_dry_run_reports_and_writes_nothing(before, dry, photos):
    assert dry["photos"] == len(photos) and dry["errors"] == 0, dry
    assert dry["found"] > 0, "the scan should find faces Recognize missed (it looks at 2048 px, from score 0.8)"
    assert counts() == before


def test_real_run_adds_what_the_dry_run_promised(before, dry, real, photos):
    assert {k: real[k] for k in ("photos", "found", "added", "named", "errors")} == \
           {k: dry[k] for k in ("photos", "found", "added", "named", "errors")}, (dry, real)
    c = counts()
    assert c["scanned"] == len(photos)
    assert c["scan"] + c["scanknn"] == real["added"]
    assert c["scanknn"] == real["named"]
    assert c["detections"] == before["detections"] + real["added"]
    assert c["manual"] == before["manual"]
    jobs = s.sql("SELECT argument FROM oc_jobs WHERE class = 'OCA\\Recognize\\BackgroundJobs\\ClusterFacesJob'")
    assert any(json.loads(j["argument"]).get("userId") == s.TEST_USER for j in jobs), "clustering should be queued"


def test_scan_names_faces_correctly(real, photos):
    rows = s.sql(f"""SELECT d.file_id, d.x, d.y, d.width, d.height, c.title FROM oc_facetag_manual m
                     JOIN oc_recognize_face_detections d ON d.id = m.detection_id
                     JOIN oc_recognize_face_clusters c ON c.id = d.cluster_id
                     WHERE m.source = 'scanknn' AND d.user_id = '{s.TEST_USER}'""")
    judged = [(r["title"], s.identity_of(photos[r["file_id"]], r)) for r in rows]
    known = [(named, truth) for named, truth in judged if truth]
    wrong = [(named, truth) for named, truth in known if named != truth]
    print(f"\nscan named {len(rows)} faces; {len(known)} are ground-truth faces, {len(wrong)} named wrong: {wrong}")
    assert not wrong


def test_second_run_has_nothing_left(real):
    stats, out = scan("--settle-hours", "0")
    assert stats["photos"] == 0, out


def test_recheck(real):
    named_by_scan = s.scalar("""SELECT count(*) FROM oc_facetag_manual m JOIN oc_recognize_face_detections d ON d.id = m.detection_id
                                JOIN oc_recognize_face_clusters c ON c.id = d.cluster_id
                                WHERE m.source IN ('scan', 'scanknn') AND c.title <> ''""")
    r = s.occ("memories_facetag:scan", "--recheck", "--dry-run").stdout
    assert f"Checked {named_by_scan} faces; would take out" in r, r
    before = counts()
    r = s.occ("memories_facetag:scan", "--recheck").stdout
    assert f"Checked {named_by_scan} faces; took out" in r, r
    assert counts()["detections"] == before["detections"], "recheck takes faces out of people; it deletes nothing"
    # Straight after a recheck there is nothing more to take out
    again = s.occ("memories_facetag:scan", "--recheck").stdout
    assert "took out 0" in again, again


def test_undo(real):
    before = counts()
    kept = s.scalar("""SELECT count(*) FROM oc_facetag_manual m JOIN oc_recognize_face_detections d ON d.id = m.detection_id
                       JOIN oc_recognize_face_clusters c ON c.id = d.cluster_id
                       WHERE m.source = 'scan' AND c.title <> ''""")
    r = s.occ("memories_facetag:scan", "--undo").stdout
    deleted = before["scan"] + before["scanknn"] - kept
    assert f"Deleted {deleted} scanned faces; kept {kept}" in r, r
    c = counts()
    assert c["scanknn"] == 0 and c["scan"] == kept and c["scanned"] == 0
    assert c["manual"] == before["manual"], "undo leaves hand-placed tags alone"
    assert c["detections"] == before["detections"] - deleted

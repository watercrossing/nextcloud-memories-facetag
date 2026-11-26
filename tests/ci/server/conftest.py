"""Fixtures for the server-side tests against the stack `tests/ci/stack.sh up && dataset` built.

The modules run in file order and share the stack: test_1_* reads, test_2_* tags through the
API, test_3_* scans, test_4_* flips the version guard. Each module cleans up what it must for the
next one.
"""

from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stackctl as s  # noqa: E402


def pytest_collection_modifyitems(items):
    items.sort(key=lambda i: i.fspath.basename)  # module order is part of the design


@pytest.fixture(scope="session")
def password() -> str:
    out = s.occ("user:auth-tokens:add", "--name=facetag-ci", s.TEST_USER).stdout
    return out.strip().splitlines()[-1]


class Api:
    def __init__(self, user: str, password: str):
        self.user, self.password = user, password

    def __call__(self, method: str, path: str, data: dict | None = None) -> s.Response:
        return s.http(method, "/apps/memories_facetag/api" + path, self.user, self.password, data)


@pytest.fixture(scope="session")
def api(password) -> Api:
    return Api(s.TEST_USER, password)


@pytest.fixture(scope="session")
def other_api() -> Api:
    """A second user, who can see none of alice's photos."""
    s.run(["exec", "-T", "-u", "www-data", "-e", "OC_PASS=other-user-throwaway-9f3a", "nextcloud",
           "php", "occ", "user:add", "--password-from-env", "carol"], check=False)
    out = s.occ("user:auth-tokens:add", "carol").stdout
    return Api("carol", out.strip().splitlines()[-1])


@pytest.fixture(scope="session")
def photos() -> dict[int, dict]:
    """Nextcloud file id → manifest entry."""
    ids = s.file_ids()
    return {ids[p["file"]]: p for p in s.manifest()["photos"]}


@pytest.fixture(scope="session")
def people() -> dict[str, int]:
    """Ground-truth person → their cluster id (as name_clusters.py named them; the largest if several).
    Other named clusters — test_2 leaves an empty one — are left out, so reruns on one stack work."""
    rows = s.sql(f"""SELECT c.id, c.title, count(d.id) AS n FROM oc_recognize_face_clusters c
                     LEFT JOIN oc_recognize_face_detections d ON d.cluster_id = c.id
                     WHERE c.user_id = '{s.TEST_USER}' AND c.title <> '' GROUP BY c.id, c.title ORDER BY n""")
    identities = set(s.manifest()["identities"].values())
    return {r["title"]: int(r["id"]) for r in rows if r["title"] in identities}


def detections(file_id: int) -> list[dict]:
    return s.sql(f"""SELECT d.id, d.x, d.y, d.width, d.height, d.cluster_id, d.threshold, d.face_vector,
                            m.source, m.found_face
                     FROM oc_recognize_face_detections d LEFT JOIN oc_facetag_manual m ON m.detection_id = d.id
                     WHERE d.user_id = '{s.TEST_USER}' AND d.file_id = {file_id} ORDER BY d.id""")


@pytest.fixture(scope="session")
def missed(photos) -> list[tuple[int, dict]]:
    """Ground-truth faces Recognize did not find: (file id, person), with that person not tagged on the photo."""
    out = []
    for fid, photo in photos.items():
        dets = detections(fid)
        for p in photo["people"]:
            if not any(s.matches(p, d) for d in dets):
                out.append((fid, p))
    return out


def box_around(p: dict, side: float | None = None) -> dict:
    """A tag box centred on a ground-truth face, as a user's tap would place it."""
    side = side or p["size"]
    return {"x": max(0.0, p["x"] - side / 2), "y": max(0.0, p["y"] - side / 2), "width": side, "height": side}


DONE = re.compile(r"Done: (\d+) photos, (\d+) new faces, (\d+) detection rows (?:would be )?added \((\d+) named by the scan\), (\d+) errors, (\d+) photos left")


def scan(*args: str, check: bool = True) -> tuple[dict, str]:
    """Run the scan; parse its summary line."""
    r = s.occ("memories_facetag:scan", *args, check=check)
    out = r.stdout + r.stderr
    m = DONE.search(out)
    stats = dict(zip(("photos", "found", "added", "named", "errors", "left"), map(int, m.groups()))) if m else {}
    stats["exit"] = r.returncode
    return stats, out


def counts() -> dict[str, int]:
    rows = s.sql("SELECT source, count(*) AS n FROM oc_facetag_manual GROUP BY source")
    c = collections.Counter({r["source"]: int(r["n"]) for r in rows})
    c["scanned"] = int(s.scalar("SELECT count(*) FROM oc_facetag_scanned"))
    c["detections"] = int(s.scalar(f"SELECT count(*) FROM oc_recognize_face_detections WHERE user_id = '{s.TEST_USER}'"))
    return c

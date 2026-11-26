"""The stack as the other tests need it: the app's self-test, Recognize's results, named people."""

import os

import stackctl as s


def test_self_check_passes():
    r = s.occ("memories_facetag:check", check=False)
    assert r.returncode == 0, f"occ memories_facetag:check failed:\n{r.stdout}{r.stderr}"
    assert r.stdout.startswith("OK")


def test_installed_versions_are_the_requested_ones():
    apps = s.occ("app:list", "--output=json").stdout
    for app, env in (("memories", "MEMORIES_VERSION"), ("recognize", "RECOGNIZE_VERSION")):
        if os.environ.get(env):
            assert f'"{app}":"{os.environ[env]}"' in apps.replace(" ", ""), f"{app} is not {os.environ[env]}: {apps}"


def test_recognize_queue_is_drained():
    # The scan skips files still in Recognize's queue
    assert s.scalar("SELECT count(*) FROM oc_recognize_queue_faces") == 0


def test_recognize_found_and_clustered_faces(photos):
    n = s.scalar(f"SELECT count(*) FROM oc_recognize_face_detections WHERE user_id = '{s.TEST_USER}'")
    assert n >= 120, f"Recognize found {n} faces; it clusters only from 120"
    assert len(photos) == len(s.manifest()["photos"])


def test_every_identity_is_a_named_person(people):
    expected = set(s.manifest()["identities"].values())
    missing = expected - set(people)
    # A person Recognize found too few faces of may stay unclustered; most must be there
    assert len(missing) <= 2, f"not named after clustering: {sorted(missing)}"

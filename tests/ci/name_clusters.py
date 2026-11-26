#!/usr/bin/env python3
"""Name Recognize's face clusters from the photo set's ground truth, as a user would in Memories.

Each of the test user's detections is matched to a ground-truth person by position
(stackctl.identity_of). A cluster whose matched detections are mostly one person gets that
person's name (recognize_face_clusters.title). Prints a table and fails if Recognize made no
clusters or no cluster could be named.
"""

from __future__ import annotations

import collections
import sys

import stackctl as s


def main() -> None:
    photos = {p["file"]: p for p in s.manifest()["photos"]}
    ids = s.file_ids()
    by_id = {ids[f]: photos[f] for f in photos if f in ids}
    missing = [f for f in photos if f not in ids]
    if missing:
        sys.exit(f"{len(missing)} photos of the manifest are not in Nextcloud, e.g. {missing[:3]}")

    dets = s.sql(f"SELECT id, file_id, x, y, width, height, cluster_id FROM oc_recognize_face_detections WHERE user_id = '{s.TEST_USER}'")
    clusters = s.sql(f"SELECT id FROM oc_recognize_face_clusters WHERE user_id = '{s.TEST_USER}'")
    print(f"Recognize: {len(dets)} detections, {sum(1 for d in dets if d['cluster_id'])} clustered, {len(clusters)} clusters")
    if not clusters:
        sys.exit("Recognize made no clusters (it needs 120 detections of at least 3% of the image)")

    votes: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    found = collections.Counter()
    for d in dets:
        who = s.identity_of(by_id[d["file_id"]], d) if d["file_id"] in by_id else None
        if who:
            found[who] += 1
        if d["cluster_id"]:
            votes[d["cluster_id"]][who or "(unnamed)"] += 1

    named = 0
    print(f"{'cluster':>8}  {'size':>4}  name")
    for cid, v in sorted(votes.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(v.values())
        who, n = next(((w, n) for w, n in v.most_common() if w != "(unnamed)"), (None, 0))
        name = who if who and n >= 2 and n / total >= 0.5 else ""
        if name:
            s.execute(f"UPDATE oc_recognize_face_clusters SET title = '{name.replace(chr(39), chr(39) * 2)}' WHERE id = {cid}")
            named += 1
        print(f"{cid:>8}  {total:>4}  {name or '-'}  {dict(v.most_common(4))}")
    expected = collections.Counter(p["name"] for ph in photos.values() for p in ph["people"])
    print("Ground-truth faces Recognize found: " + ", ".join(f"{k} {found[k]}/{expected[k]}" for k in sorted(expected)))
    if not named:
        sys.exit("No cluster could be named from the ground truth")
    print(f"Named {named} clusters")


if __name__ == "__main__":
    main()

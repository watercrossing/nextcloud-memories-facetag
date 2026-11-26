#!/usr/bin/env python3
"""Offline evaluation of the app's face-distance limits, for re-tuning them when Recognize's face
model changes (tests/ci/server/test_1_model.py fails then).

    # the CI stack's photo set (ground-truth identities; few faces, no meaningful dates)
    uv run --no-project --with numpy python tests/ui-harness/eval_suggestions.py --stack
    # a real library: an export of named faces (much better for tuning)
    uv run --no-project --with numpy python tests/ui-harness/eval_suggestions.py faces.txt clusters.txt

The export is one line per named face, `id|cluster_id|file_id|epoch datetaken|source|found_face|face_vector`,
plus `cluster_id|title` per person. Export only Recognize's and hand-placed faces (source not
`scan`/`scanknn`), or the scan's guesses pollute the ground truth; delete the export afterwards.
For example (PostgreSQL, Nextcloud's oc_ prefix, one user; `psql -qtA` writes `|`-separated rows):

    SELECT d.id, d.cluster_id, d.file_id, coalesce(extract(epoch FROM m.datetaken)::bigint, 0),
           coalesce(f.source, ''), CASE WHEN coalesce(f.found_face, true) THEN 't' ELSE 'f' END, d.face_vector
    FROM oc_recognize_face_detections d JOIN oc_recognize_face_clusters c ON c.id = d.cluster_id
    LEFT JOIN oc_facetag_manual f ON f.detection_id = d.id LEFT JOIN oc_memories m ON m.fileid = d.file_id
    WHERE d.user_id = '<user>' AND c.title <> '' AND coalesce(f.source, '') NOT IN ('scan', 'scanknn')

What it reports, leave-one-out over a sample of faces:
- suggestions (FaceStore::suggest): top-1/top-3 for the mean distance to a person's K=5 nearest
  faces, padded, excluding people already on the photo, with the time-context bonus at several
  windows and weights (the app: 2 days, 0.04 × ln(1 + photos)); and how often the right person
  scores under the picker's cut-off (js/facetag.js SUGGEST_MAX_SCORE 0.55);
- the scan's naming rule (Scanner assignDistance 0.40, assignMargin 0.10) at a grid of limits:
  how many faces it would name, and how many of those wrongly.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import numpy as np

K = 5
REFS_PER_PERSON = 60  # FaceStore::SAMPLE_PER_PERSON
PAD = 0.65
SUGGEST_MAX_SCORE = 0.55


def load_export(faces: str, clusters: str):
    rows = [line.rstrip("\n").split("|", 6) for line in open(faces) if line.strip()]
    return (np.array([int(r[0]) for r in rows]), np.array([int(r[1]) for r in rows]),
            np.array([int(r[2]) for r in rows]), np.array([int(r[3]) for r in rows]),
            np.array([r[5] == "t" for r in rows]), np.array([json.loads(r[6]) for r in rows], dtype=np.float32))


def load_stack():
    """Recognize's own detections of the CI photo set's ground-truth faces; people from the manifest."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci"))
    import stackctl as s
    by_file = {p["file"]: p for p in s.manifest()["photos"]}
    photos = {fid: by_file[f] for f, fid in s.file_ids().items() if f in by_file}
    rows = s.sql(f"""SELECT d.id, d.file_id, d.x, d.y, d.width, d.height, d.face_vector,
                            coalesce(extract(epoch FROM m.datetaken)::bigint, 0) AS ts
                     FROM oc_recognize_face_detections d
                     LEFT JOIN oc_facetag_manual f ON f.detection_id = d.id
                     LEFT JOIN oc_memories m ON m.fileid = d.file_id
                     WHERE d.user_id = '{s.TEST_USER}' AND f.detection_id IS NULL""")
    out, person = [], {}
    for r in rows:
        photo = photos.get(int(r["file_id"]))
        who = photo and s.identity_of(photo, r)
        if who:
            out.append((int(r["id"]), person.setdefault(who, len(person) + 1), int(r["file_id"]), int(r["ts"]),
                        json.loads(r["face_vector"])))
    a = list(zip(*out))
    return (np.array(a[0]), np.array(a[1]), np.array(a[2]), np.array(a[3]), np.ones(len(out), bool),
            np.array(a[4], dtype=np.float32))


def main() -> None:
    if sys.argv[1:] == ["--stack"]:
        ids, cl, fid, ts, real, V = load_stack()
    elif len(sys.argv) == 3:
        ids, cl, fid, ts, real, V = load_export(sys.argv[1], sys.argv[2])
    else:
        sys.exit(__doc__)
    real &= np.abs(V).sum(1) > 0
    # Reference set as in production: the newest REFS_PER_PERSON real faces per person
    refmask, seen = np.zeros(len(ids), bool), collections.Counter()
    for i in np.argsort(-ids):
        if real[i] and seen[cl[i]] < REFS_PER_PERSON:
            refmask[i] = True
            seen[cl[i]] += 1
    people = sorted(set(cl.tolist()))
    pidx = {c: k for k, c in enumerate(people)}
    candidates = np.where(real)[0]
    tests = list(np.random.default_rng(1).choice(candidates, min(1500, len(candidates)), replace=False))
    on_file = collections.defaultdict(list)
    for i in range(len(ids)):
        on_file[fid[i]].append(i)
    print(f"{len(ids)} named faces of {len(people)} people; {len(tests)} scored leave-one-out\n")

    def knn(i, pad=None):
        m = refmask.copy()
        m[i] = False
        d = np.linalg.norm(V[m] - V[i], axis=1)
        c = cl[m]
        out = np.full(len(people), np.inf)
        for p in people:
            dp = np.sort(d[c == p])[:K]
            if len(dp) == 0:
                continue
            if pad is not None and len(dp) < K:
                dp = np.concatenate([dp, np.full(K - len(dp), pad)])
            out[pidx[p]] = dp.mean()
        return out

    def exclude_others_on_photo(i, s):
        s = s.copy()
        for j in on_file[fid[i]]:
            if j != i and cl[j] != cl[i]:
                s[pidx[cl[j]]] = np.inf
        return s

    def context(i, window):
        if not ts[i]:
            return np.zeros(len(people))
        near = (np.abs(ts - ts[i]) <= window) & (fid != fid[i]) & (ts > 0)
        cnt = collections.Counter()
        for f in set(fid[near].tolist()):
            for p in {cl[j] for j in on_file[f]}:
                cnt[p] += 1
        return np.array([cnt[p] for p in people], float)

    base = {i: knn(i) for i in tests}
    padded = {i: knn(i, PAD) for i in tests}
    windows = [(6 * 3600, "6h"), (2 * 86400, "2d"), (7 * 86400, "7d")]
    ctx = {w: {i: context(i, w) for i in tests} for w, _ in windows}

    def evaluate(name, score, subset):
        t1 = t3 = under = 0
        for i in subset:
            s = score(i)
            rank = np.argsort(s)
            truth = pidx[cl[i]]
            t1 += rank[0] == truth
            t3 += truth in rank[:3]
            under += s[truth] < SUGGEST_MAX_SCORE
        n = len(subset)
        print(f"  {name:50s} top1 {100 * t1 / n:5.1f}%  top3 {100 * t3 / n:5.1f}%  right one < {SUGGEST_MAX_SCORE}: {100 * under / n:5.1f}%")

    hard = [i for i in tests if np.sort(base[i])[0] > 0.42]
    for label, subset in (("all", tests), ("hard: nearest person further than 0.42", hard)):
        if not subset:
            continue
        print(f"--- suggestions, {label} (n={len(subset)})")
        evaluate(f"mean of the {K} nearest", lambda i: base[i], subset)
        evaluate(f"padded to {K} with {PAD}", lambda i: padded[i], subset)
        evaluate("padded, people already on the photo excluded", lambda i: exclude_others_on_photo(i, padded[i]), subset)
        if ts[subset].any():
            for w, wl in windows:
                for a in (0.02, 0.04, 0.08):
                    evaluate(f"... + context {wl}, {a} × ln(1 + photos)",
                             lambda i, w=w, a=a: exclude_others_on_photo(i, padded[i]) - a * np.log1p(ctx[w][i]), subset)
        else:
            print("  (no dates: context bonus not evaluated)")

    print(f"\n--- scan naming: nearest person within D, the next at least M further (n={len(tests)})")
    for D in (0.35, 0.40, 0.45, 0.50):
        line = []
        for M in (0.05, 0.10, 0.15):
            named = wrong = 0
            for i in tests:
                s = exclude_others_on_photo(i, base[i])
                order = np.argsort(s)
                if s[order[0]] <= D and (len(order) < 2 or s[order[1]] - s[order[0]] >= M):
                    named += 1
                    wrong += order[0] != pidx[cl[i]]
            line.append(f"M={M:.2f}: names {100 * named / len(tests):5.1f}%, wrong {100 * wrong / max(named, 1):4.1f}%")
        print(f"  D={D:.2f}  " + "   ".join(line))
    print("\nThe app: suggestions padded, others on the photo excluded, context 2d × 0.04, cut-off 0.55; scan D=0.40, M=0.10.")


if __name__ == "__main__":
    main()

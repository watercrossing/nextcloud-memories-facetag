#!/usr/bin/env python3
"""Recognize's face model, measured on the photo set: a baseline to notice when a release changes it.

Every distance limit in the app was tuned against the face model Recognize 12 ships (scan naming
0.40 with a 0.10 margin, suggestions under 0.55, the context bonus 0.04 × ln(1 + photos within 2
days)). A Recognize release with another model — or other preprocessing — would shift what those
numbers mean without anything failing outright. This measures, on the stack `stack.sh dataset`
loaded, Recognize's own detections (not this app's) of the photo set's ground-truth faces:

- how many ground-truth faces Recognize detects at all, and how many faces it finds in total;
- the descriptors' length and norm;
- distances between faces of the same person and of different people (quantiles);
- nearest-person accuracy, scored the way the app scores suggestions (mean distance to the
  person's 5 nearest faces, leave-one-out);
- one descriptor per person (rounded), to tell "the model changed" from "the photos changed".

    python3 tests/ci/model_baseline.py            # print the measurements for the running stack
    python3 tests/ci/model_baseline.py --write    # store them as the baseline (tests/data/model-baseline.json)

test_1_model.py compares each CI cell with the stored baseline (tolerances in TOLERANCE below).
"""

from __future__ import annotations

import datetime
import json
import math
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stackctl as s  # noqa: E402

BASELINE = s.ROOT / "tests/data/model-baseline.json"
K = 5  # the app's suggestion score: mean distance to a person's K nearest faces
QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)
DIGITS = 4  # stored descriptors; rounding moves a 128-d descriptor by ~0.0003

# How far a cell may be from the baseline and still count as the same model. Measured across the
# matrix (different GitHub runner CPUs, Recognize 12.0.2/12.0.3): descriptors of the same face
# differ by < 1e-4 there, detection counts not at all. A changed model moves descriptors by
# ~0.1-1 and the distance distributions by several hundredths.
TOLERANCE = {
    "descriptor": 0.01,        # max distance between a face's stored and current descriptor
    "detected": 2,             # ground-truth faces detected, ± count
    "total_faces": 6,          # all faces Recognize found, ± count
    "quantile": 0.015,         # each within/between-person distance quantile, ± absolute
    "accuracy": 0.03,          # nearest-person accuracy may drop this much (fraction)
    "norm": 0.02,              # mean descriptor norm, ± absolute
}


def dist(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def quantiles(xs: list[float]) -> dict[str, float]:
    xs = sorted(xs)
    out = {}
    for q in QUANTILES:
        i = q * (len(xs) - 1)
        lo, hi = math.floor(i), math.ceil(i)
        out[f"p{round(q * 100)}"] = round(xs[lo] + (xs[hi] - xs[lo]) * (i - lo), 4)
    out["mean"] = round(statistics.fmean(xs), 4)
    return out


def faces() -> tuple[list[dict], int, int]:
    """Recognize's own detections of ground-truth faces: [{file, person, box, vector}], plus how
    many ground-truth faces there are and how many faces Recognize found in all."""
    photos = {p["file"]: p for p in s.manifest()["photos"]}
    by_id = {fid: photos[f] for f, fid in s.file_ids().items() if f in photos}
    rows = s.sql(f"""SELECT d.file_id, d.x, d.y, d.width, d.height, d.face_vector
                     FROM oc_recognize_face_detections d
                     LEFT JOIN oc_facetag_manual m ON m.detection_id = d.id
                     WHERE d.user_id = '{s.TEST_USER}' AND m.detection_id IS NULL
                     ORDER BY d.file_id, d.id""")
    out, seen = [], set()
    for r in rows:
        photo = by_id.get(int(r["file_id"]))
        who = photo and s.identity_of(photo, r)
        if not who or (photo["file"], who) in seen:
            continue
        seen.add((photo["file"], who))
        out.append({"file": photo["file"], "person": who,
                    "box": [round(float(r[k]), 4) for k in ("x", "y", "width", "height")],
                    "vector": [float(v) for v in json.loads(r["face_vector"])]})
    gt = sum(len(p["people"]) for p in photos.values())
    return out, gt, len(rows)


def measure() -> dict:
    fs, gt, total = faces()
    within, between = [], []
    for i, a in enumerate(fs):
        for b in fs[i + 1:]:
            (within if a["person"] == b["person"] else between).append(dist(a["vector"], b["vector"]))
    # Nearest person, leave-one-out, scored like the app's suggestions
    right = n = 0
    for i, a in enumerate(fs):
        per: dict[str, list[float]] = {}
        for j, b in enumerate(fs):
            if j != i:
                per.setdefault(b["person"], []).append(dist(a["vector"], b["vector"]))
        if a["person"] not in per:
            continue  # the only face of this person
        score = {p: statistics.fmean(sorted(d)[:K]) for p, d in per.items()}
        n += 1
        right += min(score, key=score.get) == a["person"]
    # One descriptor per person: their first portrait (or first face) in file order
    ref: dict[str, dict] = {}
    for f in sorted(fs, key=lambda f: (not f["file"].startswith("portraits/"), f["file"])):
        ref.setdefault(f["person"], f)
    return {
        "ground_truth_faces": gt,
        "detected": len(fs),
        "total_faces": total,
        "dimensions": sorted({len(f["vector"]) for f in fs}),
        "norm": quantiles([math.sqrt(sum(v * v for v in f["vector"])) for f in fs]),
        "within_person": quantiles(within),
        "between_people": quantiles(between),
        "nearest_person_accuracy": round(right / n, 4) if n else None,
        "nearest_person_scored": n,
        "reference_faces": [{**f, "vector": [round(v, DIGITS) for v in f["vector"]]}
                            for f in sorted(ref.values(), key=lambda f: f["person"])],
    }


def compare(now: dict, base: dict) -> list[str]:
    """Differences beyond TOLERANCE, as readable lines (empty: the same model)."""
    t, problems = TOLERANCE, []
    if now["dimensions"] != base["dimensions"]:
        problems.append(f"descriptor length is {now['dimensions']}, was {base['dimensions']}")
    if abs(now["detected"] - base["detected"]) > t["detected"]:
        problems.append(f"Recognize detects {now['detected']} of the {now['ground_truth_faces']} ground-truth faces, "
                        f"baseline {base['detected']} (± {t['detected']})")
    if abs(now["total_faces"] - base["total_faces"]) > t["total_faces"]:
        problems.append(f"Recognize finds {now['total_faces']} faces in all, baseline {base['total_faces']} (± {t['total_faces']})")
    if abs(now["norm"]["mean"] - base["norm"]["mean"]) > t["norm"]:
        problems.append(f"mean descriptor norm {now['norm']['mean']}, baseline {base['norm']['mean']} (± {t['norm']})")
    for key, label in (("within_person", "same person"), ("between_people", "different people")):
        off = max(abs(now[key][q] - v) for q, v in base[key].items())
        if off > t["quantile"]:
            problems.append(f"distances between faces of {label} moved by up to {off:.3f} (± {t['quantile']}): "
                            + ", ".join(f"{q} {now[key][q]:.3f} (was {v:.3f})" for q, v in base[key].items()))
    if now["nearest_person_accuracy"] < base["nearest_person_accuracy"] - t["accuracy"]:
        problems.append(f"nearest-person accuracy {now['nearest_person_accuracy']:.1%}, baseline "
                        f"{base['nearest_person_accuracy']:.1%} (may drop {t['accuracy']:.0%})")
    current = {(f["file"], f["person"]): f for f in now["reference_faces"]}
    moved, missing = [], []
    for f in base["reference_faces"]:
        c = current.get((f["file"], f["person"]))
        if c is None:
            missing.append(f["person"])
        elif len(c["vector"]) == len(f["vector"]):
            d = dist(c["vector"], f["vector"])
            if d > t["descriptor"]:
                moved.append((d, f["person"]))
    if missing:
        problems.append(f"reference faces no longer detected: {', '.join(missing)}")
    if moved:
        moved.sort(reverse=True)
        problems.append(f"{len(moved)} of {len(base['reference_faces'])} reference faces got a different descriptor "
                        f"(distance to the stored one up to {moved[0][0]:.3f}, tolerance {t['descriptor']}): "
                        + ", ".join(f"{p} {d:.3f}" for d, p in moved[:5]) + (" …" if len(moved) > 5 else ""))
    return problems


ADVICE = """\
Recognize's face model (or how it prepares faces) is not the one the app's limits were tuned on.
Re-tune before widening Compatibility::TESTED_MAJORS:
  - lib/Service/Scanner.php DEFAULTS: assignDistance 0.40, assignMargin 0.10 (the scan naming faces)
  - js/facetag.js SUGGEST_MAX_SCORE 0.55 (the picker's cut-off for suggestions)
  - lib/Service/FaceStore.php KNN 5, CONTEXT_WEIGHT 0.04, CONTEXT_SECONDS 2 days (the suggestion score)
  - the expected numbers asserted in tests/ci/server/test_2_api.py and test_3_scan.py
Measure candidate limits with tests/ui-harness/eval_suggestions.py: --stack for this photo set,
better an export of a real library's named faces. Then store the new baseline:
  python3 tests/ci/model_baseline.py --write"""


def main() -> None:
    now = measure()
    if "--write" in sys.argv:
        apps = json.loads(s.occ("app:list", "--output=json").stdout)["enabled"]
        version = json.loads(s.occ("status", "--output=json").stdout)["versionstring"]
        now = {"recorded": {"date": datetime.date.today().isoformat(), "nextcloud": version,
                            "memories": apps.get("memories"), "recognize": apps.get("recognize")},
               "tolerance": TOLERANCE, **now}
        BASELINE.write_text(json.dumps(now, indent=1, ensure_ascii=False) + "\n")
        print(f"wrote {BASELINE.relative_to(s.ROOT)} ({BASELINE.stat().st_size >> 10} KB)")
    else:
        show = {k: v for k, v in now.items() if k != "reference_faces"}
        print(json.dumps(show, indent=1))
        if BASELINE.exists() and os.environ.get("COMPARE", "1") == "1":
            problems = compare(now, json.loads(BASELINE.read_text()))
            print("\nagainst the baseline:", "same model" if not problems else "\n  " + "\n  ".join(problems))


if __name__ == "__main__":
    main()

"""Recognize's face model against the stored baseline (tests/data/model-baseline.json).

Every distance limit in the app was tuned against the model Recognize 12 ships; a release that
changes it must fail here, with a report of what moved and what to re-tune. See
tests/ci/model_baseline.py for what is measured and the tolerances.
"""

import json

import model_baseline as mb


def test_face_model_matches_the_baseline():
    base = json.loads(mb.BASELINE.read_text())
    now = mb.measure()
    shown = {k: v for k, v in now.items() if k != "reference_faces"}
    print("\nface model on this cell:", json.dumps(shown))
    moved = [mb.dist(f["vector"], b["vector"]) for f in now["reference_faces"] for b in base["reference_faces"]
             if (f["file"], f["person"]) == (b["file"], b["person"])]
    if moved:
        print(f"reference descriptors vs baseline: max distance {max(moved):.6f}, mean {sum(moved) / len(moved):.6f}")
    problems = mb.compare(now, base)
    rec = base["recorded"]
    assert not problems, (
        f"Recognize's face model differs from the baseline (recorded {rec['date']} with Recognize {rec['recognize']}, "
        f"Memories {rec['memories']}, Nextcloud {rec['nextcloud']}):\n  - " + "\n  - ".join(problems) + "\n\n" + mb.ADVICE)

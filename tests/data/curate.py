#!/usr/bin/env python3
"""Build tests/data/manifest.json: the test photo set, chosen from Wikimedia Commons.

Run once by hand when the set is (re)built; CI only runs fetch.py. Three steps:

    curate.py candidates WORK            # Commons search per identity → WORK/candidates.json + 1280px thumbs
    curate.py detect WORK                # faces in every thumb, with Recognize's own model, in the running
                                         #   test stack (tests/ci/stack.sh up) → WORK/faces.json
    curate.py select WORK                # → tests/data/manifest.json and tests/data/ATTRIBUTION.md

Who is in a photo comes from Commons, never from looking at faces: a candidate must carry a
structured-data "depicts" (P180) statement for the person and name them in its title or
description. Which face is theirs is then worked out statistically: across all the photos said
to depict someone, the face that recurs (has a near match in the most other photos) is them;
their centre is the medoid of those matches, and in each photo their face is the one near that
centre, accepted only when no other face in the photo comes close. Photos placing a single
person are "portraits" (others may be in them, unnamed); photos placing two or more are "group".

Standard library only, except detect, which runs node in the stack.
"""

from __future__ import annotations

import html
import json
import math
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
USER_AGENT = "memories-facetag-ci/1 (+https://github.com/watercrossing/nextcloud-memories-facetag; test photo set)"
API = "https://commons.wikimedia.org/w/api.php"

# Wikidata id → name. Public figures with many freely licensed photos, several of whom appear
# together at summits, so there are group photos.
IDENTITIES = {
    "Q22686": "Donald Trump",
    "Q3052772": "Emmanuel Macron",
    "Q567": "Angela Merkel",
    "Q3099714": "Justin Trudeau",
    "Q132345": "Shinzō Abe",
    "Q264766": "Theresa May",
    "Q180589": "Boris Johnson",
    "Q57661": "Jean-Claude Juncker",
    "Q946": "Donald Tusk",
    "Q1058": "Narendra Modi",
    "Q7747": "Vladimir Putin",
    "Q15031": "Xi Jinping",
}
PORTRAITS_PER_IDENTITY = 12
GROUP_PHOTOS = 25
CANDIDATES_PER_IDENTITY = 80
MAX_BYTES = 10 << 20
LICENCE_OK = re.compile(r"^(CC0|Public domain|PD\b|CC BY(-SA)? [\d.]+)", re.I)

# Face-model thresholds for choosing photos (descriptor distances are Euclidean, as Recognize's)
MIN_SCORE = 0.8                  # faces considered at all
MIN_HEIGHT = 0.05                # ... and their height, relative to the image
SAME_PERSON = 0.45               # a face to its person's centre
MARGIN = 0.10                    # ... and the photo's next face at least this much further
MIN_PHOTOS = 6                   # identities with fewer usable photos are left out
DISTINCT = 0.12                  # two portraits closer than this are the same shot; keep one


def api(params: dict) -> dict:
    params = {**params, "format": "json", "formatversion": "2"}
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - retry anything transient
            if attempt == 4:
                raise
            print(f"  retry after {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    raise AssertionError


def download(url: str, target: Path) -> None:
    if target.exists():
        return
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            target.write_bytes(data)
            return
        except Exception as e:  # noqa: BLE001
            if attempt == 4:
                raise
            print(f"  retry {target.name} after {e}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def meta(page: dict) -> dict | None:
    info = (page.get("imageinfo") or [None])[0]
    if not info or info.get("mime") != "image/jpeg" or info["size"] > MAX_BYTES or min(info["width"], info["height"]) < 800:
        return None
    ext = info.get("extmetadata", {})
    lic = ext.get("LicenseShortName", {}).get("value", "")
    if not LICENCE_OK.match(lic) or re.search(r"\bNC\b|\bND\b", lic):
        return None
    return {
        "title": page["title"],
        "page": info["descriptionurl"],
        "url": info["url"].split("?")[0],
        "thumb": info.get("thumburl"),
        "sha1": info["sha1"],
        "bytes": info["size"],
        "width": info["width"],
        "height": info["height"],
        "licence": lic,
        "licence_url": ext.get("LicenseUrl", {}).get("value", ""),
        "author": strip_html(ext.get("Artist", {}).get("value", "")) or strip_html(ext.get("Credit", {}).get("value", "")),
        "description": strip_html(ext.get("ImageDescription", {}).get("value", ""))[:500],
    }


def depicts(titles: list[str]) -> dict[str, list[str]]:
    """File title → the Wikidata ids its structured data says it depicts."""
    out: dict[str, list[str]] = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        r = api({"action": "query", "titles": "|".join(chunk), "prop": "info"})
        ids = {p["title"]: f"M{p['pageid']}" for p in r["query"]["pages"] if "pageid" in p}
        if not ids:
            continue
        e = api({"action": "wbgetentities", "ids": "|".join(ids.values()), "props": "claims"})
        for title, mid in ids.items():
            claims = (e.get("entities", {}).get(mid, {}).get("statements") or e.get("entities", {}).get(mid, {}).get("claims") or {})
            out[title] = [c["mainsnak"]["datavalue"]["value"]["id"] for c in claims.get("P180", [])
                          if c["mainsnak"].get("datavalue")]
    return out


def candidates(work: Path) -> None:
    thumbs = work / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    found: dict[str, dict] = {}
    for qid, name in IDENTITIES.items():
        surname = name.split()[-1].lower()
        got = 0
        offset = 0
        while got < CANDIDATES_PER_IDENTITY and offset is not None and offset < 1000:
            r = api({"action": "query", "generator": "search", "gsrsearch": f"haswbstatement:P180={qid} filetype:bitmap",
                     "gsrnamespace": "6", "gsrlimit": "50", "gsroffset": str(offset), "prop": "imageinfo",
                     "iiprop": "url|size|sha1|mime|extmetadata", "iiurlwidth": "1280"})
            offset = r.get("continue", {}).get("gsroffset")
            for page in sorted(r.get("query", {}).get("pages", []), key=lambda p: p.get("index", 0)):
                m = meta(page)
                if m is None:
                    continue
                text = (m["title"] + " " + m["description"]).lower()
                if surname not in text and surname.replace("ō", "o") not in text:
                    continue
                entry = found.setdefault(m["title"], m)
                entry.setdefault("named_qids", [])
                if qid not in entry["named_qids"]:
                    entry["named_qids"].append(qid)
                    got += 1
        print(f"{name}: {got} candidates", file=sys.stderr)
    titles = list(found)
    d = depicts(titles)
    for t in titles:
        found[t]["depicts"] = [q for q in d.get(t, []) if q in IDENTITIES]
    for i, (t, m) in enumerate(found.items()):
        m["file"] = f"{m['sha1']}.jpg"
        download(m["thumb"], thumbs / m["file"])
        if i % 50 == 0:
            print(f"thumbs {i}/{len(found)}", file=sys.stderr)
    (work / "candidates.json").write_text(json.dumps(found, indent=1, ensure_ascii=False))
    print(f"{len(found)} candidates", file=sys.stderr)


def detect(work: Path) -> None:
    """Run the app's batch face script under Recognize's node in the running stack."""
    found = json.loads((work / "candidates.json").read_text())
    rel = work.resolve().relative_to((ROOT / ".cache").resolve())
    paths = "\n".join(f"/ci-cache/{rel}/thumbs/{m['file']}" for m in found.values()) + "\n"
    stack = ["docker", "compose", "-f", str(ROOT / "tests/ci/compose.yml"), "exec", "-T", "-u", "www-data",
             "-e", "NODE_PATH=/var/www/html/custom_apps/recognize/node_modules", "-e", "FACETAG_MIN_SCORE=0.5",
             "-e", "TF_CPP_MIN_LOG_LEVEL=2", "nextcloud",
             "custom_apps/recognize/bin/node", "custom_apps/memories_facetag/bin/face-scan.cjs"]
    out = subprocess.run(stack, input=paths, capture_output=True, text=True, check=True).stdout.splitlines()
    assert json.loads(out[0]).get("ready"), out[0]
    faces = {}
    for m, line in zip(found.values(), out[1:]):
        r = json.loads(line)
        faces[m["file"]] = r if isinstance(r, list) else []
    (work / "faces.json").write_text(json.dumps(faces))
    print(f"faces in {len(faces)} thumbs", file=sys.stderr)


def dist(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def medoid(vectors: list[list[float]]) -> list[float]:
    return min(vectors, key=lambda v: sum(dist(v, w) for w in vectors))


def usable(fs: list[dict]) -> list[dict]:
    return [f for f in fs if f["score"] >= MIN_SCORE and f["height"] >= MIN_HEIGHT]


def select(work: Path) -> None:
    found = json.loads((work / "candidates.json").read_text())
    faces = {f: usable(fs) for f, fs in json.loads((work / "faces.json").read_text()).items()}
    by_file = {m["file"]: m for m in found.values()}
    depicted = {q: [f for f, m in by_file.items() if q in m["depicts"] and q in m["named_qids"] and faces.get(f)]
                for q in IDENTITIES}

    # Each person's centre: the face that recurs across the photos said to depict them
    centres: dict[str, list[float]] = {}
    for q, files in depicted.items():
        best = None
        for f in files:
            for v in faces[f]:
                support = sum(1 for g in files if g != f and any(dist(v["vector"], w["vector"]) < SAME_PERSON for w in faces[g]))
                if best is None or support > best[0]:
                    best = (support, v["vector"])
        if best is None:
            continue
        c = best[1]
        for _ in range(3):
            near = [min(faces[g], key=lambda w: dist(w["vector"], c))["vector"] for g in files]
            near = [v for v in near if dist(v, c) < SAME_PERSON]
            c = medoid(near) if near else c
        centres[q] = c
        print(f"{IDENTITIES[q]}: {len(files)} photos depict them; recurring face in {best[0] + 1}", file=sys.stderr)

    def place(f: str, q: str) -> dict | None:
        ranked = sorted(faces[f], key=lambda w: dist(w["vector"], centres[q]))
        if not ranked or dist(ranked[0]["vector"], centres[q]) >= SAME_PERSON:
            return None
        if len(ranked) > 1 and dist(ranked[1]["vector"], centres[q]) < dist(ranked[0]["vector"], centres[q]) + MARGIN:
            return None
        return ranked[0]

    # Everyone depicted in each photo that can be placed
    placed: dict[str, list[tuple[str, dict]]] = {}
    for f, m in by_file.items():
        ppl = []
        for q in m["depicts"]:
            if q in centres and q in m["named_qids"] + m["depicts"] and faces.get(f):
                face = place(f, q)
                if face is not None and all(face is not g for _, g in ppl):
                    ppl.append((q, face))
        if ppl:
            placed[f] = ppl

    chosen: dict[str, list[tuple[str, dict]]] = {}
    count = {q: 0 for q in centres}
    # Group photos first (most people placed), then portraits to fill each person up
    groups = sorted((f for f, ppl in placed.items() if len(ppl) >= 2), key=lambda f: -len(placed[f]))
    for f in groups[:GROUP_PHOTOS]:
        chosen[f] = placed[f]
        for q, _ in placed[f]:
            count[q] += 1
    for q in centres:
        own = sorted((f for f, ppl in placed.items() if len(ppl) == 1 and ppl[0][0] == q),
                     key=lambda f: dist(placed[f][0][1]["vector"], centres[q]))
        taken = [placed[g][0][1]["vector"] for g in chosen if len(placed[g]) == 1 and placed[g][0][0] == q]
        for f in own:
            if count[q] >= PORTRAITS_PER_IDENTITY:
                break
            v = placed[f][0][1]["vector"]
            if any(dist(v, w) < DISTINCT for w in taken):
                continue  # the same shot twice
            chosen[f] = placed[f]
            taken.append(v)
            count[q] += 1
    keep = {q for q, n in count.items() if n >= MIN_PHOTOS}
    for q in centres:
        print(f"{IDENTITIES[q]}: {count[q]} photos{'' if q in keep else ' — too few, left out'}", file=sys.stderr)

    photos = []
    for f, ppl in chosen.items():
        ppl = [(q, face) for q, face in ppl if q in keep]
        if ppl:
            photos.append(entry(by_file[f], "group" if len(ppl) >= 2 else "portrait", [person(q, face) for q, face in ppl],
                                len(faces[f])))
    photos.sort(key=lambda p: p["file"])
    manifest = {
        "about": "Test photo set for memories_facetag's compatibility tests; fetched by fetch.py. "
                 "Built by curate.py from Wikimedia Commons; see ATTRIBUTION.md.",
        "identities": {q: IDENTITIES[q] for q in IDENTITIES if q in keep},
        "photos": photos,
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    write_attribution(photos)
    print(f"{len(photos)} photos ({sum(p['kind'] == 'group' for p in photos)} group), "
          f"{sum(len(p['people']) for p in photos)} named faces, {sum(p['faces'] for p in photos)} faces of score >= {MIN_SCORE}",
          file=sys.stderr)


def person(qid: str, face: dict) -> dict:
    return {"qid": qid, "name": IDENTITIES[qid],
            "x": round(face["x"] + face["width"] / 2, 3), "y": round(face["y"] + face["height"] / 2, 3),
            "size": round(max(face["width"], face["height"]), 3)}


def entry(m: dict, kind: str, people: list[dict], nfaces: int) -> dict:
    stem = re.sub(r"[^A-Za-z0-9]+", "_", Path(m["title"].removeprefix("File:")).stem).strip("_")[:60]
    return {
        "file": f"{kind}s/{stem}_{m['sha1'][:8]}.jpg",
        "kind": kind,
        "page": m["page"],
        "url": m["url"],
        "sha1": m["sha1"],
        "sha256": None,  # filled in by fetch.py --pin
        "bytes": m["bytes"],
        "licence": m["licence"],
        "licence_url": m["licence_url"],
        "author": m["author"],
        "people": people,
        "faces": nfaces,  # faces the model finds (score >= MIN_SCORE, at curation size), named or not
    }


def write_attribution(photos: list[dict]) -> None:
    lines = [
        "# Test photo set: attribution",
        "",
        "The compatibility tests use these photos from [Wikimedia Commons](https://commons.wikimedia.org/).",
        "They are not in this repository: `tests/data/fetch.py` downloads them from the URLs in `manifest.json`, checks them, and scales them down to at most 2048 px for the tests.",
        "Each is used under the licence shown, by its author as credited on its Commons page.",
        "",
        "| File | Author | Licence | People (for the tests' ground truth) |",
        "|---|---|---|---|",
    ]
    for p in photos:
        lic = f"[{p['licence']}]({p['licence_url']})" if p["licence_url"] else p["licence"]
        title = urllib.parse.unquote(p["page"].rsplit("/", 1)[-1]).replace("_", " ")
        author = p["author"].replace("|", "/")[:200] or "see Commons page"
        lines.append(f"| [{title}]({p['page']}) | {author} | {lic} | {', '.join(x['name'] for x in p['people'])} |")
    (HERE / "ATTRIBUTION.md").write_text("\n".join(lines) + "\n")


def main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[0] not in ("candidates", "detect", "select"):
        sys.exit(__doc__)
    globals()[argv[0]](Path(argv[1]))


if __name__ == "__main__":
    main(sys.argv[1:])

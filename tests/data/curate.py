#!/usr/bin/env python3
"""Build tests/data/manifest.json: the test photo set, chosen from Wikimedia Commons.

Run once by hand when the set is (re)built; CI only runs fetch.py. Three steps:

    curate.py candidates WORK            # Commons search per identity → WORK/candidates.json + 1280px thumbs
    curate.py detect WORK                # faces in every thumb, with Recognize's own model, in the running
                                         #   test stack (tests/ci/stack.sh up) → WORK/faces.json
    curate.py select WORK                # → tests/data/manifest.json and tests/data/ATTRIBUTION.md

Who is in a photo comes from Commons, never from looking at faces: a candidate must carry a
structured-data "depicts" (P180) statement for the person and name them in its title or
description. Candidates come from a search per person and one per pair of people (photos of two
of them together are rare among each person's first results). Which face is theirs is then
worked out statistically: their centre starts as the medoid of the faces in their photos that
show a single face and depict no one else (or, with too few of those, the face that recurs
across their photos), refined to the medoid of the nearest faces; in each photo their face is
the one near that centre, accepted only when no other face in the photo comes close. Photos
placing a single person are "portraits" (others may be in them, unnamed); photos placing two or
more are "group". Photos touching politics are left out (see POLITICAL).

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
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
USER_AGENT = "memories-facetag-ci/1 (+https://github.com/watercrossing/nextcloud-memories-facetag; test photo set)"
API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA = "https://www.wikidata.org/w/api.php"

# Wikidata id → name. Tennis players with many freely licensed photos, half women, half men, who
# share podiums, nets, doubles and team events (Laver Cup, Fed Cup), so there are group photos.
IDENTITIES = {
    "Q11459": "Serena Williams",
    "Q26840238": "Iga Świątek",
    "Q230156": "Simona Halep",
    "Q30767": "Caroline Wozniacki",
    "Q30812": "Petra Kvitová",
    "Q77178": "Angelique Kerber",
    "Q1426": "Roger Federer",
    "Q10132": "Rafael Nadal",
    "Q10125": "Andy Murray",
    "Q193661": "Stan Wawrinka",
    "Q88762": "Dominic Thiem",
    "Q311222": "Kei Nishikori",
}
PORTRAITS_PER_IDENTITY = 16
GROUP_PHOTOS = 25
CANDIDATES_PER_IDENTITY = 150  # many are match shots with faces too small to use
PAIR_CANDIDATES = 40
# No politics in the test photos: photos depicting a politician or office holder (Wikidata
# occupation politician, or any position held), whose title or description names an office, or
# published by a government body, are left out — trophy presentations, receptions and a town's
# own photographer bring in mayors, ministers and presidents.
POLITICIAN = "Q82955"
POLITICAL = re.compile(r"\b(president|presidente|pr[ée]sident|prime minister|minister|ministr|mayor|alcald|"
                       r"b[üu]rgermeister|governor|senator|congress|parliament|la moneda|white house|kremlin)", re.I)
OFFICIAL = re.compile(r"\b(municipio|municipality|city of|council|government|gobierno|presidencia|white house|kremlin|"
                      r"embassy|ministry|state department)", re.I)
MAX_BYTES = 10 << 20
LICENCE_OK = re.compile(r"^(CC0|Public domain|PD\b|CC BY(-SA)? [\d.]+)", re.I)

# Face-model thresholds for choosing photos (descriptor distances are Euclidean, as Recognize's)
MIN_SCORE = 0.8                  # faces considered at all
MIN_HEIGHT = 0.05                # ... and their height, relative to the image
SAME_PERSON = 0.50               # a face to its person's centre
MARGIN = 0.10                    # ... and the photo's next face at least this much further
MIN_PHOTOS = 6                   # identities with fewer usable photos are left out
DISTINCT = 0.12                  # two portraits closer than this are the same shot; keep one


def api(params: dict, endpoint: str = API) -> dict:
    params = {**params, "format": "json", "formatversion": "2"}
    req = urllib.request.Request(endpoint + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
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


def fold(s: str) -> str:
    """Lower case without accents, so 'Świątek' matches a title spelling it 'Swiatek'."""
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c)).replace("ł", "l")


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


def search(query: str, limit: int):
    """Commons files matching a search, best first, that pass meta()."""
    seen = 0
    offset = 0
    while seen < limit and offset is not None and offset < 1000:
        r = api({"action": "query", "generator": "search", "gsrsearch": query + " filetype:bitmap",
                 "gsrnamespace": "6", "gsrlimit": "50", "gsroffset": str(offset), "prop": "imageinfo",
                 "iiprop": "url|size|sha1|mime|extmetadata", "iiurlwidth": "1280"})
        offset = r.get("continue", {}).get("gsroffset")
        for page in sorted(r.get("query", {}).get("pages", []), key=lambda p: p.get("index", 0)):
            m = meta(page)
            if m is not None:
                seen += 1
                yield m


def is_political(m: dict) -> bool:
    return m["depicts_politician"] or bool(POLITICAL.search(m["title"] + " " + m["description"])) \
        or bool(OFFICIAL.search(m["author"]))


def politicians(qids: set[str]) -> set[str]:
    """Those of qids that Wikidata says are politicians or have held a position."""
    out = set()
    qids = sorted(qids)
    for i in range(0, len(qids), 50):
        e = api({"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]), "props": "claims"}, WIKIDATA)
        for q, ent in e.get("entities", {}).items():
            claims = ent.get("claims", {})
            jobs = {c["mainsnak"].get("datavalue", {}).get("value", {}).get("id") for c in claims.get("P106", [])}
            if POLITICIAN in jobs or claims.get("P39"):
                out.add(q)
    return out


def candidates(work: Path) -> None:
    thumbs = work / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    found: dict[str, dict] = {}

    def add(m: dict, qids: list[str]) -> int:
        """Record m as a candidate for each of qids whose surname its title or description gives."""
        text = fold(m["title"] + " " + m["description"])
        named = [q for q in qids if fold(IDENTITIES[q].split()[-1]) in text]
        if not named:
            return 0
        entry = found.setdefault(m["title"], m)
        entry.setdefault("named_qids", [])
        new = [q for q in named if q not in entry["named_qids"]]
        entry["named_qids"] += new
        return len(new)

    for qid, name in IDENTITIES.items():
        got = 0
        for m in search(f"haswbstatement:P180={qid}", 10 * CANDIDATES_PER_IDENTITY):
            got += add(m, [qid])
            if got >= CANDIDATES_PER_IDENTITY:
                break
        print(f"{name}: {got} candidates", file=sys.stderr)
    # Photos of two of them together are rare among each person's first results: search each pair
    qids = list(IDENTITIES)
    for i, a in enumerate(qids):
        for b in qids[i + 1:]:
            got = sum(add(m, [a, b]) > 0 for m in search(f"haswbstatement:P180={a} haswbstatement:P180={b}", PAIR_CANDIDATES))
            if got:
                print(f"{IDENTITIES[a]} + {IDENTITIES[b]}: {got} candidates", file=sys.stderr)
    titles = list(found)
    d = depicts(titles)
    for t in titles:
        found[t]["depicts"] = [q for q in d.get(t, []) if q in IDENTITIES]
        found[t]["depicts_others"] = [q for q in d.get(t, []) if q not in IDENTITIES]
    political = politicians({q for m in found.values() for q in m["depicts_others"]})
    for m in found.values():
        m["depicts_politician"] = bool(set(m["depicts_others"]) & political)
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
    left_out = [m for m in found.values() if is_political(m)]
    print(f"{len(left_out)} candidates left out as political", file=sys.stderr)
    by_file = {m["file"]: m for m in found.values() if not is_political(m)}
    depicted = {q: [f for f, m in by_file.items() if q in m["depicts"] and q in m["named_qids"] and faces.get(f)]
                for q in IDENTITIES}

    # Each person's centre. Where they have enough photos showing one face and depicting no one
    # else, start from the medoid of those faces; otherwise from the face that recurs across the
    # photos said to depict them. (Recurrence alone can settle on a "hub": a blurred or tiny face
    # near everyone's, which recurs everywhere; and two people often photographed together, as in
    # a final, would both get the more frequent face.)
    centres: dict[str, list[float]] = {}
    for q, files in depicted.items():
        alone = [faces[f][0]["vector"] for f in files if by_file[f]["depicts"] == [q] and len(faces[f]) == 1]
        if len(alone) >= 2 * MIN_PHOTOS:
            c = medoid(alone)
            how = f"medoid of {len(alone)} single-face photos"
        else:
            best = None
            for f in files:
                for v in faces[f]:
                    support = sum(1 for g in files if g != f and any(dist(v["vector"], w["vector"]) < SAME_PERSON for w in faces[g]))
                    if best is None or support > best[0]:
                        best = (support, v["vector"])
            if best is None:
                continue
            c = best[1]
            how = f"recurring face in {best[0] + 1}"
        for _ in range(3):
            near = [min(faces[g], key=lambda w: dist(w["vector"], c))["vector"] for g in files]
            near = [v for v in near if dist(v, c) < SAME_PERSON]
            c = medoid(near) if near else c
        centres[q] = c
        print(f"{IDENTITIES[q]}: {len(files)} photos depict them; centre from the {how}", file=sys.stderr)

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

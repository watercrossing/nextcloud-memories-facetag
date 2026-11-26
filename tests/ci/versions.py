#!/usr/bin/env python3
"""Which Memories and Recognize releases the compatibility tests run against.

The pinned set lives in tests/ci/versions.json; CI reads only that file, so a run is reproducible.

    versions.py pin                  # re-resolve from the App Store and rewrite versions.json
    versions.py matrix               # GitHub Actions matrix (JSON) from versions.json
    versions.py resolve APP VERSION  # one release: prints "url sha256", verifying the App Store signature
    versions.py fetch APP VERSION DIR  # download a release tarball into DIR (if absent), check its sha256

What gets pinned: for each Nextcloud major in NEXTCLOUD, the latest RELEASES_PER_APP stable
releases of each app, within the major versions listed in lib/Service/Compatibility.php's
TESTED_MAJORS and compatible with that Nextcloud major. Widening TESTED_MAJORS and re-pinning
widens the matrix.

Standard library only (plus the `openssl` command for signature checks).
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINNED = Path(__file__).with_name("versions.json")
NEXTCLOUD = ["34"]
RELEASES_PER_APP = 2
STORE = "https://apps.nextcloud.com/api/v1/platform/{nc}.0.0/apps.json"
USER_AGENT = "memories-facetag-ci/1 (+https://github.com/watercrossing/nextcloud-memories-facetag)"


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def tested_majors() -> dict[str, list[int]]:
    """Parse Compatibility::TESTED_MAJORS out of the PHP source."""
    src = (ROOT / "lib/Service/Compatibility.php").read_text()
    block = re.search(r"TESTED_MAJORS\s*=\s*\[(.*?)\];", src, re.S)
    if not block:
        sys.exit("cannot find TESTED_MAJORS in Compatibility.php")
    return {app: [int(m) for m in re.findall(r"\d+", majors)]
            for app, majors in re.findall(r"'(\w+)'\s*=>\s*\[([^\]]*)\]", block.group(1))}


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


_store_cache: dict[str, list] = {}


def store(nc: str) -> list:
    if nc not in _store_cache:
        _store_cache[nc] = json.loads(http_get(STORE.format(nc=nc)))
    return _store_cache[nc]


def releases(nc: str, app: str) -> tuple[dict, list[dict]]:
    """(app entry, its stable releases for this Nextcloud major, newest first)"""
    entry = next((a for a in store(nc) if a["id"] == app), None)
    if entry is None:
        sys.exit(f"{app} is not in the App Store for Nextcloud {nc}")
    stable = [r for r in entry["releases"]
              if not r["isNightly"] and re.fullmatch(r"\d+\.\d+\.\d+", r["version"])]
    stable.sort(key=lambda r: vkey(r["version"]), reverse=True)
    return entry, stable


def verify(entry: dict, release: dict, data: bytes) -> None:
    """Check the App Store's RSA-SHA512 signature of a tarball against the app's certificate."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / "cert.pem").write_text(entry["certificate"])
        (t / "sig").write_bytes(base64.b64decode(release["signature"]))
        (t / "app.tgz").write_bytes(data)
        pub = subprocess.run(["openssl", "x509", "-in", t / "cert.pem", "-pubkey", "-noout"],
                             check=True, capture_output=True).stdout
        (t / "pub.pem").write_bytes(pub)
        r = subprocess.run(["openssl", "dgst", "-sha512", "-verify", t / "pub.pem", "-signature", t / "sig", t / "app.tgz"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"{entry['id']} {release['version']}: App Store signature does not verify: {r.stdout}{r.stderr}")


def resolve(nc: str, app: str, version: str) -> dict:
    entry, rels = releases(nc, app)
    rel = next((r for r in rels if r["version"] == version), None)
    if rel is None:
        sys.exit(f"{app} {version} is not a stable release for Nextcloud {nc} "
                 f"(have: {', '.join(r['version'] for r in rels[:8])})")
    data = http_get(rel["download"])
    verify(entry, rel, data)
    return {"version": version, "url": rel["download"], "sha256": hashlib.sha256(data).hexdigest()}


def pin() -> None:
    majors = tested_majors()
    out = {"nextcloud": NEXTCLOUD, "apps": {}}
    for nc in NEXTCLOUD:
        for app, ms in majors.items():
            _, rels = releases(nc, app)
            chosen = [r["version"] for r in rels if vkey(r["version"])[0] in ms][:RELEASES_PER_APP]
            if not chosen:
                sys.exit(f"no {app} release of major {ms} for Nextcloud {nc}")
            for v in chosen:
                print(f"nc {nc}: {app} {v}", file=sys.stderr)
                out["apps"].setdefault(app, {}).setdefault(nc, []).append(resolve(nc, app, v))
    PINNED.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {PINNED.relative_to(ROOT)}", file=sys.stderr)


def matrix() -> None:
    pinned = json.loads(PINNED.read_text())
    cells = []
    for nc in pinned["nextcloud"]:
        mem = pinned["apps"]["memories"].get(nc, [])
        rec = pinned["apps"]["recognize"].get(nc, [])
        for m, r in itertools.product(mem, rec):
            cells.append({"nextcloud": nc, "memories": m["version"], "recognize": r["version"]})
    print(json.dumps({"include": cells}))


def pinned_release(app: str, version: str) -> dict | None:
    pinned = json.loads(PINNED.read_text())
    for rels in pinned["apps"].get(app, {}).values():
        for r in rels:
            if r["version"] == version:
                return r
    return None


def fetch(app: str, version: str, dest: Path, nc: str) -> None:
    """Download APP VERSION to DEST/APP-VERSION.tar.gz, from the pin if there is one, else the App Store."""
    rel = pinned_release(app, version) or resolve(nc, app, version)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"{app}-{version}.tar.gz"
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == rel["sha256"]:
        print(f"{target.name}: cached", file=sys.stderr)
        return
    data = http_get(rel["url"])
    got = hashlib.sha256(data).hexdigest()
    if got != rel["sha256"]:
        sys.exit(f"{app} {version}: sha256 {got} does not match the pinned {rel['sha256']}")
    target.write_bytes(data)
    print(f"{target.name}: downloaded, {len(data) >> 20} MB", file=sys.stderr)


def main(argv: list[str]) -> None:
    if argv[:1] == ["pin"]:
        pin()
    elif argv[:1] == ["matrix"]:
        matrix()
    elif argv[:1] == ["resolve"] and len(argv) == 3:
        r = resolve(NEXTCLOUD[-1], argv[1], argv[2])
        print(r["url"], r["sha256"])
    elif argv[:1] == ["fetch"] and len(argv) in (4, 5):
        fetch(argv[1], argv[2], Path(argv[3]), argv[4] if len(argv) == 5 else NEXTCLOUD[-1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

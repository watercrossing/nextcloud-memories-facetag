#!/usr/bin/env python3
"""Which Memories and Recognize releases the compatibility tests run against.

The pinned set lives in tests/ci/versions.json; CI reads only that file, so a run is reproducible.

    versions.py pin                  # re-resolve from the App Store and rewrite versions.json
    versions.py matrix               # GitHub Actions matrix (JSON) from versions.json
    versions.py resolve APP VERSION  # one release: prints "url sha256", verifying the App Store signature
    versions.py fetch APP VERSION DIR  # download a release tarball into DIR (if absent), check its sha256
    versions.py nextcloud            # Nextcloud majors tested, released (Docker image) and in prerelease
    versions.py server VERSION DIR   # a Nextcloud prerelease (36.0.0beta1) or "master" (daily build):
                                     # download, verify (sha512 and Nextcloud's GPG signature), unpack into
                                     # DIR/VERSION/nextcloud; prints that path, the Docker base major and its own major

What gets pinned: for each Nextcloud major the app declares (appinfo/info.xml's min-version to
max-version), the latest stable releases of each app within the major versions listed in
lib/Service/Compatibility.php's TESTED_MAJORS and compatible with that Nextcloud major: the latest
RELEASES_PER_APP on the newest Nextcloud major, the latest one on older majors. Widening
TESTED_MAJORS (or raising max-version) and re-pinning widens the matrix; tests/ci/watch.py does
that when new releases pass.

FACETAG_WATCH_FIXTURE=FILE fakes what the App Store, the Docker image and the prerelease server
offer, for testing the watcher (see tests/ci/watch-fixtures/README.md).

Standard library only (plus the `openssl` command for signature checks).
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINNED = Path(__file__).with_name("versions.json")
RELEASES_PER_APP = 2
STORE = "https://apps.nextcloud.com/api/v1/platform/{nc}.0.0/apps.json"
# What the official Docker image publishes (its build script's own list), and Nextcloud's prereleases
DOCKER_VERSIONS = "https://raw.githubusercontent.com/nextcloud/docker/master/versions.json"
PRERELEASES = "https://download.nextcloud.com/server/prereleases/"
# Nextcloud's release signing key (the official Docker image checks tarballs against the same one)
NEXTCLOUD_GPG_KEY = "https://nextcloud.com/nextcloud.asc"
NEXTCLOUD_GPG_FINGERPRINT = "28806A878AE423A28372792ED75899B9A724937A"
DAILY = "https://download.nextcloud.com/server/daily/latest-{branch}.tar.bz2"
USER_AGENT = "memories-facetag-ci/1 (+https://github.com/watercrossing/nextcloud-memories-facetag)"


def http_get(url: str, attempts: int = 6) -> bytes:
    """GET, retrying rate limits (the App Store answers bursts with 429) and server errors."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if attempt == attempts - 1 or not (e.code == 429 or e.code >= 500):
                raise
            wait = min(int(e.headers.get("Retry-After") or 0) or 15 * 2 ** attempt, 300)
            print(f"{url}: HTTP {e.code}, retrying in {wait} s", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def fixture() -> dict:
    path = os.environ.get("FACETAG_WATCH_FIXTURE")
    return json.loads(Path(path).read_text()) if path else {}


def nextcloud_majors(info: str | None = None) -> list[str]:
    """The Nextcloud majors appinfo/info.xml allows: min-version to max-version."""
    info = info if info is not None else (ROOT / "appinfo/info.xml").read_text()
    m = re.search(r'<nextcloud\s+min-version="(\d+)"\s+max-version="(\d+)"', info)
    if not m:
        sys.exit("cannot find <nextcloud min-version max-version> in appinfo/info.xml")
    return [str(v) for v in range(int(m.group(1)), int(m.group(2)) + 1)]


def nextcloud_releases() -> dict[str, str]:
    """{major: newest release} of the official Docker image (stable releases only)."""
    fx = fixture()
    if "nextcloud_releases" in fx:
        return fx["nextcloud_releases"]
    data = json.loads(http_get(DOCKER_VERSIONS))
    return {major: v["version"] for major, v in data.items()}


def prereleases() -> list[str]:
    """Nextcloud prereleases of a major (x.0.0betaN / rcN) published on the download server, oldest first."""
    fx = fixture()
    if "prereleases" in fx:
        return fx["prereleases"]
    listing = http_get(PRERELEASES).decode()
    found = set(re.findall(r'nextcloud-(\d+\.0\.0(?:beta|rc|RC)\d+)\.tar\.bz2"', listing))

    def key(v: str):
        m = re.fullmatch(r"(\d+)\.0\.0(beta|rc|RC)(\d+)", v)
        return int(m.group(1)), m.group(2).lower() == "rc", int(m.group(3))
    return sorted(found, key=key)


def tested_majors(src: str | None = None) -> dict[str, list[int]]:
    """Parse Compatibility::TESTED_MAJORS out of the PHP source."""
    src = src if src is not None else (ROOT / "lib/Service/Compatibility.php").read_text()
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
        apps = json.loads(http_get(STORE.format(nc=nc)))
        fake = fixture().get("store", {})
        for entry in apps:
            hide = set(fake.get("hide", {}).get(entry["id"], []))
            rels = [r for r in entry["releases"] if r["version"] not in hide]
            # a fake release: a new version number served by a real release (its download and signature)
            for version, real in fake.get("alias", {}).get(entry["id"], {}).items():
                src = next((r for r in entry["releases"] if r["version"] == real), None)
                if src:
                    rels.append({**src, "version": version})
            entry["releases"] = rels
        _store_cache[nc] = apps
    return _store_cache[nc]


def releases(nc: str, app: str) -> tuple[dict | None, list[dict]]:
    """(app entry, its stable releases for this Nextcloud major, newest first); (None, []) if none"""
    entry = next((a for a in store(nc) if a["id"] == app), None)
    if entry is None:
        return None, []
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
    """APP VERSION from the App Store for Nextcloud NC (else for any other major, newest first:
    a Nextcloud prerelease is tested with releases not yet declared compatible with it)."""
    entry, rels = releases(nc, app)
    rel = next((r for r in rels if r["version"] == version), None)
    if rel is None:
        for other in sorted(nextcloud_releases(), key=int, reverse=True):
            entry, others = releases(other, app)
            rel = next((r for r in others if r["version"] == version), None)
            if rel:
                print(f"{app} {version}: not declared compatible with Nextcloud {nc}; taken from the store for {other}",
                      file=sys.stderr)
                break
    if rel is None:
        sys.exit(f"{app} {version} is not a stable release for Nextcloud {nc} "
                 f"(have: {', '.join(r['version'] for r in rels[:8])})")
    data = http_get(rel["download"])
    verify(entry, rel, data)
    return {"version": version, "url": rel["download"], "sha256": hashlib.sha256(data).hexdigest()}


def choose(nc_majors: list[str], majors: dict[str, list[int]]) -> dict[str, dict[str, list[str]]]:
    """{app: {nc: [versions]}} to pin, without downloading anything. Empty list: no release fits."""
    out: dict[str, dict[str, list[str]]] = {}
    for nc in nc_majors:
        per_app = RELEASES_PER_APP if nc == nc_majors[-1] else 1
        for app, ms in majors.items():
            _, rels = releases(nc, app)
            out.setdefault(app, {})[nc] = [r["version"] for r in rels if vkey(r["version"])[0] in ms][:per_app]
    return out


def compute_pin(nc_majors: list[str], majors: dict[str, list[int]]) -> dict:
    """versions.json's content for these Nextcloud majors and tested app majors (downloads and verifies)."""
    out = {"nextcloud": nc_majors, "apps": {}}
    for app, per_nc in choose(nc_majors, majors).items():
        for nc, versions in per_nc.items():
            if not versions:
                sys.exit(f"no {app} release of major {majors[app]} for Nextcloud {nc}")
            for v in versions:
                print(f"nc {nc}: {app} {v}", file=sys.stderr)
                out["apps"].setdefault(app, {}).setdefault(nc, []).append(resolve(nc, app, v))
    return out


def pin() -> None:
    PINNED.write_text(json.dumps(compute_pin(nextcloud_majors(), tested_majors()), indent=2) + "\n")
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


def server(version: str, dest: Path) -> None:
    """Fetch, verify and unpack a Nextcloud prerelease or daily build for stack.sh's image."""
    if version == "master":
        url = DAILY.format(branch="master")
    elif re.fullmatch(r"\d+\.\d+\.\d+(beta|rc|RC)\d+", version):
        url = f"{PRERELEASES}nextcloud-{version}.tar.bz2"
    else:
        sys.exit(f"{version}: not a prerelease (like 36.0.0beta1) or master")
    want = http_get(url + ".sha512").decode().split()[0]
    target = dest / version
    stamp = target / ".sha512"
    if not (stamp.exists() and stamp.read_text() == want):
        data = http_get(url)
        if hashlib.sha512(data).hexdigest() != want:
            sys.exit(f"{url}: sha512 does not match {url}.sha512")
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "nc.tar.bz2").write_bytes(data)
            (t / "nc.asc").write_bytes(http_get(url + ".asc"))
            (t / "key.asc").write_bytes(http_get(NEXTCLOUD_GPG_KEY))
            gpg = ["gpg", "--homedir", str(t), "--batch", "--quiet"]
            subprocess.run(gpg + ["--import", str(t / "key.asc")], check=True, capture_output=True)
            keys = subprocess.run(gpg + ["--with-colons", "--fingerprint"], check=True, capture_output=True, text=True).stdout
            if NEXTCLOUD_GPG_FINGERPRINT not in keys:
                sys.exit(f"{NEXTCLOUD_GPG_KEY} is not the key {NEXTCLOUD_GPG_FINGERPRINT}")
            r = subprocess.run(gpg + ["--trust-model", "always", "--verify", str(t / "nc.asc"), str(t / "nc.tar.bz2")],
                               capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit(f"{url}: GPG signature does not verify: {r.stderr}")
            subprocess.run(["rm", "-rf", str(target)], check=True)
            target.mkdir(parents=True)
            subprocess.run(["tar", "-xjf", str(t / "nc.tar.bz2"), "-C", str(target)], check=True)
        stamp.write_text(want)
        print(f"Nextcloud {version}: downloaded, signature verified", file=sys.stderr)
    else:
        print(f"Nextcloud {version}: cached", file=sys.stderr)
    released = sorted(nextcloud_releases(), key=int)
    nc_major = re.search(r"OC_Version\s*=\s*(?:array\(|\[)\s*(\d+)", (target / "nextcloud/version.php").read_text()).group(1)
    print(target / "nextcloud", released[-1], nc_major)


def main(argv: list[str]) -> None:
    if argv[:1] == ["pin"]:
        pin()
    elif argv[:1] == ["matrix"]:
        matrix()
    elif argv[:1] == ["resolve"] and len(argv) == 3:
        r = resolve(nextcloud_majors()[-1], argv[1], argv[2])
        print(r["url"], r["sha256"])
    elif argv[:1] == ["fetch"] and len(argv) in (4, 5):
        fetch(argv[1], argv[2], Path(argv[3]), argv[4] if len(argv) == 5 else nextcloud_majors()[-1])
    elif argv[:1] == ["server"] and len(argv) == 3:
        server(argv[1], Path(argv[2]))
    elif argv[:1] == ["nextcloud"]:
        print(json.dumps({"tested": nextcloud_majors(), "released": nextcloud_releases(),
                          "prereleases": prereleases()}, indent=2))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

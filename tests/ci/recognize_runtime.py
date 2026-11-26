#!/usr/bin/env python3
"""Download the native runtime a Recognize release needs for tfjs-node, for stack.sh to install.

    recognize_runtime.py RECOGNIZE_TARBALL CACHE_DIR

Recognize's install step (lib/Migration/InstallDeps.php) downloads, on every fresh install: a node
binary from nodejs.org, libtensorflow for CPU *and* GPU (112 MB + 411 MB) from Google's storage,
the tfjs-node binding, and ffmpeg. Face detection needs only node, the CPU libtensorflow and the
binding, so the stack fetches exactly those — at the versions the release itself declares — into
CACHE_DIR (cached in CI), and installs them where InstallDeps would have.

Prints `node=PATH libtensorflow=PATH binding=PATH napi=N` (paths inside CACHE_DIR).
Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

USER_AGENT = "memories-facetag-ci/1 (+https://github.com/watercrossing/nextcloud-memories-facetag)"
TF_STORAGE = "https://storage.googleapis.com/"


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def member(tar: tarfile.TarFile, suffix: str) -> str:
    text = tar.extractfile(next(m for m in tar.getmembers() if m.name.endswith(suffix))).read().decode()
    return text


def download(url: str, target: Path, sha256: str | None = None) -> Path:
    if target.exists() and (sha256 is None or hashlib.sha256(target.read_bytes()).hexdigest() == sha256):
        print(f"{target.name}: cached", file=sys.stderr)
        return target
    data = http_get(url)
    if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
        sys.exit(f"{url}: sha256 mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(f"{target.name}: downloaded, {len(data) >> 20} MB", file=sys.stderr)
    return target


def main(tarball: Path, cache: Path) -> None:
    with tarfile.open(tarball) as tar:
        node_version = re.search(r"NODE_VERSION\s*=\s*'(v[\d.]+)'", member(tar, "lib/Migration/InstallDeps.php")).group(1)
        tf_version = re.search(r"LIBTENSORFLOW_VERSION\s*=\s*'([\d.]+)'",
                               member(tar, "node_modules/@tensorflow/tfjs-node/scripts/deps-constants.js")).group(1)
        tfjs = json.loads(member(tar, "node_modules/@tensorflow/tfjs-node/package.json"))
    napi = max(tfjs["binary"]["napi_versions"])
    cache = cache / "runtime"

    name = f"node-{node_version}-linux-x64"
    sums = http_get(f"https://nodejs.org/dist/{node_version}/SHASUMS256.txt").decode()
    want = next(line.split()[0] for line in sums.splitlines() if line.endswith(f" {name}.tar.gz"))
    node = download(f"https://nodejs.org/dist/{node_version}/{name}.tar.gz", cache / f"{name}.tar.gz", want)

    lib = download(f"{TF_STORAGE}tensorflow/libtensorflow/libtensorflow-cpu-linux-x86_64-{tf_version}.tar.gz",
                   cache / f"libtensorflow-cpu-linux-x86_64-{tf_version}.tar.gz")
    binding = download(f"{TF_STORAGE}tf-builds/pre-built-binary/napi-v{napi}/{tfjs['version']}/CPU-linux-{tfjs['version']}.tar.gz",
                       cache / f"tfjs-binding-napi-v{napi}-CPU-linux-{tfjs['version']}.tar.gz")
    print(f"node={node.relative_to(cache.parent)} libtensorflow={lib.relative_to(cache.parent)} "
          f"binding={binding.relative_to(cache.parent)} napi={napi} node_dir={name}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))

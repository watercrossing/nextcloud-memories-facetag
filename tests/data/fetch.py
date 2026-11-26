#!/usr/bin/env python3
"""Download the test photo set in manifest.json, verify it, and write test-sized copies.

    uv run --no-project --with pillow python tests/data/fetch.py OUT_DIR [--pin]

Each original is downloaded from Wikimedia Commons and checked against the manifest's SHA-256
(with --pin: against Commons' SHA-1, and the SHA-256 is written into the manifest). The copy in
OUT_DIR/<file> is upright (EXIF orientation applied), at most MAX_SIDE pixels on its long side,
and keeps the rest of its EXIF (Memories reads the date from it). Photos already in OUT_DIR are
skipped, so OUT_DIR can be cached.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
USER_AGENT = "memories-facetag-ci/1 (+https://github.com/watercrossing/nextcloud-memories-facetag; test photo set)"
MAX_SIDE = 2048


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - retry anything transient
            if attempt == 5:
                raise
            # Commons answers 429 to bursts; it sends Retry-After
            retry_after = getattr(e, "headers", None) and e.headers.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 15 * (attempt + 1)
            print(f"  {url}: {e}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError


def scaled(data: bytes) -> bytes:
    im = Image.open(io.BytesIO(data))
    exif = im.getexif()
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    exif[0x0112] = 1  # orientation: already applied
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90, exif=exif.tobytes())
    return out.getvalue()


def main(out: Path, pin: bool) -> None:
    manifest_path = HERE / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    out.mkdir(parents=True, exist_ok=True)
    fetched = 0
    bad = []
    for i, p in enumerate(manifest["photos"]):
        target = out / p["file"]
        if target.exists() and not pin:
            continue
        data = get(p["url"])
        if pin:
            if hashlib.sha1(data).hexdigest() != p["sha1"]:
                bad.append(f"{p['page']}: SHA-1 differs from Commons'")
                continue
            p["sha256"] = hashlib.sha256(data).hexdigest()
        elif hashlib.sha256(data).hexdigest() != p["sha256"]:
            bad.append(f"{p['page']}: SHA-256 differs from the manifest (file replaced on Commons?)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(scaled(data))
        fetched += 1
        if fetched % 20 == 0:
            print(f"  {i + 1}/{len(manifest['photos'])}", file=sys.stderr)
        time.sleep(1.0)  # Commons rate-limits bursts of downloads
    if pin:
        manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(manifest['photos'])} photos in {out} ({fetched} downloaded)", file=sys.stderr)
    if bad:
        sys.exit("\n".join(["Photos that failed verification:"] + bad))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--pin"]
    if len(args) != 1:
        sys.exit(__doc__)
    main(Path(args[0]), "--pin" in sys.argv)

"""Talk to the stack stack.sh brings up: occ, SQL, HTTP, and the photo set's ground truth.

Everything goes through `docker compose exec`, so the stack publishes no ports. Used by
name_clusters.py and the pytest suite in tests/ci/server/.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TEST_USER = "alice"
PHOTOS = "Photos"  # folder in the test user's files


def _compose() -> list[str]:
    cmd = ["docker", "compose", "-f", str(HERE / "compose.yml")]
    if os.environ.get("FACETAG_NETWORK"):
        cmd += ["-f", str(HERE / "compose.network.yml")]
    return cmd


def run(args: list[str], *, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(_compose() + args, input=input, capture_output=True, text=True, check=check)


def occ(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = run(["exec", "-T", "-u", "www-data", "nextcloud", "php", "occ", *args], check=False)
    if check and r.returncode != 0:
        raise AssertionError(f"occ {' '.join(args)} exited {r.returncode}:\n{r.stdout}\n{r.stderr}")
    return r


def sql(query: str) -> list[dict]:
    """Rows of a SELECT, as dicts. Table names take Nextcloud's oc_ prefix."""
    wrapped = f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({query}) t"
    r = run(["exec", "-T", "db", "psql", "-U", "nextcloud", "-d", "nextcloud", "-qtA", "-v", "ON_ERROR_STOP=1", "-c", wrapped], check=False)
    if r.returncode != 0:
        raise AssertionError(f"SQL failed: {query}\n{r.stderr}")
    return json.loads(r.stdout)


def execute(statement: str) -> None:
    r = run(["exec", "-T", "db", "psql", "-U", "nextcloud", "-d", "nextcloud", "-qtA", "-v", "ON_ERROR_STOP=1", "-c", statement], check=False)
    if r.returncode != 0:
        raise AssertionError(f"SQL failed: {statement}\n{r.stderr}")


def scalar(query: str):
    rows = sql(query)
    return next(iter(rows[0].values())) if rows else None


@dataclass
class Response:
    status: int
    body: str

    def json(self):
        return json.loads(self.body)


def http(method: str, path: str, user: str, password: str, data: dict | None = None) -> Response:
    """A request to Nextcloud from inside its container, as `user` with an app password."""
    args = ["exec", "-T", "nextcloud", "curl", "-sS", "-X", method, "-u", f"{user}:{password}",
            "-H", "OCS-APIRequest: true", "-H", "Accept: application/json",
            "-w", "\n%{http_code}", f"http://localhost/index.php{path}"]
    if data is not None:
        args[-1:-1] = ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    r = run(args, input=json.dumps(data) if data is not None else None, check=False)
    if r.returncode != 0:
        raise AssertionError(f"{method} {path}: curl failed: {r.stderr}")
    body, _, code = r.stdout.rpartition("\n")
    return Response(int(code), body)


def manifest() -> dict:
    return json.loads((ROOT / "tests/data/manifest.json").read_text())


def file_ids() -> dict[str, int]:
    """Manifest file (e.g. 'portraits/x.jpg') → Nextcloud file id, for the test user."""
    rows = sql(f"""SELECT f.fileid, f.path FROM oc_filecache f JOIN oc_storages s ON s.numeric_id = f.storage
                   WHERE s.id = 'home::{TEST_USER}' AND f.path LIKE 'files/{PHOTOS}/%'""")
    prefix = f"files/{PHOTOS}/"
    return {r["path"][len(prefix):]: int(r["fileid"]) for r in rows}


def centre(d: dict) -> tuple[float, float]:
    return float(d["x"]) + float(d["width"]) / 2, float(d["y"]) + float(d["height"]) / 2


def matches(person: dict, box: dict) -> bool:
    """Is this detection box the ground-truth face of `person`? (centres within 0.6 face sizes)"""
    cx, cy = centre(box)
    reach = 0.6 * max(person["size"], float(box["width"]), float(box["height"]))
    return ((cx - person["x"]) ** 2 + (cy - person["y"]) ** 2) ** 0.5 <= reach


def identity_of(photo: dict, box: dict) -> str | None:
    for p in photo["people"]:
        if matches(p, box):
            return p["name"]
    return None

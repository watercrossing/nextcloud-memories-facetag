# memories_facetag — notes for working on this repo

A Nextcloud app (id `memories_facetag`) adding manual face tagging to Memories' viewer on top of Recognize, plus a library re-scan. Start with `README.md`.
This repo is public: no hostnames, paths, container names or people from any real installation in code, tests or CI.

## Compatibility tests (`tests/ci/`)

Real Nextcloud + PostgreSQL + Memories + Recognize, with Recognize's real face pipeline, in Docker.

```sh
tests/ci/stack.sh up        # Nextcloud (nextcloud:<ver>-apache) + Postgres; pinned Memories/Recognize; this app from the checkout
tests/ci/stack.sh dataset   # photo set into user alice, files:scan, memories:index, recognize:classify + cluster-faces, name clusters
tests/ci/stack.sh test      # server-side pytest suite (tests/ci/server/)
tests/ci/stack.sh down      # remove containers and volumes
MEMORIES_VERSION=11.1.3 RECOGNIZE_VERSION=12.0.2 tests/ci/stack.sh all
```

- **Versions are pinned** in `tests/ci/versions.json` (download URL + sha256, App Store signature verified when pinning). `tests/ci/versions.py pin` re-resolves: per Nextcloud major in `NEXTCLOUD`, the latest two stable releases of each app within `Compatibility::TESTED_MAJORS`. Widen `TESTED_MAJORS`, re-pin, and the matrix widens. CI's matrix is `versions.py matrix`.
- **Current matrix:** Nextcloud 34 (latest 34.x image) × Memories 11.1.4, 11.1.3 × Recognize 12.0.3, 12.0.2. Scope is Nextcloud 34 and newer only.
- **The containers need no network.** Everything is downloaded on the host into `FACETAG_CACHE` (default `.cache/`, gitignored): app tarballs, Recognize's native runtime (`recognize_runtime.py`), photos. Postgres shares Nextcloud's network namespace, and the tests reach Nextcloud via `docker compose exec … curl`, so nothing is published and no traffic between containers is needed. `FACETAG_NETWORK` puts the stack on an existing docker network instead.
- **Recognize's runtime:** its install step would download node, CPU *and* GPU libtensorflow and ffmpeg (~650 MB) on every run. `stack.sh` puts node in place first, lets the step's libtensorflow download fail at once (`TFJS_NODE_CDN_STORAGE` pointed at a dead address — only a warning), then installs the CPU libtensorflow and tfjs-node binding the release declares. Faces need no `recognize:download-models`: face-api's models ship in the release tarball.
- **Photo set:** `tests/data/manifest.json` (Wikimedia Commons, freely licensed; credits in `tests/data/ATTRIBUTION.md`), fetched and verified by `tests/data/fetch.py`; images are never committed. `tests/data/curate.py` rebuilt the set; identities come from Commons' "depicts" statements and titles, never from looking at faces.

- **Tests** (`tests/ci/server/`, pytest, run in file order on one shared stack): `test_1_stack` (self-check, Recognize's results), `test_2_api` (every route, as `alice` with an app password, rows checked in `recognize_face_detections` / `facetag_manual` after each call), `test_3_scan` (settle time, `--dry-run`, real run, re-run, `--recheck`, `--undo`), `test_4_guard` (installed `Compatibility.php` stubbed to an untested Memories major: API 503, scan refuses, check fails; `ignore_versions` lets it run). Helpers for occ/SQL/HTTP and the ground truth are in `tests/ci/stackctl.py`. One test: `tests/ci/stack.sh test -k locate -s`. After changing app code: `tests/ci/stack.sh app`, then rerun.
- **Expected numbers on the pinned versions** (Nov 2026): Recognize finds 235 faces and makes 12 clusters, each one person; locate finds all 27 ground-truth faces Recognize missed and names each right first; the scan adds 32 faces and names 9, all right. The tests assert looser floors, so a drop shows as a failure, small noise doesn't.
- **Timing:** about 6 minutes per stack locally from cold (cached downloads), of which Recognize ~30 s and the tests ~5 minutes.
- **CI** (`.github/workflows/compat.yml`): on push, pull_request, and workflow_dispatch with `nextcloud` / `memories` / `recognize` inputs (an unpinned release is resolved from the App Store and its signature checked). Caches: app tarballs per version, Recognize's runtime per Recognize version, photos per manifest hash. Logs (nextcloud.log, container logs, Recognize's classify output) are uploaded on failure.

## Adding to the harness (sessions 2 and 3)

- **UI tests:** `stack.sh up && stack.sh dataset` gives a Memories with 118 indexed photos and 12 named people for `alice`. A browser needs a login: `stack.sh` gives alice a random password, so set a known one for Playwright (`OC_PASS=… occ user:resetpassword --password-from-env alice`) and reach Apache from the host (no port is published: add one bound to 127.0.0.1 in a compose override, or run Playwright in a container sharing Nextcloud's network namespace like `db` does). Ground-truth face positions per photo are in `tests/data/manifest.json` (`people[].x/y/size`, relative to the upright image).
- **Embedding baseline:** Recognize's descriptors are in `recognize_face_detections.face_vector`; match them to ground-truth people with `stackctl.identity_of` and compare to a stored baseline. `curate.py detect` shows how to run the face model on arbitrary files in the stack.
- **New versions:** `versions.py pin` re-resolves the matrix from `TESTED_MAJORS`; `versions.py resolve APP VERSION` checks one release.

## Traps (all found the hard way)

**Server side**

- Nextcloud's root `package.json` has `"type": "module"`, so the node scripts in `bin/` must be `.cjs`.
- They need `@tensorflow/tfjs-node` (Recognize's tfjs-node mode), not the pure-JS/WASM fallback.
- The scan skips photos Memories indexed in the last 24 h (`Scanner::SETTLE_SECONDS`, to let Recognize go first) — in a fresh instance that is every photo. Use `--settle-hours 0`.
- The scan also skips files still in `recognize_queue_faces`; drain Recognize's queue first (`recognize:classify` clears and processes it).
- Recognize never re-processes a photo that already has a face row, so run Recognize before adding manual tags.
- Recognize clusters only once a user has 120 face detections of at least 3% of the image (`FaceClusterAnalyzer::MIN_DATASET_SIZE`, `MIN_DETECTION_SIZE`).
- Bumping `appinfo/info.xml`'s version makes Nextcloud answer 503 to everything until `occ upgrade` runs (`./deploy` does it).
- Suggestion and scan-naming references deliberately exclude faces the scan added (`facetag_manual.source` `scan`/`scanknn`); including them caused a runaway feedback loop in production. Keep it that way.
- `ScanJob` runs only inside Nextcloud's maintenance window if one is set; tests call the scan via `occ`.
- Nextcloud's `maintenance_window_start = N` runs time-insensitive jobs from N:00 to (N+4):59, not (N+4):00.
- `FaceStore::referenceFaces` uses a `ROW_NUMBER()` window — test on PostgreSQL, as production runs.
- Nextcloud 34 caches app config in APCu for 3 s on the web side (`AppConfig::LOCAL_CACHE_TTL`); `occ config:app:set` can't clear it, so wait before expecting the API to see a change.
- opcache notices a changed PHP file by its mtime, to the second: a file swapped and swapped back within one second keeps the old bytecode. The stack sets `opcache.revalidate_freq=0` (`tests/ci/php-ci.ini`) and the guard test sets distinct mtimes.
- Restarting the `nextcloud` container cuts `db` off (it lives in Nextcloud's network namespace): `stack.sh down` and `up` instead.
- Wikimedia Commons answers bursts of downloads with 429; `fetch.py` paces itself and honours `Retry-After`, and CI fetches the photos once, in the matrix job.

**Viewer (`js/facetag.js`)**

- In Memories, a slide's PhotoSwipe zoom wrapper is full-width and **zero high**, so measure `currSlide.content.element` (`div.pswp__img`) for the image's on-screen box.
- Memories' `_m.viewer.currentPhoto` is looked up in its loaded list and **lags or is empty right after a swipe**; take the photo from `photoswipe.currSlide.data.photo`.
- PhotoSwipe's **focus trap** takes focus back from anything outside `.pswp` that Memories doesn't exempt (its sidebar, popups); the overlay swallows `focusin` for its own elements.
- On touch screens the **click after a tap** lands on whatever is now under the finger; the picker only acts on clicks that started with a press inside it.
- The overlay must let pointer events through (`pointer-events: none` on the layer) so PhotoSwipe keeps swipe, arrows and zoom; a plain tap is detected separately and PhotoSwipe's own tap actions are cancelled only on the photo.
- `_m.viewer.photoswipe` is marked "Debugging only" in Memories' source — the likeliest thing to vanish; `occ memories_facetag:check` looks for it.

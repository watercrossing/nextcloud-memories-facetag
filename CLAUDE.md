# memories_facetag — notes for working on this repo

A Nextcloud app (id `memories_facetag`) adding manual face tagging to Memories' viewer on top of Recognize, plus a library re-scan. Start with `README.md`.
This repo is public: no hostnames, paths, container names or people from any real installation in code, tests or CI.

## Compatibility tests (`tests/ci/`)

Real Nextcloud + PostgreSQL + Memories + Recognize, with Recognize's real face pipeline, in Docker.

```sh
tests/ci/stack.sh up        # Nextcloud (nextcloud:<ver>-apache) + Postgres; pinned Memories/Recognize; this app from the checkout
tests/ci/stack.sh dataset   # photo set into user alice, files:scan, memories:index, recognize:classify + cluster-faces, name clusters
tests/ci/stack.sh test      # server-side pytest suite (tests/ci/server/)
tests/ci/stack.sh ui        # UI suite in Chromium against the real Memories (tests/ci/ui/; needs NC_PORT at `up`)
tests/ci/stack.sh down      # remove containers and volumes
NC_PORT=18080 MEMORIES_VERSION=11.1.3 RECOGNIZE_VERSION=12.0.2 tests/ci/stack.sh all   # up, dataset, test, ui
```

- **Versions are pinned** in `tests/ci/versions.json` (download URL + sha256, App Store signature verified when pinning). `tests/ci/versions.py pin` re-resolves: per Nextcloud major in `appinfo/info.xml`'s min-version…max-version range, the latest stable releases of each app within `Compatibility::TESTED_MAJORS` — two per app on the newest Nextcloud major, one on older ones (so two Nextcloud majors are 5 cells, not 8). Widen `TESTED_MAJORS` or raise max-version, re-pin, and the matrix widens — the release watcher (below) does that. CI's matrix is `versions.py matrix`. The App Store answers bursts with 429; `http_get` retries with `Retry-After`.
- **Current matrix:** whatever `versions.py matrix` says (Nov 2026: Nextcloud 34 × Memories 11.1.4, 11.1.3 × Recognize 12.0.3, 12.0.2, until the watcher's widen PR for Nextcloud 35 + Recognize 13 is merged). Scope is Nextcloud 34 and newer only.
- **The containers need no network.** Everything is downloaded on the host into `FACETAG_CACHE` (default `.cache/`, gitignored): app tarballs, Recognize's native runtime (`recognize_runtime.py`), photos. Postgres shares Nextcloud's network namespace, and the tests reach Nextcloud via `docker compose exec … curl`, so nothing is published and no traffic between containers is needed. `FACETAG_NETWORK` puts the stack on an existing docker network instead.
- **Recognize's runtime:** its install step would download node, CPU *and* GPU libtensorflow and ffmpeg (~650 MB) on every run. `stack.sh` puts node in place first, lets the step's libtensorflow download fail at once (`TFJS_NODE_CDN_STORAGE` pointed at a dead address — only a warning), then installs the CPU libtensorflow and tfjs-node binding the release declares. Faces need no `recognize:download-models`: face-api's models ship in the release tarball.
- **Photo set:** `tests/data/manifest.json` (Wikimedia Commons, freely licensed; credits in `tests/data/ATTRIBUTION.md`), fetched and verified by `tests/data/fetch.py`; images are never committed. `tests/data/curate.py` rebuilt the set; identities come from Commons' "depicts" statements and titles, never from looking at faces.

- **Tests** (`tests/ci/server/`, pytest, run in file order on one shared stack): `test_1_model` (Recognize's face model against the baseline, below), `test_1_stack` (self-check, Recognize's results), `test_2_api` (every route, as `alice` with an app password, rows checked in `recognize_face_detections` / `facetag_manual` after each call), `test_3_scan` (settle time, `--dry-run`, real run, re-run, `--recheck`, `--undo`), `test_4_guard` (installed `Compatibility.php` stubbed to an untested Memories major: API 503, scan refuses, check fails; `ignore_versions` lets it run). Helpers for occ/SQL/HTTP and the ground truth are in `tests/ci/stackctl.py`. One test: `tests/ci/stack.sh test -k locate -s`. After changing app code: `tests/ci/stack.sh app`, then rerun.
- **Expected numbers on the pinned versions** (Nov 2026): Recognize finds 235 faces and makes 12 clusters, each one person; locate finds all 27 ground-truth faces Recognize missed and names each right first; the scan adds 32 faces and names 9, all right. The tests assert looser floors, so a drop shows as a failure, small noise doesn't.
- **Timing:** on GitHub 9–11 minutes per cell with warm caches (varies with the runner): stack ~1 min, photos + Recognize ~45 s, server tests 4–5 min (the guard test's `occ setupchecks` is the slow part), UI tests ~3 min; Chromium installs in the background during the stack's start. Caches: Memories ~55 MB, Recognize + runtime ~240 MB per version, photos ~50 MB, Chromium ~700 MB (shared by all cells). The stand-in job takes under a minute.
- **CI** (`.github/workflows/compat.yml`): on pull requests, pushes to `main`, and workflow_dispatch with `nextcloud` / `memories` / `recognize` inputs (an unpinned release is resolved from the App Store and its signature checked; `nextcloud` may be an image tag, a prerelease like `36.0.0beta1`, or `master`; empty inputs: every pinned cell). Caches: app tarballs per version, Recognize's runtime per Recognize version, photos per manifest hash. Logs (nextcloud.log, container logs, Recognize's classify output) are uploaded on failure.

- **UI tests** (`tests/ci/ui/`, Playwright/Chromium, Python): after the server tests, on the same stack, Nextcloud published on `127.0.0.1:NC_PORT`. The session fixture undoes the scan and hand-placed tags; each test gets a fresh browser context (cookies from one login, tagging mode not yet remembered) and `restore_faces` puts every face row and person back afterwards. Files: `test_ui_1_mode` (button, on/off, sticky across closing the viewer, a reload and reopening from the folder view, Esc), `test_ui_2_placement` (on-screen boxes vs their detection rows and the manifest's ground truth, desktop and Pixel 7; wheel zoom and drag-pan over a face), `test_ui_3_picker` (tap a missed face → found, right person first, picked → `recognize_face_detections`/`facetag_manual` rows; typing filters with accents folded, a new person, focus kept, Enter; change and remove an existing tag; Esc closes the picker first), `test_ui_4_navigation` (arrows, arrow keys, a CDP touch swipe on a Pixel 7, also one starting on a face: the new photo's tags, and a tap locates on the new photo), `test_ui_5_touch` (landscape Pixel 7, where the picker opens under the finger: the click after the tap picks nothing; tapping a tag removes nothing). Helpers in `tests/ci/ui/viewer.py` (shared with the stand-in). On failure: a screenshot, and with `UI_TRACE=retain-on-failure` (CI) a Playwright trace, in `UI_ARTIFACTS` (CI uploads them as `ui-…`). Playwright is pinned in `stack.sh` (`PLAYWRIGHT=`), so CI's Chromium cache matches.
- **Stand-in viewer** (`tests/ui-harness/`, see its README): PhotoSwipe set up as Memories does with the API mocked, `js/facetag.js` unchanged; 7 tests in ~10 s, no stack — for iterating on the script. CI runs it as the `ui-standin` job. It copies only the Memories behaviours that hid bugs, so the real suite decides.
- **Face-model baseline** (`tests/data/model-baseline.json`, `tests/ci/model_baseline.py`): Recognize's own detections of the ground-truth faces — how many it detects, all faces it finds, descriptor length and norm, within/between-person distance quantiles, leave-one-out nearest-person accuracy scored like the app's suggestions, and one rounded descriptor per person. `test_1_model` fails, listing what moved and what to re-tune, when a cell differs beyond `TOLERANCE`. Regenerate on a freshly loaded stack (`stack.sh dataset`, before the scan) with `python3 tests/ci/model_baseline.py --write`; without `--write` it prints the measurements and the comparison. `tests/ui-harness/eval_suggestions.py --stack` (or on a real library's export) is the re-tuning tool.

## Release watcher (`tests/ci/watch.py`, `.github/workflows/release-watch.yml`)

Nightly at 02:17 UTC, and by hand (workflow_dispatch: `dry_run`, `only`, `force`, `fixture`). `watch.py plan` shows what it would do without touching anything; `watch.py run --dry-run` also reads the open PRs and issues.

- **repin** — a newer release within the tested majors: branch `watch/repin` re-pinning `versions.json`, a PR, and `compat.yml` dispatched on the branch; that run is the PR's check. A PR closed unmerged with the same releases is not proposed again.
- **widen** — a new major of Memories or Recognize, or a newly released Nextcloud major (from the official Docker image's `versions.json`): branch `watch/widen` with `TESTED_MAJORS` widened, `info.xml` max-version raised and its patch version bumped, the README's `<!--tested:…-->` markers and `versions.json` updated; the matrix is dispatched on it and **awaited**. All green (face-model test included) → a PR saying it needs `./deploy` after merging; anything red → one issue (label `release-watch`) naming the failed cells and linking the run, updated (and reopened) rather than duplicated, closed when a later run passes. A Nextcloud major is only added once both apps have a release for it; until then the run's summary says which app is missing.
- **prerelease** — a beta/RC of a Nextcloud major newer than any release (`download.nextcloud.com/server/prereleases/`): branch `watch/nextcloud-next` (the widened state plus max-version raised to it) and one cell with the newest app releases, **force-enabled** (`app:enable --force`) where they don't declare support yet. Allowed to fail: one issue per major, open while failing, closed while passing, closed for good once that major is released (it is then a widen).
- **Idempotent:** every PR/issue body carries `<!-- release-watch:KIND key=… base=… -->`; the key hashes what is proposed (Nextcloud majors, tested majors, chosen releases, dispatch inputs). Same key → nothing happens. A failed widen/prerelease is retried when `main`'s tree changes (`base=`), e.g. after a fix.
- **Tokens:** pushes and PRs made with `GITHUB_TOKEN` start no workflows, so the watcher dispatches `compat.yml` on its branches itself (`actions: write`; workflow_dispatch is exempt). It needs `contents`, `pull-requests`, `issues: write` too, all declared in the workflow, and the repository setting "Allow GitHub Actions to create and approve pull requests" (on since 2026-11-26) — without it the run fails after pushing the branch, naming the setting. Dispatched runs attach to the branch's head commit, so the PR shows them as its checks.
- **Simulations:** `tests/ci/watch-fixtures/*.json` fake the release sources (`FACETAG_WATCH_FIXTURE`; see its README): `nothing-new`, `new-minor`, `new-major-pass` (empty: the real world), `new-major-fail`, `prerelease`. With a fixture, branches are `watch-sim/NAME/…`, titles `[simulation NAME]`, label `release-watch-sim`, PRs drafts. An `alias` fixture release installs the real one, so the installed-version test fails — that is how `new-major-fail` fails.
- **Nextcloud prereleases:** the official image has published no beta/RC tags since 21.0.0beta7 / 20.0.8-rc (Feb 2021; its `update.sh` filters them out). `stack.sh` takes `NEXTCLOUD_VERSION=36.0.0beta1` or `master` (daily build): `versions.py server` downloads the tarball, checks its sha512 and Nextcloud's GPG signature (key `28806A87…937A`, the one the image uses), and the image is built from the newest released `nextcloud:N-apache` with `/usr/src/nextcloud` swapped (its `*.config.php` snippets kept). PHP comes from that base image: a major needing a newer PHP would fail at install.
- **Cost:** a night with nothing new is one ~1 min job (three App Store listings, no downloads). A new release costs one matrix (5 cells × 9–11 min on two Nextcloud majors) plus the watcher job waiting for it; a new prerelease one cell.
- **Flakes don't decide a major:** a failed run is rerun once (`gh run rerun --failed`) before the watcher calls it red; the PR or issue says which cells needed it. The Chromium install is retried too (Ubuntu's mirrors sometimes 404 a package the runner's apt index lists).
- **Bot PRs get an extra `pull_request` run in state `action_required`** (GitHub holds runs for PRs opened by `github-actions[bot]` for approval). Ignore it: the dispatched run is the check. Approving it just runs the matrix twice.
- **Shown working 2026-11-26** (draft PR #3), each path on GitHub with the workflow's own token, through a temporary `push` trigger on `watch-test/FIXTURE` branches (removed; after merging, use workflow_dispatch's `fixture` input): `nothing-new` → nothing; `new-minor` → [PR #4](https://github.com/watercrossing/nextcloud-memories-facetag/pull/4) with its dispatched run; `new-major-pass` → 5 cells green, [PR #7](https://github.com/watercrossing/nextcloud-memories-facetag/pull/7); `new-major-fail` → [issue #5](https://github.com/watercrossing/nextcloud-memories-facetag/issues/5); `prerelease` → Nextcloud 35.0.0rc4 green, [issue #6](https://github.com/watercrossing/nextcloud-memories-facetag/issues/6) opened and closed. Rerunning each on the same commit: "nothing new" / "unchanged". Before the repository setting was on, `new-minor` failed naming it. The simulation PRs and issues are closed.
- **Nextcloud 35 and 36 as of 2026-11-26:** 35 is released (35.0.3; the image's `latest`, not yet `stable`), Recognize 13 exists only for 35+, Memories 11 declares 34–35. Locally all 40 tests pass on 35.0.3 + Memories 11.1.4 + Recognize 13.2.1, and on `master` (36.0.0 dev, daily build of 2026-11-25, on the 35 image's PHP 8.5) with Memories 11.1.4 force-enabled + Recognize 13.2.1. No 36 prerelease is published yet; the watcher picks up the first beta.
- **Trap:** a cell's `nextcloud` value is passed to `stack.sh` as-is, so it can be a major (`35`), a full tag (`35.0.3`), a prerelease or `master`.

## README screenshots and animation (`tests/readme-media/`)

`capture.py` regenerates `docs/media/` (before/after/picker stills and the `tagging.webp` clip) from the real Memories in the stack — after a UI change, a Memories/Recognize upgrade, or to show new timings:

```sh
export NC_PORT=18080
tests/ci/stack.sh up && tests/ci/stack.sh dataset
uv run --no-project --with playwright python tests/readme-media/capture.py   # --reset to run again on the same stack
```

Needs ffmpeg with libwebp. Scripted motion (zoom, the drawn fingertip) is captured in even steps; waits (face detection) in real time, so the clip shows real speed. Stills are lossy WebP to keep git history small. It refuses if the expected person isn't suggested first, saving `docs/media/failed-animation.png`. CI can run it by hand (workflow_dispatch, `readme_media`), uploading `docs/media` as an artifact.

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
- **Memories publishes `_m.viewer.photoswipe` before `init()`** (Viewer.vue: `createBase` sets it, `openDynamic` calls `init()` only after `await createBase(...)`), and re-renders in between: hook PhotoSwipe only once `pswp.element` exists, or the hook can be marked done with no listeners and taps never register for that viewer — intermittently, depending on timing. Found by the UI suite as a whole run failing every tap after a point; fixed in 0.6.2; the stand-in reproduces the order deterministically.
- **Tags take no pointer events** (box and label): PhotoSwipe listens on its own element, so anything on top that takes events swallows a swipe, pinch, drag or wheel zoom starting there — and on a phone faces cover much of a portrait. Taps on tags are found by hit-testing in the tap detector, and hover (`.hover`, pointer cursor) is tracked from `pointermove`. Found by the UI suite; fixed in 0.6.2.

**UI tests (Playwright)**

- The sync API delivers events (`page.on("request")` etc.) only while a Playwright call runs: poll with `page.wait_for_timeout`, never `time.sleep`.
- Don't wait for `networkidle` on Nextcloud (it polls); wait for the request you mean to have finished.
- PhotoSwipe zooms only up to the size Memories loaded: on some photos max zoom = fit, and a wheel does nothing. Pick a photo with `zoomLevels.max / fit >= 2`.
- Tagging is sticky: a test that opens a second photo finds it already on, and clicking the button again switches it off (`tagging_on` handles both).
- Click a folder-view thumbnail by `.img-outer`; its `img` is covered by it.
- Playwright's `touchscreen.tap` is a real touch tap, ghost click included; it has no swipe, so `viewer.swipe` sends CDP `Input.dispatchTouchEvent`.
- **Memories hides the viewer controls 2 s after the mouse last moved over the photo** (desktop; `pswp--ui-visible` removed, controls get `pointer-events: none`), and keeps them while the pointer is over a control. Click controls with `viewer.click_control`, which moves onto them first — a plain `locator.click()` raced the timer after a tap and the picker's ~2 s of suggestions.
- **File ids follow the order `files:scan` met the files, which varies between runs**; the viewer orders a folder by date taken. Pick "a photo with neighbours" by `datetaken`, not by file id (the navigation test once started next to the last photo, so ArrowRight had nowhere to go).
- The ghost-click test only means something when the picker opens under the finger: on a Pixel 7 in portrait it opens below and never does, so the test uses landscape and asserts it happened at least once.

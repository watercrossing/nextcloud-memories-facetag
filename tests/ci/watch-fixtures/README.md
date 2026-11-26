# Release-watcher fixtures

Fake views of the release sources for `tests/ci/watch.py`, so each of its paths can be exercised
without waiting for a real release. Point `FACETAG_WATCH_FIXTURE` at one (`watch.py --fixture FILE`
does), or pass its name as the `fixture` input of the Release watch workflow.

A fixture may set:

- `nextcloud_releases`: `{major: version}` in place of the official Docker image's list;
- `prereleases`: Nextcloud prereleases in place of the download server's list (oldest first);
- `store.hide`: `{app: [versions]}` — App Store releases to leave out;
- `store.alias`: `{app: {fake version: real version}}` — a release that doesn't exist, served by a
  real one (its download, signature and platform range). The stack installs the real one, so the
  test that the installed version is the requested one fails: an alias always fails the matrix.

Everything else is real: the App Store, signatures, downloads, the stack.

| Fixture | The world it pretends | What the watcher does |
| --- | --- | --- |
| `nothing-new.json` | Nextcloud 34 is the newest release, no prereleases | nothing |
| `new-minor.json` | Memories 11.1.4 doesn't exist: the newest two are 11.1.3, 11.1.2 | re-pin PR, matrix dispatched on it |
| `new-major-pass.json` | the real world (empty fixture): on 2026-11-26, Nextcloud 35 released and Recognize 13 only for it | matrix on the widened branch (5 cells) passes → PR |
| `new-major-fail.json` | a "Memories 12.0.0" (really 11.1.4) | matrix on the widened branch fails → issue |
| `prerelease.json` | September 2026: Nextcloud 35 in RC, 34 the newest release | one allowed-to-fail cell on 35.0.0rc4 → issue |

With a fixture, the watcher's branches are `watch-sim/<fixture>/…`, its titles say
`[simulation <fixture>]`, its label is `release-watch-sim` and its pull requests are drafts, so a
simulation never touches the real `watch/…` branches, PRs or issues.

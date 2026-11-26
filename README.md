# Manual face tags for Nextcloud Memories

A Nextcloud app that adds Facebook-style manual face tagging to the [Memories](https://github.com/pulsejet/memories) photo viewer, on top of [Recognize](https://github.com/nextcloud/recognize)'s face recognition, plus a second detection pass that finds the faces Recognize misses.
It addresses recognize issue [#611](https://github.com/nextcloud/recognize/issues/611).

Recognize misses a lot of faces in group photos: it detects on a 1024px preview, drops any face it scores under 0.9 or that is tilted more than 30°, and never revisits a photo once it has found a face in it.
On one 12-person group photo it kept 8.

## What you get

**Tagging in the viewer.**
Open a photo in Memories and press the tag button in the viewer's top bar.

- Every face already on the photo shows a box and a name; faces Recognize found but nobody named are dashed, with "Who is this?".
- **Click anywhere on the photo** to tag someone.
  A fixed-size square appears; the app looks for a face under it with Recognize's own model, snaps the square onto it if it finds one, and puts the most likely people at the top of the list (★).
  Below that is every named person, filtered as you type; a name that doesn't exist yet creates that person.
- **Click a tag** to change who it is, or to remove it.
- **Finished tagging** or Esc to leave. The arrow keys still move between photos while you tag.

Tags are ordinary Recognize face detections, so they appear in Memories' People views like any other face.

**A second pass over the library** (`occ memories_facetag:scan`, or the background job).
It looks at every photo again at 2048px, keeps faces from score 0.8, skips the roll check (it misfires on small faces), and adds only faces that don't overlap one already there.
A new face whose nearest named person is a clear match is put under that person; the rest go in unassigned for Recognize's clustering to place, and show up as "Who is this?" in tagging mode.

## Requirements

- Nextcloud 34, with Memories 11 and Recognize 12, face recognition enabled and running on Recognize's **tfjs-node** backend (not the pure-JS/WASM fallback).
- The app runs Recognize's bundled `node` and models; it ships no models of its own.

## Installing

It is not in the App Store. Copy `appinfo lib js css bin` into `custom_apps/memories_facetag` and enable it:

```sh
occ app:enable memories_facetag
```

If Nextcloud runs in Docker, `./deploy` does that (container name in `CONTAINER`, default `nextcloud`), and runs `occ upgrade` when the version changes.

⚠️ Because it is not from the App Store, `occ app:update` leaves it alone — but would replace it if an App Store app ever took the id `memories_facetag`.

## The library scan

Try it on one folder first:

```sh
occ memories_facetag:scan --user alice --path "Photos/2023 Holiday" --dry-run -v
occ memories_facetag:scan --user alice --path "Photos/2023 Holiday" -v
```

Then either run it over everything (`occ memories_facetag:scan --max-minutes 600`, resumable), or switch on the background job, which works through the backlog in 8-minute slices every 10 minutes and afterwards gives each new photo its second pass a day after Recognize's first:

```sh
occ config:app:set memories_facetag scan_enabled --value=yes
occ config:app:set memories_facetag scan_minutes --value=8     # optional; minutes per 10-minute slot
```

Throughput is about 1.5–2 photos a second on a modern CPU, with the detector at `nice 19`.

Options worth knowing:

- `--assign-distance 0` leaves all naming to Recognize's clustering.
  The default (0.40, with `--assign-margin 0.10`) names a new face only if its nearest person, by the mean distance to their five closest faces, is within 0.40 and at least 0.10 closer than anyone else.
  On a library of ~55,000 photos and 128 named people this named 63% of faces and disagreed with the existing name on 1.3% of them — some of which were Recognize's own mistakes.
  Recognize's clustering alone placed 18 of 75 new faces on a trial folder (all correctly) and rejected the rest, many of them unambiguous.
- `--min-score`, `--max-yaw`, `--min-size` change the detection limits.
- `--undo` removes every face the scan named itself and every face it added that has not since been put under a named person, and forgets which photos were scanned.

Photos indexed in the last 24 hours, and photos still in Recognize's queue, are left for Recognize to go first.

## How it works

- `js/facetag.js` is a plain script with no build step, loaded only on Memories' own pages and only for logged-in users.
  It finds Memories' viewer through the `globalThis._m.viewer` object Memories exposes (`isOpen`, `currentPhoto`, `photoswipe`), adds one button to `.memories-viewer .top-bar`, and draws everything else in an overlay of its own.
  Memories is not modified, so Memories updates don't overwrite it — but a Memories release that renames those hooks makes the button disappear (nothing else breaks).
- **Finding a face** (`bin/face-locate.cjs`) runs under Recognize's `node` with Recognize's `node_modules`, so it uses the same models (face-api SSD-MobileNet, 68-point landmarks, 128-d recognition net) and its descriptors live in the space Recognize clusters in.
  It looks at a 2048px preview: first the whole image with a 0.3 confidence floor, taking the face nearest the click; failing that, a crop five tag-boxes wide around the click.
  About 1.2 s per click.
- **Suggestions** score each named person by the mean distance to their 5 nearest faces among their 60 newest, and offer people under 0.55.
  Tested against 80 faces Recognize had already named: right person first 95% of the time, in the top three every time.
- **Storing:** a tag is a row in `recognize_face_detections` with `cluster_id` set.
  Its descriptor is the one found under the click; if none was found, the person's centroid (zeros for a brand-new person), so it neither drags Recognize's clustering nor attracts strangers.
  `facetag_manual` records which rows this app added, and how (`manual`, `scan`, `scanknn`); `facetag_scanned` is the scan's progress.
- **Removing:** hand-placed tags and unnamed faces are deleted; a named face Recognize found is taken out of its person the way Recognize's own "remove from person" does (cluster cleared, threshold set to its distance from the centroid so it isn't put straight back).

## Caveats

- Tags are per user, as Recognize's detections are: tagging a shared photo tags it for you only.
- Recognize skips any photo that already has a face, so a manual tag on a photo Recognize has *not yet processed* means it never auto-detects that photo.
- `occ recognize:reset-faces` and `reset-face-clusters` delete these tags along with everything else.
- Recognize has no PHP API for faces; `lib/Service/FaceStore.php` reads and writes its tables directly, so re-check them on a Recognize major release.
- `appinfo/info.xml` caps Nextcloud at 34: a major upgrade disables the app until the cap is raised and it is retested.

## Licence

AGPL-3.0-or-later, like Recognize and Memories.

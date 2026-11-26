# Stand-in viewer and suggestion evaluation

## The stand-in viewer (`site/`, `conftest.py`, `test_standin.py`)

A stand-in for Memories' photo viewer that `js/facetag.js` runs against unchanged, with the app's API mocked in memory: for iterating on the script in seconds, without the test stack.

```sh
uv run --no-project --with pytest --with playwright==1.65.2 --with pillow python -m pytest tests/ui-harness
```

The real-Memories suite (`tests/ci/ui/`) covers the same ground and more, and is what decides; CI runs both.

- PhotoSwipe 5.4.4, the build Memories 11 ships (MIT), is fetched from npm into `.cache/ui-harness/` and checked against its sha256; the photo (four ovals on 8192×5464) is drawn with Pillow.
- `site/index.html` sets PhotoSwipe up the way Memories does, and copies the Memories behaviours that hid real bugs:
  - slide content is a `div.pswp__img` holding `img.ximg`, and the zoom wrapper is `width:100%` and zero high (Memories' CSS) — the cause of tags drawn in the corner;
  - `_m.viewer.currentPhoto` comes from a list indexed by `currIndex` that only has the first photo loaded — the cause of "no face found" after a swipe;
  - PhotoSwipe's focus trap, exempting only Memories' own sidebar and popups — the cause of the name box not taking typing;
  - `_m.viewer.photoswipe` published, and the page re-rendered, before PhotoSwipe's `init()` — the cause of taps intermittently never registering;
  - Memories' PhotoSwipe options (`wheelToZoom`, no loop), its viewer layout with a 400px sidebar collapsing on phones, and a `viewport` meta tag (without it a phone emulation lays out at 980px).
- It does not copy Memories' gestures beyond PhotoSwipe's own, its top bar, closing and reopening the viewer, or anything server-side.

## `eval_suggestions.py`

Leave-one-out evaluation of the app's distance limits on named faces (numpy): suggestion ranking (nearest faces, people already on the photo excluded, the time-context bonus at several windows and weights, the 0.55 cut-off) and the scan's naming rule (0.40 / 0.10) over a grid.
Run it when `tests/ci/server/test_1_model.py` reports that Recognize's face model changed:

```sh
uv run --no-project --with numpy python tests/ui-harness/eval_suggestions.py --stack                    # the CI photo set
uv run --no-project --with numpy python tests/ui-harness/eval_suggestions.py faces.txt clusters.txt     # a real library's export
```

The CI photo set has 12 well-separated people and no useful dates, so it only shows whether the limits still roughly fit; tune on an export from a real library (the script's docstring has the query).
The limits were tuned this way on about 1,500 named faces in November 2026.

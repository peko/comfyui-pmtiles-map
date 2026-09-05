# comfyui-pmtiles-map

Save ComfyUI renders into a **PMTiles v3** archive as map tiles, and browse the
whole session as a zoomable map — with search, saved points of interest, live
updates as renders land, and full-resolution previews.

A long run leaves hundreds of loose PNGs that you scroll past once. This turns it
into a map: every render has coordinates, the coarser zoom levels are built
automatically, and the prompt, seed and model behind any tile are one click away.

```
SavePMTilesMap  ->  output/maps/<name>.pmtiles   (+ <name>.tiles.db)
                    http://127.0.0.1:8188/map/
```

Renders you already have go in too: `tools/pmtiles_import.py <folder-or-.7z>`
pads each image into its own block of tiles and lays the blocks along the same
Hilbert curve, so a finished collection becomes a map without ComfyUI running at
all. And `tools/pmtiles_export.py` writes a directory you can upload anywhere —
or hand to someone with no web server, since it carries a dependency-free one.

## Install

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/peko/comfyui-pmtiles-map
pip install -r comfyui-pmtiles-map/requirements.txt     # pmtiles>=3.7
```

Restart ComfyUI. The viewer is then served by ComfyUI itself at
**`/map/`**, and the nodes appear under **`image/pmtiles`**.

Requires Pillow (ComfyUI already depends on it) and `pmtiles` (pure python, ~30 KB).
Developed against ComfyUI 0.30 / Python 3.13; nothing in it is version-specific.

## Nodes

### Save PMTiles Map Tile (`SavePMTilesMap`)

Writes each image into the archive at `z/x/y` and rebuilds the coarser pyramid
levels down to `pyramid_to_zoom`.

| input | meaning |
|---|---|
| `images` | renders to place; every image in a batch gets its own cell |
| `map_name` | writes `output/maps/<name>.pmtiles` and `<name>.tiles.db` |
| `z` | zoom level the render is placed on — the deepest level of the map |
| `x`, `y` | tile coordinates at that zoom; ignored when `coords_mode` is `auto_grid`. Can be wired instead of typed |
| `placement` | `slice` cuts the render into a tile grid at native pixels (1024² at 256 px → 4×4 tiles, which is exactly one tile at `z-2`); `single_tile` resizes the whole render into one tile |
| `coords_mode` | `manual` uses `x`/`y`; `auto_grid` takes the next free block, scanning in growing squares so re-queueing grows a compact map |
| `tile_size` | 256 or 512. One tile size per map — the store refuses to mix |
| `webp_quality` | quality of the tiles written into the `.pmtiles`; independent of how the store keeps its own |
| `store_format` | how tiles are kept in the store — `png` (default), `webp_lossless`, `webp_lossy`. See below |
| `store_quality` | quality for `webp_lossy` only. Default 92 — this is the source of truth, so keep it above `webp_quality` |
| `pyramid_to_zoom` | build derived levels down to this zoom (`0` = a single world tile) |
| `y_scheme` | how to read the `y` input: `xyz` (y from the top, Leaflet/PMTiles) or `tms`. Tiles are always *stored* XYZ |
| `write_archive` | whether the saver ever writes the `.pmtiles` at all. Off = manual only (the viewer's build button or the CLI) |
| `archive_every` *(optional)* | when `write_archive` is on, how many tiles must be waiting before it rewrites. `0` = every save |
| `embed_tile_metadata` | carry per-tile records inside the archive, so the single file is self-describing |
| `store_full_prompt` | additionally keep the entire prompt graph per tile in the store |
| `title`, `tags` | free text, shown in the viewer and searchable |
| `preview` *(optional)* | the thumbnail the node shows in the graph, written to `ComfyUI/temp`: `thumbnail` (384 px WebP, ~42 KB, default), `full` (the whole render, ~2.8 MB), `off` (nothing) |
| `prompt_text`, `negative_text` *(optional)* | the prompt to record when the graph **builds it at runtime** (`FormattedString`, wildcards, a list selector) and it therefore cannot be read off the graph — wire the same string that feeds `CLIPTextEncode.text`. Single-line: the frontend's `addMultilineWidget` hardcodes `minNodeSize = [400, 200]` with no height option, so one multiline field would set the node's minimum size, and these are meant to be wired rather than typed |

Outputs `images` (passthrough, so it can sit mid-chain) and `info` (what was
placed where).

### How tiles are kept in the store

The archive is always WebP at `webp_quality`. `store_format` is about the
**source of truth** behind it, and it matters because a big map is mostly store:
341 KB a tile x 65536 tiles is 22 GB.

Measured on real 512 px render tiles, and on a 4x4 pyramid rebuilt from one
image (PSNR against the lossless pyramid):

| `store_format` | per tile | encode | leaf | z-1 | z-2 |
|---|---|---|---|---|---|
| `png` (default) | 341 KB | 17 ms | — | — | — |
| `webp_lossless` | **238 KB** | 84 ms | **inf** | **inf** | **inf** |
| `webp_lossy` q95 | ~60 KB | ~20 ms | 45.2 dB | 40.8 | 37.2 |
| `webp_lossy` q80 | ~34 KB | 16 ms | 41.3 dB | 36.7 | 33.8 |

**`webp_lossless` is the free one** — a third smaller for pixels that come back
bit for bit, verified against the source array and against the derived pyramid.
It is not the default only because switching an existing map's format is
something to ask for rather than inherit. It also removes the encode cache from
the picture entirely: WebP in the store is already what a browser wants, so
serving passes the stored bytes straight through.

The lossy loss compounds **per pyramid level, not per save**. A parent is
recomposed from its *children*, never from itself, so re-saving the same tile
five times is bit-identical to saving it once (asserted in the tests) — but each
level down encodes an already-encoded image, and that costs ~3.5 dB a level. If
you want `webp_lossy` and depth, set `pyramid_to_zoom == z` while rendering and
build the pyramid once at the end, from leaves that have only been encoded once.

Formats may be mixed within one map: the decoder sniffs the signature, so
changing `store_format` mid-run leaves the older tiles readable.

Recorded per tile: prompt, negative, seed, steps, cfg, sampler, scheduler,
denoise, model files, latent size, timestamp, title and tags.

### Hilbert Index → Tile X/Y (`HilbertXY`)

Turns one integer into `x`/`y` along a Hilbert curve. Wire it into the saver's
`x`/`y` with `coords_mode: manual` and drive `index` from a counter
(`PrimitiveInt` with control **increment**): consecutive renders then land next to
each other on the map *and* contiguously inside the archive, because PMTiles
orders its tiles by the same curve.

`block_size` is tiles per render (a 1024 px render at 256 px tiles is 4), so the
curve walks blocks and multi-tile renders cannot overlap. At `z=5` with 4×4-tile
renders, indices 0–3 give (0,0) (0,4) (4,4) (4,0).

### PMTiles Map Info (`PMTilesMapInfo`)

Reports tile counts per zoom, bounds and archive size for a map, as text.

## Building the pyramid at the end, not on every save

Recomposing ancestors on every save costs one recomposition *per level, per
render*, and rewrites the shallow tiles once per render — on a full z=8 map
(65536 leaves) the z=0 tile would be rebuilt 65536 times. Measured on 256 leaves
at z=4: **1024 recompositions and 4.2 s**, against **85 and 0.6 s** for one
bottom-up pass at the end. The archive rewrite is worse: it is O(whole map) per
save, so a 135 MB archive over 1000 renders writes ~135 GB.

### The two costs are not comparable

Measured with distinct tiles (identical ones are deduplicated by hash and cost
almost nothing):

| | one save | scales with |
|---|---|---|
| store (SQLite) | **~40 KB** of WAL, 0.1 s | changed pages, *not* file size — 40 KB at 4 MB, 44 KB at 35 MB |
| archive (`.pmtiles`) | **the whole file** — 22 MB at 2k tiles, 135 MB at 20k | the map |
| node preview in `temp/` | **2.8 MB** as `full`, **42 KB** as `thumbnail`, 0 as `off` | the render |

So the GB-sized `.tiles.db` files next to your archives are *not* rewritten per
tile; only the archive is. That makes `archive_every` the one knob that matters
for disk wear.

For a large run:

1. set the saver's **`pyramid_to_zoom` equal to `z`** (nothing is recomposed);
2. set **`archive_every`** to a few hundred tiles — or turn `write_archive` off
   entirely for manual-only. A map with tiles in the store but no `.pmtiles` yet
   is still listed in the viewer, marked *not serialized yet*, with the build
   button ready;
3. when the run is done, press **⛰ build** in the viewer — or
   `POST /map/<name>/build`, or `tools/pmtiles_map.py --rebuild-pyramid <map>`
   followed by `--rebuild`.

Measured over 20 saves of 16 leaves each: `archive_every` 0 → 20 rewrites
(3.5 MB), 50 → 10 rewrites (1.8 MB), 200 → 2 rewrites (0.3 MB). The viewer shows
how many tiles are waiting, and the build button lights up.

**What batching costs you:** tiles are served *from the archive*, and the change
feed only publishes what the archive already holds, so live updates lag by up to
`archive_every` tiles. Nothing is lost — the store has every tile — but the map
stops moving in real time.

The build runs in a worker thread with progress, so the viewer (and a running
ComfyUI) stay responsive. While the pyramid is missing the saver marks the map
`pyramid_stale`, the viewer's build button lights up, and **zooming out is capped
two levels below the deepest stored level** — see the note in the viewer section.

## Two ways to read a map

The same page serves both, picked automatically and switchable in the header:

| source | reads | for |
|---|---|---|
| **store** (default when a `.tiles.db` exists) | SQLite, WebP straight from the encode cache | watching a run: a render is visible the moment it is saved, and the archive is never touched — which is what makes `archive_every` batching free of consequences |
| **archive** | the `.pmtiles` itself | checking the file that will be uploaded |

Store reads are also the faster of the two: **0.35 ms vs 2.87 ms** per tile
(median of 300 interleaved requests over one keep-alive connection on a
21846-tile map), because there is no directory to walk. Viewing during a run also
warms the WebP cache that `build_archive` later reuses, so the final build costs
less.

In store mode the change feed drops its `archive_seq` ceiling — a tile is
announced as soon as it is written, since that is when it becomes fetchable.

## Viewer

`http://127.0.0.1:8188/map/` — or, with ComfyUI stopped:

```bash
python tools/serve_pmtiles.py --port 8899        # binds 0.0.0.0
```

* **Live updates**: the saver records which tiles it wrote; the viewer subscribes
  to that feed and refreshes *only those tiles*, flashing each one. No layer
  reload, no polling storm.
* **Search** (left pane, `/`): title, tags and prompt, one result per render.
  Clicking frames the whole render.
* **Liked list**: saved points of interest in `localStorage`, drag to reorder,
  double-click to rename, copy/paste the list as JSON to move it between
  browsers. Hovering any row outlines the target on the map.
* **Double-click a tile**: the full original render, stitched from the lossless
  store rather than the archive's WebP.
* **The view lives in the URL** (`?map=&z=&x=&y=`), so refresh, bookmark and a
  pasted link all restore the same place. Switching archives *keeps* the view —
  two maps built from the same index hold the same subject at the same
  coordinate, which is what makes them comparable.
* **Zoom is bounded by what the archive holds** — two levels below its
  shallowest, one above its deepest, via Leaflet's own `minZoom`/`maxZoom`.
  Zooming out matters most: Leaflet fills a missing level from the nearest
  native one *at the current scale*, so with only z=8 in the archive, map zoom 0
  would ask for 2^8 × 2^8 = **65536 tiles** and hang the browser. Two levels of
  upscaling is ~16 screenfuls. Build the pyramid and the cap lifts by itself.
* **Switching archives reconciles the zoom** into the new map's range explicitly
  (widen the limits, place the view, clamp, tighten) rather than letting Leaflet
  clamp it — which would animate mid-switch and make the outgoing layer fetch
  tiles for a map you are leaving.

### Selecting tiles, and seeing which level they came from

Two canvas overlays, both off by default, toggled by the `mark` and `debug`
checkboxes in the top bar (`x` and `d`).

**`mark`** — triage tiles by dragging:

| shift | alt | ctrl |
|---|---|---|
| approve — green outline with an outward shadow, nothing painted over the tile | reject — dimmed to 62.5% black, so it recedes | deselect |

**Drag** to mark a rectangle of tiles; **click** to mark the whole render under
the cursor. Marking the single tile you happened to click is almost never what
is meant — a render here is a 2×3 block, so five sixths of it would stay
unmarked. The click resolves the deepest tile carrying real metadata under the
point (so a zoomed-out click still lands on the render that made what you are
looking at) and marks the block its own `grid` describes.

The three numbers worth tuning are constants at the top of `overlays.js`:
`GROUPS.reject.stored` (the dimming, `.625`), `OUTLINE_PX` (`3`),
`SHADOW_COLOR` (`rgba(0,0,0,1)`) and `SHADOW_BLUR` (`36`). A perpendicular scan
across an approved edge, measured on the rendered canvas, reads: shadow ramping
to alpha 125 just outside, then exactly three pixels of `(126, 230, 134)`, then
nothing at all over the render itself.

`Esc` clears everything; the header shows the tally with `copy` (both groups as
JSON) and `clear`. A tile is in one group or none, so marking a rejected tile
approved moves it rather than stacking.

The two are drawn in opposite ways deliberately. Rejecting means you want to see
*less* of that render, which a bright highlight cannot express — dimming can.
Approving means you want to keep looking at it, so the mark stays off the pixels
entirely and only traces the region.

Shift-drag is otherwise Leaflet's *box zoom* — zoom-to-rectangle, not a
selection — and both cannot own the gesture, so `boxZoom` stands down while
`mark` is on and gets it back when it is off. Either overlay can also be set
from the URL (`&select=1`, `&debug=1`), which wins over the stored preference,
so a link can turn one on for someone whose last session had it off.

#### The marks tab

Marks live on the map until you save them, and the **marks** tab in the left
pane is where they are kept:

| | |
|---|---|
| `add` | save the current marks as a named set (rename by double-click) |
| `clear` | clear the map's marks — the saved sets are untouched |
| `reject rest` | reject every tile that **holds a render** and is not marked yet |
| `copy` | the current marks to the clipboard, as JSON |

plus a search box that marks by query: `approve matches` / `reject matches` take
whatever the same search the sidebar uses returns, and mark each result's whole
`nx × ny` block, because half a marked render is not a marked render.

Together those are a triage pass: approve what you want, `reject rest`, done.
That last step is the reason for the `leaves` route — the client knows the map's
extent but not which of those coordinates carry anything, and on a
Hilbert-filled map most of them do not. A list of 19 494 coordinate triples is
about a megabyte of JSON to say what 8 KB of bits says, so the server sends the
bits.

Sets are kept in `localStorage` under `pmtiles.marks.v1`, as flat cell indices
(`y * side + x`) per group rather than `{z, x, y}` objects — a full pass over a
3249-render map is ~19 500 tiles, which is ~1 MB of objects against ~120 KB of
integers, and the whole origin only gets about 5 MB. A set records the zoom it
was made at and refuses to load onto a map that marks at a different
granularity, rather than silently landing somewhere else.

#### Outlining a region without a shape

The textbook way to get an outline from a flat fill is `S - erode(S)`: draw the
shape, intersect four translated copies with `source-in` to get the erosion,
then subtract it with `destination-out`. It needs two scratch canvases and, more
awkwardly, padding across tile borders — erosion near an edge depends on pixels
that belong to the neighbouring tile.

The shadow, on the other hand, *is* the generic canvas drop shadow — used the
way round that gets it outside only. Clip to everything outside the region, then
fill the region with `shadowBlur` on: the fill lands entirely in the clip's hole
and is discarded, and only the part of its shadow that spilled outwards
survives. The approved render underneath stays completely untouched, which a
symmetric glow would not manage. The shape is built over a padded range of cells
— `ceil(SHADOW_BLUR / scale)` of them — so the blur runs continuously across a
tile border instead of stopping dead at the seam and drawing a shadow along a
boundary that is not one.

None of that is necessary for the outline when the mask is one pixel per tile. The boundary is
then exactly *"a marked cell whose neighbour is not marked"*, so a single padded
`getImageData` per tile yields every edge directly, at any zoom, with no
cross-tile state. Each edge is inset by half the line width so the stroke lands
inside its own tile instead of being clipped in half at the border, and the runs
are extended by the same half so corners meet. Holes and concave shapes come out
right for free, because the rule is local.

The selection is not DOM: it lives in an off-screen canvas at **one pixel per
tile**, and a `GridLayer` blits the matching crop into each visible tile with
smoothing off. So it is zoom-independent — panning costs one `drawImage` a tile,
and marking ten thousand tiles costs the same as marking one. (The technique is
from [peko/nn-lineart](https://github.com/peko/nn-lineart); clear-then-fill with
`destination-out` is what stops a re-selected region accumulating alpha.)

Because the mask is keyed by granularity rather than by map, **the selection
survives an archive switch** whenever the two maps share a `max_zoom` — which is
exactly the case the view-keeping above exists for: mark a region in one
rendering, press `2`, compare the same subjects in the other.

**`debug`** — per tile: archive `z/x/y`, map zoom, tile pixel size and the NW
corner's lat/lng, with the border coloured by where the tile *actually* came
from: stored, **upscaled** from a shallower level, or **over-zoomed** past the
deepest one. Neither of the latter two is a tile the archive holds, and nothing
else in the UI distinguishes them.

Two things to know if you touch this code: the overlays take their `tileSize`
from the archive, so `coords.x/y` are archive tile indices and
`archiveZoom(coords.z)` recovers the zoom (a 512 px archive runs the map one
level ahead — see the tile-size trap below); and a box corner projected through
lat/lng lands ~1e-7 px off an exact tile boundary, so the span is snapped with
an epsilon or `floor`/`ceil` silently takes an extra row.

| key | |
|---|---|
| `1`–`9` | switch archive (view kept) |
| `/` | focus search |
| `s` | save the selected tile to the liked list |
| `b` | show/hide the left pane |
| `r` | refetch every visible tile |
| `x`, `d` | mark / per-tile debug overlay |
| `?` | the help tab — every gesture and key, in the app |
| `Esc` | clear the selection, or close the preview |

The **?** tab in the left pane documents all of the above where you need it,
rather than here: what each top-bar control does, the map gestures, and the
per-tab actions for search, liked and marks.

## HTTP API

| route | |
|---|---|
| `GET /map/list` | archives in the maps dir, with zoom range and bounds |
| `GET /map/{name}/meta.json` | header + whether a store is present |
| `GET /map/{name}/tiles/{z}/{x}/{y}.webp` | one tile; a hole is a transparent placeholder, `?missing=404` for strict semantics |
| `GET /map/{name}/tilemeta/{z}/{x}/{y}` | that tile's metadata (store first, archive as fallback) |
| `GET /map/{name}/leaves?z=` | packed bitmap of which cells hold a render — 1 bit per cell, `X-Zoom`/`X-Side` headers |
| `GET /map/{name}/render/{z}/{x}/{y}` | the original render, stitched; `?format=webp` |
| `GET /map/{name}/search?q=` | title/tags/prompt, terms ANDed |
| `POST /map/{name}/build` | recompose the pyramid and re-serialize, in a worker thread; `?pyramid=0` / `?archive=0` / `?min_zoom=` |
| `GET /map/{name}/build` | that job's progress |
| `GET /map/{name}/changes?since=` | which tiles changed since a sequence number |
| `GET /map/{name}/events?since=` | the same feed as SSE |
| `GET /map/{name}/file` | the raw archive, served with `Range` — point pmtiles.js or MapLibre at it |

## CLI

```bash
python tools/pmtiles_map.py --list
python tools/pmtiles_map.py --info <map>
python tools/pmtiles_map.py --rebuild <map> [--quality 80]
python tools/pmtiles_map.py --rebuild-pyramid <map>
python tools/pmtiles_map.py --refresh-meta <map> [--dry-run]
python tools/pmtiles_map.py --dump <map> <z> <x> <y> out.webp

python tools/pmtiles_import.py <dir-or-.7z> --map-name <map> [--dry-run]

python tools/pmtiles_map_test.py            # 50 checks, no ComfyUI needed
python tools/pmtiles_map_test.py --bench 1000
```

The maps directory is found automatically (first `output/maps` at or above the
pack), or set `PMTILES_MAPS_DIR`.

## Importing images you already have

The saver node takes an IMAGE tensor out of a running graph, and it slices only
when the render is an exact multiple of the tile size. A folder of finished
images is usually neither, so `tools/pmtiles_import.py` pads instead: an image
becomes the smallest `nx × ny` block of tiles that holds it, **centred**, with
the remainder transparent. A 960×1408 render at `--tile-size 512` is a 2×3
block with 32 px left/right and 64 px top/bottom.

```bash
python tools/pmtiles_import.py photos/ --map-name gallery --dry-run
python tools/pmtiles_import.py photos/ --map-name gallery --jobs 10
python tools/pmtiles_import.py pack.7z --subdir "root/Booru" --map-name booru
```

Blocks are laid along the Hilbert curve, one per manifest index, at the shallowest
zoom that holds them all — so consecutive images are adjacent on the map *and*
contiguous in the archive, exactly as with the `HilbertXY` node, except that the
step may differ per axis. `--dry-run` prints the whole layout plus size and time
estimates and writes nothing; it refuses to start if the estimate does not fit in
the free space.

Because an image's position **is** its index in the manifest, the importer records
the manifest's hash and refuses to run against a map built from a different list —
adding one file would otherwise relocate everything after it on top of tiles
already written. `--resume` (the default) skips any image whose block is already
present, and leaves the pyramid alone when there was nothing to write.

Three phases, in this order:

| | |
|---|---|
| leaves | parallel — the workers decode, pad, crop and encode, the parent only inserts, because SQLite stays single-threaded |
| pyramid | one `rebuild_pyramid` at the end, never `recompose_ancestors` per image |
| archive | the WebP encode is ~345 ms a 512 px tile, so the cache is warmed in parallel first and `build_archive` then just copies bytes |

Two settings worth choosing rather than accepting:

* **`--store-format`** defaults to `webp_lossy` at `--store-quality 80`, which is
  right when the sources are already JPEG. Each pyramid level is composed from
  the one below and re-encoded, so lossy storage compounds per level;
  `webp_lossless` costs ~5× the disk and removes that entirely.
* **`--tile-meta`** defaults to `origin`: the full record goes on the render's
  top-left tile — all `search()` and the export ever read — and a stub on the
  rest. With `full`, a map of a few thousand images pushes the archive's 8 MB
  metadata budget, at which point `build_archive` embeds **no** per-tile
  metadata at all. The store keeps it either way, and that is what the viewer
  reads.

If a `readme.txt` sits at the source root it is parsed for prompt templates
(`--prompt-template` overrides), with `--artist-token` (default `xxx`) replaced
by each image's title, so the map is searchable by prompt and not just by
filename. `--meta KEY=VALUE` adds anything else.

## How it works

**Two files per map.** PMTiles v3 is write-once and clustered — tiles sit in
Hilbert tile-id order behind a varint directory, so a tile cannot be appended in
place. A SQLite store holds the tiles and the `.pmtiles` is serialized from it.
The store is the source of truth; the archive is a build product.

**The store keeps lossless PNG.** Every render touches one tile per zoom level,
so parents are rewritten constantly; storing lossy tiles would compound
quantisation on every save. Parents are *recomposed from their children* rather
than pasted into, which is idempotent and costs one resample per level. WebP is
applied once, at archive time — and cached per tile in the store, so a rebuild
re-encodes only what changed.

**Tiles are extracted server-side**, so the browser fetches plain image URLs — no
client-side range reads. Freshness comes from per-tile ETags over the tile bytes,
which is what lets tile URLs stay stable while still being correct.

## Measured

On an RTX 4060 (8 GB), 1024² renders at 256 px tiles:

| | |
|---|---|
| save overhead per render | 0.8 s (16 leaves + 8 ancestors, archive rewritten) |
| archive rebuild, 1000 tiles | 7.1 s cold, **0.74 s** warm after one new render |
| search across 5463 tiles / 34 MB of metadata | 0.04 s |
| full-render preview, 1024² | 0.16 s |
| per render, live feed | one event naming ~23 tiles |

Bulk import, on 12 threads: **3249 JPEGs of 960×1408** into a 2×3-tile block
each, `--tile-size 512`, `--store-format webp_lossy --store-quality 80`,
`--jobs 10`:

| phase | | |
|---|---|---|
| leaves | 19 494 tiles, 0 failures | ~18 min |
| pyramid | 6504 derived, z7→z0 | **5:43** |
| prewarm | 6504 WebP encodes, parallel | ~5 min |
| archive | 25 998 tiles, 697 MB, **0 encoded on the fly** | **2 s** |
| | store 1.46 GB, archive 697 MB | ~33 min total |

The archive line is the whole argument for warming the cache first: at 345 ms a
512 px tile, `build_archive` would otherwise have spent about **2.5 hours** in
`encode_webp` for the same bytes.

## Exporting for a CDN

```bash
python tools/pmtiles_export.py <map> ./out --build      # build, then bundle
python tools/pmtiles_export.py <map> ./out --serve 8080 # and look at it first
```

The bundle is a directory you upload as-is — nothing server-side to run:

```
out/  index.html  static.js  config.js  search.js
      <map>.pmtiles          leaflet/   pmtiles/
      serve.py               README.md
```

`index.html` reads the archive **by HTTP range request** through the vendored
`pmtiles.js`, so only the bytes on screen transfer. Verified end to end: a
range-reading client pulled the header, the metadata and a real WebP tile with
**6 requests totalling 5.3 KB out of a 531 KB archive**.

Search still works because the index is baked at export time into `search.js`
(one entry per render, not per tile — the sidecar for a 5463-tile map is ~160 KB
raw, under 10 KB gzipped). It is a smaller client than the one ComfyUI serves:
live updates, the build button and full-render previews all need a server and the
store, and none of that survives the trip.

Export refuses to run when the archive is behind the store or has no pyramid,
naming which — uploading a half-serialized map is the mistake worth catching.
`--build` fixes both first; `--force` exports anyway.

### It carries its own server

A recipient with no web server — and no `pip` — can still open the map:

```bash
cd out && python3 serve.py                 # http://127.0.0.1:8000/
python3 serve.py 8080 --host 0.0.0.0       # and from the rest of the LAN
```

`serve.py` is standard library only. It exists for exactly one reason:

**`python -m http.server` does not implement `Range`.** Measured on a real
bundle — asking for the archive's 127-byte header returns
`HTTP/1.0 200` with `Content-Length: 10918696`, the whole file, and it would do
that for every one of the ~16 KB reads `pmtiles.js` makes per tile. The page
loads, the map stays blank, and nothing says why. `serve.py` answers the same
request with `206` and `Content-Range: bytes 0-126/10918696`.

`--serve` runs that same file rather than a second implementation, so what is
checked here is what ships. Its behaviour is covered by section 21 of the test
suite: 206 with byte-exact content, suffix ranges, `416` for a range past the
end, a malformed `Range` falling back to a full `200` per RFC 9110, and an
archive reassembled from 49 sequential range reads coming out identical.

## Using an archive somewhere else

The `.pmtiles` files are spec-conformant and `clustered`, with root directories
small enough to fetch in one request — upload one to S3/R2 and point `pmtiles.js`
or MapLibre at it, no server code. Two caveats: serve the object
**untransformed** (a CDN that gzips it breaks byte offsets), and per-tile
metadata is only inside the archive when it fits the embed budget — search and
the change feed are backed by the store and do not travel with the file.

## Credits

* [PMTiles](https://github.com/protomaps/PMTiles) — the archive format and its
  python implementation (BSD-3-Clause).
* [Leaflet](https://leafletjs.com/) 1.9.4 — vendored under `web/vendor/leaflet/`
  (BSD-2-Clause, see the LICENSE file there).

MIT, see [LICENSE](LICENSE).

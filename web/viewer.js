/* Leaflet viewer over a PMTiles archive served tile-by-tile by routes.py.
 *
 * Tiles come from /map/<name>/tiles/{z}/{x}/{y}.webp, so this is an ordinary
 * XYZ layer -- the archive's internal layout is the server's problem.  The
 * per-tile metadata written by SavePMTilesMap rides along in the archive's own
 * metadata blob and arrives via /map/<name>/meta.json.
 */
'use strict';

const TRANSPARENT_PX =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYGD4DwABBAEAX+RxlwAAAABJRU5ErkJggg==';

/* Tile coordinates always use Leaflet's CRS unit of 256 px, never the archive's
 * tile_size. A tile (z, x, y) covers 1/2^z of the world whatever its pixel size,
 * and map.project/unproject work in a 256·2^zoom space. Multiplying by an
 * archive's 512 px tile_size here is what put a 512 map's grid out of step with
 * its own coordinates. See TILE_PX and `shift` below. */
const TILE_PX = 256;

/* How far below the archive's shallowest level the map may still zoom out.
 * Leaflet serves a missing level from the nearest native one at the current
 * scale, so each level costs 4x the tiles: two levels is ~16 screenfuls,
 * eight (a z=8 map with no pyramid, viewed at z=0) is 65536 tiles. */
const UPSCALE_LEVELS = 2;

/* And how far past the deepest stored level zooming in may go. Over-zoom costs
 * no extra requests -- Leaflet upscales tiles it already has -- but past a level
 * or so it is just a blurry lie about what the archive holds. */
const OVERZOOM_LEVELS = 1;

const el = (id) => document.getElementById(id);
const state = {
  map: null,
  layer: null,
  name: null,
  meta: null,
  mtime: 0,
  selection: null,
  // Tiles the feed reported while they were off screen; busted on next load.
  dirty: new Set(),
  // Tiles whose last request failed (a hole, or a cached 404).
  errored: new Set(),
};

const map = L.map('map', {
  crs: L.CRS.EPSG3857,
  minZoom: 0,
  zoomControl: true,
  attributionControl: true,
  zoomSnap: 1,
  // A results map is not a globe; wrapping it would be nonsense.
  worldCopyJump: false,
  // Own wheel handling below.
  scrollWheelZoom: false,
  // Double-click opens the full render instead of zooming.
  doubleClickZoom: false,
});
state.map = map;
map.attributionControl.setPrefix('');

/* One wheel notch = one zoom level.
 *
 * Leaflet's own handler computes
 *   n = 4 * log2(2 / (1 + exp(-|delta| / (4 * wheelPxPerZoomLevel))))
 *   levels = ceil(n / zoomSnap) * zoomSnap
 * and X11/GTK reports ~120 px per notch, which gives n = 1.264 -> **ceil to 2**.
 * Anything above ~70 px lands the same way, so on a normal desktop mouse every
 * notch jumps two levels. Raising wheelPxPerZoomLevel only moves the threshold
 * around and guesses at the device, so handle the wheel directly instead:
 * accumulate pixels, step one level per threshold crossing, and zoom about the
 * cursor the way Leaflet does. A trackpad's flurry of small deltas then needs a
 * real gesture per level rather than flying through the pyramid.
 */
const WHEEL_PX_PER_LEVEL = 50;
const WHEEL_GESTURE_GAP = 300;          // ms of quiet that starts a new gesture
let wheelAccum = 0;
let wheelAt = 0;

function wheelPixels(ev) {
  if (ev.deltaMode === 1) return ev.deltaY * 20;    // lines
  if (ev.deltaMode === 2) return ev.deltaY * 400;   // pages
  return ev.deltaY;                                 // pixels
}

el('map').addEventListener('wheel', (ev) => {
  if (ev.ctrlKey) return;               // leave browser page-zoom alone
  ev.preventDefault();
  const now = performance.now();
  if (now - wheelAt > WHEEL_GESTURE_GAP) wheelAccum = 0;
  wheelAt = now;
  wheelAccum += wheelPixels(ev);
  if (Math.abs(wheelAccum) < WHEEL_PX_PER_LEVEL) return;
  const dir = wheelAccum > 0 ? -1 : 1;  // positive deltaY = scroll down = zoom out
  wheelAccum = 0;                       // never bank a second level from one notch
  const target = Math.max(map.getMinZoom(),
    Math.min(map.getMaxZoom(), map.getZoom() + dir));
  if (target !== map.getZoom()) {
    map.setZoomAround(map.mouseEventToContainerPoint(ev), target, { animate: false });
  }
}, { passive: false });

/* ---------------------------------------------------------------- url state */
/* The view lives in the query string — ?map=&z=&x=&y= — so a refresh, a
 * bookmark and a pasted link all restore the same place. x/y are the *centre* in
 * fractional tile units at that zoom (12.5 = middle of tile 12), which keeps
 * them in the same coordinate system as the saver's z/x/y instead of introducing
 * lat/lng that mean nothing on a map of renders. */

function readUrlState() {
  const q = new URLSearchParams(location.search);
  const num = (k) => (q.has(k) && q.get(k) !== '' && Number.isFinite(+q.get(k))
    ? +q.get(k) : null);
  return { map: q.get('map'), z: num('z'), x: num('x'), y: num('y') };
}

function writeUrlState() {
  if (!state.meta || !state.map) return;
  const q = new URLSearchParams(location.search);
  const z = state.map.getZoom();
  const p = state.map.project(state.map.getCenter(), z);
  q.set('map', state.name);
  q.set('z', String(z));
  q.set('x', (p.x / TILE_PX).toFixed(3));
  q.set('y', (p.y / TILE_PX).toFixed(3));
  // replaceState, not pushState: panning must not fill the back button.
  history.replaceState(null, '', `${location.pathname}?${q}`);
}

/** Centre on fractional tile coords, the inverse of writeUrlState.
 *
 * Applied across a map switch too, not just a refresh: archives built from the
 * same index hold the same subject at the same coordinate, so holding position
 * is what makes two visualisations comparable. x/y are in *tile* units rather
 * than pixels precisely because the tile is the invariant.
 */
function applyUrlView(view, maxZoom) {
  if (view.z === null || view.x === null || view.y === null) return false;
  const z = Math.max(state.floorZoom || 0, Math.min(view.z, maxZoom));
  const latlng = state.map.unproject(
    L.point(view.x * TILE_PX, view.y * TILE_PX), view.z);
  state.map.setView(latlng, z, { animate: false });
  return true;
}

/* ------------------------------------------------------------------ helpers */

function fmtBytes(n) {
  if (n === undefined || n === null) return '?';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

/** Tile containing a latlng at a given *tile* zoom. */
function tileAt(latlng, zoom) {
  const p = map.project(latlng, zoom);
  return { z: zoom, x: Math.floor(p.x / TILE_PX), y: Math.floor(p.y / TILE_PX) };
}

/** Which backing store this map is being read from.
 *
 * `store` is the live source: tiles come straight out of SQLite, so a render is
 * visible the moment it is saved and the archive is never touched -- which is
 * what lets archive_every batch the .pmtiles writes without freezing the map.
 * `archive` reads the .pmtiles itself, i.e. exactly what would be uploaded.
 */
function sourceFor(info) {
  const want = state.ui.source || 'auto';
  if (want === 'store' && info.has_store) return 'store';
  if (want === 'archive' && info.built !== false) return 'archive';
  return info.has_store ? 'store' : 'archive';
}

/** Map zoom - tile zoom, for an archive whose tiles are bigger than 256 px. */
function zoomShift(tileSize) {
  return Math.max(0, Math.round(Math.log2((Number(tileSize) || 256) / TILE_PX)));
}

/* -------------------------------------------------------------------- layer */

function showMap(info) {
  state.name = info.name;
  state.meta = info;
  state.mtime = info.mtime;

  const tileSize = Number(info.tile_size) || 256;
  const shift = zoomShift(tileSize);
  const floor = Math.max(0, info.min_zoom + shift - UPSCALE_LEVELS);
  const ceiling = info.max_zoom + shift + OVERZOOM_LEVELS;
  state.shift = shift;
  state.floorZoom = floor;
  state.ceilingZoom = ceiling;
  const [w, s, e, n] = info.bounds;
  const bounds = L.latLngBounds([[s, w], [n, e]]);
  // Kept so the poll and the change feed can widen it: GridLayer._isValidTile
  // refuses to REQUEST a tile outside options.bounds, and a run that grows the
  // map past the extent it had at page load would otherwise be invisible until
  // a reload -- renders landing, feed reporting them, no tile ever fetched.
  state.boundsKey = info.bounds.join(',');

  // Drop the outgoing layer before touching the zoom: a clamp below would
  // otherwise send it fetching tiles for the map we are leaving.
  if (state.layer) { state.layer.remove(); state.layer = null; }

  state.source = sourceFor(info);
  // Only the archive source needs a .pmtiles. A store-only map is perfectly
  // viewable live; it just has nothing to upload yet.
  if (state.source === 'archive' && info.built === false) {
    state.floorZoom = floor;
    setStats(info);
    el('stats').textContent += ' · not serialized yet — press ⛰ build';
    writeUrlState();
    return;
  }

  // Switching archives usually leaves the current zoom outside the new map's
  // range. Do it explicitly rather than letting Leaflet clamp: widen the limits
  // to the union first (so setView is not clipped by the map we are leaving),
  // place the view, then tighten to the new range -- by which point the zoom is
  // already inside it, so nothing has to be clamped, animated or re-fired.
  map.setMinZoom(Math.min(floor, map.getMinZoom()));
  map.setMaxZoom(Math.max(ceiling, map.getMaxZoom()));

  const view = readUrlState();
  if (state.pendingCoord || !applyUrlView(view, ceiling)) {
    // fitBounds has a maxZoom option but no minZoom, so clamp after it.
    map.fitBounds(bounds, { animate: false, padding: [8, 8], maxZoom: ceiling });
  }
  if (map.getZoom() < floor) map.setZoom(floor, { animate: false });
  if (map.getZoom() > ceiling) map.setZoom(ceiling, { animate: false });
  map.setMinZoom(floor);
  map.setMaxZoom(ceiling);

  // No ?v= cache-buster in the template: it would make every rebuild a new URL
  // for every tile, i.e. a full reload. Freshness comes from per-tile ETags,
  // and only the tiles the change feed names get refetched.
  state.layer = L.tileLayer(
    `/map/${encodeURIComponent(info.name)}/tiles/{z}/{x}/{y}.webp`
    + `?source=${state.source}`,
    {
      tileSize,
      // Never let the map zoom far below the shallowest level the archive
      // actually has. Leaflet fills a missing level by requesting the nearest
      // native one at the *current* scale: with only z=8 in the archive, map
      // zoom 0 asks for 2^8 x 2^8 = 65536 tiles and the browser dies. Two levels
      // of upscaling is ~16 screenfuls of tiles, which is survivable; beyond
      // that, build the pyramid.
      minZoom: floor,
      // Zoom past the deepest stored level so single tiles can be inspected;
      // Leaflet upscales rather than 404-ing once past maxNativeZoom.
      maxZoom: ceiling,
      // minNativeZoom/maxNativeZoom are compared against the MAP zoom
      // (GridLayer._clampZoom(map.getZoom())), while zoomOffset is applied only
      // when building the URL (_getZoomForUrl). With 512 px tiles the map runs
      // one zoom ahead of the archive, so the native bounds shift up and the URL
      // shifts back down.
      minNativeZoom: info.min_zoom + shift,
      maxNativeZoom: info.max_zoom + shift,
      zoomOffset: -shift,
      bounds,
      noWrap: true,
      errorTileUrl: TRANSPARENT_PX,
      attribution: info.attribution || `${info.name}.pmtiles`,
      keepBuffer: 4,
    },
  ).addTo(map);

  // A tile that changed while off screen loads with a busted URL, once. Also any
  // tile that previously failed: its 404 may sit in the browser cache from before
  // the no-store fix, and the plain URL would be answered from there forever.
  state.layer.on('tileloadstart', (ev) => {
    // ev.coords is in map-zoom terms, which is exactly how state.dirty is keyed;
    // the URL needs the archive zoom back.
    const key = `${ev.coords.x}:${ev.coords.y}:${ev.coords.z}`;
    if (!state.dirty.has(key) && !state.errored.has(key)) return;
    state.dirty.delete(key);
    state.errored.delete(key);
    ev.tile.src = `${tileUrl(archiveZoom(ev.coords.z), ev.coords.x, ev.coords.y)}`
      + `&t=${state.seq || 0}`;
  });

  state.layer.on('tileerror', (ev) => {
    if (ev.coords) state.errored.add(`${ev.coords.x}:${ev.coords.y}:${ev.coords.z}`);
  });

  // A saved POI in another archive outranks the view restored above.
  if (state.pendingCoord) {
    const target = state.pendingCoord;
    state.pendingCoord = null;
    gotoTile(target);
  }
  writeUrlState();
  setStats(info);

  // Keep the sidebar on the same tile after a switch: comparing two archives
  // means comparing what each recorded for the same coordinate.
  if (state.selection) {
    const coord = state.selection;
    fetchTileMeta(coord).then((meta) => describe(coord, meta));
  }

  // Search results belong to one archive, so re-run or clear them on a switch.
  if (el('search-input').value.trim()) runSearch(); else renderSearch(null, '');

  // overlays.js, if it loaded: the canvas layers take their tile size from the
  // archive, so they have to be rebuilt when the archive changes.
  if (typeof overlaysMapChanged === 'function') overlaysMapChanged(info);

  // Subscribe from "now": what is on screen was just fetched, so there is no
  // backlog worth replaying.
  state.seq = undefined;
  const watchable = state.source === 'store';
  el('live-toggle').disabled = !watchable;
  el('live-toggle').parentElement.title = watchable
    ? 'refresh tiles as renders land'
    : 'archive mode: the .pmtiles only changes when it is rebuilt';
  if (!watchable) {
    stopFeed();
    return;
  }
  fetch(`/map/${encodeURIComponent(info.name)}/changes?live=1`)
    .then((res) => (res.ok ? res.json() : null))
    .then((payload) => {
      state.seq = payload ? payload.seq : 0;
      startFeed();
    })
    .catch(() => { state.seq = 0; });
}

function setStats(info) {
  el('stats').textContent =
    `${info.tiles} tiles (${info.unique_tiles} unique) · z${info.min_zoom}–${info.max_zoom} · `
    + `${info.tile_size || 256}px · ${fmtBytes(info.bytes)}`
    + (info.has_tile_metadata || info.has_store ? '' : ' · no per-tile metadata')
    + (info.pyramid_stale
      ? ` · no pyramid, zoom-out capped at z${state.floorZoom}` : '')
    + (info.pending_tiles ? ` · ${info.pending_tiles} tile(s) not in the archive` : '');
  el('build-map').classList.toggle('stale',
    !!info.pyramid_stale || !!info.pending_tiles);
  el('stats').textContent += ` · ${state.source === 'store'
    ? 'live from store' : 'from archive'}`;
}

/* ------------------------------------------------------------------ sidebar */

const PRETTY = [
  ['prompt', 'prompt'],
  ['negative', 'negative'],
  ['title', 'title'],
  ['tags', 'tags'],
  ['seed', 'seed'],
  ['steps', 'steps'],
  ['cfg', 'cfg'],
  ['sampler_name', 'sampler'],
  ['scheduler', 'scheduler'],
  ['denoise', 'denoise'],
  ['models', 'models'],
  ['latent_size', 'latent'],
  ['source_size', 'render'],
  ['placement', 'placement'],
  ['grid', 'grid x/y of nx/ny'],
  ['time', 'saved'],
  ['prompt_note', 'note'],
];

function describe(coord, meta) {
  el('side-empty').hidden = true;
  el('side-body').hidden = false;
  el('tile-coord').textContent = `${coord.z}/${coord.x}/${coord.y}`;
  el('tile-thumb').src =
    `/map/${encodeURIComponent(state.name)}/tiles/${coord.z}/${coord.x}/${coord.y}.webp?v=${state.mtime}`;

  const dl = el('tile-fields');
  dl.textContent = '';
  if (!meta) {
    const dd = document.createElement('dd');
    dd.textContent = 'No metadata recorded for this tile.';
    dl.append(dd);
  } else {
    for (const [key, label] of PRETTY) {
      if (meta[key] === undefined || meta[key] === '' ) continue;
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.textContent = Array.isArray(meta[key]) ? meta[key].join(' × ') : String(meta[key]);
      dl.append(dt, dd);
    }
  }
  el('tile-raw').textContent = meta ? JSON.stringify(meta, null, 2) : '';
  el('tile-raw-wrap').hidden = !meta;
  // The default name for a saved POI: what the tile calls itself.
  state.selectionTitle = meta
    ? (meta.title || (meta.prompt ? meta.prompt.slice(0, 70) : '')) : '';
  // Where this tile sits inside its render, so saving from any tile of a sliced
  // render stores the render, not the corner you happened to click.
  state.selectionSpan = (meta && Array.isArray(meta.grid) && meta.grid.length === 4)
    ? { ix: meta.grid[0], iy: meta.grid[1], nx: meta.grid[2], ny: meta.grid[3] }
    : null;
  markActive();
}

/** One tile's metadata, fetched on demand -- the whole-map blob is megabytes on
 *  a few-thousand-tile map and the sidebar only ever shows the tile you clicked. */
async function fetchTileMeta(coord) {
  try {
    const res = await fetch(`/map/${encodeURIComponent(state.name)}`
      + `/tilemeta/${coord.z}/${coord.x}/${coord.y}`);
    if (!res.ok) return null;
    return (await res.json()).meta;
  } catch (err) {
    return null;
  }
}

/** Deepest tile with metadata under a point, so a zoomed-out click still finds
 *  the render that produced what you are looking at. */
async function resolveUnder(latlng) {
  let fallback = null;
  for (let z = state.meta.max_zoom; z >= state.meta.min_zoom; z -= 1) {
    const coord = tileAt(latlng, z);
    if (coord.x < 0 || coord.y < 0 || coord.x >= 2 ** z || coord.y >= 2 ** z) continue;
    const meta = await fetchTileMeta(coord);       // eslint-disable-line no-await-in-loop
    if (meta && meta.kind !== 'derived') return { coord, meta };
    if (!fallback) fallback = { coord, meta };
  }
  return fallback;
}

async function describeUnder(latlng) {
  const hit = await resolveUnder(latlng);
  if (!hit) return;
  state.selection = hit.coord;
  describe(hit.coord, hit.meta);
}

map.on('click', (ev) => {
  if (state.meta) describeUnder(ev.latlng);
});

/* --------------------------------------------------------- full-render preview */
/* Double-click opens the original image. The store keeps lossless PNG tiles, so
 * the server stitches the render's block back into the render's own pixels —
 * not the archive's q80 WebP, and not a single 256px tile. */

function openPreview(coord, meta) {
  if (!coord || !state.name) return;
  const url = `/map/${encodeURIComponent(state.name)}`
    + `/render/${coord.z}/${coord.x}/${coord.y}`;
  const label = (meta && (meta.title || meta.prompt)) || `${coord.z}/${coord.x}/${coord.y}`;
  el('preview-caption').textContent = `${label} — loading…`;
  el('preview-link').href = url;
  const img = el('preview-img');
  img.src = url;
  img.onload = () => {
    const derived = meta && meta.kind === 'derived';
    el('preview-caption').textContent =
      `${label} — ${img.naturalWidth}×${img.naturalHeight}`
      + (derived ? ' (derived pyramid tile, not an original render)' : '');
  };
  img.onerror = () => {
    el('preview-caption').textContent = `${label} — no stored image for this tile`;
  };
  el('preview').hidden = false;
}

function closePreview() {
  el('preview').hidden = true;
  el('preview-img').src = '';        // stop a large download we no longer need
}

map.on('dblclick', async (ev) => {
  if (!state.meta) return;
  const hit = await resolveUnder(ev.latlng);
  if (!hit) return;
  state.selection = hit.coord;
  describe(hit.coord, hit.meta);
  openPreview(hit.coord, hit.meta);
});

el('open-preview').addEventListener('click', () => {
  openPreview(state.selection, null);
});
el('preview-close').addEventListener('click', closePreview);
el('preview').addEventListener('click', (ev) => {
  // Backdrop only: a click on the image itself should not dismiss it.
  if (ev.target === el('preview')) closePreview();
});
map.on('moveend zoomend', writeUrlState);

el('copy-coord').addEventListener('click', () => {
  const text = el('tile-coord').textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = el('copy-coord');
    btn.textContent = 'copied';
    setTimeout(() => { btn.textContent = 'copy'; }, 900);
  });
});

el('grid-toggle').addEventListener('change', (ev) => {
  el('map').classList.toggle('grid', ev.target.checked);
});

/* ----------------------------------------------------------------- left pane */

const UI_KEY = 'pmtiles.ui.v1';

function storeUi() {
  try { localStorage.setItem(UI_KEY, JSON.stringify(state.ui)); } catch (err) { /* full */ }
}

/* Tabs register themselves rather than being listed twice -- the pane list and
 * the click handler were separate before, and a tab added to one but not the
 * other is a header that simply does nothing when clicked. */
const TABS = [];

function registerTab(name) {
  if (TABS.includes(name)) return;
  TABS.push(name);
  el(`tab-${name}`).addEventListener('click', () => setTab(name));
}

function setTab(name, remember = true) {
  // `remember` exists for the initial restore: overlays.js registers its tab
  // after this file runs, so a stored preference for a not-yet-registered tab
  // falls back to search -- and persisting that fallback would erase the
  // preference before the tab that owns it ever loads.
  if (remember) {
    state.ui.tab = name;
    storeUi();
  }
  for (const tab of TABS) {
    el(`tab-${tab}`).classList.toggle('active', tab === name);
    el(`pane-${tab}`).hidden = tab !== name;
  }
}

function setLeft(visible) {
  state.ui.left = visible;
  storeUi();
  el('left').classList.toggle('hidden', !visible);
  el('left-toggle').textContent = visible ? '☰' : '▶';
  // Leaflet caches the container size; without this the tiles stay laid out for
  // the old width and the map goes grey down one side.
  map.invalidateSize({ animate: false });
}

el('source-picker').addEventListener('change', (ev) => {
  state.ui.source = ev.target.value;
  storeUi();
  state.name = null;                 // force a full showMap with the new source
  refreshMaps({ initial: true });
});

registerTab('search');
registerTab('saved');
el('left-toggle').addEventListener('click', () => setLeft(el('left').classList.contains('hidden')));

/* ------------------------------------------------------- navigate to a tile */

/* ------------------------------------------------------- highlight on the map */

/** Geographic bounds of a tile, or of a whole render block when nx/ny are set. */
function blockBounds(target) {
  const nx = target.nx || 1;
  const ny = target.ny || 1;
  return L.latLngBounds(
    map.unproject(L.point(target.x * TILE_PX, target.y * TILE_PX), target.z),
    map.unproject(L.point((target.x + nx) * TILE_PX, (target.y + ny) * TILE_PX),
      target.z),
  );
}

/** Outline a target on the map. Pass null to clear. */
function highlight(target, { hold = 0 } = {}) {
  clearTimeout(state.holdTimer);
  if (!target || !state.meta || (target.map && state.name && target.map !== state.name)) {
    if (state.box) { state.box.remove(); state.box = null; }
    return;
  }
  const bounds = blockBounds(target);
  if (!state.box) {
    state.box = L.rectangle(bounds, {
      color: '#7cc4ff', weight: 2, dashArray: '5 4', fillOpacity: 0.08,
      interactive: false,          // must not swallow clicks meant for the map
    }).addTo(map);
  } else {
    state.box.setBounds(bounds);
    if (!map.hasLayer(state.box)) state.box.addTo(map);
  }
  if (hold) {
    state.holdTimer = setTimeout(() => {
      if (state.box) { state.box.remove(); state.box = null; }
    }, hold);
  }
}

/** Is the target somewhere else than the current view? */
function offscreen(target) {
  if (!state.meta) return false;
  return !map.getBounds().intersects(blockBounds(target));
}

/** Wire hover-to-highlight onto a list row. */
function bindHover(li, target) {
  li.addEventListener('mouseenter', () => {
    if (target.map && state.name && target.map !== state.name) {
      li.classList.add('foreign');
      return;                      // cannot draw another archive's coordinates here
    }
    li.classList.toggle('offscreen', offscreen(target));
    highlight(target);
  });
  li.addEventListener('mouseleave', () => {
    li.classList.remove('offscreen');
    highlight(null);
  });
}

/** Centre on a tile and show its metadata; switches archive if needed.
 *
 * `nx`/`ny` (a sliced render's block size) zoom out far enough to frame the
 * whole render rather than landing inside its top-left corner tile.
 */
async function gotoTile(target) {
  if (target.map && state.name && target.map !== state.name) {
    // The switch reloads the layer, so hand the coordinate over and let
    // showMap land on it once the new archive's tile size is known.
    state.pendingCoord = target;
    selectMap(target.map);
    return;
  }
  if (!state.meta) return;
  const nx = target.nx || 1;
  const ny = target.ny || 1;
  const shrink = Math.ceil(Math.log2(Math.max(nx, ny, 1)));
  const zoom = Math.max(map.getMinZoom(),
    Math.min(target.z + (state.shift || 0) - shrink, map.getMaxZoom()));
  const centre = map.unproject(
    L.point((target.x + nx / 2) * TILE_PX, (target.y + ny / 2) * TILE_PX), target.z);
  map.setView(centre, zoom, { animate: false });
  // Hold the outline briefly after landing: without it the thing you navigated
  // to is indistinguishable from its neighbours.
  highlight(target, { hold: 1800 });
  const coord = { z: target.z, x: target.x, y: target.y };
  state.selection = coord;
  describe(coord, await fetchTileMeta(coord));
  markActive();
}

/* -------------------------------------------------------------------- search */

function resultName(row) {
  if (row.title) return row.title;
  if (row.prompt) return row.prompt.length > 70 ? `${row.prompt.slice(0, 70)}…` : row.prompt;
  return row.tags || `${row.z}/${row.x}/${row.y}`;
}

function renderSearch(payload, query) {
  const list = el('search-results');
  highlight(null);
  list.textContent = '';
  const rows = (payload && payload.results) || [];
  el('search-hint').textContent = !query
    ? ''
    : `${rows.length}${payload && payload.truncated ? '+' : ''} match`
      + `${rows.length === 1 ? '' : 'es'}`;
  for (const row of rows) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = resultName(row);
    name.title = row.prompt || '';
    const coord = document.createElement('span');
    coord.className = 'coord';
    coord.textContent = (row.nx > 1 || row.ny > 1)
      ? `${row.z}/${row.x}/${row.y} ${row.nx}×${row.ny}`
      : `${row.z}/${row.x}/${row.y}`;
    li.append(name, coord);
    li.dataset.coord = `${row.z}/${row.x}/${row.y}`;
    li.addEventListener('click', () => gotoTile(row));
    bindHover(li, row);
    list.append(li);
  }
}

let searchTimer = null;
async function runSearch() {
  const query = el('search-input').value.trim();
  if (!query || !state.name) { renderSearch(null, query); return; }
  try {
    const res = await fetch(`/map/${encodeURIComponent(state.name)}`
      + `/search?q=${encodeURIComponent(query)}`);
    renderSearch(res.ok ? await res.json() : null, query);
  } catch (err) {
    el('search-hint').textContent = 'search unavailable';
  }
}

el('search-input').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 250);   // a keystroke should not be a query
});
el('search-input').addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') { el('search-input').value = ''; renderSearch(null, ''); }
});

/* --------------------------------------------------------------- saved tiles */
/* Points of interest, kept in localStorage. Copy/paste moves the list between
 * browsers, which is enough of a backup for something rebuildable. */

const SAVED_KEY = 'pmtiles.saved.v1';

function loadSaved() {
  try {
    const raw = JSON.parse(localStorage.getItem(SAVED_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter(validPoi) : [];
  } catch (err) {
    return [];
  }
}

function validPoi(p) {
  return p && typeof p === 'object'
    && Number.isInteger(p.z) && Number.isInteger(p.x) && Number.isInteger(p.y);
}

function storeSaved() {
  try {
    localStorage.setItem(SAVED_KEY, JSON.stringify(state.saved));
  } catch (err) {
    flashCount('not saved: storage full');
  }
}

function markActive() {
  const here = state.selection
    ? `${state.selection.z}/${state.selection.x}/${state.selection.y}` : '';
  for (const li of document.querySelectorAll('#saved-list li, #search-results li')) {
    li.classList.toggle('active', li.dataset.coord === here);
  }
}

function renderSaved() {
  const list = el('saved-list');
  highlight(null);                   // rows are about to be replaced under the cursor
  list.textContent = '';
  el('saved-count').textContent = state.saved.length || '';
  el('saved-empty').hidden = state.saved.length > 0;

  state.saved.forEach((poi, index) => {
    const li = document.createElement('li');
    li.draggable = true;
    li.dataset.index = String(index);
    li.dataset.coord = `${poi.z}/${poi.x}/${poi.y}`;

    const grip = document.createElement('span');
    grip.className = 'grip';
    grip.textContent = '⠿';

    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = poi.name || `${poi.z}/${poi.x}/${poi.y}`;
    name.title = poi.map ? `${poi.map} — ${poi.z}/${poi.x}/${poi.y}` : '';

    const coord = document.createElement('span');
    coord.className = 'coord';
    coord.textContent = `${poi.z}/${poi.x}/${poi.y}`;

    const del = document.createElement('button');
    del.className = 'del';
    del.textContent = '×';
    del.title = 'remove';
    del.addEventListener('click', (ev) => {
      ev.stopPropagation();
      state.saved.splice(index, 1);
      storeSaved();
      renderSaved();
    });

    li.addEventListener('click', () => gotoTile(poi));
    // Rename in place: the title is the POI's name, and the one from the render
    // is rarely the one you want six hours later.
    name.addEventListener('dblclick', (ev) => {
      ev.stopPropagation();
      name.contentEditable = 'true';
      name.focus();
      document.getSelection().selectAllChildren(name);
    });
    name.addEventListener('blur', () => {
      if (name.contentEditable !== 'true') return;
      name.contentEditable = 'false';
      state.saved[index].name = name.textContent.trim()
        || `${poi.z}/${poi.x}/${poi.y}`;
      storeSaved();
      renderSaved();
    });
    name.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); name.blur(); }
      if (ev.key === 'Escape') { name.textContent = poi.name || ''; name.blur(); }
      ev.stopPropagation();          // do not let 1-9 switch archives mid-rename
    });

    bindHover(li, poi);
    li.append(grip, name, coord, del);
    list.append(li);
  });
  markActive();
}

/* Drag to reorder. HTML5 DnD rather than a library: no CDN is reachable from a
 * published artifact-free page anyway, and this is 30 lines. */
let dragFrom = null;
el('saved-list').addEventListener('dragstart', (ev) => {
  const li = ev.target.closest('li');
  if (!li) return;
  dragFrom = Number(li.dataset.index);
  li.classList.add('dragging');
  ev.dataTransfer.effectAllowed = 'move';
  ev.dataTransfer.setData('text/plain', String(dragFrom));
});
el('saved-list').addEventListener('dragover', (ev) => {
  const li = ev.target.closest('li');
  if (!li || dragFrom === null) return;
  ev.preventDefault();
  const box = li.getBoundingClientRect();
  const after = ev.clientY > box.top + box.height / 2;
  for (const other of el('saved-list').children) {
    other.classList.remove('drop-before', 'drop-after');
  }
  li.classList.add(after ? 'drop-after' : 'drop-before');
});
el('saved-list').addEventListener('drop', (ev) => {
  const li = ev.target.closest('li');
  if (!li || dragFrom === null) return;
  ev.preventDefault();
  const box = li.getBoundingClientRect();
  let to = Number(li.dataset.index) + (ev.clientY > box.top + box.height / 2 ? 1 : 0);
  if (to > dragFrom) to -= 1;                        // account for the removal
  const [moved] = state.saved.splice(dragFrom, 1);
  state.saved.splice(to, 0, moved);
  dragFrom = null;
  storeSaved();
  renderSaved();
});
el('saved-list').addEventListener('dragend', () => {
  dragFrom = null;
  for (const li of el('saved-list').children) {
    li.classList.remove('dragging', 'drop-before', 'drop-after');
  }
});

function saveSelected() {
  if (!state.selection || !state.name) return;
  const span = state.selectionSpan;
  const z = state.selection.z;
  const x = state.selection.x - (span ? span.ix : 0);
  const y = state.selection.y - (span ? span.iy : 0);
  if (state.saved.some((p) => p.map === state.name && p.z === z && p.x === x && p.y === y)) {
    flashCount('(already saved)');
    return;
  }
  state.saved.push({
    map: state.name, z, x, y,
    nx: span ? span.nx : 1,
    ny: span ? span.ny : 1,
    name: (state.selectionTitle || `${z}/${x}/${y}`).slice(0, 90),
  });
  storeSaved();
  renderSaved();
}

/** Transient message under the liked-pane buttons. */
function flashCount(text) {
  el('saved-msg').textContent = text;
  setTimeout(() => { el('saved-msg').textContent = ''; }, 1400);
}

el('save-tile').addEventListener('click', saveSelected);

el('saved-copy').addEventListener('click', async () => {
  const text = JSON.stringify(state.saved, null, 2);
  try {
    await navigator.clipboard.writeText(text);
    flashCount('(copied)');
  } catch (err) {
    window.prompt('Copy this:', text);               // clipboard denied
  }
});

el('saved-paste').addEventListener('click', async () => {
  let text = null;
  try {
    text = await navigator.clipboard.readText();
  } catch (err) {
    text = window.prompt('Paste the saved list JSON:');
  }
  if (!text) return;
  let incoming;
  try {
    incoming = JSON.parse(text);
  } catch (err) {
    flashCount('(not JSON)');
    return;
  }
  const valid = Array.isArray(incoming) ? incoming.filter(validPoi) : [];
  if (!valid.length) {
    flashCount('(nothing usable)');
    return;
  }
  // Replacing is destructive, so say what is being replaced.
  if (state.saved.length
      && !window.confirm(`Replace ${state.saved.length} saved tile(s) with `
        + `${valid.length} from the clipboard?`)) return;
  state.saved = valid;
  storeSaved();
  renderSaved();
});

/* ------------------------------------------------------------- change feed */
/* The saver knows exactly which tiles it wrote, so the viewer refreshes those
 * and leaves the rest alone. Reloading the layer instead meant re-requesting
 * every visible tile on every save — on a few-thousand-tile map that is real
 * load for, typically, sixteen changed tiles. */

function tileUrl(z, x, y) {
  return `/map/${encodeURIComponent(state.name)}/tiles/${z}/${x}/${y}.webp`
    + `?source=${state.source || 'auto'}`;
}

/* Leaflet keys its live tiles by the *map* zoom (GridLayer._tileCoordsToKey uses
 * _tileZoom), while the change feed speaks *archive* coordinates. On a 512 px
 * archive those differ by `shift`, so keying the lookup with the feed's z found
 * nothing: no targeted refresh and no flash, only on 512 maps. x and y agree —
 * that is what the zoomOffset shift buys. */
function leafletKey(z, x, y) {
  return `${x}:${y}:${z + (state.shift || 0)}`;
}

function archiveZoom(mapZoom) {
  return mapZoom - (state.shift || 0);
}

/** Repaint the given tiles if they are on screen. */
/** Widen the layer's tile bounds to include a tile, if it falls outside.
 *
 * The 3 s poll picks growth up too, but the feed knows the coordinate the
 * instant it is written, and waiting a poll for the *first* tile of a new column
 * is what makes a growing map look stuck.
 */
function includeTile(z, x, y) {
  const layer = state.layer;
  if (!layer) return false;
  const current = layer.options.bounds;
  // Same projection as blockBounds: TILE_PX is Leaflet's 256, never the
  // archive's tile size -- a tile covers 1/2^z of the world whatever its pixels.
  const tile = L.latLngBounds(
    map.unproject(L.point(x * TILE_PX, y * TILE_PX), z),
    map.unproject(L.point((x + 1) * TILE_PX, (y + 1) * TILE_PX), z),
  );
  if (current && current.contains(tile)) return false;
  layer.options.bounds = current ? L.latLngBounds(current).extend(tile) : tile;
  return true;
}

function refreshTiles(changes, seq) {
  const layer = state.layer;
  if (!layer || !layer._tiles) return 0;
  let touched = 0;
  let widened = false;
  for (const [z, x, y] of changes) {
    if (includeTile(z, x, y)) widened = true;
  }
  if (widened) {
    // Only creates the tiles that are now valid and missing; the ones already
    // on screen are untouched, so this is not a redraw.
    map.fire('moveend');
  }
  for (const [z, x, y] of changes) {
    // Leaflet keys its live tiles "x:y:z" (GridLayer._tileCoordsToKey). Private,
    // but there is no public "refetch one tile", and redraw() is the whole layer.
    const tile = layer._tiles[leafletKey(z, x, y)];
    if (!tile || !tile.el) {
      // Changed while off screen. Remember it, so that when it does load we ask
      // for it with a fresh URL instead of whatever the cache is holding —
      // notably a cached 404 from when the tile did not exist yet.
      state.dirty.add(leafletKey(z, x, y));
      continue;
    }
    // A one-off query param: the plain URL may sit in the memory cache, which
    // is not revalidated, so re-assigning the same src can be a no-op.
    tile.el.src = `${tileUrl(z, x, y)}&t=${seq}`;
    flash(tile.el);
    touched += 1;
  }
  if (touched) pulse();
  return touched;
}

/** Brief highlight, so a long run is visibly landing on the map. */
function flash(element) {
  if (!el('flash-toggle').checked) return;
  clearTimeout(element._flashTimer);
  element.classList.remove('tile-fresh');
  void element.offsetWidth;          // restart the animation on a repeat hit
  element.classList.add('tile-fresh');
  element._flashTimer = setTimeout(
    () => element.classList.remove('tile-fresh'), 1400);
}

/** Spend pending dirty flags on tiles that are present now.
 *
 * `tileloadstart` only fires when Leaflet *creates* a tile. Panning back to an
 * area you have already visited reuses the retained tile instead, so a dirty flag
 * set while that tile was off screen would never be spent — the tile stayed as it
 * was until the page was reloaded. Sweeping on moveend/zoomend covers that.
 */
function consumeDirty() {
  const layer = state.layer;
  if (!layer || !layer._tiles || !state.dirty.size) return;
  for (const key of Array.from(state.dirty)) {
    const tile = layer._tiles[key];
    if (!tile || !tile.el) continue;
    const [x, y, z] = key.split(':').map(Number);
    state.dirty.delete(key);
    tile.el.src = `${tileUrl(archiveZoom(z), x, y)}&t=${state.seq || 0}`;
    flash(tile.el);
  }
}

map.on('moveend zoomend', consumeDirty);

function applyChanges(payload) {
  if (!payload) return;
  if (typeof payload.seq === 'number') state.seq = payload.seq;
  if (payload.truncated) {
    // Fell too far behind for the feed to be authoritative: one full redraw is
    // cheaper than a wrong picture.
    state.layer.redraw();
    return;
  }
  if (payload.changes && payload.changes.length) {
    refreshTiles(payload.changes, state.seq);
    if (state.selection) {
      const hit = payload.changes.some(([z, x, y]) => z === state.selection.z
        && x === state.selection.x && y === state.selection.y);
      if (hit) fetchTileMeta(state.selection).then((m) => describe(state.selection, m));
    }
  }
}

function stopFeed() {
  clearTimeout(state.feedRetry);
  if (state.feed) { state.feed.close(); state.feed = null; }
}

/** Prefer the event stream; fall back to polling the same feed. */
function startFeed() {
  stopFeed();
  if (!state.name || !el('live-toggle').checked) return;
  // Nothing to watch in archive mode: a .pmtiles only changes when someone
  // rebuilds it, and that is what the 3 s map poll already notices.
  if (state.source !== 'store') return;
  const url = `/map/${encodeURIComponent(state.name)}/events`
    + `?since=${state.seq === undefined ? '' : state.seq}&live=1`;
  try {
    const feed = new EventSource(url);
    feed.onmessage = (ev) => {
      state.feedOk = true;
      // The stream is authoritative again; polling as well would apply every
      // change twice.
      state.pollChanges = false;
      try { applyChanges(JSON.parse(ev.data)); } catch (err) { /* ignore a bad frame */ }
    };
    feed.onerror = () => {
      // Reconnect ourselves rather than letting EventSource retry: it reuses the
      // original URL, so it would resubscribe from a stale `since` and replay
      // (or overflow into a full redraw). Rebuilding the URL picks up the seq we
      // have actually applied.
      stopFeed();
      if (!state.feedOk) state.pollChanges = true;   // never worked; poll instead
      state.feedRetry = setTimeout(startFeed, 3000);
    };
    state.feed = feed;
  } catch (err) {
    state.pollChanges = true;
  }
}

async function pollChanges() {
  if (!state.name || state.seq === undefined || state.source !== 'store') return;
  try {
    const res = await fetch(`/map/${encodeURIComponent(state.name)}`
      + `/changes?since=${state.seq}&live=1`);
    if (res.ok) applyChanges(await res.json());
  } catch (err) { /* the next tick will try again */ }
}

/* --------------------------------------------------------------- map picker */

async function refreshMaps({ initial = false } = {}) {
  let maps = [];
  try {
    const res = await fetch('/map/list');
    maps = (await res.json()).maps || [];
  } catch (err) {
    el('stats').textContent = 'server unreachable';
    return;
  }
  const picker = el('map-picker');
  const usable = maps.filter((m) => !m.error);
  state.maps = usable;
  const names = usable.map((m) => m.name).join(' ');
  if (names !== state.pickerNames) {
    state.pickerNames = names;
    picker.textContent = '';
    usable.forEach((m, i) => {
      const opt = document.createElement('option');
      opt.value = m.name;
      // The number is the key that selects it - a binding nobody can guess is a
      // binding nobody uses.
      opt.textContent = i < 9 ? `${i + 1} \u00b7 ${m.name}` : m.name;
      picker.append(opt);
    });
  }
  if (!usable.length) {
    el('stats').textContent = 'no maps in output/maps yet';
    return;
  }

  // ?map= chooses the archive, so a link restores the layer as well as the view.
  const wanted = state.name || readUrlState().map;
  const target = usable.find((m) => m.name === wanted) || usable[0];
  picker.value = target.name;

  if (initial || target.name !== state.name) {
    showMap(target);
  } else if (target.mtime !== state.mtime) {
    // Same map, new bytes: a render just landed. The tiles themselves are the
    // change feed's business — this only keeps the header honest, and must never
    // re-fit the view or the map would jump under the cursor mid-session.
    state.mtime = target.mtime;
    state.meta = target;
    // Both sides in MAP-zoom terms. Comparing the archive's max_zoom against the
    // layer's shifted maxNativeZoom made this always true on a 512 px archive, so
    // every poll took the branch below and redrew the whole layer — a tile would
    // update from the feed and then blink out as the redraw refetched it.
    const shift = state.shift || 0;
    if (target.max_zoom + shift !== state.layer.options.maxNativeZoom
        || target.min_zoom + shift !== state.layer.options.minNativeZoom) {
      // The pyramid grew or was built. Only the zoom limits change; existing
      // tiles are still valid, so do NOT redraw — the new levels load when
      // zoomed to, and changed tiles arrive through the feed. Both ends move:
      // building the pyramid drops min_zoom, which lifts the zoom-out cap.
      state.floorZoom = Math.max(0, target.min_zoom + shift - UPSCALE_LEVELS);
      state.ceilingZoom = target.max_zoom + shift + OVERZOOM_LEVELS;
      state.layer.options.maxNativeZoom = target.max_zoom + shift;
      state.layer.options.minNativeZoom = target.min_zoom + shift;
      state.layer.options.minZoom = state.floorZoom;
      state.layer.options.maxZoom = state.ceilingZoom;
      map.setMinZoom(state.floorZoom);
      map.setMaxZoom(state.ceilingZoom);
    }
    // A map grows sideways as well as deeper, and the layer's bounds are what
    // decide whether a tile is even requested. Track the extent, or every render
    // past the page-load extent stays invisible.
    const key = (target.bounds || []).join(',');
    if (key && key !== state.boundsKey) {
      state.boundsKey = key;
      const [bw, bs, be, bn] = target.bounds;
      state.layer.options.bounds = L.latLngBounds([[bs, bw], [bn, be]]);
      map.fire('moveend');
    }
    setStats(target);
    pulse();
  }
}

/** Switch archive, holding the view. */
function selectMap(name) {
  if (!name || name === state.name) return;
  state.name = name;
  el('map-picker').value = name;
  refreshMaps({ initial: true });
}

el('map-picker').addEventListener('change', (ev) => {
  selectMap(ev.target.value);
  ev.target.blur();          // hand the number keys back to the document
});

/* 1-9 select the nth archive. Same position, same zoom, different rendering --
 * which is the whole point of switching. */
window.addEventListener('keydown', (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if (ev.key === 'Escape' && !el('preview').hidden) {
    ev.preventDefault();
    closePreview();
    return;
  }
  const tag = (ev.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || ev.target.isContentEditable) return;
  if (ev.key === 's') { ev.preventDefault(); saveSelected(); setTab('saved'); return; }
  if (ev.key === 'b') { ev.preventDefault(); setLeft(el('left').classList.contains('hidden')); return; }
  if (ev.key === 'r') { ev.preventDefault(); reloadTiles(); return; }
  if (ev.key === '/') {
    ev.preventDefault();
    setLeft(true);                   // focusing a hidden input does nothing useful
    setTab('search');
    el('search-input').focus();
    el('search-input').select();
    return;
  }
  const n = Number(ev.key);
  if (!Number.isInteger(n) || n < 1 || n > 9) return;
  const target = (state.maps || [])[n - 1];
  if (!target) return;
  ev.preventDefault();
  selectMap(target.name);
});

/** Refetch everything on screen, without losing the view or reloading the page. */
function reloadTiles() {
  if (!state.layer) return;
  // redraw() alone re-requests the same URLs, which a cache may answer; bump a
  // per-layer nonce so every request is new.
  state.reloadNonce = (state.reloadNonce || 0) + 1;
  state.layer.setUrl(`/map/${encodeURIComponent(state.name)}`
    + `/tiles/{z}/{x}/{y}.webp?r=${state.reloadNonce}`, false);
  pulse();
}

el('reload-tiles').addEventListener('click', reloadTiles);

/* ------------------------------------------------------------ build on demand */
/* Recomposing the pyramid on every save costs one recomposition per level per
 * render, and rewrites the shallow tiles once per render — 65536 times for z=0
 * on a full z=8 map. Set the saver's pyramid_to_zoom to its z while rendering,
 * then build the levels here, once, in a single bottom-up pass. */

async function buildMap() {
  if (!state.name || state.building) return;
  const button = el('build-map');
  state.building = true;
  button.disabled = true;
  try {
    const res = await fetch(`/map/${encodeURIComponent(state.name)}/build`,
      { method: 'POST' });
    if (!res.ok) {
      setStats(state.meta);
      el('stats').textContent = `build refused: ${res.status}`;
      state.building = false;
      button.disabled = false;
      return;
    }
  } catch (err) {
    state.building = false;
    button.disabled = false;
    return;
  }
  pollBuild();
}

async function pollBuild() {
  if (!state.name) return;
  let job = null;
  try {
    const res = await fetch(`/map/${encodeURIComponent(state.name)}/build`);
    job = res.ok ? await res.json() : null;
  } catch (err) { /* keep polling; the server may be busy encoding */ }

  if (job && job.state === 'running') {
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    el('stats').textContent = `building ${job.phase}`
      + (job.level !== undefined && job.phase === 'pyramid' ? ` z${job.level}` : '')
      + ` — ${job.done}/${job.total} (${pct}%)`;
    setTimeout(pollBuild, 500);
    return;
  }

  state.building = false;
  el('build-map').disabled = false;
  if (job && job.state === 'error') {
    el('stats').textContent = `build failed: ${job.error}`;
    return;
  }
  if (job && job.state === 'done') {
    el('stats').textContent = `built ${job.derived || 0} derived tile(s) in `
      + `${(job.seconds || 0).toFixed(1)} s`;
  }
  // The zoom range changed, so rebuild the layer from fresh info.
  state.name = null;               // force showMap on the next poll
  refreshMaps({ initial: true });
}

el('build-map').addEventListener('click', buildMap);

function pulse() {
  const dot = el('pulse');
  dot.classList.add('on');
  setTimeout(() => dot.classList.remove('on'), 500);
}

let timer = null;
function setLive(on) {
  if (timer) clearInterval(timer);
  if (!on) { stopFeed(); timer = null; return; }
  startFeed();
  timer = setInterval(() => {
    refreshMaps();                   // header stats, new archives, zoom growth
    if (state.pollChanges) pollChanges();   // only if the event stream failed
  }, 3000);
}
el('live-toggle').addEventListener('change', (ev) => setLive(ev.target.checked));

try {
  state.ui = JSON.parse(localStorage.getItem(UI_KEY) || '{}') || {};
} catch (err) {
  state.ui = {};
}
el('source-picker').value = state.ui.source || 'auto';
setLeft(state.ui.left !== false);
setTab(TABS.includes(state.ui.tab) ? state.ui.tab : 'search', false);
state.saved = loadSaved();
renderSaved();
refreshMaps({ initial: true });
setLive(true);

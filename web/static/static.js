/* Static viewer for an exported map: reads the .pmtiles directly by HTTP range
 * request, so the whole bundle is uploadable to any static host or CDN and needs
 * no server code at all.
 *
 * This is deliberately a smaller client than the one ComfyUI serves. Live
 * updates, the build button and the full-render preview all need a running
 * server and a SQLite store; what survives the trip to a CDN is the map itself,
 * the metadata that was baked into search.json, and search over it.
 *
 * CONFIG is written by tools/pmtiles_export.py (config.js).
 */
'use strict';

const TILE_PX = 256;          // Leaflet's CRS unit, never the archive's tile size
const UPSCALE_LEVELS = 2;     // see the note in the served viewer: zooming out
const OVERZOOM_LEVELS = 1;    // past what exists costs 4x tiles per level

const el = (id) => document.getElementById(id);
const shift = Math.max(0, Math.round(Math.log2((CONFIG.tile_size || 256) / TILE_PX)));
const floorZoom = Math.max(0, CONFIG.min_zoom + shift - UPSCALE_LEVELS);
const ceilingZoom = CONFIG.max_zoom + shift + OVERZOOM_LEVELS;

const map = L.map('map', {
  crs: L.CRS.EPSG3857,
  minZoom: floorZoom,
  maxZoom: ceilingZoom,
  zoomSnap: 1,
  worldCopyJump: false,
  attributionControl: true,
});
map.attributionControl.setPrefix('');

const archive = new pmtiles.PMTiles(CONFIG.archive);
const layer = pmtiles.leafletRasterLayer(archive, {
  tileSize: CONFIG.tile_size || 256,
  // Same one-zoom shift as the served viewer: Leaflet's pixel space is always
  // 256 * 2^zoom, so a 512 px archive runs one map zoom ahead of its tiles.
  zoomOffset: -shift,
  minZoom: floorZoom,
  maxZoom: ceilingZoom,
  minNativeZoom: CONFIG.min_zoom + shift,
  maxNativeZoom: CONFIG.max_zoom + shift,
  noWrap: true,
  attribution: CONFIG.attribution || CONFIG.name,
}).addTo(map);

const [w, s, e, n] = CONFIG.bounds;
map.fitBounds(L.latLngBounds([[s, w], [n, e]]), { animate: false, padding: [8, 8],
  maxZoom: ceilingZoom });

archive.getHeader().then((h) => {
  el('stats').textContent =
    `${h.numAddressedTiles} tiles · z${h.minZoom}–${h.maxZoom} · `
    + `${CONFIG.tile_size || 256}px`;
}).catch(() => {
  el('stats').textContent = 'could not read the archive header';
});

/* --------------------------------------------------------------------- search */
/* One entry per render, baked at export time -- there is no server to query. */

const entries = Array.isArray(window.SEARCH) ? window.SEARCH : [];
el('hint').textContent = entries.length
  ? `${entries.length} renders indexed`
  : 'no metadata was exported with this map';

function haystack(entry) {
  return `${entry.title || ''} ${entry.tags || ''} ${entry.prompt || ''}`.toLowerCase();
}

function centreOf(entry) {
  const nx = entry.nx || 1;
  const ny = entry.ny || 1;
  return {
    latlng: map.unproject(
      L.point((entry.x + nx / 2) * TILE_PX, (entry.y + ny / 2) * TILE_PX), entry.z),
    zoom: Math.max(floorZoom, Math.min(
      entry.z + shift - Math.ceil(Math.log2(Math.max(nx, ny, 1))), ceilingZoom)),
  };
}

function show(entry) {
  const dl = document.createElement('dl');
  for (const [key, label] of [['title', 'title'], ['prompt', 'prompt'],
    ['negative', 'negative'], ['tags', 'tags'], ['seed', 'seed'], ['models', 'models'],
    ['time', 'saved']]) {
    if (entry[key] === undefined || entry[key] === '') continue;
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = Array.isArray(entry[key]) ? entry[key].join(' · ') : String(entry[key]);
    dl.append(dt, dd);
  }
  const where = document.createElement('div');
  where.className = 'coord';
  where.textContent = `${entry.z}/${entry.x}/${entry.y}`;
  el('detail').textContent = '';
  el('detail').append(where, dl);
}

function goto(entry) {
  const { latlng, zoom } = centreOf(entry);
  map.setView(latlng, zoom, { animate: false });
  show(entry);
}

function render(rows, query) {
  const list = el('results');
  list.textContent = '';
  el('hint').textContent = query
    ? `${rows.length} match${rows.length === 1 ? '' : 'es'}`
    : `${entries.length} renders indexed`;
  for (const entry of rows.slice(0, 200)) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = entry.title
      || (entry.prompt ? entry.prompt.slice(0, 70) : `${entry.z}/${entry.x}/${entry.y}`);
    const coord = document.createElement('span');
    coord.className = 'coord';
    coord.textContent = `${entry.z}/${entry.x}/${entry.y}`;
    li.append(name, coord);
    li.addEventListener('click', () => goto(entry));
    list.append(li);
  }
}

let timer = null;
el('q').addEventListener('input', () => {
  clearTimeout(timer);
  timer = setTimeout(() => {
    const terms = el('q').value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length) { render([], ''); return; }
    render(entries.filter((entry) => {
      const hay = haystack(entry);
      return terms.every((t) => hay.includes(t));
    }), el('q').value);
  }, 200);
});

/* Clicking the map finds the render whose block covers that point. */
map.on('click', (ev) => {
  const p = map.project(ev.latlng, CONFIG.max_zoom);
  const tx = p.x / TILE_PX;
  const ty = p.y / TILE_PX;
  const hit = entries.find((entry) => entry.z === CONFIG.max_zoom
    && tx >= entry.x && tx < entry.x + (entry.nx || 1)
    && ty >= entry.y && ty < entry.y + (entry.ny || 1));
  if (hit) show(hit);
});

/* Freeze the first row and/or column of *renders* against the edge of the map
 * -- a spreadsheet's frozen panes, where axes.js is its frozen headers.
 *
 * On a model x lora sweep the first column is each model with no LoRA and the
 * first row is the LoRA on the base model: the two baselines every other cell
 * is read against. Scroll away from them and the comparison becomes memory.
 * Pinned, they stay on screen next to whatever you are looking at.
 *
 * They are two more Leaflet maps, not a reimplementation: same tile layer,
 * same zoom, centre slaved to the main map on one axis and nailed to row 0 /
 * column 0 on the other. Leaflet already knows how to fetch, cache and scale
 * those tiles, and a strip one render thick is a handful of them.
 *
 * No UI and no interaction -- every handler off, `pointer-events: none`, so a
 * drag that starts on a strip belongs to the map underneath, as it would on a
 * frozen pane you cannot scroll independently.
 *
 * Two details that are the whole trick:
 *
 *  * **The strips must align with the main map's grid, and Leaflet centres a
 *    view in its own container.** A strip inset from the top by `d` is `d`
 *    shorter, so handing it the main map's centre would offset its content by
 *    d/2. It is given the latlng the main map shows at container
 *    `(height + d) / 2` instead, which is the same pixel row.
 *  * **A strip is one render thick, but never thicker than `FROZEN_MAX_PX`.**
 *    At 1:1 on a 1024px render two untruncated strips would be the screen, so
 *    past that the strip stops growing and shows the middle of the row or
 *    column instead -- cropped, centred, still the baseline you wanted. A drag
 *    that *starts on a strip* then scrolls that crop along the axis the strip
 *    froze, within its own render: see `stripDrag` at the bottom.
 */
'use strict';

/* How thick a strip may get. Beyond this it stops tracking the render's size
 * and crops to the middle of it: two strips that keep growing with the zoom
 * would leave no map between them. */
const FROZEN_MAX_PX = 256;

const frozen = {
  mode: 'off',             // off | row | col | both
  dom: null,
  maps: {},                // { row: L.Map, col: L.Map }
  layers: {},
  grid: null,              // { minX, minY, nx, ny, zoom } in archive tile units
  size: { row: null, col: null },   // last applied px, to skip invalidateSize
  block: { w: 0, h: 0 },   // one render on screen, px at the current zoom
  // How far each strip is scrolled along the axis it has frozen, as a fraction
  // of the render. A fraction and not pixels: it has to mean the same part of
  // the image after a zoom, and the clamp below recomputes itself.
  shift: { row: 0, col: 0 },
};

function frozenRoot() {
  if (frozen.dom) return frozen.dom;
  const host = el('map');
  const make = (cls) => {
    const node = document.createElement('div');
    node.className = `pm-frozen ${cls}`;
    node.hidden = true;
    host.append(node);
    return node;
  };
  frozen.dom = { row: make('pm-frozen-row'), col: make('pm-frozen-col') };
  return frozen.dom;
}

/** A Leaflet map with everything that makes it a map switched off. */
function stripMap(node) {
  return L.map(node, {
    crs: L.CRS.EPSG3857,
    zoomControl: false,
    attributionControl: false,
    zoomSnap: 1,
    worldCopyJump: false,
    dragging: false,
    scrollWheelZoom: false,
    doubleClickZoom: false,
    boxZoom: false,
    keyboard: false,
    touchZoom: false,
    tap: false,
    inertia: false,
    // The strip is driven frame by frame from the main map's `move`; an
    // animation of its own would lag a pan by its own duration.
    fadeAnimation: false,
    zoomAnimation: false,
    markerZoomAnimation: false,
  });
}

/** The same tiles the main layer is showing, on a strip's map. */
function stripLayer() {
  const main = state.layer;
  if (!main || !state.tileUrl) return null;
  const o = main.options;
  return L.tileLayer(state.tileUrl, {
    tileSize: o.tileSize,
    minZoom: o.minZoom,
    maxZoom: o.maxZoom,
    minNativeZoom: o.minNativeZoom,
    maxNativeZoom: o.maxNativeZoom,
    zoomOffset: o.zoomOffset,
    bounds: o.bounds,
    noWrap: true,
    errorTileUrl: o.errorTileUrl,
    // One screen of a strip is a handful of tiles; buffering four rings of
    // them around a 128px-tall viewport is pure waste.
    keepBuffer: 1,
  });
}

/** Where the grid starts and how big one render is, in archive tile units. */
function readGrid(info) {
  if (!info || !state.map) return null;
  const block = /^(\d+)x(\d+)$/.exec(String(info.render_block || ''));
  const zoom = info.max_zoom;
  if (typeof zoom !== 'number') return null;
  const extent = mapExtent(info);
  if (!Array.isArray(extent) || extent.length !== 4) return null;
  const [west, , , north] = extent;
  // The map's own north-west corner is cell (0, 0) -- the saver fills from the
  // origin, and `bounds` is the occupied extent, not the whole world.
  const nw = map.project(L.latLng(north, west), zoom);
  return {
    zoom,
    minX: Math.round(nw.x / TILE_PX),
    minY: Math.round(nw.y / TILE_PX),
    nx: block ? Number(block[1]) : 1,
    ny: block ? Number(block[2]) : 1,
  };
}

/** Screen px between two tile boundaries along one axis. */
function tileSpan(from, to, axis) {
  const { zoom } = frozen.grid;
  const point = (index) => (axis === 'x' ? L.point(index * TILE_PX, 0)
    : L.point(0, index * TILE_PX));
  const a = map.latLngToContainerPoint(map.unproject(point(from), zoom));
  const b = map.latLngToContainerPoint(map.unproject(point(to), zoom));
  return axis === 'x' ? b.x - a.x : b.y - a.y;
}

/** Centre of row 0 / column 0, as a latlng component. */
function bandCentre(axis) {
  const g = frozen.grid;
  const point = axis === 'x'
    ? L.point((g.minX + g.nx / 2) * TILE_PX, 0)
    : L.point(0, (g.minY + g.ny / 2) * TILE_PX);
  const at = map.unproject(point, g.zoom);
  return axis === 'x' ? at.lng : at.lat;
}

function hideStrips() {
  if (!frozen.dom) return;
  frozen.dom.row.hidden = true;
  frozen.dom.col.hidden = true;
}

function syncStrips() {
  if (frozen.mode === 'off' || !state.meta || !state.layer) { hideStrips(); return; }
  if (!frozen.grid) frozen.grid = readGrid(state.meta);
  if (!frozen.grid) { hideStrips(); return; }

  const g = frozen.grid;
  const blockW = tileSpan(g.minX, g.minX + g.nx, 'x');
  const blockH = tileSpan(g.minY, g.minY + g.ny, 'y');
  // Zoomed out far enough that a whole render is a couple of pixels: a strip
  // that thin is noise, and a zero-height Leaflet map is a division by zero.
  if (blockW < 2 || blockH < 2) { hideStrips(); return; }
  // The strip follows the render's size only up to the cap. Past it the band's
  // centre is still the strip's centre, so what shows is the middle of the row
  // or column rather than its top-left corner.
  const stripH = Math.min(blockH, FROZEN_MAX_PX);
  const stripW = Math.min(blockW, FROZEN_MAX_PX);
  frozen.block = { w: blockW, h: blockH };
  // The window may be scrolled only as far as its own render: half the part
  // that does not fit, either way. Nothing is cropped -> nothing to scroll,
  // and zooming out until it fits pulls the view back to the middle by itself.
  const limit = (block, strip) => (block > strip ? (block - strip) / (2 * block) : 0);
  const clampShift = (value, max) => Math.max(-max, Math.min(max, value));
  frozen.shift.row = clampShift(frozen.shift.row, limit(blockH, stripH));
  frozen.shift.col = clampShift(frozen.shift.col, limit(blockW, stripW));

  const size = map.getSize();
  const zoom = map.getZoom();
  const want = {
    row: frozen.mode === 'row' || frozen.mode === 'both',
    col: frozen.mode === 'col' || frozen.mode === 'both',
  };
  const root = frozenRoot();

  for (const which of ['row', 'col']) {
    const node = root[which];
    if (!want[which]) { node.hidden = true; continue; }
    node.hidden = false;
    // The column strip starts below the row strip when both are up, so the
    // corner belongs to the row -- which is the baseline for the column you
    // are looking at, rather than a stale corner of the grid.
    const px = which === 'row'
      ? { height: stripH, top: 0 }
      : { width: stripW, top: want.row ? stripH : 0 };
    const stamp = JSON.stringify(px) + size.x + ':' + size.y;
    if (!frozen.maps[which]) {
      frozen.maps[which] = stripMap(node);
      frozen.maps[which].setMinZoom(state.floorZoom || 0);
      frozen.maps[which].setMaxZoom(state.ceilingZoom || 22);
    }
    if (!frozen.layers[which]) {
      const layer = stripLayer();
      if (layer) {
        frozen.layers[which] = layer.addTo(frozen.maps[which]);
      }
    }
    if (frozen.size[which] !== stamp) {
      frozen.size[which] = stamp;
      if (which === 'row') {
        node.style.height = `${Math.round(px.height)}px`;
      } else {
        node.style.width = `${Math.round(px.width)}px`;
        node.style.top = `${Math.round(px.top)}px`;
      }
      // Leaflet caches the container size; without this the strip keeps
      // fetching for its previous height and leaves a grey band.
      frozen.maps[which].invalidateSize({ animate: false, pan: false });
    }
    // Slaved on one axis, nailed on the other. The free axis is read off the
    // main map at the pixel row/column the strip's own centre lines up with,
    // not from its centre latlng -- the strip is shorter than the map when the
    // other strip is up, and Leaflet centres in the container it was given.
    //
    // Both halves are assembled in PROJECTED pixels at the current zoom, where
    // one unit is one screen pixel: that is what lets the drag offset be a
    // plain addition rather than a second coordinate conversion.
    const free = map.project(which === 'row'
      ? map.containerPointToLatLng(L.point(size.x / 2, 0))
      : map.containerPointToLatLng(L.point(0, (size.y + px.top) / 2)), zoom);
    const band = map.project(L.latLng(bandCentre('y'), bandCentre('x')), zoom);
    const offset = which === 'row'
      ? frozen.shift.row * blockH : frozen.shift.col * blockW;
    const centre = map.unproject(which === 'row'
      ? L.point(free.x, band.y + offset)
      : L.point(band.x + offset, free.y), zoom);
    frozen.maps[which].setView(centre, zoom, { animate: false });
  }
}

function setFrozen(mode) {
  frozen.mode = mode;
  state.ui.freeze = mode;
  storeUi();
  frozenRoot();
  if (mode === 'off') { hideStrips(); return; }
  syncStrips();
}

/** viewer.js calls this from showMap: new archive, new grid and new tiles. */
function frozenMapChanged(info) {                        // eslint-disable-line no-unused-vars
  frozen.grid = readGrid(info);
  // The layers point at the archive we just left; drop them and let syncStrips
  // rebuild from the main map's new one.
  for (const which of ['row', 'col']) {
    if (frozen.layers[which]) {
      frozen.layers[which].remove();
      frozen.layers[which] = null;
    }
    if (frozen.maps[which]) {
      frozen.maps[which].setMinZoom(state.floorZoom || 0);
      frozen.maps[which].setMaxZoom(state.ceilingZoom || 22);
    }
    frozen.size[which] = null;
  }
  frozen.shift = { row: 0, col: 0 };
  syncStrips();
}

/** viewer.js calls this from the change feed: a render that lands in row 0 or
 *  column 0 has to reach the strips too, which are layers of their own and so
 *  are not touched by the main layer's targeted refresh. */
function frozenChanged(changes) {                        // eslint-disable-line no-unused-vars
  if (frozen.mode === 'off' || !frozen.grid) return;
  const g = frozen.grid;
  const inRow = changes.some(([, , y]) => y >= g.minY && y < g.minY + g.ny);
  const inCol = changes.some(([, x]) => x >= g.minX && x < g.minX + g.nx);
  // A strip is a handful of tiles, so redrawing the whole thing is cheaper
  // than keeping a second copy of the main layer's dirty-tile bookkeeping.
  if (inRow && frozen.layers.row) frozen.layers.row.redraw();
  if (inCol && frozen.layers.col) frozen.layers.col.redraw();
}

/* A drag that starts ON a strip scrolls that strip along the axis it froze --
 * the row strip vertically, the column strip horizontally, each only within
 * the render it is showing. Past 256 px a strip is a crop of a render, and the
 * middle is not always the part worth comparing.
 *
 * The strips stay `pointer-events: none`, so the gesture still reaches Leaflet
 * and the map pans underneath exactly as before: one drag, both motions. The
 * strip is therefore found by hit-testing the pointer against its box rather
 * than by letting it receive the event -- and only at `pointerdown`, which is
 * what confines this to drags that *began* on a strip.
 */
const stripDrag = { which: null, x: 0, y: 0 };

function stripUnder(ev) {
  if (frozen.mode === 'off' || !frozen.dom) return null;
  // Row first: the column strip starts below it, so the corner is the row's.
  for (const which of ['row', 'col']) {
    const node = frozen.dom[which];
    if (!node || node.hidden) continue;
    const box = node.getBoundingClientRect();
    if (ev.clientX >= box.left && ev.clientX <= box.right
        && ev.clientY >= box.top && ev.clientY <= box.bottom) return which;
  }
  return null;
}

el('map').addEventListener('pointerdown', (ev) => {
  const which = stripUnder(ev);
  if (!which) return;
  stripDrag.which = which;
  stripDrag.x = ev.clientX;
  stripDrag.y = ev.clientY;
});

window.addEventListener('pointermove', (ev) => {
  const which = stripDrag.which;
  if (!which || !frozen.grid) return;
  const delta = which === 'row' ? ev.clientY - stripDrag.y : ev.clientX - stripDrag.x;
  stripDrag.x = ev.clientX;
  stripDrag.y = ev.clientY;
  const span = which === 'row' ? frozen.block.h : frozen.block.w;
  if (!span) return;
  // Content follows the pointer, so the centre moves against it.
  frozen.shift[which] -= delta / span;
  syncStrips();
});

for (const type of ['pointerup', 'pointercancel']) {
  window.addEventListener(type, () => { stripDrag.which = null; });
}

map.on('move zoom moveend zoomend resize', syncStrips);

el('freeze-mode').addEventListener('change', (ev) => {
  setFrozen(ev.target.value);
  ev.target.blur();                    // hand the number keys back
});

frozenRoot();
el('freeze-mode').value = ['row', 'col', 'both'].includes(state.ui.freeze)
  ? state.ui.freeze : 'off';
setFrozen(el('freeze-mode').value);

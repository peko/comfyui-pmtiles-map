/* Two canvas overlays over the tile layer: a selection mask, and a per-tile
 * debug readout.  Loaded after viewer.js, whose top-level `const`s (map, state,
 * el, TILE_PX, archiveZoom) are global lexical bindings and therefore visible
 * here.
 *
 * The technique is the one from peko/nn-lineart: keep the selection in an
 * off-screen canvas at **one pixel per tile**, and have a GridLayer blit the
 * matching crop of it into each visible tile, scaled up with smoothing off.
 * The mask is then independent of zoom -- panning and zooming cost a
 * drawImage per tile and nothing else, and there is no per-tile DOM to keep in
 * step with a selection that may cover thousands of tiles.
 *
 * The one adaptation this map needs: nn-lineart used CRS.Simple with negative
 * zooms and a fixed 256 px tile, so it could hardcode `1 << -coords.z`.  Here
 * the archive may use 512 px tiles, in which case the map runs one zoom ahead
 * of the archive (see TRAP 19 / `shift` in viewer.js).  Giving the overlay the
 * archive's own `tileSize` makes `coords.x`/`coords.y` archive tile indices
 * directly, and `archiveZoom(coords.z)` recovers the archive's zoom -- so the
 * overlay grid is the archive grid, at any tile size.
 */
'use strict';

/* One pixel per tile, so a z=11 map is a 2048x2048 mask = 16 MB of RGBA. Past
 * that the mask is kept at a coarser zoom and one pixel covers a block of
 * tiles: a selection is a UI affordance, not a reason to allocate a gigabyte. */
const MASK_MAX_PX = 2048;

/* Two groups, drawn in opposite ways on purpose.
 *
 * `reject` dims what it covers, so a rejected tile recedes -- the point is to
 * see *less* of it, which a bright highlight cannot do.  `approve` is an
 * outline and paints nothing over the tile at all, because the whole reason to
 * approve a render is to keep looking at it.
 *
 * A tile belongs to at most one group, so they get a canvas each: `reject` can
 * then be blitted with one drawImage like any fill, while `approve` needs its
 * own pixels to trace a boundary from. */
const GROUPS = {
  reject: { stored: 'rgba(0, 0, 0, .625)', fill: true },
  approve: { stored: 'rgba(255, 255, 255, 1)', stroke: 'rgba(255, 255, 255, .95)' },
};
const GROUP_KEYS = Object.keys(GROUPS);
const OUTLINE_PX = 2;
/* A drop shadow thrown *outward* from the approved region, so the outline reads
 * as raised off the map rather than drawn on it -- and so a white line stays
 * legible over a pale render, which it would not on its own. */
const SHADOW_COLOR = 'rgba(0, 0, 0, .55)';
const SHADOW_BLUR = 12;

const overlay = {
  masks: null,                    // { reject: {canvas, ctx}, approve: {...} }
  maskZ: null,
  select: null,
  debug: null,
  counts: { reject: 0, approve: 0 },
  on: { select: false, debug: false },
};

Object.defineProperty(overlay, 'count', {
  get: () => overlay.counts.reject + overlay.counts.approve,
});

/* ------------------------------------------------------------------- mask */

function maskZoomFor(info) {
  let z = Number(info.max_zoom) || 0;
  while ((1 << z) > MASK_MAX_PX) z -= 1;
  return Math.max(0, z);
}

function ensureMask(info) {
  const want = maskZoomFor(info);
  if (overlay.masks && overlay.maskZ === want) return false;
  // Reallocating only when the granularity changes is what lets a selection
  // survive an archive switch: two maps built from the same manifest have the
  // same max_zoom, and the same coordinate means the same render in both, so
  // keeping the mask is the whole point of switching.
  overlay.masks = {};
  for (const key of GROUP_KEYS) {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1 << want;
    overlay.masks[key] = {
      canvas,
      ctx: canvas.getContext('2d', { willReadFrequently: true }),
    };
    overlay.counts[key] = 0;
  }
  overlay.maskZ = want;
  return true;
}

function countPainted(ctx, x, y, w, h) {
  if (w <= 0 || h <= 0) return 0;
  const data = ctx.getImageData(x, y, w, h).data;
  let n = 0;
  for (let i = 3; i < data.length; i += 4) if (data[i]) n += 1;
  return n;
}

/* A pixel point is a projection of a latlng that was itself unprojected from a
 * mouse position, so a corner that lands exactly on a tile boundary arrives as
 * boundary +- 1e-7 -- and a bare floor()/ceil() then takes a whole extra row or
 * column. Snap to the boundary first; a real drag is never within 1e-6 of one,
 * so nothing but the degenerate case moves. */
const TILE_EPS = 1e-6;

/** Two pixel corners -> the half-open tile rectangle they cover. */
function tileSpan(nw, se) {
  const x0 = Math.floor(nw.x / TILE_PX + TILE_EPS);
  const y0 = Math.floor(nw.y / TILE_PX + TILE_EPS);
  return [
    x0, y0,
    Math.max(x0 + 1, Math.ceil(se.x / TILE_PX - TILE_EPS)),
    Math.max(y0 + 1, Math.ceil(se.y / TILE_PX - TILE_EPS)),
  ];
}

/* Marking a whole search result set calls paintTiles once per render, and a
 * redraw of every visible tile per call is 500 redraws for one click. */
let paintBatch = 0;

function repaint() {
  if (paintBatch) return;
  if (overlay.select) overlay.select.redraw();
  renderSelectionInfo();
}

/** Paint a rectangle of tiles into one group, or erase it from all of them. */
function paintTiles(x0, y0, x1, y1, group) {
  const side = 1 << overlay.maskZ;
  const x = Math.max(0, Math.min(side, x0));
  const y = Math.max(0, Math.min(side, y0));
  const w = Math.max(0, Math.min(side, x1) - x);
  const h = Math.max(0, Math.min(side, y1) - y);
  if (!w || !h) return;

  // Clear from every group first: a tile is in one group or none, so marking a
  // rejected tile approved has to move it rather than stack. It is also what
  // stops a re-marked region accumulating alpha, the fill being translucent.
  for (const key of GROUP_KEYS) {
    const { ctx } = overlay.masks[key];
    overlay.counts[key] -= countPainted(ctx, x, y, w, h);
    ctx.globalCompositeOperation = 'destination-out';
    ctx.fillStyle = '#000';
    ctx.fillRect(x, y, w, h);
    ctx.globalCompositeOperation = 'source-over';
  }
  if (group) {
    const { ctx } = overlay.masks[group];
    ctx.fillStyle = GROUPS[group].stored;
    ctx.fillRect(x, y, w, h);
    overlay.counts[group] += w * h;
  }
  repaint();
}

function clearSelection() {
  if (!overlay.masks) return;
  for (const key of GROUP_KEYS) {
    const { canvas, ctx } = overlay.masks[key];
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    overlay.counts[key] = 0;
  }
  if (overlay.select) overlay.select.redraw();
  renderSelectionInfo();
}

/** Every marked tile, by group, as archive coordinates at the mask's zoom. */
function selectedTiles() {
  const out = {};
  for (const key of GROUP_KEYS) {
    out[key] = [];
    if (!overlay.masks || !overlay.counts[key]) continue;
    const side = 1 << overlay.maskZ;
    const data = overlay.masks[key].ctx.getImageData(0, 0, side, side).data;
    for (let i = 0, p = 3; p < data.length; p += 4, i += 1) {
      if (data[p]) out[key].push({ z: overlay.maskZ, x: i % side, y: (i / side) | 0 });
    }
  }
  return out;
}

/* ----------------------------------------------------------------- layers */

/** Archive-grid GridLayer options, so coords.x/y are archive tile indices. */
function gridOptions(zIndex) {
  const info = state.meta || {};
  return {
    tileSize: Number(info.tile_size) || 256,
    zIndex,
    noWrap: true,
    // Not `pane: 'overlayPane'`: the tile pane is where the grid geometry
    // lives, and these have to line up with the tiles exactly.
    className: 'pm-overlay-tile',
    updateWhenZooming: false,
  };
}

/** Blit a group's mask into the tile as a flat fill. */
function drawFill(ctx, mask, sx, sy, span, size) {
  if (span >= 1) {
    ctx.drawImage(mask.canvas, sx, sy, span, span, 0, 0, size.x, size.y);
    return;
  }
  // Below one mask pixel per tile a fractional source rect is at the mercy of
  // the browser's sampling; read the pixel and fill instead.
  const px = mask.ctx.getImageData(Math.floor(sx), Math.floor(sy), 1, 1).data;
  if (!px[3]) return;
  ctx.fillStyle = `rgba(${px[0]},${px[1]},${px[2]},${px[3] / 255})`;
  ctx.fillRect(0, 0, size.x, size.y);
}

/** Trace the boundary of a group's marked cells and stroke it.
 *
 * The general recipe for an outline from a flat fill is `S - erode(S)`: draw
 * the shape, intersect four translated copies with `source-in` to get the
 * erosion, then subtract it with `destination-out`. That needs two scratch
 * canvases and, worse, padding across tile borders -- erosion near an edge
 * depends on pixels belonging to the neighbouring tile.
 *
 * None of that is necessary when the mask is one pixel per tile: the boundary
 * is exactly "a marked cell whose neighbour is not marked", so one padded
 * getImageData per tile gives every edge directly. Each edge is inset by half
 * the line width so the stroke lands inside its own tile instead of being
 * clipped in half at the border.
 */
function drawOutline(ctx, mask, sx, sy, span, size, color) {
  const side = 1 << overlay.maskZ;
  const first = Math.floor(sx);
  const last = Math.ceil(sx + span) - 1;
  const firstY = Math.floor(sy);
  const lastY = Math.ceil(sy + span) - 1;
  // Pad by enough cells to cover the blur, not just one: the shadow shape has to
  // continue past the tile edge or the blur stops dead at the seam and draws a
  // shadow along a boundary that is not one.
  const scale = size.x / span;                 // tile pixels per mask cell
  const pad = Math.max(1, Math.ceil(SHADOW_BLUR / scale));
  const w = last - first + 1 + 2 * pad;
  const h = lastY - firstY + 1 + 2 * pad;
  const ox = first - pad;
  const oy = firstY - pad;

  // getImageData clamps to the canvas, so read the overlap and index by hand;
  // anything outside the mask counts as unmarked, which is what makes the
  // outline close along the edge of the world.
  const rx = Math.max(0, ox);
  const ry = Math.max(0, oy);
  const rw = Math.min(side, ox + w) - rx;
  const rh = Math.min(side, oy + h) - ry;
  if (rw <= 0 || rh <= 0) return;
  const data = mask.ctx.getImageData(rx, ry, rw, rh).data;
  const at = (mx, my) => {
    if (mx < rx || my < ry || mx >= rx + rw || my >= ry + rh) return false;
    return data[((my - ry) * rw + (mx - rx)) * 4 + 3] !== 0;
  };

  /* The marked cells as a path, over the padded range so the shape -- and so the
   * blur -- runs continuously across tile borders. */
  const addShape = () => {
    for (let my = firstY - pad; my <= lastY + pad; my += 1) {
      for (let mx = first - pad; mx <= last + pad; mx += 1) {
        if (at(mx, my)) {
          ctx.rect((mx - sx) * scale, (my - sy) * scale, scale, scale);
        }
      }
    }
  };

  /* Outer shadow, the standard way round: clip to everything *outside* the
   * shape, then fill the shape with a shadow on. The fill itself lands entirely
   * inside the clip's hole and is discarded -- only the part of its shadow that
   * spilled outwards survives, which is exactly the halo wanted and leaves the
   * approved render itself untouched. */
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, size.x, size.y);
  addShape();
  ctx.clip('evenodd');
  ctx.shadowColor = SHADOW_COLOR;
  ctx.shadowBlur = SHADOW_BLUR;
  ctx.fillStyle = '#000';                      // never seen; only its shadow is
  ctx.beginPath();
  addShape();
  ctx.fill();
  ctx.restore();

  const half = OUTLINE_PX / 2;
  ctx.beginPath();
  for (let my = firstY; my <= lastY; my += 1) {
    for (let mx = first; mx <= last; mx += 1) {
      if (!at(mx, my)) continue;
      const x0 = (mx - sx) * scale;
      const y0 = (my - sy) * scale;
      const x1 = x0 + scale;
      const y1 = y0 + scale;
      // Extend each run by `half` so the corners meet instead of leaving a nick.
      if (!at(mx, my - 1)) { ctx.moveTo(x0 - half, y0 + half); ctx.lineTo(x1 + half, y0 + half); }
      if (!at(mx, my + 1)) { ctx.moveTo(x0 - half, y1 - half); ctx.lineTo(x1 + half, y1 - half); }
      if (!at(mx - 1, my)) { ctx.moveTo(x0 + half, y0 - half); ctx.lineTo(x0 + half, y1 + half); }
      if (!at(mx + 1, my)) { ctx.moveTo(x1 - half, y0 - half); ctx.lineTo(x1 - half, y1 + half); }
    }
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = OUTLINE_PX;
  ctx.stroke();
}

function makeSelectLayer() {
  const Layer = L.GridLayer.extend({
    createTile(coords) {
      const tile = L.DomUtil.create('canvas', 'leaflet-tile');
      const size = this.getTileSize();
      tile.width = size.x;
      tile.height = size.y;
      if (!overlay.masks) return tile;
      const ctx = tile.getContext('2d');
      ctx.imageSmoothingEnabled = false;

      // Mask pixels covered by this tile, per side. >= 1 zoomed out of the
      // mask's level, < 1 when zoomed past it.
      const span = Math.pow(2, overlay.maskZ - archiveZoom(coords.z));
      const sx = coords.x * span;
      const sy = coords.y * span;
      const side = 1 << overlay.maskZ;
      if (sx >= side || sy >= side || sx < 0 || sy < 0) return tile;
      for (const key of GROUP_KEYS) {
        if (!overlay.counts[key]) continue;
        const group = GROUPS[key];
        if (group.fill) drawFill(ctx, overlay.masks[key], sx, sy, span, size);
        if (group.stroke) {
          drawOutline(ctx, overlay.masks[key], sx, sy, span, size, group.stroke);
        }
      }
      return tile;
    },
  });
  return new Layer(gridOptions(300));
}

function makeDebugLayer() {
  const Layer = L.GridLayer.extend({
    createTile(coords) {
      const tile = L.DomUtil.create('canvas', 'leaflet-tile');
      const size = this.getTileSize();
      tile.width = size.x;
      tile.height = size.y;
      const ctx = tile.getContext('2d');
      const az = archiveZoom(coords.z);
      const info = state.meta || {};

      // The border says which level the tile really came from, which is the
      // question worth asking when a map looks wrong: below min_zoom Leaflet is
      // upscaling a shallower level (TRAP 20), above max_zoom it is over-zooming
      // a tile it already has, and neither is a tile the archive holds.
      let edge = 'rgba(124, 196, 255, .55)';
      let note = '';
      if (az < info.min_zoom) { edge = 'rgba(126, 231, 135, .7)'; note = ' upscaled'; }
      else if (az > info.max_zoom) { edge = 'rgba(255, 206, 107, .8)'; note = ' over-zoom'; }

      ctx.strokeStyle = edge;
      ctx.lineWidth = 1;
      ctx.beginPath();               // top and left only, so neighbours do not
      ctx.moveTo(size.x - 0.5, 0.5); // draw the same line twice
      ctx.lineTo(0.5, 0.5);
      ctx.lineTo(0.5, size.y - 0.5);
      ctx.stroke();

      const nw = map.unproject(
        L.point(coords.x * TILE_PX, coords.y * TILE_PX), az);
      const lines = [
        `${az}/${coords.x}/${coords.y}${note}`,
        `map z${coords.z} · ${size.x}px`,
        `${nw.lat.toFixed(5)}, ${nw.lng.toFixed(5)}`,
      ];
      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
      const width = Math.max(...lines.map((t) => ctx.measureText(t).width)) + 12;
      ctx.fillStyle = 'rgba(16, 18, 23, .72)';
      ctx.fillRect(0, 0, Math.min(width, size.x), 4 + lines.length * 14);
      ctx.fillStyle = '#e6e9ef';
      lines.forEach((text, i) => ctx.fillText(text, 6, 15 + i * 14));
      return tile;
    },
  });
  return new Layer(gridOptions(400));
}

/* ------------------------------------------------------------ box selector */
/* L.Map.BoxZoom, but it reports the drag instead of zooming to it, and only
 * engages with a modifier held so an ordinary drag still pans. Leaflet's own
 * boxZoom is shift+drag, so it has to be disabled while this is on. */

const BoxSelector = L.Map.BoxZoom.extend({
  _onMouseDown(e) {
    if (!(e.shiftKey || e.ctrlKey || e.altKey)) return false;
    if (e.which !== 1 && e.button !== 0) return false;
    this._map.dragging.disable();
    this._clearDeferredResetState();
    this._resetState();
    L.DomUtil.disableTextSelection();
    L.DomUtil.disableImageDrag();
    this._startPoint = this._map.mouseEventToContainerPoint(e);
    L.DomEvent.on(document, {
      contextmenu: L.DomEvent.stop,
      mousemove: this._onMouseMove,
      mouseup: this._onMouseUp,
      keydown: this._onKeyDown,
    }, this);
    return true;
  },

  _onMouseUp(e) {
    if (e.which !== 1 && e.button !== 0) return;
    this._map.dragging.enable();
    this._finish();
    if (!this._moved) return;
    this._clearDeferredResetState();
    this._resetStateTimeout = setTimeout(L.Util.bind(this._resetState, this), 0);
    this._map.fire('boxselectend', {
      bounds: new L.LatLngBounds(
        this._map.containerPointToLatLng(this._startPoint),
        this._map.containerPointToLatLng(this._point)),
      shift: e.shiftKey, ctrl: e.ctrlKey, alt: e.altKey,
    });
  },
});
/* NOT `L.Map.addInitHook('addHandler', ...)`, which is how this is usually
 * written: an init hook runs inside the Map constructor, and viewer.js built
 * the map before this file was even fetched, so the handler would never be
 * created and shift-drag would do nothing at all. Attach it to the live
 * instance instead. (`addHandler` also only enables a handler when
 * `map.options[name]` is truthy, so that spelling needs a mergeOptions too --
 * two ways for the same silence.) */
map.boxSelector = new BoxSelector(map);

map.on('boxselectend', (ev) => {
  if (!overlay.on.select || !overlay.masks) return;
  // shift approves (outlined), alt rejects (dimmed), ctrl deselects.
  const group = ev.ctrl ? null : ev.alt ? 'reject' : 'approve';
  // Tile indices at the mask's zoom. TILE_PX is Leaflet's 256 whatever the
  // archive's tile size -- the same rule as blockBounds in viewer.js.
  const nw = map.project(ev.bounds.getNorthWest(), overlay.maskZ);
  const se = map.project(ev.bounds.getSouthEast(), overlay.maskZ);
  paintTiles(...tileSpan(nw, se), group);
});

/* --------------------------------------------------------------------- ui */

function renderSelectionInfo() {
  const box = el('sel-info');
  if (!box) return;
  box.hidden = !overlay.on.select || !overlay.count;
  const label = el('sel-count');
  if (!label) return;
  const each = overlay.maskZ === (state.meta || {}).max_zoom ? 'tile' : 'cell';
  const parts = GROUP_KEYS
    .filter((k) => overlay.counts[k])
    .map((k) => `${overlay.counts[k]} ${k === 'reject' ? 'rejected' : 'approved'}`);
  label.textContent = `${parts.join(' · ')} ${each}${overlay.count === 1 ? '' : 's'}`;
}

/* Present in the URL wins over the stored preference, absent falls back to it --
 * so a pasted link can turn an overlay on for someone whose last session had it
 * off. writeUrlState copies the existing query, so these survive a pan. */
function urlFlag(name) {
  const q = new URLSearchParams(location.search);
  if (!q.has(name)) return null;
  return q.get(name) !== '0' && q.get(name) !== 'false';
}

function writeUrlFlag(name, on) {
  const q = new URLSearchParams(location.search);
  if (on) q.set(name, '1'); else q.delete(name);
  history.replaceState(null, '', `${location.pathname}?${q}`);
}

function setOverlay(kind, on) {
  overlay.on[kind] = on;
  state.ui[`overlay_${kind}`] = on;
  storeUi();
  writeUrlFlag(kind, on);
  const toggle = el(`${kind === 'select' ? 'select' : 'debug'}-toggle`);
  if (toggle) toggle.checked = on;

  if (kind === 'select') {
    // Shift-drag is Leaflet's *box zoom* -- zoom-to-rectangle, not a selection.
    // Both cannot own the gesture, so boxZoom stands down while selecting and
    // gets it back afterwards. (nn-lineart did the same, permanently.)
    if (on) { map.boxZoom.disable(); map.boxSelector.enable(); }
    else { map.boxSelector.disable(); map.boxZoom.enable(); }
    el('map').classList.toggle('selecting', on);
    if (on && !overlay.select) { overlay.select = makeSelectLayer(); overlay.select.addTo(map); }
    else if (!on && overlay.select) { overlay.select.remove(); overlay.select = null; }
    renderSelectionInfo();
  } else if (on && !overlay.debug) {
    overlay.debug = makeDebugLayer();
    overlay.debug.addTo(map);
  } else if (!on && overlay.debug) {
    overlay.debug.remove();
    overlay.debug = null;
  }
}

/** Called by showMap once a map is on screen. */
function overlaysMapChanged(info) {
  const reset = ensureMask(info);
  // Rebuild the layers so their tileSize follows the new archive's.
  for (const kind of ['select', 'debug']) {
    if (!overlay.on[kind]) continue;
    setOverlay(kind, false);
    setOverlay(kind, true);
  }
  if (reset) renderSelectionInfo();
}

el('select-toggle').addEventListener('change', (ev) => setOverlay('select', ev.target.checked));
el('debug-toggle').addEventListener('change', (ev) => setOverlay('debug', ev.target.checked));
el('sel-clear').addEventListener('click', clearSelection);
el('sel-copy').addEventListener('click', () => {
  navigator.clipboard.writeText(JSON.stringify(
    { map: state.name, zoom: overlay.maskZ, ...selectedTiles() }, null, 2)).then(() => {
    const btn = el('sel-copy');
    btn.textContent = 'copied';
    setTimeout(() => { btn.textContent = 'copy'; }, 900);
  }, () => { /* clipboard denied */ });
});

window.addEventListener('keydown', (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const tag = (ev.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || ev.target.isContentEditable) return;
  if (ev.key === 'd') { ev.preventDefault(); setOverlay('debug', !overlay.on.debug); return; }
  if (ev.key === 'x') { ev.preventDefault(); setOverlay('select', !overlay.on.select); return; }
  // Only once the preview has taken its own Escape.
  if (ev.key === 'Escape' && el('preview').hidden && overlay.count) {
    ev.preventDefault();
    clearSelection();
  }
});

// state.ui is read from localStorage at the end of viewer.js, i.e. before this
// file runs.
const wantSelect = urlFlag('select');
const wantDebug = urlFlag('debug');
setOverlay('select', wantSelect === null ? state.ui.overlay_select === true : wantSelect);
setOverlay('debug', wantDebug === null ? state.ui.overlay_debug === true : wantDebug);

/* ---------------------------------------------------------------- marks tab */
/* Saved sets of marks, kept in localStorage like the liked list.
 *
 * Stored as flat cell indices (y * side + x) per group rather than {z,x,y}
 * objects: a full triage pass over this dataset is ~19 500 tiles, which is
 * ~1 MB of objects against ~120 KB of integers, and localStorage is a ~5 MB
 * budget for the whole origin. */

const MARKS_KEY = 'pmtiles.marks.v1';
registerTab('marks');

function loadMarks() {
  try {
    const raw = JSON.parse(localStorage.getItem(MARKS_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter((m) => m && typeof m === 'object'
      && Number.isInteger(m.z) && Array.isArray(m.approve)
      && Array.isArray(m.reject)) : [];
  } catch (err) {
    return [];
  }
}

function storeMarks() {
  try {
    localStorage.setItem(MARKS_KEY, JSON.stringify(state.marks));
    return true;
  } catch (err) {
    marksMsg('browser storage is full — delete a set and try again');
    return false;
  }
}

function marksMsg(text) {
  el('marks-msg').textContent = text || '';
  if (text) setTimeout(() => { el('marks-msg').textContent = ''; }, 4000);
}

/** The current marks as flat indices, the form they are stored in. */
function marksSnapshot() {
  const side = 1 << overlay.maskZ;
  const out = { z: overlay.maskZ, side, approve: [], reject: [] };
  for (const key of GROUP_KEYS) {
    if (!overlay.masks || !overlay.counts[key]) continue;
    const data = overlay.masks[key].ctx.getImageData(0, 0, side, side).data;
    for (let i = 0, p = 3; p < data.length; p += 4, i += 1) {
      if (data[p]) out[key].push(i);
    }
  }
  return out;
}

/** Paint a stored snapshot back onto the mask, replacing what is there. */
function applyMarks(entry) {
  if (!overlay.masks) return false;
  if (entry.z !== overlay.maskZ) {
    marksMsg(`saved at zoom ${entry.z}, this map marks at ${overlay.maskZ}`);
    return false;
  }
  clearSelection();
  const side = 1 << overlay.maskZ;
  for (const key of GROUP_KEYS) {
    const list = entry[key] || [];
    if (!list.length) continue;
    const { ctx } = overlay.masks[key];
    ctx.fillStyle = GROUPS[key].stored;
    for (const i of list) ctx.fillRect(i % side, (i / side) | 0, 1, 1);
    overlay.counts[key] = list.length;
  }
  if (overlay.select) overlay.select.redraw();
  renderSelectionInfo();
  return true;
}

function renderMarks() {
  const list = el('marks-list');
  list.textContent = '';
  el('marks-count').textContent = state.marks.length ? String(state.marks.length) : '';
  el('marks-empty').hidden = state.marks.length > 0;
  state.marks.forEach((entry, index) => {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = entry.name || `set ${index + 1}`;
    name.title = 'click to load, double-click to rename';
    name.addEventListener('dblclick', (ev) => {
      ev.stopPropagation();
      name.contentEditable = 'true';
      name.focus();
      document.execCommand('selectAll', false, null);
    });
    name.addEventListener('blur', () => {
      name.contentEditable = 'false';
      const next = name.textContent.trim();
      if (next && next !== entry.name) { entry.name = next; storeMarks(); }
      renderMarks();
    });
    name.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); name.blur(); }
      if (ev.key === 'Escape') { name.textContent = entry.name; name.blur(); }
    });

    const count = document.createElement('span');
    count.className = 'coord';
    count.textContent = `${entry.approve.length}✓ ${entry.reject.length}✕`;

    const del = document.createElement('button');
    del.className = 'del';
    del.textContent = '×';
    del.title = 'delete this set';
    del.addEventListener('click', (ev) => {
      ev.stopPropagation();
      state.marks.splice(index, 1);
      storeMarks();
      renderMarks();
    });

    li.append(name, count, del);
    li.addEventListener('click', () => {
      if (name.isContentEditable) return;
      if (applyMarks(entry)) marksMsg(`loaded “${entry.name}”`);
    });
    list.append(li);
  });
}

/* --------------------------------------------------------------- mark by search */

async function markMatches(group) {
  const query = el('marks-search').value.trim();
  if (!query || !state.name) return;
  if (!overlay.masks) { marksMsg('no map loaded'); return; }
  el('marks-search-hint').textContent = 'searching…';
  let payload;
  try {
    // limit high enough to mark a whole category in one go; the route caps at 500.
    const res = await fetch(`/map/${encodeURIComponent(state.name)}`
      + `/search?q=${encodeURIComponent(query)}&limit=500`);
    payload = res.ok ? await res.json() : null;
  } catch (err) {
    payload = null;
  }
  if (!payload) { el('marks-search-hint').textContent = 'search unavailable'; return; }
  const rows = payload.results || [];
  if (!overlay.on.select) setOverlay('select', true);
  // A search result is one *render*: its origin tile plus its nx x ny block, and
  // the whole block has to be marked or half a render would stay unmarked.
  paintBatch += 1;
  for (const row of rows) {
    const span = Math.pow(2, overlay.maskZ - row.z);
    const nx = Math.max(1, row.nx || 1);
    const ny = Math.max(1, row.ny || 1);
    paintTiles(row.x * span, row.y * span,
      (row.x + nx) * span, (row.y + ny) * span, group);
  }
  paintBatch -= 1;
  repaint();
  el('marks-search-hint').textContent =
    `${rows.length}${payload.truncated ? '+' : ''} render`
    + `${rows.length === 1 ? '' : 's'} ${group === 'approve' ? 'approved' : 'rejected'}`;
}

/* ------------------------------------------------------------- reject the rest */

/** Reject every cell that holds a render and is not marked yet.
 *
 * The client knows the map's extent but not which coordinates carry anything,
 * and most of a Hilbert-filled map is empty -- so this asks the server for a
 * bitmap of occupied cells rather than rejecting the void as well.
 */
async function rejectRest() {
  if (!overlay.masks) { marksMsg('no map loaded'); return; }
  marksMsg('reading which tiles hold a render…');
  let bits;
  let side;
  try {
    const res = await fetch(`/map/${encodeURIComponent(state.name)}`
      + `/leaves?z=${overlay.maskZ}`);
    if (!res.ok) throw new Error(String(res.status));
    side = Number(res.headers.get('X-Side')) || (1 << overlay.maskZ);
    bits = new Uint8Array(await res.arrayBuffer());
  } catch (err) {
    marksMsg('the server could not report leaf coverage (store-only feature)');
    return;
  }
  if (side !== (1 << overlay.maskZ)) {
    marksMsg(`coverage came back at a different zoom (${side} vs ${1 << overlay.maskZ})`);
    return;
  }
  if (!overlay.on.select) setOverlay('select', true);

  const marked = {};
  for (const key of GROUP_KEYS) {
    marked[key] = overlay.masks[key].ctx.getImageData(0, 0, side, side).data;
  }
  const { ctx } = overlay.masks.reject;
  ctx.fillStyle = GROUPS.reject.stored;
  let added = 0;
  for (let i = 0; i < side * side; i += 1) {
    if (!(bits[i >> 3] & (0x80 >> (i & 7)))) continue;      // nothing rendered here
    if (marked.approve[i * 4 + 3] || marked.reject[i * 4 + 3]) continue;
    ctx.fillRect(i % side, (i / side) | 0, 1, 1);
    added += 1;
  }
  overlay.counts.reject += added;
  if (overlay.select) overlay.select.redraw();
  renderSelectionInfo();
  marksMsg(`rejected ${added} previously unmarked tile${added === 1 ? '' : 's'}`);
}

/* ------------------------------------------------------------------ wiring */

el('marks-add').addEventListener('click', () => {
  if (!overlay.count) { marksMsg('nothing marked on the map'); return; }
  const snap = marksSnapshot();
  snap.name = `${state.name || 'map'} ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`;
  snap.map = state.name;
  state.marks.unshift(snap);
  if (storeMarks()) marksMsg(`saved ${snap.approve.length}✓ ${snap.reject.length}✕`);
  renderMarks();
});
el('marks-clear').addEventListener('click', clearSelection);
el('marks-reject-rest').addEventListener('click', rejectRest);
el('marks-copy').addEventListener('click', () => {
  navigator.clipboard.writeText(JSON.stringify(
    { map: state.name, zoom: overlay.maskZ, ...selectedTiles() }, null, 2)).then(
    () => marksMsg('copied the current marks'),
    () => marksMsg('the browser refused clipboard access'));
});
el('marks-search-approve').addEventListener('click', () => markMatches('approve'));
el('marks-search-reject').addEventListener('click', () => markMatches('reject'));
el('marks-search').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') { ev.preventDefault(); markMatches('approve'); }
});

state.marks = loadMarks();
renderMarks();
// viewer.js restored the tab before 'marks' existed, so a session that was left
// on it fell back to search.
if (state.ui.tab === 'marks') setTab('marks');

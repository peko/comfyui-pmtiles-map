/* Compare two renders inside the double-click preview.
 *
 * A sweep is read by difference: this cell against the one before it, or
 * against the baseline. The preview showed one render at a time, so the
 * comparison happened in memory -- open, close, open, remember. `compare` puts
 * both on screen, either side by side or superimposed under a wipe.
 *
 * What it compares against, in order:
 *   1. the render you opened *previously*, if there was one on this map;
 *   2. cell 0,0 otherwise -- the first render of the grid, which on a
 *      model x lora sweep is the baseline everything else is a variation of.
 *
 * And the reference is not fixed: two scrollbars walk it along its row and
 * down its column, so from any cell you can reach any other cell in the same
 * row or column without closing the preview. The main image never moves.
 *
 * Cells, not tiles. A render occupies `render_block` tiles (`"2x3"`), so the
 * grid is addressed in blocks from the map's north-west corner, and the server
 * stitches a whole render from any tile of it -- the top-left one here.
 */
'use strict';

const COMPARE_MODES = ['off', 'side', 'slider'];

const compare = {
  mode: 'off',
  main: null,              // {c, r} of the render that was opened
  ref: null,               // {c, r} of the reference
  last: null,              // the previous preview's cell, i.e. tomorrow's ref
  lastMap: null,           // which archive `last` belongs to
  grid: null,
  split: 50,               // wipe position, % of the image's width
};

/** The cell lattice: where it starts, how big a cell is, how many there are. */
function compareGrid() {
  const info = state.meta;
  if (!info || typeof info.max_zoom !== 'number') return null;
  const extent = mapExtent(info);
  if (!Array.isArray(extent) || extent.length !== 4) return null;
  const block = /^(\d+)x(\d+)$/.exec(String(info.render_block || ''));
  const nx = block ? Number(block[1]) : 1;
  const ny = block ? Number(block[2]) : 1;
  const zoom = info.max_zoom;
  const [west, south, east, north] = extent;
  const nw = map.project(L.latLng(north, west), zoom);
  const se = map.project(L.latLng(south, east), zoom);
  const minX = Math.round(nw.x / TILE_PX);
  const minY = Math.round(nw.y / TILE_PX);
  return {
    zoom, nx, ny, minX, minY,
    cols: Math.max(1, Math.round((se.x / TILE_PX - minX) / nx)),
    rows: Math.max(1, Math.round((se.y / TILE_PX - minY) / ny)),
  };
}

/** Tile coordinate -> cell, snapped to the block it belongs to. */
function cellOf(coord, grid) {
  if (!coord || !grid || coord.z !== grid.zoom) return null;
  return {
    c: Math.max(0, Math.floor((coord.x - grid.minX) / grid.nx)),
    r: Math.max(0, Math.floor((coord.y - grid.minY) / grid.ny)),
  };
}

function tileOf(cell, grid) {
  return {
    z: grid.zoom,
    x: grid.minX + cell.c * grid.nx,
    y: grid.minY + cell.r * grid.ny,
  };
}

function renderUrl(cell, grid) {
  const t = tileOf(cell, grid);
  return `/map/${encodeURIComponent(state.name)}/render/${t.z}/${t.x}/${t.y}`;
}

function sameCell(a, b) {
  return !!a && !!b && a.c === b.c && a.r === b.r;
}

/** Name the reference in its own column: cell, then whatever it calls itself. */
async function describeRef() {
  const node = el('ref-caption');
  const { ref, grid } = compare;
  if (!ref || !grid) { node.textContent = ''; return; }
  const head = `ref ${ref.c},${ref.r}`;
  node.textContent = head;
  const token = head;
  compare.refToken = token;
  const meta = await fetchTileMeta(tileOf(ref, grid));
  // Dragging a scrollbar fires these faster than they come back.
  if (compare.refToken !== token) return;
  const label = meta && (meta.title || meta.prompt);
  node.textContent = label ? `${head}\n${String(label).slice(0, 200)}` : head;
}

function applySplit(pct) {
  compare.split = Math.max(0, Math.min(100, pct));
  el('preview-pair').style.setProperty('--split', `${compare.split}%`);
  el('preview-divider').style.left = `${compare.split}%`;
}

/** Draw whatever the current mode calls for. */
function renderCompare() {
  const pair = el('preview-pair');
  const refImg = el('preview-ref');
  const divider = el('preview-divider');
  const on = compare.mode !== 'off' && compare.ref && compare.grid;
  pair.classList.toggle('side', on && compare.mode === 'side');
  pair.classList.toggle('wipe', on && compare.mode === 'slider');
  // One class drives the whole frame: the reference's image, its scrollbars and
  // its caption column are *reserved* when off, not removed, so switching
  // modes does not shuffle the main image sideways.
  el('preview-frame').classList.toggle('compare-on', !!on);
  divider.hidden = !on || compare.mode !== 'slider';
  if (!on) {
    refImg.removeAttribute('src');
    el('ref-caption').textContent = '';
    return;
  }
  // Half of this map may have no render at a given cell, and the scrollbars
  // walk straight through those. A failed load keeps its dark panel -- which
  // reads as "nothing here" far better than the main image showing through --
  // but it has no intrinsic width, so in `side` it would collapse to a sliver
  // and shift the layout. Pin it to the main image's width instead.
  refImg.onload = () => { refImg.style.width = ''; };
  refImg.onerror = () => {
    refImg.style.width = `${el('preview-img').getBoundingClientRect().width}px`;
    el('ref-caption').textContent =
      `ref ${compare.ref.c},${compare.ref.r}\nnothing rendered here`;
    compare.refToken = null;          // and do not let a late title overwrite it
  };
  refImg.style.width = '';
  refImg.src = renderUrl(compare.ref, compare.grid);
  const col = el('ref-col');
  const row = el('ref-row');
  col.max = String(Math.max(0, compare.grid.cols - 1));
  row.max = String(Math.max(0, compare.grid.rows - 1));
  col.value = String(compare.ref.c);
  row.value = String(compare.ref.r);
  applySplit(compare.split);
  describeRef();
}

/** viewer.js calls this from openPreview. */
function compareOpened(coord) {                          // eslint-disable-line no-unused-vars
  compare.grid = compareGrid();
  const main = cellOf(coord, compare.grid);
  compare.main = main;
  if (!main) { compare.ref = null; renderCompare(); return; }
  // The previous preview is the reference -- unless there was none on this
  // map, or it was this very cell, in which case fall back to the grid's
  // first render.
  const previous = (compare.lastMap === state.name && !sameCell(compare.last, main))
    ? compare.last : null;
  compare.ref = previous || { c: 0, r: 0 };
  // ...and this cell is what the *next* preview will compare against.
  compare.last = main;
  compare.lastMap = state.name;
  renderCompare();
}

/** viewer.js calls this from closePreview: stop loading a second render. */
function compareClosed() {                               // eslint-disable-line no-unused-vars
  el('preview-ref').removeAttribute('src');
}

function setCompare(mode) {
  compare.mode = COMPARE_MODES.includes(mode) ? mode : 'off';
  state.ui.compare = compare.mode;
  storeUi();
  el('compare-mode').value = compare.mode;
  renderCompare();
}

for (const [id, axis] of [['ref-col', 'c'], ['ref-row', 'r']]) {
  el(id).addEventListener('input', (ev) => {
    if (!compare.ref) return;
    compare.ref = Object.assign({}, compare.ref, { [axis]: Number(ev.target.value) });
    renderCompare();
  });
}

el('compare-mode').addEventListener('change', (ev) => setCompare(ev.target.value));

/* The wipe is dragged, not hovered: hovering would make the split jump every
 * time the pointer crossed the image on its way to the bar. */
let wiping = false;
el('preview-pair').addEventListener('pointerdown', (ev) => {
  if (compare.mode !== 'slider' || !compare.ref) return;
  wiping = true;
  const box = el('preview-img').getBoundingClientRect();
  applySplit(((ev.clientX - box.left) / box.width) * 100);
  ev.preventDefault();
  // Capture keeps the drag alive past the image's edge. It throws on a pointer
  // id the browser does not consider active -- which is every synthetic event,
  // so the split has to be applied before this, not after.
  try { el('preview-pair').setPointerCapture(ev.pointerId); } catch (err) { /* fine */ }
});
el('preview-pair').addEventListener('pointermove', (ev) => {
  if (!wiping) return;
  const box = el('preview-img').getBoundingClientRect();
  applySplit(((ev.clientX - box.left) / box.width) * 100);
});
for (const type of ['pointerup', 'pointercancel']) {
  el('preview-pair').addEventListener(type, () => { wiping = false; });
}

setCompare(state.ui.compare);

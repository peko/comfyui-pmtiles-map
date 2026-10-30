/* Row and column headers over the map -- a spreadsheet's frozen headers, or a
 * paper map's index grid.  Loaded after viewer.js, whose top-level `const`s
 * (map, state, el, TILE_PX) are global lexical bindings and visible here.
 *
 * Two 22px strips pinned to the map container: columns across the top, rows
 * down the left reading bottom-to-top, and a corner patch where they cross.
 * The map is untouched underneath -- the strips are an overlay, not a margin,
 * so nothing resizes and Leaflet never has to be told the viewport changed --
 * and the labels are bare white text over the renders, marked off by a 1px
 * rule at each end of their run rather than sitting on a plate. Vertical row
 * names are what keep that gutter at 22px: a horizontal one needed 140, which
 * is a whole column of renders covered up. The axis pickers live in the top
 * bar with the other controls, not in the corner.
 *
 * Why not a plugin. Leaflet.EdgeMarker projects *off-screen* markers onto the
 * viewport border with a rotated arrow -- the opposite problem, and none of its
 * maths applies to a column that is on screen. leaflet.latlng-graticule has the
 * right mechanism (a canvas sized to the container, redrawn on `move`, labels
 * drawn at the edge) but every line of its content is degrees: ticks from
 * lat/lng intervals, text from a dd/dms formatter, no hook for arbitrary
 * strings. What is borrowed here is the mechanism -- about thirty lines of it
 * -- and the rest is this map's own grid.
 *
 * The labels come from the map itself: a sweep already writes `model:…`,
 * `lora:…`, `prompt:…` into every tile's tags, so /map/<name>/axes measures
 * which tag varies along which axis and returns the runs. Nothing had to be
 * recorded for this, and it works on maps rendered long before it existed.
 *
 * Ticks are `[start, end)` in ARCHIVE tile units at the leaf zoom, which is the
 * same coordinate system the saver, the search results and `blockBounds` use --
 * never the map zoom, and never the archive's pixel tile size (see TRAP 19).
 */
'use strict';

/* A run narrower than this on screen gets no label: at the zoom where a whole
 * sweep fits, 85 rows would otherwise be 85 overlapping names, which is less
 * legible than none. The label reappears on the way in. */
const AXIS_MIN_PX = 26;

/** The strips' own thickness, read from the stylesheet so there is one source
 *  of truth for it. A label must not slide under the corner patch: the column
 *  strip starts where the row strip ends, which is what a spreadsheet does. */
function axisGutter(axis) {
  const css = getComputedStyle(document.documentElement);
  return parseFloat(css.getPropertyValue(axis === 'x' ? '--axis-w' : '--axis-h')) || 0;
}

const axes = {
  data: null,              // the last /axes payload
  name: null,              // which map it is for
  on: false,
  pending: null,           // in-flight fetch, so a map switch can ignore it
};

function axesRoot() {
  if (axes.dom) return axes.dom;
  const host = el('map');
  const make = (cls) => {
    const node = document.createElement('div');
    node.className = cls;
    host.append(node);
    return node;
  };
  // Inside the map container but outside Leaflet's panes: these must not pan
  // with the map, and must not intercept a drag meant for it.
  axes.dom = {
    x: make('pm-axis pm-axis-x'),
    y: make('pm-axis pm-axis-y'),
    // The patch where the two strips cross. A spreadsheet has one too, for the
    // same reason: without it the first column's header sits under the row
    // headers and reads as a third, nameless column.
    corner: make('pm-axis-corner'),
  };
  for (const axis of ['x', 'y']) {
    el(`axis-${axis}-key`).addEventListener('change', (ev) => {
      axes.override = Object.assign({}, axes.override, { [axis]: ev.target.value });
      loadAxes(state.name, axes.override);
      ev.target.blur();                  // hand the number keys back
    });
  }
  return axes.dom;
}

/** The header's two key pickers, filled from whatever tags the map carries. */
function fillPickers(data) {
  for (const axis of ['x', 'y']) {
    const select = el(`axis-${axis}-key`);
    const label = axis === 'x' ? 'cols' : 'rows';
    select.textContent = '';
    const none = document.createElement('option');
    none.value = '';
    none.textContent = `${label}: —`;
    select.append(none);
    for (const key of data.keys) {
      const option = document.createElement('option');
      option.value = key;
      option.textContent = `${label}: ${key}`;
      select.append(option);
    }
    select.value = data[axis].key || '';
    // A map with no tags at all has nothing to pick between.
    select.disabled = !data.keys.length;
  }
  axes.dom.corner.title = `rows: ${data.y.key || '—'} · cols: ${data.x.key || '—'}`;
}

/** Screen x (or y) of an archive tile boundary at the axes' zoom. */
function tileEdge(index, axis, zoom) {
  const point = axis === 'x' ? L.point(index * TILE_PX, 0)
    : L.point(0, index * TILE_PX);
  const at = map.latLngToContainerPoint(map.unproject(point, zoom));
  return axis === 'x' ? at.x : at.y;
}

function paintAxis(axis) {
  const data = axes.data;
  const strip = axes.dom[axis];
  const ticks = (data && data[axis].ticks) || [];
  const size = map.getSize();
  const extent = axis === 'x' ? size.x : size.y;
  const gutter = axisGutter(axis);
  const frag = document.createDocumentFragment();
  let shown = 0;

  for (const [start, end, label] of ticks) {
    const from = tileEdge(start, axis, data.zoom);
    const to = tileEdge(end, axis, data.zoom);
    if (to <= gutter || from >= extent) continue;    // off screen, or behind the corner
    if (to - from < AXIS_MIN_PX) continue;           // too thin to read
    const cell = document.createElement('div');
    cell.className = 'pm-axis-cell';
    cell.textContent = label;
    cell.title = label;
    // Sticky, in the sense a list's section header is: the label stays at the
    // edge for as long as any part of its run is on screen, then leaves with
    // it. Clamped to the run's own end so it never sits over the next column.
    const offset = Math.max(gutter, from);
    const span = Math.min(to, extent) - offset;
    if (span < AXIS_MIN_PX) continue;                // only a sliver is in the strip
    if (axis === 'x') {
      cell.style.left = `${offset}px`;
      cell.style.width = `${span}px`;
    } else {
      cell.style.top = `${offset}px`;
      cell.style.height = `${span}px`;
    }
    // Clicking a header frames its row or column -- on an 85-row map that is
    // the difference between navigating and scrolling.
    cell.addEventListener('click', () => frameRun(start, end, axis));
    frag.append(cell);
    shown += 1;
  }
  strip.textContent = '';
  strip.append(frag);
  return shown;
}

/** Centre the view on a run, keeping the other axis where it is. */
function frameRun(start, end, axis) {
  const zoom = axes.data.zoom;
  const centre = map.getCenter();
  const here = map.project(centre, zoom);
  const middle = ((start + end) / 2) * TILE_PX;
  const point = axis === 'x' ? L.point(middle, here.y) : L.point(here.x, middle);
  map.panTo(map.unproject(point, zoom));
}

function paintAxes() {
  if (!axes.on || !axes.data) return;
  paintAxis('x');
  paintAxis('y');
}

/** Fetch the axis tags for a map. `override` forces one or both keys. */
async function loadAxes(name, override) {
  if (!name) return;
  const query = new URLSearchParams();
  if (override && override.x) query.set('x', override.x);
  if (override && override.y) query.set('y', override.y);
  const token = {};
  axes.pending = token;
  let payload = null;
  try {
    const res = await fetch(`/map/${encodeURIComponent(name)}/axes?${query}`);
    payload = res.ok ? await res.json() : null;
  } catch (err) { /* a map with no store simply has no axes */ }
  // A map switch (or a second picker change) while this was in flight wins.
  if (axes.pending !== token || name !== state.name) return;
  axes.pending = null;
  axes.data = payload;
  axes.name = name;
  if (!payload) {
    // Either the map has no store to read tags from, or the server predates
    // this route -- an old ComfyUI that has not been restarted yet.
    axes.dom.corner.title = 'no axis tags for this map';
    axes.dom.x.textContent = '';
    axes.dom.y.textContent = '';
    for (const axis of ['x', 'y']) el(`axis-${axis}-key`).disabled = true;
    return;
  }
  fillPickers(payload);
  paintAxes();
  const found = (payload.x.key ? 1 : 0) + (payload.y.key ? 1 : 0);
  if (!found && payload.keys.length) {
    // Honest about the common case: a grouped map (one block per prompt, a
    // lattice of models inside it) has no column that holds a single value of
    // anything, so there is no axis to name. The picker is still there.
    axes.dom.corner.title =
      'no tag varies along one axis only — this map is not a table; '
      + 'pick a tag to label whatever rows or columns are uniform';
  } else {
    axes.dom.corner.title = '';
  }
}

function setAxes(on) {
  axes.on = on;
  state.ui.axes = on;
  storeUi();
  const root = axesRoot();
  el('map').classList.toggle('axes', on);
  for (const key of ['x', 'y', 'corner']) root[key].hidden = !on;
  for (const axis of ['x', 'y']) el(`axis-${axis}-key`).hidden = !on;
  if (!on) return;
  if (axes.name !== state.name || !axes.data) loadAxes(state.name, axes.override);
  else paintAxes();
}

/** viewer.js calls this from showMap: a new archive has its own tags. */
function axesMapChanged() {                              // eslint-disable-line no-unused-vars
  axes.data = null;
  axes.name = null;
  axes.override = null;
  if (axes.on) loadAxes(state.name, null);
}

map.on('move zoom moveend zoomend resize', paintAxes);

el('axes-toggle').addEventListener('change', (ev) => setAxes(ev.target.checked));
window.addEventListener('keydown', (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const tag = (ev.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || ev.target.isContentEditable) return;
  if (ev.key !== 'i') return;
  ev.preventDefault();
  el('axes-toggle').checked = !el('axes-toggle').checked;
  setAxes(el('axes-toggle').checked);
});

axesRoot();
el('axes-toggle').checked = state.ui.axes === true;
setAxes(state.ui.axes === true);

/* A heart on the render itself, so liking one is a click rather than a trip to
 * the sidebar.  Loaded after viewer.js, whose top-level bindings (map, state,
 * el, TILE_PX, blockBounds, fetchTileMeta, storeSaved, renderSaved) are global
 * lexical and visible here.
 *
 * Two rules shape the whole thing:
 *
 *   * the unit is a **render**, not a tile.  A 960x1408 image is a 2x3 block of
 *     512 px tiles, so a heart per tile would be six hearts on one picture and
 *     five of them in its middle.  The block comes from the tile's own `grid`.
 *   * it turns itself **off** once a render is drawn smaller than one tile of
 *     the map. Below that the heart is a fair fraction of the picture it marks,
 *     neighbours overlap, and the thing being pointed at is not identifiable
 *     anyway -- so the markers go and the hover stops resolving, rather than
 *     leaving decoration that cannot be aimed at.
 *
 * L.marker with a divIcon rather than positioned DOM: Leaflet then owns the
 * pane, the zoom animation and the anchoring, all of which hand-rolled markers
 * get subtly wrong.
 */
'use strict';

/* A liked render is worth a marker; a hovered one gets the same treatment so
 * the two cannot drift apart visually. Big enough to be an obvious target on a
 * 1024 px render, small enough not to cover its corner. */
const LIKE_PX = 34;

/* Kept in step with the .leaving transition in viewer.css: the element has to
 * outlive the click that removed it for the fade to be seen at all. */
const LIKE_OUT_MS = 120;

/* Hovering must not depend on the network.
 *
 * The first version asked /tilemeta for the tile under the cursor and read its
 * `grid`. Locally that is 5 ms; against a ComfyUI that is busy rendering it is
 * a second or two, because the request waits on the same event loop as the
 * graph -- so the heart appeared long after the cursor had moved on.
 *
 * It does not have to be a question for the server at all. Which tiles form one
 * image follows from two numbers -- the tile size and the render's size in tiles
 * -- and both are properties of the *map*:
 *
 *   * `render_block` ("3x4") comes with meta.json, recorded by the saver and the
 *     importer. Renders sit on an aligned grid (HilbertXY, auto_grid and the
 *     importer all place a block at (bx*nx, by*ny)), so the origin of the block
 *     containing any tile is floor(x/nx)*nx, floor(y/ny)*ny.
 *   * `/leaves` says which cells hold a render at all -- 8 KB for a whole z=8
 *     map, one bit per cell, fetched once per archive.
 *
 * So hovering costs nothing. A map written before `render_block` existed has no
 * shape to read, and one /tilemeta then learns it; a map whose grid turns out
 * not to be aligned gives up on the arithmetic and asks per render.
 */
const HOVER_MS = 16;

const like = {
  markers: new Map(),      // key -> L.marker, for liked renders in view
  hover: null,             // the marker for the render under the cursor
  hoverKey: null,
  blocks: new Map(),       // "z/x/y" -> {z,x,y,nx,ny,title} | null
  timer: null,
  enabled: false,
  shape: null,             // {nx, ny} once one render has been measured
  aligned: true,           // until a render is found off the shape's grid
  occupancy: null,         // Uint8Array, one bit per leaf cell
  occupancySide: 0,
  pending: false,
};

function likeKey(target) {
  return `${target.z}/${target.x}/${target.y}`;
}

/** Is a render currently drawn at least one tile wide and tall?
 *
 * Measured off the projected bounds rather than derived from the zoom, so it
 * stays right whatever the archive's tile size and the map's own zoom shift do.
 */
function renderIsBigEnough(target) {
  return spanIsBigEnough(target.nx || 1, target.ny || 1);
}

/** Would a render of `nx` x `ny` tiles be drawn at least one tile across?
 *
 * The measurement that matters is the *render's* on-screen size, not a tile's:
 * a 2x3 render at 512 px tiles is 1024x1536 at its native zoom, 512x768 one
 * level out -- still a tile wide, so still likeable -- and 256x384 the level
 * after, which is where it goes.
 */
function spanIsBigEnough(nx, ny) {
  if (!state.meta) return false;
  const z = state.meta.max_zoom;
  const a = map.latLngToContainerPoint(map.unproject(L.point(0, 0), z));
  const b = map.latLngToContainerPoint(
    map.unproject(L.point(TILE_PX * (nx || 1), TILE_PX * (ny || 1)), z));
  const tile = Number(state.meta.tile_size) || 256;
  return Math.abs(b.x - a.x) >= tile && Math.abs(b.y - a.y) >= tile;
}

/** Whether hovering is worth resolving at all.
 *
 * Per-render is the authority (`renderIsBigEnough`), but a global answer avoids
 * a /tilemeta round trip per mousemove on a map zoomed far out where nothing
 * could qualify. It is derived from the render shapes actually seen -- the liked
 * list and the block cache -- and errs open while nothing is known yet, because
 * refusing to resolve would then be self-fulfilling: the first hover is exactly
 * what teaches it a shape.
 */
function likeEnabledAt() {
  if (!state.meta) return false;
  let nx = 0;
  let ny = 0;
  for (const poi of state.saved) {
    if (poi.map && state.name && poi.map !== state.name) continue;
    nx = Math.max(nx, poi.nx || 1);
    ny = Math.max(ny, poi.ny || 1);
  }
  for (const block of like.blocks.values()) {
    if (!block) continue;
    nx = Math.max(nx, block.nx || 1);
    ny = Math.max(ny, block.ny || 1);
  }
  if (!nx || !ny) return spanIsBigEnough(1, 1) || nothingKnownYet();
  return spanIsBigEnough(nx, ny);
}

/** No render shape seen yet on this map, so keep resolving until one is. */
function nothingKnownYet() {
  return like.blocks.size === 0;
}

function isLiked(target) {
  return state.saved.some((p) => p.map === state.name
    && p.z === target.z && p.x === target.x && p.y === target.y);
}

function makeMarker(target, liked) {
  const bounds = blockBounds(target);
  const icon = L.divIcon({
    className: `pm-like${liked ? ' liked' : ''}`,
    // The glyph is a child span so the marker element itself carries the hit
    // area and the transform, and the heart does not shift when it scales.
    html: '<span aria-hidden="true">♥</span>',
    iconSize: [LIKE_PX, LIKE_PX],
    // Anchored to the block's north-east corner and pulled inside it, so the
    // heart sits in the render's top-right rather than straddling its edge.
    iconAnchor: [LIKE_PX + 6, -6],
  });
  const marker = L.marker(bounds.getNorthEast(), {
    icon,
    interactive: true,
    keyboard: false,
    // Above the tiles, and above the mark overlays, which are non-interactive.
    zIndexOffset: 1000,
    title: liked ? 'liked — click to remove' : 'like this render',
  });
  marker.on('click', (ev) => {
    // Otherwise the map's own click handler also fires and re-opens the sidebar
    // for the tile beneath the heart.
    L.DomEvent.stopPropagation(ev);
    L.DomEvent.preventDefault(ev);
    toggleLike(target);
  });
  return marker;
}

function toggleLike(target) {
  if (!state.name) return;
  const at = state.saved.findIndex((p) => p.map === state.name
    && p.z === target.z && p.x === target.x && p.y === target.y);
  if (at >= 0) {
    state.saved.splice(at, 1);
  } else {
    const entry = {
      map: state.name,
      z: target.z, x: target.x, y: target.y,
      nx: target.nx || 1, ny: target.ny || 1,
      name: (target.title || `${target.z}/${target.x}/${target.y}`).slice(0, 90),
    };
    state.saved.push(entry);
    if (!target.title) {
      // The block came from arithmetic, so the title was never fetched. Name it
      // properly in the background -- a click can afford a round trip, a hover
      // cannot, and the coordinates are already right either way.
      const name = state.name;
      fetchBlock({ z: target.z, x: target.x, y: target.y }).then((block) => {
        if (!block || !block.title || name !== state.name) return;
        if (!state.saved.includes(entry)) return;
        entry.name = block.title.slice(0, 90);
        storeSaved();
        renderSaved();
      });
    }
  }
  storeSaved();
  renderSaved();          // the pane and the map show one list, not two
  refreshLikes();
}

/** Pinned hearts for every liked render in view, plus the hovered one. */
function refreshLikes() {
  const on = likeEnabledAt();
  if (on !== like.enabled) {
    like.enabled = on;
    if (!on) clearHover();
  }
  const wanted = new Map();
  if (on && state.meta) {
    const view = map.getBounds();
    for (const poi of state.saved) {
      if (poi.map && state.name && poi.map !== state.name) continue;
      if (!view.intersects(blockBounds(poi))) continue;
      if (!renderIsBigEnough(poi)) continue;     // the same gate, per render
      wanted.set(likeKey(poi), poi);
    }
  }
  // Drop what is no longer wanted, keep what is: rebuilding every marker on
  // every pan would restart the CSS transition and flicker.
  for (const [key, marker] of like.markers) {
    if (!wanted.has(key)) {
      fadeOut(marker);
      like.markers.delete(key);
    }
  }
  for (const [key, poi] of wanted) {
    if (like.markers.has(key)) continue;
    const marker = makeMarker(poi, true).addTo(map);
    like.markers.set(key, marker);
  }
  // A hovered render that has just been liked now has a pinned marker, so the
  // transient one would sit on top of it.
  if (like.hoverKey && like.markers.has(like.hoverKey)) clearHover();
}

/** Remove a marker, letting it fade first.
 *
 * Leaflet's own `remove()` is immediate, so the CSS transition never runs --
 * hearts would blink out. Mark it, then remove once the animation has had its
 * time; the marker is inert in the meantime (`pointer-events: none`).
 */
function fadeOut(marker) {
  const el_ = marker.getElement();
  if (!el_) { marker.remove(); return; }
  el_.classList.add('leaving');
  setTimeout(() => marker.remove(), LIKE_OUT_MS);
}

function clearHover() {
  if (like.hover) { fadeOut(like.hover); like.hover = null; }
  like.hoverKey = null;
}

/** The leaf tile under a point, or null outside the level. */
function tileUnder(latlng) {
  if (!state.meta) return null;
  const z = state.meta.max_zoom;
  const p = map.project(latlng, z);
  const x = Math.floor(p.x / TILE_PX);
  const y = Math.floor(p.y / TILE_PX);
  if (x < 0 || y < 0 || x >= 2 ** z || y >= 2 ** z) return null;
  return { z, x, y };
}

/** Does a leaf cell hold a render? Unknown (null) until /leaves has arrived. */
function occupied(tile) {
  if (!like.occupancy || like.occupancySide !== 2 ** tile.z) return null;
  const i = tile.y * like.occupancySide + tile.x;
  return (like.occupancy[i >> 3] & (0x80 >> (i & 7))) !== 0;
}

/** One request per archive: which cells hold a render. */
async function loadOccupancy(name) {
  try {
    const res = await fetch(`/map/${encodeURIComponent(name)}`
      + `/leaves?z=${state.meta.max_zoom}`);
    if (!res.ok) return;
    const side = Number(res.headers.get('X-Side'));
    const bits = new Uint8Array(await res.arrayBuffer());
    if (name !== state.name) return;             // archive switched mid-flight
    like.occupancy = bits;
    like.occupancySide = side;
  } catch (err) { /* fall back to asking per render */ }
}

/** The render containing a tile, computed from the learned shape. */
function blockFromShape(tile) {
  const { nx, ny } = like.shape;
  return {
    z: tile.z,
    x: Math.floor(tile.x / nx) * nx,
    y: Math.floor(tile.y / ny) * ny,
    nx,
    ny,
  };
}

/** Ask the server for a tile's real block, and learn from the answer. */
async function fetchBlock(tile) {
  const key = `${tile.z}/${tile.x}/${tile.y}`;
  if (like.blocks.has(key)) return like.blocks.get(key);
  const meta = await fetchTileMeta(tile);
  let block = null;
  if (meta && meta.kind !== 'derived') {
    const grid = (Array.isArray(meta.grid) && meta.grid.length === 4)
      ? meta.grid.map(Number) : [0, 0, 1, 1];
    block = {
      z: tile.z, x: tile.x - grid[0], y: tile.y - grid[1],
      nx: grid[2], ny: grid[3], title: meta.title || '',
    };
    if (!like.shape) like.shape = { nx: block.nx, ny: block.ny };
    // A grid the arithmetic cannot predict: stop trusting it rather than
    // drawing hearts on the wrong corners.
    if (like.shape.nx !== block.nx || like.shape.ny !== block.ny
        || block.x % like.shape.nx || block.y % like.shape.ny) {
      like.aligned = false;
    }
  }
  like.blocks.set(key, block);
  return block;
}

/** The render under a point, without waiting on the network where possible. */
function blockUnder(latlng) {
  const tile = tileUnder(latlng);
  if (!tile) return null;
  const key = `${tile.z}/${tile.x}/${tile.y}`;
  if (like.blocks.has(key)) return like.blocks.get(key);
  if (like.shape && like.aligned) {
    const here = occupied(tile);
    if (here === false) return null;             // known empty, no request
    if (here === true) return blockFromShape(tile);
  }
  return undefined;                              // not knowable locally yet
}

function hoverAt(latlng) {
  if (!like.enabled) return;
  const block = blockUnder(latlng);
  if (block === undefined) {
    // Only reachable before the first render has been measured on this
    // archive, or on a map whose grid is not aligned. One request, then the
    // answer is arithmetic from here on.
    const tile = tileUnder(latlng);
    if (!tile || like.pending) return;
    like.pending = true;
    fetchBlock(tile).finally(() => { like.pending = false; }).then(() => {
      // Re-run against the cursor's *current* position, not this stale one.
      if (like.last) hoverAt(like.last);
    });
    return;
  }
  if (!block) { clearHover(); return; }
  const key = likeKey(block);
  if (key === like.hoverKey) return;             // same render, nothing to do
  clearHover();
  if (like.markers.has(key)) return;             // already pinned and pink
  if (!renderIsBigEnough(block)) return;
  like.hoverKey = key;
  like.hover = makeMarker(block, false).addTo(map);
}

map.on('mousemove', (ev) => {
  if (!like.enabled) return;
  like.last = ev.latlng;
  // A frame's worth of coalescing, not a wait: resolution is local, so there is
  // nothing to debounce against beyond doing it once per pointer burst.
  clearTimeout(like.timer);
  like.timer = setTimeout(() => hoverAt(like.last), HOVER_MS);
});

// Leaving the map, or leaving for the heart itself, are different things: the
// marker is a child of the map container, so moving onto it does not fire
// mouseout on the container.
map.on('mouseout', () => {
  clearTimeout(like.timer);
  clearHover();
});

map.on('zoomend moveend', () => {
  clearHover();
  refreshLikes();
});

/** Called by showMap: a new archive means new coordinates and a new list. */
/** "3x4" -> {nx: 3, ny: 4}, or null. */
function parseBlock(text) {
  const m = /^(\d+)x(\d+)$/.exec(String(text || '').trim());
  if (!m) return null;
  const nx = Number(m[1]);
  const ny = Number(m[2]);
  return (nx > 0 && ny > 0) ? { nx, ny } : null;
}

function likesMapChanged() {
  // No fade here: the layer is being torn down, and a marker outliving its
  // archive by 120 ms would be placed with the next archive's coordinates.
  for (const marker of like.markers.values()) marker.remove();
  like.markers.clear();
  clearHover();
  like.blocks.clear();           // grids belong to the archive that was loaded
  // The map says what shape its renders are, so there is nothing to learn.
  like.shape = parseBlock(state.meta && state.meta.render_block);
  like.aligned = true;
  like.occupancy = null;
  like.occupancySide = 0;
  refreshLikes();
  if (state.name && state.meta) loadOccupancy(state.name);
}

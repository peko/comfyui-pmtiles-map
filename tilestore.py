"""Mutable tile store behind the PMTiles archive.

PMTiles v3 is a write-once, clustered format: tiles live in Hilbert tile-id
order behind a varint directory, so a tile cannot be appended in place.  Every
render therefore lands in a SQLite store first, and the .pmtiles file is
serialized from it (see archive.py).

Blobs here are PNG, i.e. lossless, on purpose.  A new render touches one tile
per zoom level, so a parent tile gets rewritten many times; if the store held
WebP q80 the pyramid would compound quantisation loss on every save.  Parents
are recomposed from their children (never pasted into in place), which is both
idempotent and lossless apart from one resample per level.  WebP is applied
once, at archive time.

Tile coordinates are XYZ throughout (y from the top) -- the Leaflet and
PMTiles convention.  TMS input is flipped at the door by the caller.
"""
import contextlib
import io
import json
import os
import sqlite3
import threading
import time

from PIL import Image

LEAF = "leaf"
DERIVED = "derived"

_TILE_WEBP_DDL = """
CREATE TABLE IF NOT EXISTS tile_webp (
    z    INTEGER NOT NULL,
    x    INTEGER NOT NULL,
    y    INTEGER NOT NULL,
    blob BLOB    NOT NULL,
    q    INTEGER NOT NULL,
    PRIMARY KEY (z, x, y)
)"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    z    INTEGER NOT NULL,
    x    INTEGER NOT NULL,
    y    INTEGER NOT NULL,
    png  BLOB    NOT NULL,
    w    INTEGER NOT NULL,
    h    INTEGER NOT NULL,
    kind TEXT    NOT NULL,
    mtime REAL   NOT NULL,
    -- Legacy WebP cache, read-only now: see tile_webp below. Kept so an
    -- existing map keeps its cache across the upgrade instead of re-encoding
    -- every tile the first time it is served or serialized.
    webp   BLOB,
    webp_q INTEGER,
    PRIMARY KEY (z, x, y)
);
-- WebP encode cache, deliberately NOT a column on `tiles`.
--
-- Re-serializing the archive would re-encode every tile otherwise (7 s at 1000
-- tiles, paid on every save for tiles that did not change), so the encode is
-- kept. But `tiles` rows carry the lossless PNG -- 321 KB on a real 512 px map
-- against 18 KB for the encode -- and SQLite rewrites a row's overflow pages
-- when any column changes. Caching an 18 KB blob into that row therefore cost
-- the whole 321 KB, twice over WAL plus checkpoint. Measured on disk with
-- autocheckpoint off, 256 tiles, 769 KB PNG + 73 KB WebP:
--
--     same row        429 MB   1637 KB/tile
--     this table       22 MB     86 KB/tile
--
-- A separate table keeps the PNG untouched, so the cost is the blob and little
-- else. Invalidated by put_tile, which replaces the pixels.
-- (created from _TILE_WEBP_DDL, see below)
CREATE INDEX IF NOT EXISTS tiles_z_kind ON tiles (z, kind);
CREATE TABLE IF NOT EXISTS tile_meta (
    z    INTEGER NOT NULL,
    x    INTEGER NOT NULL,
    y    INTEGER NOT NULL,
    meta TEXT    NOT NULL,
    PRIMARY KEY (z, x, y)
);
CREATE TABLE IF NOT EXISTS map_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Which tiles changed, so the viewer can refresh those and only those. Written
-- by put_tile; the routes replay it from a sequence number the client holds.
CREATE TABLE IF NOT EXISTS tile_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    z   INTEGER NOT NULL,
    x   INTEGER NOT NULL,
    y   INTEGER NOT NULL,
    ts  REAL    NOT NULL
);
"""

# The log is a change feed, not history: a client that has fallen this far behind
# is better served by reloading than by replaying.
EVENT_LOG_LIMIT = 20000


# How a tile is kept in the store. The archive is always WebP at
# `webp_quality`; this is about the source of truth behind it.
#
# Measured per tile on real 512 px renders, and on a 4x4 pyramid rebuilt from
# one image (PSNR against the lossless pyramid, dB):
#
#     format         size    encode   z (leaf)   z-1    z-2
#     png            341 KB   17 ms      --       --     --     the original default
#     webp_lossless  238 KB   84 ms     inf      inf    inf     bit-identical
#     webp_lossy q95  ~60 KB  ~20 ms   45.2     40.8   37.2
#     webp_lossy q80  ~34 KB   16 ms   41.3     36.7   33.8
#
# **webp_lossless is free**: 25-30% smaller than PNG for pixels that come back
# bit for bit, so nothing downstream can tell the difference. It is not the
# default only because changing an existing map's format mid-run is the kind of
# thing that should be asked for.
#
# The lossy loss compounds **per pyramid level, not per save**: a parent is
# recomposed from its *children*, never from itself, so re-saving the same tile
# five times is bit-identical to saving it once (verified) -- but each level down
# encodes an already-encoded image, and that costs ~3.5 dB a level.
PNG = "png"
WEBP_LOSSLESS = "webp_lossless"
WEBP_LOSSY = "webp_lossy"
STORE_FORMATS = (PNG, WEBP_LOSSLESS, WEBP_LOSSY)


def encode_tile(img, fmt=PNG, quality=92):
    """Tile image -> bytes for the store, in the map's chosen format."""
    buf = io.BytesIO()
    if fmt == WEBP_LOSSLESS:
        # method=1, swept against PNG compress_level=1 on real 512 px render
        # tiles and on a synthetic flat-colour fixture:
        #
        #     m=0   72% / 32 ms   but 106% on the synthetic -- LARGER than PNG
        #     m=1   67% / 84 ms        82%
        #     m=4   63% / 124 ms       83%
        #
        # m=0 is the cheapest and the only one that can lose to PNG outright, so
        # it is not a safe default for an option whose whole point is "smaller,
        # same pixels". Past m=1 the extra 4% costs 40 ms a tile.
        img.save(buf, format="WEBP", lossless=True, method=1)
    elif fmt == WEBP_LOSSY:
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
        img.save(buf, format="WEBP", quality=int(quality), method=4)
    else:
        img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def png_bytes(img):
    """Back-compat alias: the lossless default."""
    return encode_tile(img, PNG)


def is_webp(blob):
    """RIFF....WEBP -- so serving can pass stored bytes straight through."""
    return len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"


def open_png(blob):
    """Decode a stored tile. Format-agnostic on purpose: Pillow sniffs the
    signature, so a map whose `store_format` changed mid-run keeps working and
    old PNG tiles stay readable beside new WebP ones."""
    img = Image.open(io.BytesIO(blob))
    img.load()
    return img


class TileSizeMismatch(Exception):
    pass


# Read-only connections, kept open per store. A fresh connection per request is
# not just the ~0.015 ms of connect(): it re-reads the schema and starts with an
# empty page cache, which measured 0.139 ms per tile read against 0.030 ms on a
# kept connection, and p95 1.96 ms against 0.047 ms. A viewport pulls a hundred
# tiles at once, so that tail is what you feel.
#
# Reads run in the aiohttp event-loop thread, but build jobs run in worker
# threads, so the connections are opened with check_same_thread=False and every
# use is serialized by one lock. Uncontended, that lock costs nothing; without it
# a stray cross-thread call would corrupt cursor state instead of raising.
#
# Autocommit reads (no explicit transaction) see each statement's own snapshot,
# so a kept connection never serves stale tiles while ComfyUI writes.
_READERS = {}
_READERS_LOCK = threading.Lock()
_READER_LIMIT = 8


@contextlib.contextmanager
def reading(db_path):
    """A cached read-only connection, or None if there is no such store.

    Keyed by identity, not just by path: a store that is deleted and recreated
    (which happens -- a map gets restarted from scratch) is a different inode, and
    a kept connection would go on serving the old one forever. One stat per read
    is ~1 us against the 0.1 ms a reopen costs.
    """
    try:
        ident = os.stat(db_path).st_ino, os.stat(db_path).st_dev
    except OSError:
        yield None
        return
    with _READERS_LOCK:
        con, known = _READERS.get(db_path, (None, None))
        if con is not None and known != ident:
            try:
                con.close()
            except sqlite3.Error:
                pass
            con = None
            _READERS.pop(db_path, None)
        if con is None:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0,
                                  check_same_thread=False)
            while len(_READERS) >= _READER_LIMIT:
                _, (evicted, _ident) = _READERS.popitem()
                try:
                    evicted.close()
                except sqlite3.Error:
                    pass
            _READERS[db_path] = (con, ident)
        try:
            yield con
        except sqlite3.Error:
            # A deleted or replaced store: drop it so the next call reopens.
            _READERS.pop(db_path, None)
            try:
                con.close()
            except sqlite3.Error:
                pass
            raise


def close_readers():
    """Drop every cached connection (tests, and anything that deletes a store)."""
    with _READERS_LOCK:
        for con, _ident in _READERS.values():
            try:
                con.close()
            except sqlite3.Error:
                pass
        _READERS.clear()


def read_changes(db_path, since=None, limit=2000, live=False):
    """Tiles changed after `since`, capped at the sequence the archive holds.

    Returns (seq, changes, truncated). `seq` is what the client should send next
    time. Nothing beyond `archive_seq` is reported: those tiles are in the store
    but not yet in the .pmtiles, so telling a client to refetch them would just
    hand back the old bytes and clear the dirty flag for good.

    `since=None` means "just tell me where we are" — a fresh client has current
    tiles already and wants no backlog. `live=True` lifts the archive ceiling,
    for a viewer reading tiles out of the store.
    """
    with reading(db_path) as db:
      if db is None:
        return None, [], False
      try:
        if live:
            # Live viewers read tiles straight from the store, so a tile is
            # fetchable the moment it is written -- no need to wait for the
            # archive, which is what archive_seq gates.
            row = db.execute("SELECT MAX(seq) FROM tile_events").fetchone()
        else:
            row = db.execute(
                "SELECT value FROM map_meta WHERE key='archive_seq'").fetchone()
        ceiling = int(row[0]) if row and row[0] is not None else 0
        if since is None:
            return ceiling, [], False
        rows = db.execute(
            "SELECT seq, z, x, y FROM tile_events WHERE seq > ? AND seq <= ? "
            "ORDER BY seq LIMIT ?", (int(since), ceiling, limit + 1)
        ).fetchall()
      except sqlite3.Error:
        return None, [], False
    truncated = len(rows) > limit
    rows = rows[:limit]
    seq = rows[-1][0] if rows else max(int(since), ceiling)
    return seq, [[z, x, y] for _, z, x, y in rows], truncated


def search(db_path, query, limit=60):
    """Leaf tiles whose title, tags or prompt contain every term in `query`.

    Straight `json_extract` over the whole table, no index: measured at 0.04 s
    across 5463 rows / 34 MB of metadata, which is well under the latency of the
    keystroke that triggered it. An index table would be one more thing to keep
    in step with the store for no gain at this size.
    """
    terms = [t for t in (query or "").lower().split() if t]
    if not terms:
        return []
    # LIKE wildcards in a user's query are literals, not operators.
    def like(term):
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    haystack = ("lower(ifnull(json_extract(meta,'$.title'),'') || ' ' || "
                "ifnull(json_extract(meta,'$.tags'),'') || ' ' || "
                "ifnull(json_extract(meta,'$.prompt'),''))")
    where = " AND ".join([f"{haystack} LIKE ? ESCAPE '\\'"] * len(terms))
    # One result per *render*, not per tile: a sliced render writes the same
    # metadata to all sixteen of its tiles, and sixteen identical rows is not a
    # result list. The top-left tile of a block is the one with grid [0, 0, ..],
    # and a single-tile save has no grid at all.
    sql = (f"SELECT z, x, y, json_extract(meta,'$.title'), "
           f"json_extract(meta,'$.tags'), json_extract(meta,'$.prompt'), "
           f"json_extract(meta,'$.grid[2]'), json_extract(meta,'$.grid[3]') "
           f"FROM tile_meta WHERE json_extract(meta,'$.kind') = 'leaf' AND {where} "
           f"AND (json_extract(meta,'$.grid') IS NULL OR "
           f"     (json_extract(meta,'$.grid[0]') = 0 AND "
           f"      json_extract(meta,'$.grid[1]') = 0)) "
           f"ORDER BY z DESC, y, x LIMIT ?")
    with reading(db_path) as db:
        if db is None:
            return []
        try:
            rows = db.execute(sql,
                              [like(t) for t in terms] + [int(limit) + 1]).fetchall()
        except sqlite3.Error:
            return []
    truncated = len(rows) > limit
    return {
        "truncated": truncated,
        "results": [
            {"z": z, "x": x, "y": y, "title": title, "tags": tags,
             "prompt": (prompt or "")[:160],
             "nx": nx or 1, "ny": ny or 1}
            for z, x, y, title, tags, prompt, nx, ny in rows[:limit]
        ],
    }


def write_webp_cache(db_path, z, x, y, blob, quality):
    """Best-effort: keep an on-the-fly encode for next time.

    Costs the blob itself and makes the eventual archive build nearly free,
    since build_archive reuses exactly this cache. Goes to `tile_webp`, never
    to a column on `tiles` -- writing it beside the PNG cost 19x as much disk
    (see the schema comment). A failure here is not worth reporting: the tile
    was already served.
    """
    insert = ("INSERT INTO tile_webp (z, x, y, blob, q) VALUES (?, ?, ?, ?, ?) "
              "ON CONFLICT(z, x, y) DO UPDATE SET blob = excluded.blob, "
              "q = excluded.q")
    try:
        db = sqlite3.connect(db_path, timeout=2.0)
        try:
            try:
                db.execute(insert, (z, x, y, blob, int(quality)))
            except sqlite3.OperationalError:
                # A map last written before the cache moved out of the row has
                # no tile_webp yet, and serving is the first thing to touch it
                # after an upgrade. Swallowing this would leave the cache empty
                # forever -- every view re-encoding a tile it had already
                # encoded -- so create the table here, where the connection is
                # writable, and retry once.
                db.execute(_TILE_WEBP_DDL)
                db.execute(insert, (z, x, y, blob, int(quality)))
            db.commit()
        finally:
            db.close()
    except sqlite3.Error:
        pass


def read_tile_for_serving(db_path, z, x, y):
    """(bytes, needs_encoding) for a tile in the store, or (None, False).

    The live viewer reads here rather than from the archive, so a run never has
    to serialize just to be watched. Measured on a 21846-tile map: 0.36 ms per
    tile from this cache against 2.11 ms through the archive's directory, for
    identical bytes.

    **Any** cached WebP counts as a hit, whatever quality it was encoded at: a
    viewer wants pixels, not a particular q. Demanding a match meant that on a
    map saved at q90 every view re-encoded the tile at q80 and overwrote the
    cache, after which the next archive build re-encoded it back -- the two
    fighting over the very cache that exists to avoid the work.
    """
    with reading(db_path) as db:
        if db is None:
            return None, False
        try:
            row = db.execute(
                "SELECT c.blob, t.webp, t.png FROM tiles t "
                "LEFT JOIN tile_webp c ON c.z=t.z AND c.x=t.x AND c.y=t.y "
                "WHERE t.z=? AND t.x=? AND t.y=?", (z, x, y)).fetchone()
        except sqlite3.OperationalError:
            # A store not opened for writing since the cache moved out of the
            # row has no tile_webp yet, and this connection is read-only, so it
            # cannot create one. Serve from the legacy column instead of
            # turning every tile into a hole.
            try:
                row = db.execute(
                    "SELECT NULL, webp, png FROM tiles WHERE z=? AND x=? AND y=?",
                    (z, x, y)).fetchone()
            except sqlite3.Error:
                return None, False
        except sqlite3.Error:
            return None, False
    if row is None:
        return None, False
    cached, legacy, stored = row
    if cached is not None:
        return cached, False
    if legacy is not None:      # written before the cache moved out of the row
        return legacy, False
    if is_webp(stored):
        # A WebP-backed store needs no encode cache at all: the bytes on disk
        # are already what the browser wants.
        return stored, False
    return stored, True         # caller encodes; the blob is the lossless PNG


def read_occupancy(db_path, zoom=None):
    """Which cells at `zoom` have a leaf tile under them, as a packed bitmap.

    Returns `(zoom, side, bytes)` -- one bit per cell, row-major, MSB first, so
    a z=8 map is 8 KB whatever it holds.  The client needs this to answer "mark
    everything I have not already looked at": it knows the map's extent but not
    which of those millions of coordinates actually carry a render, and a list
    of 19 494 triples is a megabyte of JSON to say what 8 KB of bits says.

    `zoom` coarser than the leaf level sets a bit when *any* leaf falls under
    the cell, which is what makes the answer usable at whatever granularity the
    caller's own mask happens to be.
    """
    with reading(db_path) as db:
        if db is None:
            return None
        row = db.execute(
            "SELECT max(z) FROM tiles WHERE kind = ?", (LEAF,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        leaf_z = int(row[0])
        z = leaf_z if zoom is None else max(0, min(int(zoom), leaf_z))
        shift = leaf_z - z
        side = 1 << z
        bits = bytearray((side * side + 7) // 8)
        for x, y in db.execute(
            "SELECT x, y FROM tiles WHERE z = ? AND kind = ?", (leaf_z, LEAF)
        ):
            index = (y >> shift) * side + (x >> shift)
            bits[index >> 3] |= 0x80 >> (index & 7)
    return z, side, bytes(bits)


def read_extent(db_path):
    """Zoom range, tile count and the deepest level's x/y span, from SQL only.

    Lets a store be described before it has ever been serialized -- which is the
    state a run with `write_archive` off lives in, and which the viewer has to be
    able to show, or the build button is unreachable.
    """
    with reading(db_path) as db:
      if db is None:
        return None
      try:
        minz, maxz, count = db.execute(
            "SELECT MIN(z), MAX(z), COUNT(*) FROM tiles").fetchone()
        if maxz is None:
            return None
        x0, x1, y0, y1 = db.execute(
            "SELECT MIN(x), MAX(x), MIN(y), MAX(y) FROM tiles WHERE z = ?",
            (maxz,)).fetchone()
        size = db.execute("SELECT value FROM map_meta WHERE key='tile_size'").fetchone()
        pending = db.execute(
            "SELECT value FROM map_meta WHERE key='pending_tiles'").fetchone()
        stale = db.execute(
            "SELECT value FROM map_meta WHERE key='pyramid_stale'").fetchone()
      except sqlite3.Error:
        return None
    return {
        "min_zoom": int(minz), "max_zoom": int(maxz), "tiles": int(count),
        "x0": int(x0), "x1": int(x1), "y0": int(y0), "y1": int(y1),
        "tile_size": int(size[0]) if size else 256,
        "pending_tiles": int(pending[0]) if pending and pending[0] else 0,
        "pyramid_stale": (stale[0] == "1") if stale else (minz == maxz and count > 1),
    }


def read_tile_png(db_path, z, x, y):
    """One tile's stored PNG bytes, read-only. Lossless, unlike the archive."""
    with reading(db_path) as db:
        if db is None:
            return None
        try:
            row = db.execute(
                "SELECT png FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)
            ).fetchone()
        except sqlite3.Error:
            return None
    return None if row is None else row[0]


def read_map_meta(db_path, key, default=None):
    with reading(db_path) as db:
        if db is None:
            return default
        try:
            row = db.execute("SELECT value FROM map_meta WHERE key = ?",
                             (key,)).fetchone()
        except sqlite3.Error:
            return default
    return default if row is None else row[0]


def read_meta(db_path, z, x, y):
    """One tile's metadata from a store, read-only and without migrating it.

    Used by the HTTP routes: the viewer asks per tile, so a map with thousands of
    tiles never has to ship one giant metadata blob (and stays informative even
    when the archive's own blob was skipped for size).
    """
    with reading(db_path) as db:
        if db is None:
            return None
        try:
            row = db.execute(
                "SELECT meta FROM tile_meta WHERE z=? AND x=? AND y=?", (z, x, y)
            ).fetchone()
        except sqlite3.Error:
            return None
    return None if row is None else json.loads(row[0])


class TileStore:
    """SQLite-backed tile pyramid.  One file per map."""

    def __init__(self, db_path, tile_size=None, store_format=None,
                 store_quality=None):
        """`tile_size=None` adopts whatever the store was created with.

        Only a caller that *states* a size gets the mismatch guard -- that is the
        saver, where the node's widget could disagree with the map. Maintenance
        and read paths (the CLI, PMTilesMapInfo, the build job) have no opinion
        and must not force 256 onto a 512 map.
        """
        self.path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db = sqlite3.connect(db_path, timeout=60.0)
        self.db.executescript(_SCHEMA)
        self.db.execute(_TILE_WEBP_DDL)
        self._migrate()
        # WAL so a reader (the standalone server, a test) never blocks a render.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # Unlike tile_size, the format may change mid-map: the decoder sniffs
        # the signature, so old and new tiles coexist. A stated format wins and
        # is remembered; otherwise adopt what the map already uses.
        if store_format is None:
            self.store_format = self.get_map_meta("store_format", PNG)
            if self.store_format not in STORE_FORMATS:
                self.store_format = PNG
        else:
            if store_format not in STORE_FORMATS:
                raise ValueError(f"unknown store_format {store_format!r}; "
                                 f"expected one of {', '.join(STORE_FORMATS)}")
            self.store_format = store_format
            self.set_map_meta("store_format", store_format)
        if store_quality is None:
            self.store_quality = int(self.get_map_meta("store_quality", 92))
        else:
            self.store_quality = int(store_quality)
            self.set_map_meta("store_quality", self.store_quality)

        stored = self.get_map_meta("tile_size")
        if stored is None:
            self.tile_size = int(tile_size if tile_size is not None else 256)
            self.set_map_meta("tile_size", str(self.tile_size))
        else:
            self.tile_size = int(stored)
            if tile_size is not None and int(tile_size) != self.tile_size:
                raise TileSizeMismatch(
                    f"{db_path} was created with tile_size={self.tile_size}, "
                    f"cannot mix in tile_size={tile_size} -- the pyramid assumes "
                    f"one tile size per map; use a different map_name"
                )
        self.db.commit()

    def _migrate(self):
        """CREATE TABLE IF NOT EXISTS does not add columns to an older store."""
        have = {row[1] for row in self.db.execute("PRAGMA table_info(tiles)")}
        for column, decl in (("webp", "BLOB"), ("webp_q", "INTEGER")):
            if column not in have:
                self.db.execute(f"ALTER TABLE tiles ADD COLUMN {column} {decl}")

    def close(self):
        self.db.commit()
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- map meta

    def get_map_meta(self, key, default=None):
        row = self.db.execute(
            "SELECT value FROM map_meta WHERE key = ?", (key,)
        ).fetchone()
        return default if row is None else row[0]

    def set_map_meta(self, key, value):
        self.db.execute(
            "INSERT INTO map_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # ------------------------------------------------------------------ tiles

    def get_png(self, z, x, y):
        row = self.db.execute(
            "SELECT png FROM tiles WHERE z = ? AND x = ? AND y = ?", (z, x, y)
        ).fetchone()
        return None if row is None else row[0]

    def get_image(self, z, x, y):
        blob = self.get_png(z, x, y)
        return None if blob is None else open_png(blob)

    def put_tile(self, z, x, y, img, kind=LEAF, meta=None):
        """Store one tile image.  Replaces whatever was there."""
        return self.put_tile_encoded(
            z, x, y,
            encode_tile(img, self.store_format, self.store_quality),
            img.width, img.height, kind=kind, meta=meta,
        )

    def put_tile_encoded(self, z, x, y, blob, w, h, kind=LEAF, meta=None):
        """Store one already-encoded tile.  Replaces whatever was there.

        Split out of `put_tile` so a bulk importer can do the expensive part --
        decode, pad, crop, encode -- in worker processes and leave the parent
        with nothing but the insert, since SQLite has to stay single-threaded.
        `blob` must be in this store's `store_format`; nothing re-checks it,
        `open_png` sniffs the signature on the way back out.
        """
        if (1 << z) <= max(x, y) or min(x, y) < 0:
            raise ValueError(f"tile {z}/{x}/{y} is outside the z={z} extent")
        self.db.execute(
            "INSERT INTO tiles (z, x, y, png, w, h, kind, mtime, webp, webp_q) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL) "
            "ON CONFLICT(z, x, y) DO UPDATE SET png = excluded.png, "
            "w = excluded.w, h = excluded.h, kind = excluded.kind, "
            "mtime = excluded.mtime, webp = NULL, webp_q = NULL",
            (z, x, y, blob, w, h, kind, time.time()),
        )
        # New pixels, so any encode of the old ones is wrong. The row's own
        # legacy columns are cleared above; the cache table needs its own delete.
        self.db.execute("DELETE FROM tile_webp WHERE z=? AND x=? AND y=?", (z, x, y))
        if meta is not None:
            self.db.execute(
                "INSERT INTO tile_meta (z, x, y, meta) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(z, x, y) DO UPDATE SET meta = excluded.meta",
                (z, x, y, json.dumps(meta, ensure_ascii=False)),
            )
        self.db.execute(
            "INSERT INTO tile_events (z, x, y, ts) VALUES (?, ?, ?, ?)",
            (z, x, y, time.time()),
        )

    def delete_tile(self, z, x, y):
        self.db.execute("DELETE FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y))
        self.db.execute("DELETE FROM tile_meta WHERE z=? AND x=? AND y=?", (z, x, y))
        self.db.execute("DELETE FROM tile_webp WHERE z=? AND x=? AND y=?", (z, x, y))

    def get_webp_cached(self, z, x, y, quality):
        """Encoded bytes for this tile at this quality, or None.

        Falls back to the pre-move column so upgrading a map does not throw its
        cache away -- a full re-encode of an existing store is minutes of CPU
        for bytes that are already correct.
        """
        row = self.db.execute(
            "SELECT blob FROM tile_webp WHERE z=? AND x=? AND y=? AND q = ?",
            (z, x, y, int(quality)),
        ).fetchone()
        if row is not None:
            return row[0]
        row = self.db.execute(
            "SELECT webp FROM tiles WHERE z=? AND x=? AND y=? AND webp_q = ?",
            (z, x, y, int(quality)),
        ).fetchone()
        return None if row is None or row[0] is None else row[0]

    def set_webp_cached(self, z, x, y, blob, quality):
        self.db.execute(
            "INSERT INTO tile_webp (z, x, y, blob, q) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(z, x, y) DO UPDATE SET blob = excluded.blob, q = excluded.q",
            (z, x, y, blob, int(quality)),
        )

    def get_meta(self, z, x, y):
        row = self.db.execute(
            "SELECT meta FROM tile_meta WHERE z=? AND x=? AND y=?", (z, x, y)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def current_seq(self):
        row = self.db.execute("SELECT MAX(seq) FROM tile_events").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def bump_pending(self, n):
        """Count tiles written since the archive was last serialized."""
        total = int(self.get_map_meta("pending_tiles", "0") or 0) + int(n)
        self.set_map_meta("pending_tiles", total)
        return total

    def pending_tiles(self):
        return int(self.get_map_meta("pending_tiles", "0") or 0)

    def mark_archived(self, seq=None):
        """Record which changes the .pmtiles now contains, and trim the log."""
        self.set_map_meta("archive_seq", self.current_seq() if seq is None else seq)
        self.set_map_meta("pending_tiles", 0)
        self.db.execute(
            "DELETE FROM tile_events WHERE seq <= "
            "(SELECT MAX(seq) - ? FROM tile_events)", (EVENT_LOG_LIMIT,)
        )

    def all_coords(self):
        return [
            (z, x, y)
            for z, x, y in self.db.execute("SELECT z, x, y FROM tiles ORDER BY z, x, y")
        ]

    def all_meta(self):
        return {
            f"{z}/{x}/{y}": json.loads(meta)
            for z, x, y, meta in self.db.execute(
                "SELECT z, x, y, meta FROM tile_meta ORDER BY z, x, y"
            )
        }

    def stats(self):
        rows = self.db.execute(
            "SELECT z, kind, COUNT(*), SUM(LENGTH(png)) FROM tiles GROUP BY z, kind "
            "ORDER BY z"
        ).fetchall()
        per_zoom = {}
        total = 0
        for z, kind, n, nbytes in rows:
            per_zoom.setdefault(z, {})[kind] = n
            total += n
        return {
            "tiles": total,
            "per_zoom": per_zoom,
            "min_zoom": min(per_zoom) if per_zoom else None,
            "max_zoom": max(per_zoom) if per_zoom else None,
            "tile_size": self.tile_size,
        }

    def leaf_cells(self, z):
        return {
            (x, y)
            for x, y in self.db.execute(
                "SELECT x, y FROM tiles WHERE z = ?", (z,)
            )
        }

    # -------------------------------------------------------------- placement

    def next_free_block(self, z, nx=1, ny=1, limit=1 << 20):
        """First free nx x ny block at zoom z, on the nx/ny-aligned block grid.

        Scans in growing-square shells rather than row-major: row-major would
        march along a 2^z-wide first row, which for any interesting z is not a
        map so much as a ribbon.  Shells keep the accumulated results compact.
        """
        occupied = self.leaf_cells(z)
        extent = min((1 << z) // max(nx, 1), (1 << z) // max(ny, 1))
        if extent < 1:
            raise ValueError(f"a {nx}x{ny} tile block does not fit at z={z}")

        def free(bx, by):
            ox, oy = bx * nx, by * ny
            return all(
                (ox + dx, oy + dy) not in occupied
                for dx in range(nx)
                for dy in range(ny)
            )

        seen = 0
        for k in range(extent):
            for i in range(k + 1):          # row y=k, x=0..k
                if free(i, k):
                    return i * nx, k * ny
                seen += 1
            for j in range(k):              # column x=k, y=0..k-1
                if free(k, j):
                    return k * nx, j * ny
                seen += 1
            if seen > limit:
                break
        raise ValueError(f"no free {nx}x{ny} block at z={z}")

    # ---------------------------------------------------------------- pyramid

    def recompose_ancestors(self, coords, min_zoom=0):
        """Rebuild every ancestor of `coords` down to min_zoom.

        Returns the list of (z, x, y) that were written.  Ancestors are deduped
        per level, so a 4x4 slice does one chain of work, not sixteen.
        """
        written = []
        level = {(z, x, y) for z, x, y in coords}
        while True:
            zs = {z for z, _, _ in level}
            if not zs:
                break
            z = max(zs)
            if z <= min_zoom:
                break
            parents = {(z - 1, x >> 1, y >> 1) for zz, x, y in level if zz == z}
            for pz, px, py in sorted(parents):
                if self._recompose(pz, px, py):
                    written.append((pz, px, py))
            level = {c for c in level if c[0] != z} | parents
        return written

    def _recompose(self, z, x, y):
        """Rebuild one tile from its (up to four) children.  True if written."""
        row = self.db.execute(
            "SELECT kind FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)
        ).fetchone()
        if row is not None and row[0] == LEAF:
            # A hand-placed render at this coordinate outranks an aggregate of
            # its children; clobbering it would silently discard a real image.
            return False
        ts = self.tile_size
        half = ts // 2
        canvas = None
        n_children = 0
        for dx in (0, 1):
            for dy in (0, 1):
                child = self.get_image(z + 1, x * 2 + dx, y * 2 + dy)
                if child is None:
                    continue
                child = child.convert("RGBA")
                if child.size != (ts, ts):
                    child = child.resize((ts, ts), Image.LANCZOS)
                if canvas is None:
                    canvas = Image.new("RGBA", (ts, ts), (0, 0, 0, 0))
                # XYZ y grows downward, matching pixel y -- no flip here.
                canvas.paste(child.resize((half, half), Image.LANCZOS),
                             (dx * half, dy * half))
                n_children += 1
        if canvas is None:
            self.delete_tile(z, x, y)
            return False
        self.put_tile(z, x, y, canvas, kind=DERIVED,
                      meta={"kind": DERIVED, "children": n_children})
        return True

    def rebuild_pyramid(self, min_zoom=0, progress=None):
        """Recompose every derived level from scratch, bottom-up, once each.

        This is the cheap way to get a pyramid. Recomposing on every save costs
        `depth` recompositions per render and rewrites the shallow tiles once per
        render -- on a full z=8 map the z=0 tile would be rebuilt 65536 times.
        Measured on 256 leaves at z=4: 1024 recompositions and 4.2 s per-save,
        against 85 and 0.6 s in one pass. Hence `pyramid_to_zoom == z` while
        rendering, then this at the end.

        `progress(level, done, total)` is called per level, if given.
        """
        leaves = [
            (z, x, y)
            for z, x, y in self.db.execute(
                "SELECT z, x, y FROM tiles WHERE kind = ?", (LEAF,)
            )
        ]
        if not leaves:
            return []
        maxz = max(z for z, _, _ in leaves)
        self.db.execute(
            "DELETE FROM tiles WHERE kind = ? AND z < ?", (DERIVED, maxz)
        )
        written = []
        level = set(leaves)
        for z in range(maxz, min_zoom, -1):
            parents = sorted({(z - 1, x >> 1, y >> 1) for zz, x, y in level if zz == z})
            for i, (pz, px, py) in enumerate(parents):
                if self._recompose(pz, px, py):
                    written.append((pz, px, py))
                if progress and (i % 256 == 0 or i + 1 == len(parents)):
                    progress(pz, i + 1, len(parents))
            level = {c for c in level if c[0] != z} | set(parents)
        self.set_map_meta("pyramid_stale", "0")
        return written

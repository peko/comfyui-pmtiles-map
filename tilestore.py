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
import io
import json
import os
import sqlite3
import time

from PIL import Image

LEAF = "leaf"
DERIVED = "derived"

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
    -- WebP encode cache. Re-serializing the archive re-encodes every tile
    -- otherwise, which measured 7 s at 1000 tiles -- paid on every save, for
    -- tiles that did not change. Invalidated by put_tile.
    webp   BLOB,
    webp_q INTEGER,
    PRIMARY KEY (z, x, y)
);
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


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def open_png(blob):
    img = Image.open(io.BytesIO(blob))
    img.load()
    return img


class TileSizeMismatch(Exception):
    pass


def _read_only(db_path):
    if not os.path.exists(db_path):
        return None
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)


def read_changes(db_path, since=None, limit=2000):
    """Tiles changed after `since`, capped at the sequence the archive holds.

    Returns (seq, changes, truncated). `seq` is what the client should send next
    time. Nothing beyond `archive_seq` is reported: those tiles are in the store
    but not yet in the .pmtiles, so telling a client to refetch them would just
    hand back the old bytes and clear the dirty flag for good.

    `since=None` means "just tell me where we are" — a fresh client has current
    tiles already and wants no backlog.
    """
    db = _read_only(db_path)
    if db is None:
        return None, [], False
    try:
        row = db.execute("SELECT value FROM map_meta WHERE key='archive_seq'").fetchone()
        ceiling = int(row[0]) if row and row[0] is not None else 0
        if since is None:
            return ceiling, [], False
        rows = db.execute(
            "SELECT seq, z, x, y FROM tile_events WHERE seq > ? AND seq <= ? "
            "ORDER BY seq LIMIT ?", (int(since), ceiling, limit + 1)
        ).fetchall()
    except sqlite3.Error:
        return None, [], False
    finally:
        db.close()
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
    db = _read_only(db_path)
    if db is None:
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
    try:
        rows = db.execute(sql, [like(t) for t in terms] + [int(limit) + 1]).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
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


def read_tile_png(db_path, z, x, y):
    """One tile's stored PNG bytes, read-only. Lossless, unlike the archive."""
    db = _read_only(db_path)
    if db is None:
        return None
    try:
        row = db.execute(
            "SELECT png FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return None if row is None else row[0]


def read_map_meta(db_path, key, default=None):
    db = _read_only(db_path)
    if db is None:
        return default
    try:
        row = db.execute("SELECT value FROM map_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    finally:
        db.close()
    return default if row is None else row[0]


def read_meta(db_path, z, x, y):
    """One tile's metadata from a store, read-only and without migrating it.

    Used by the HTTP routes: the viewer asks per tile, so a map with thousands of
    tiles never has to ship one giant metadata blob (and stays informative even
    when the archive's own blob was skipped for size).
    """
    if not os.path.exists(db_path):
        return None
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        row = db.execute(
            "SELECT meta FROM tile_meta WHERE z=? AND x=? AND y=?", (z, x, y)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return None if row is None else json.loads(row[0])


class TileStore:
    """SQLite-backed tile pyramid.  One file per map."""

    def __init__(self, db_path, tile_size=None):
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
        self._migrate()
        # WAL so a reader (the standalone server, a test) never blocks a render.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
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
        if (1 << z) <= max(x, y) or min(x, y) < 0:
            raise ValueError(f"tile {z}/{x}/{y} is outside the z={z} extent")
        blob = png_bytes(img)
        self.db.execute(
            "INSERT INTO tiles (z, x, y, png, w, h, kind, mtime, webp, webp_q) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL) "
            "ON CONFLICT(z, x, y) DO UPDATE SET png = excluded.png, "
            "w = excluded.w, h = excluded.h, kind = excluded.kind, "
            "mtime = excluded.mtime, webp = NULL, webp_q = NULL",
            (z, x, y, blob, img.width, img.height, kind, time.time()),
        )
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

    def get_webp_cached(self, z, x, y, quality):
        """Encoded bytes for this tile at this quality, or None."""
        row = self.db.execute(
            "SELECT webp FROM tiles WHERE z=? AND x=? AND y=? AND webp_q = ?",
            (z, x, y, int(quality)),
        ).fetchone()
        return None if row is None or row[0] is None else row[0]

    def set_webp_cached(self, z, x, y, blob, quality):
        self.db.execute(
            "UPDATE tiles SET webp = ?, webp_q = ? WHERE z=? AND x=? AND y=?",
            (blob, int(quality), z, x, y),
        )

    def get_meta(self, z, x, y):
        row = self.db.execute(
            "SELECT meta FROM tile_meta WHERE z=? AND x=? AND y=?", (z, x, y)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def current_seq(self):
        row = self.db.execute("SELECT MAX(seq) FROM tile_events").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def mark_archived(self, seq=None):
        """Record which changes the .pmtiles now contains, and trim the log."""
        self.set_map_meta("archive_seq", self.current_seq() if seq is None else seq)
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

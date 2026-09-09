"""Serialize a TileStore into a PMTiles v3 archive, and read tiles back out.

Uses the official `pmtiles` package (3.7.0).  Two things about its real API that
the code below depends on, both verified by reading the installed source rather
than assumed:

* `Writer.finalize(header, metadata)` fills in `min_zoom`, `max_zoom`,
  `clustered`, `internal_compression`, every offset/length and every count
  itself -- so passing those in is pointless, and passing a *wrong* min/max zoom
  would simply be overwritten.  We supply only `tile_type`, `tile_compression`,
  the bounds and the center.
* `Writer.write_tile` marks the archive non-clustered if tile ids arrive out of
  order (it still produces a valid file, since `finalize` sorts).  Writing in
  ascending Hilbert tile-id order is what earns `clustered=True`, which is what
  lets a reader range-fetch neighbours in one go.
"""
import hashlib
import io
import json
import math
import os
import time

from PIL import Image
from pmtiles.reader import Reader
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

try:                                    # as a ComfyUI custom-node package
    from . import tilestore
except ImportError:                     # imported straight off the pack dir by tools/
    import tilestore

GENERATOR = "comfyui-pmtiles-map"
# Metadata keys kept in the SQLite store but never copied into the archive.
_STORE_ONLY_KEYS = frozenset({"full_prompt"})


# --------------------------------------------------------------------- geometry

def tile_to_lonlat(z, x, y):
    """North-west corner of an XYZ tile, in degrees."""
    n = 1 << z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def bounds_of(coords):
    """lon/lat envelope of a set of XYZ tiles (deepest zoom decides precision)."""
    if not coords:
        return -180.0, -85.05112878, 180.0, 85.05112878
    z = max(c[0] for c in coords)
    at_z = [c for c in coords if c[0] == z]
    min_x = min(c[1] for c in at_z)
    max_x = max(c[1] for c in at_z)
    min_y = min(c[2] for c in at_z)
    max_y = max(c[2] for c in at_z)
    west, north = tile_to_lonlat(z, min_x, min_y)
    east, south = tile_to_lonlat(z, max_x + 1, max_y + 1)
    return west, south, east, north


# ----------------------------------------------------------------- webp encode

def encode_webp(img, quality=80, xmp=None, method=6):
    """PNG-in-store -> WebP-in-archive.  Alpha only when the tile has any."""
    if img.mode == "RGBA":
        alpha = img.getchannel("A")
        if alpha.getextrema() == (255, 255):
            img = img.convert("RGB")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    kwargs = {"quality": int(quality), "method": int(method), "alpha_quality": 100}
    if xmp:
        kwargs["xmp"] = xmp if isinstance(xmp, bytes) else xmp.encode("utf-8")
    img.save(buf, format="WEBP", **kwargs)
    return buf.getvalue()


def xmp_packet(meta):
    """Minimal XMP packet so a tile pulled out of the archive is self-describing."""
    payload = json.dumps(meta, ensure_ascii=False).replace("]]>", "]]&gt;")
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:comfy="https://comfy.org/pmtiles/">'
        f"<comfy:json><![CDATA[{payload}]]></comfy:json>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    ).encode("utf-8")


# ------------------------------------------------------------------ archive out

def build_archive(store, out_path, webp_quality=80, embed_tile_metadata=True,
                  name=None, description="", attribution="",
                  metadata_budget=8 * 1024 * 1024, log=print, progress=None):
    """Write every tile in `store` to `out_path` as WebP.  Atomic replace.

    `embed_tile_metadata` controls the archive's single metadata blob only; a
    leaf tile always carries its own record as an XMP packet, which costs a few
    hundred bytes and is what makes an extracted tile self-describing.
    """
    t0 = time.perf_counter()
    coords = store.all_coords()
    if not coords:
        raise ValueError("store holds no tiles; nothing to serialize")

    ordered = sorted(coords, key=lambda c: zxy_to_tileid(*c))
    # `full_prompt` is a whole graph per tile — tens of KB. It belongs in the
    # store only; carrying it here blew the metadata budget on a 5463-tile map
    # and silently dropped *all* per-tile metadata from the archive.
    per_tile = ({k: {kk: vv for kk, vv in v.items() if kk not in _STORE_ONLY_KEYS}
                 for k, v in store.all_meta().items()} if embed_tile_metadata else {})
    west, south, east, north = bounds_of(coords)
    min_zoom = min(c[0] for c in coords)
    max_zoom = max(c[0] for c in coords)

    metadata = {
        "name": name or os.path.splitext(os.path.basename(out_path))[0],
        "description": description,
        "attribution": attribution,
        "format": "webp",
        "type": "overlay",
        "generator": GENERATOR,
        "tile_size": store.tile_size,
        "minzoom": min_zoom,
        "maxzoom": max_zoom,
        "bounds": f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}",
    }
    if per_tile:
        blob = json.dumps(per_tile, ensure_ascii=False).encode("utf-8")
        if len(blob) > metadata_budget:
            log(f"[pmtiles] per-tile metadata is {len(blob)/1e6:.1f} MB > budget "
                f"{metadata_budget/1e6:.1f} MB -- omitting it from the archive "
                f"(it stays in {os.path.basename(store.path)})")
        else:
            metadata["tiles"] = per_tile

    tmp = out_path + ".tmp"
    total_bytes = 0
    encoded = 0
    seen = {}
    with open(tmp, "wb") as fh:
        writer = Writer(fh)
        for index, (z, x, y) in enumerate(ordered):
            if progress and (index % 256 == 0 or index + 1 == len(ordered)):
                progress(index + 1, len(ordered))
            # Encoding dominates the rebuild (7 s for 1000 tiles), and a save
            # only changes a handful of tiles, so cache the WebP in the store and
            # re-encode just what put_tile invalidated.
            data = store.get_webp_cached(z, x, y, webp_quality)
            if data is None:
                meta = store.get_meta(z, x, y)
                data = encode_webp(
                    img=store.get_image(z, x, y), quality=webp_quality,
                    xmp=xmp_packet(meta)
                    if meta and meta.get("kind") != tilestore.DERIVED else None,
                )
                store.set_webp_cached(z, x, y, data, webp_quality)
                encoded += 1
            digest = hashlib.blake2b(data, digest_size=16).digest()
            seen.setdefault(digest, 0)
            seen[digest] += 1
            total_bytes += len(data)
            writer.write_tile(zxy_to_tileid(z, x, y), data)
        header = {
            "tile_type": TileType.WEBP,
            # WebP is already compressed; gzipping it again only costs CPU.
            "tile_compression": Compression.NONE,
            "min_lon_e7": int(west * 1e7),
            "min_lat_e7": int(south * 1e7),
            "max_lon_e7": int(east * 1e7),
            "max_lat_e7": int(north * 1e7),
            "center_zoom": min_zoom,
            "center_lon_e7": int((west + east) / 2 * 1e7),
            "center_lat_e7": int((south + north) / 2 * 1e7),
        }
        writer.finalize(header, metadata)

    os.replace(tmp, out_path)
    # Only now are those tiles actually fetchable, so this is the point at which
    # the change feed may report them.
    store.mark_archived()
    store.db.commit()                   # persist the encode cache
    return {
        "path": out_path,
        "tiles": len(ordered),
        "encoded": encoded,
        "unique_tiles": len(seen),
        "webp_bytes": total_bytes,
        "file_bytes": os.path.getsize(out_path),
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "bounds": [west, south, east, north],
        "seconds": time.perf_counter() - t0,
    }


# ------------------------------------------------------------------- archive in

class _FileSource:
    """`get_bytes(offset, length)` over a plain fd.

    Deliberately not `pmtiles.reader.MmapSource`: the archive is rewritten (and
    atomically replaced) while the viewer is open, and a stale mmap would keep
    serving the old inode's pages.  os.pread on a freshly opened fd is cheap and
    the directory is cached above this layer anyway.
    """

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY)

    def __call__(self, offset, length):
        return os.pread(self.fd, length, offset)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class ArchiveHandle:
    """A Reader plus its header/metadata, invalidated when the file changes."""

    def __init__(self, path):
        st = os.stat(path)
        self.path = path
        self.stamp = (st.st_mtime_ns, st.st_size)
        self.source = _FileSource(path)
        self.reader = Reader(self.source)
        self.header = self.reader.header()
        self.metadata = self.reader.metadata()

    def stale(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return True
        return (st.st_mtime_ns, st.st_size) != self.stamp

    def get(self, z, x, y):
        if z < self.header["min_zoom"] or z > self.header["max_zoom"]:
            return None
        if min(x, y) < 0 or max(x, y) >= (1 << z):
            return None
        return self.reader.get(z, x, y)

    def close(self):
        self.source.close()


_CACHE = {}


def open_archive(path):
    """Cached handle.  Reopens transparently after a rebuild."""
    handle = _CACHE.get(path)
    if handle is not None and not handle.stale():
        return handle
    if handle is not None:
        handle.close()
        _CACHE.pop(path, None)
    handle = ArchiveHandle(path)
    _CACHE[path] = handle
    return handle


def archive_info(path):
    h = open_archive(path)
    hdr, md = h.header, h.metadata
    return {
        "name": os.path.splitext(os.path.basename(path))[0],
        "file": os.path.basename(path),
        "min_zoom": hdr["min_zoom"],
        "max_zoom": hdr["max_zoom"],
        "tiles": hdr["addressed_tiles_count"],
        "unique_tiles": hdr["tile_contents_count"],
        "clustered": hdr["clustered"],
        "tile_type": hdr["tile_type"].name,
        "tile_size": md.get("tile_size", 256),
        "bounds": [
            hdr["min_lon_e7"] / 1e7, hdr["min_lat_e7"] / 1e7,
            hdr["max_lon_e7"] / 1e7, hdr["max_lat_e7"] / 1e7,
        ],
        "center": [hdr["center_lon_e7"] / 1e7, hdr["center_lat_e7"] / 1e7,
                   hdr["center_zoom"]],
        "description": md.get("description", ""),
        "attribution": md.get("attribution", ""),
        "has_tile_metadata": "tiles" in md,
        # The store answers /tilemeta even when the archive's own blob was
        # skipped for size, so the viewer must not report "no metadata" on the
        # strength of the archive alone.
        "has_store": os.path.isfile(f"{os.path.splitext(path)[0]}.tiles.db"),
        # Tiles in the store that this archive does not contain yet, because the
        # saver is batching archive writes (see archive_every).
        "pending_tiles": int(tilestore.read_map_meta(
            f"{os.path.splitext(path)[0]}.tiles.db", "pending_tiles", "0") or 0),
        # The flag is set by a saver that skipped the pyramid, but maps written
        # before it existed have no flag -- and a map whose every tile sits on one
        # level has no pyramid whatever the flag says.
        "pyramid_stale": (
            tilestore.read_map_meta(f"{os.path.splitext(path)[0]}.tiles.db",
                                    "pyramid_stale") == "1"
            or (hdr["min_zoom"] == hdr["max_zoom"]
                and hdr["addressed_tiles_count"] > 1)),
        # What the map was last built with, so the viewer's build control shows
        # the map's own setting rather than a guess.
        "pyramid_mode": tilestore.read_map_meta(
            f"{os.path.splitext(path)[0]}.tiles.db", "pyramid_mode",
            tilestore.PYRAMID_SCALE),
        "pyramid_min_px": int(tilestore.read_map_meta(
            f"{os.path.splitext(path)[0]}.tiles.db", "pyramid_min_px",
            tilestore.MIN_CONTENT_PX) or tilestore.MIN_CONTENT_PX),
        # "NxM": tiles one render occupies. The viewer needs it to know which
        # tiles form one image, which is otherwise a request per tile.
        "render_block": tilestore.read_map_meta(
            f"{os.path.splitext(path)[0]}.tiles.db", "render_block", None),
        "bytes": os.path.getsize(path),
        "mtime": os.stat(path).st_mtime,
    }


def stitch_render(db_path, archive_path, z, x, y, tile_size=None):
    """Rebuild the original render that a tile belongs to.

    A sliced render is stored as an nx x ny block of tiles, so the original image
    is the block stitched back together. Tiles come from the **store** — PNG,
    lossless, i.e. the render's own pixels — and fall back to the archive's WebP
    (q80, so visibly softer) only when the store is absent.

    Returns (PIL image, origin (z, x, y), (nx, ny), source) or None.
    """
    meta = tilestore.read_meta(db_path, z, x, y) or {}
    grid = meta.get("grid")
    if isinstance(grid, list) and len(grid) == 4:
        ix, iy, nx, ny = (int(v) for v in grid)
    else:
        ix = iy = 0
        nx = ny = 1
    ox, oy = x - ix, y - iy

    if tile_size is None:
        stored = tilestore.read_map_meta(db_path, "tile_size")
        tile_size = int(stored) if stored else 256

    canvas = None
    source = "store"
    handle = None
    for dx in range(nx):
        for dy in range(ny):
            blob = tilestore.read_tile_png(db_path, z, ox + dx, oy + dy)
            if blob is None:
                if handle is None and os.path.isfile(archive_path):
                    handle = open_archive(archive_path)
                blob = handle.get(z, ox + dx, oy + dy) if handle else None
                if blob is None:
                    continue
                source = "archive"
            tile = Image.open(io.BytesIO(bytes(blob)))
            tile.load()
            if canvas is None:
                canvas = Image.new("RGBA", (nx * tile_size, ny * tile_size),
                                   (0, 0, 0, 0))
            canvas.paste(tile.convert("RGBA"), (dx * tile_size, dy * tile_size))
    if canvas is None:
        return None
    return canvas, (z, ox, oy), (nx, ny), source


def store_info(db_path):
    """Describe a store that has no archive yet, in the same shape as archive_info.

    A run with `write_archive` off (or a threshold not yet reached) has tiles in
    the store and no .pmtiles at all. Leaving those maps out of the listing made
    them invisible in the viewer -- and the build button with them, so the only
    way out was the CLI.
    """
    extent = tilestore.read_extent(db_path)
    if extent is None:
        return None
    west, north = tile_to_lonlat(extent["max_zoom"], extent["x0"], extent["y0"])
    east, south = tile_to_lonlat(extent["max_zoom"],
                                 extent["x1"] + 1, extent["y1"] + 1)
    name = os.path.basename(db_path)[:-len(".tiles.db")]
    return {
        "name": name,
        "file": None,
        "built": False,
        "min_zoom": extent["min_zoom"],
        "max_zoom": extent["max_zoom"],
        "tiles": extent["tiles"],
        "unique_tiles": extent["tiles"],
        "clustered": True,
        "tile_type": "WEBP",
        "tile_size": extent["tile_size"],
        "bounds": [west, south, east, north],
        "center": [(west + east) / 2, (south + north) / 2, extent["min_zoom"]],
        "description": "",
        "attribution": "",
        "has_tile_metadata": False,
        "has_store": True,
        "pending_tiles": extent["pending_tiles"],
        "pyramid_stale": extent["pyramid_stale"],
        "pyramid_mode": extent["pyramid_mode"],
        "pyramid_min_px": extent["pyramid_min_px"],
        "render_block": extent["render_block"],
        "bytes": os.path.getsize(db_path),
        "mtime": os.stat(db_path).st_mtime,
    }


def list_archives(maps_dir):
    out = []
    if not os.path.isdir(maps_dir):
        return out
    entries = sorted(os.listdir(maps_dir))
    archived = set()
    for entry in entries:
        if not entry.endswith(".pmtiles"):
            continue
        path = os.path.join(maps_dir, entry)
        archived.add(entry[:-len(".pmtiles")])
        try:
            info = archive_info(path)
            info["built"] = True
            out.append(info)
        except Exception as exc:                     # a half-written file, say
            out.append({
                "name": os.path.splitext(entry)[0],
                "file": entry,
                "error": f"{type(exc).__name__}: {exc}",
            })
    # Stores with nothing serialized yet.
    for entry in entries:
        if not entry.endswith(".tiles.db"):
            continue
        name = entry[:-len(".tiles.db")]
        if name in archived:
            continue
        try:
            info = store_info(os.path.join(maps_dir, entry))
        except Exception as exc:
            info = {"name": name, "file": entry,
                    "error": f"{type(exc).__name__}: {exc}"}
        if info:
            out.append(info)
    return sorted(out, key=lambda m: m["name"])

#!/usr/bin/env python3
"""Verify the PMTiles map pipeline end to end, without ComfyUI.

usage:
  ./venv/bin/python tools/pmtiles_map_test.py                  # synthetic, all checks
  ./venv/bin/python tools/pmtiles_map_test.py --out /tmp/x     # where to dump images
  ./venv/bin/python tools/pmtiles_map_test.py --bench 1000     # rebuild cost at N tiles

Checks, in order:
 1. store: leaves land where asked; ancestors are recomposed down to z=0
 2. header: the first 127 bytes are hand-parsed, independently of the library
    that wrote them, because a misuse of the writer is exactly what a
    round-trip through the same library would hide
 3. round trip: every tile decodes at the right size, and leaf XMP survives
 4. pyramid content: sampled pixels match the child colour that should be there
 5. holes: a partially filled parent stays transparent, not black
Writes a contact sheet per zoom level to --out so the pyramid can be *looked at*
(CLAUDE.md rule 1: never judge an image pipeline by its exit code).
"""
import argparse
import json
import os
import re
import struct
import sys
import time

# realpath, not abspath: these are symlinked from the repo's tools/ directory, and
# the pack is the parent of wherever this file really lives.
HERE = os.path.dirname(os.path.realpath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, PACK)

from PIL import Image   # noqa: E402

import archive          # noqa: E402
import paths            # noqa: E402
import hilbert          # noqa: E402
import tilestore        # noqa: E402

FAILED = []
PASSED = []


def check(label, ok, detail=""):
    (PASSED if ok else FAILED).append(label)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))
    return ok


def colour(i, n):
    """Distinct, easily eyeballed colours."""
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(i / max(n, 1), 0.85, 0.95)
    return int(r * 255), int(g * 255), int(b * 255)


def numbered_tile(ts, rgb, text):
    from PIL import ImageDraw
    img = Image.new("RGB", (ts, ts), rgb)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, ts - 1, ts - 1], outline=(0, 0, 0))
    d.text((6, 4), text, fill=(0, 0, 0))
    return img


# ---------------------------------------------------------------- header parse

def parse_header_by_hand(path):
    """Independent reading of the PMTiles v3 fixed header (spec: 127 bytes)."""
    with open(path, "rb") as fh:
        buf = fh.read(127)
    if len(buf) != 127:
        raise AssertionError("file shorter than a header")
    magic, version = buf[0:7], buf[7]
    u64 = lambda off: struct.unpack_from("<Q", buf, off)[0]      # noqa: E731
    i32 = lambda off: struct.unpack_from("<i", buf, off)[0]      # noqa: E731
    return {
        "magic": magic, "version": version,
        "root_offset": u64(8), "root_length": u64(16),
        "metadata_offset": u64(24), "metadata_length": u64(32),
        "leaf_offset": u64(40), "leaf_length": u64(48),
        "tile_data_offset": u64(56), "tile_data_length": u64(64),
        "addressed": u64(72), "entries": u64(80), "contents": u64(88),
        "clustered": buf[96], "internal_compression": buf[97],
        "tile_compression": buf[98], "tile_type": buf[99],
        "min_zoom": buf[100], "max_zoom": buf[101],
        "min_lon": i32(102) / 1e7, "min_lat": i32(106) / 1e7,
        "max_lon": i32(110) / 1e7, "max_lat": i32(114) / 1e7,
        "center_zoom": buf[118],
    }


# -------------------------------------------------------------------- fixtures

def build_synthetic(db_path, ts=256, z=4, side=4):
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    store = tilestore.TileStore(db_path, ts)
    coords, expected = [], {}
    n = side * side
    for iy in range(side):
        for ix in range(side):
            i = iy * side + ix
            rgb = colour(i, n)
            store.put_tile(z, ix, iy, numbered_tile(ts, rgb, f"{z}/{ix}/{iy}"),
                           kind=tilestore.LEAF,
                           meta={"kind": "leaf", "prompt": f"synthetic tile {i}",
                                 "seed": 1000 + i, "idx": i})
            coords.append((z, ix, iy))
            expected[(z, ix, iy)] = rgb
    # One deliberately lonely leaf, so a parent has exactly one child and must
    # come out three-quarters transparent.
    store.put_tile(z, side + 2, side + 2, numbered_tile(ts, (255, 255, 255), "lonely"),
                   kind=tilestore.LEAF, meta={"kind": "leaf", "prompt": "lonely"})
    coords.append((z, side + 2, side + 2))
    derived = store.recompose_ancestors(coords, min_zoom=0)
    store.db.commit()
    return store, coords, expected, derived


def contact_sheet(handle, z, out_path, cell=96):
    """Every tile at zoom z, laid out on a checkerboard so alpha is visible."""
    coords = []
    for x in range(min(1 << z, 64)):
        for y in range(min(1 << z, 64)):
            if handle.get(z, x, y) is not None:
                coords.append((x, y))
    if not coords:
        return None
    min_x = min(c[0] for c in coords)
    min_y = min(c[1] for c in coords)
    w = max(c[0] for c in coords) - min_x + 1
    h = max(c[1] for c in coords) - min_y + 1
    sheet = Image.new("RGB", (w * cell, h * cell))
    for by in range(h * cell // 8 + 1):            # checkerboard backdrop
        for bx in range(w * cell // 8 + 1):
            shade = 90 if (bx + by) % 2 else 130
            sheet.paste((shade, shade, shade), (bx * 8, by * 8, bx * 8 + 8, by * 8 + 8))
    for x, y in coords:
        img = Image.open(__import__("io").BytesIO(handle.get(z, x, y)))
        img.load()
        img = img.convert("RGBA").resize((cell, cell), Image.NEAREST)
        sheet.paste(img, ((x - min_x) * cell, (y - min_y) * cell), img)
    sheet.save(out_path)
    return out_path


def bench(out_dir, n_tiles, ts=256, quality=80):
    """How long does a full archive rebuild take at N tiles?"""
    z = 0
    while (1 << (2 * z)) < n_tiles:
        z += 1
    db = os.path.join(out_dir, f"bench_{n_tiles}.tiles.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    store = tilestore.TileStore(db, ts)
    coords = []
    t0 = time.perf_counter()
    for i in range(n_tiles):
        x, y = i % (1 << z), i // (1 << z)
        store.put_tile(z, x, y, numbered_tile(ts, colour(i, n_tiles), f"{i}"),
                       kind=tilestore.LEAF, meta={"kind": "leaf", "idx": i})
        coords.append((z, x, y))
    t_write = time.perf_counter() - t0
    t0 = time.perf_counter()
    derived = store.recompose_ancestors(coords, 0)
    store.db.commit()
    t_pyramid = time.perf_counter() - t0
    info = archive.build_archive(store, os.path.join(out_dir, f"bench_{n_tiles}.pmtiles"),
                                 webp_quality=quality, embed_tile_metadata=True,
                                 name=f"bench{n_tiles}")
    store.close()
    print(f"\nbench {n_tiles} leaves at z={z} ({ts}px):")
    print(f"  store writes    {t_write:6.2f} s")
    print(f"  pyramid         {t_pyramid:6.2f} s ({len(derived)} derived)")
    print(f"  archive build   {info['seconds']:6.2f} s -> "
          f"{info['tiles']} tiles, {info['file_bytes'] / 1e6:.2f} MB")
    return info


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(PACK, "output_pmtiles_test"))
    ap.add_argument("--tile-size", type=int, default=256)
    ap.add_argument("--zoom", type=int, default=4)
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--bench", type=int, nargs="*", default=None,
                    help="also measure rebuild cost at these tile counts")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    ts, z, side = args.tile_size, args.zoom, 4

    print("1. store + pyramid")
    store, coords, expected, derived = build_synthetic(
        os.path.join(args.out, "synthetic.tiles.db"), ts, z, side)
    stats = store.stats()
    check("leaves stored", stats["per_zoom"][z]["leaf"] == side * side + 1,
          json.dumps(stats["per_zoom"][z]))
    check("pyramid reaches z=0", stats["min_zoom"] == 0)
    check("every level between 0 and z is populated",
          all(zz in stats["per_zoom"] for zz in range(0, z + 1)))
    check("z=0 has exactly one tile", stats["per_zoom"][0].get("derived") == 1)
    check("derived tiles are marked derived",
          all(stats["per_zoom"][zz].get("leaf") is None for zz in range(0, z)),
          "a derived level should hold no leaves here")

    pmt = os.path.join(args.out, "synthetic.pmtiles")
    info = archive.build_archive(store, pmt, webp_quality=args.quality,
                                 embed_tile_metadata=True, name="synthetic")
    print(f"   archive: {info['tiles']} tiles, {info['file_bytes']} bytes, "
          f"{info['seconds']:.2f} s")

    print("2. header, parsed by hand")
    hdr = parse_header_by_hand(pmt)
    check("magic is PMTiles", hdr["magic"] == b"PMTiles", repr(hdr["magic"]))
    check("spec version 3", hdr["version"] == 3, hdr["version"])
    check("tile_type == 4 (WebP)", hdr["tile_type"] == 4, hdr["tile_type"])
    check("tile_compression == 1 (none)", hdr["tile_compression"] == 1,
          hdr["tile_compression"])
    check("internal_compression == 2 (gzip)", hdr["internal_compression"] == 2,
          hdr["internal_compression"])
    check("clustered", hdr["clustered"] == 1)
    check("zoom range 0..z", (hdr["min_zoom"], hdr["max_zoom"]) == (0, z),
          f"{hdr['min_zoom']}..{hdr['max_zoom']}")
    check("addressed tile count matches the store",
          hdr["addressed"] == stats["tiles"], f"{hdr['addressed']} vs {stats['tiles']}")
    check("bounds envelope is non-degenerate",
          hdr["max_lon"] > hdr["min_lon"] and hdr["max_lat"] > hdr["min_lat"],
          f"{hdr['min_lon']:.3f},{hdr['min_lat']:.3f} .. "
          f"{hdr['max_lon']:.3f},{hdr['max_lat']:.3f}")
    check("sections are contiguous and inside the file",
          hdr["root_offset"] == 127
          and hdr["metadata_offset"] == hdr["root_offset"] + hdr["root_length"]
          and hdr["tile_data_offset"] + hdr["tile_data_length"]
          == os.path.getsize(pmt))

    print("3. round trip through the archive")
    handle = archive.open_archive(pmt)
    md = handle.metadata
    check("metadata carries per-tile records",
          len(md.get("tiles", {})) == stats["tiles"], len(md.get("tiles", {})))
    check("metadata declares tile_size", md.get("tile_size") == ts, md.get("tile_size"))
    sizes_ok, xmp_ok, missing = True, 0, []
    for zz, xx, yy in store.all_coords():
        data = handle.get(zz, xx, yy)
        if data is None:
            missing.append((zz, xx, yy))
            continue
        img = Image.open(__import__("io").BytesIO(data))
        img.load()
        if img.size != (ts, ts):
            sizes_ok = False
        if "xmp" in img.info:
            payload = re.search(rb"<!\[CDATA\[(.*?)\]\]>", img.info["xmp"], re.S)
            if payload and json.loads(payload.group(1).decode("utf-8")):
                xmp_ok += 1
    check("every stored tile is retrievable", not missing, str(missing[:4]))
    check("all tiles decode at the tile size", sizes_ok)
    check("leaf XMP survives the WebP encode", xmp_ok == side * side + 1,
          f"{xmp_ok} tiles carry parseable XMP")

    print("4. pyramid content")
    import io as _io
    z0 = Image.open(_io.BytesIO(handle.get(0, 0, 0)))
    z0.load()
    z0 = z0.convert("RGBA")
    # The 4x4 block at z sits inside a (ts / 2^z)-pixel square at z=0.
    step = ts / (1 << z)
    ok_px = 0
    for (zz, xx, yy), rgb in expected.items():
        px = int((xx + 0.5) * step)
        py = int((yy + 0.5) * step)
        got = z0.getpixel((px, py))
        if got[3] > 0 and max(abs(a - b) for a, b in zip(got[:3], rgb)) < 60:
            ok_px += 1
    check("z=0 pixels carry the right child colours",
          ok_px >= len(expected) - 1, f"{ok_px}/{len(expected)} within tolerance")

    print("5. holes stay transparent")
    parent = Image.open(_io.BytesIO(handle.get(z - 1, (side + 2) // 2, (side + 2) // 2)))
    parent.load()
    parent = parent.convert("RGBA")
    alpha = parent.getchannel("A")
    transparent = sum(1 for a in alpha.getdata() if a == 0)
    check("a one-child parent is ~3/4 transparent",
          0.6 < transparent / (ts * ts) < 0.9,
          f"{transparent / (ts * ts):.2%} transparent")

    print("6. contact sheets (open these)")
    for zz in range(0, z + 1):
        path = contact_sheet(handle, zz, os.path.join(args.out, f"level_z{zz}.png"))
        if path:
            print(f"   {path}")

    print("7. hilbert index -> x/y")
    from pmtiles.tile import zxy_to_tileid
    for order in (0, 1, 2, 5):
        side = 1 << order
        seen = [hilbert.d2xy(order, d) for d in range(side * side)]
        check(f"order {order}: bijective over {side * side} cells",
              len(set(seen)) == side * side
              and all(0 <= x < side and 0 <= y < side for x, y in seen))
        steps = [abs(a[0] - b[0]) + abs(a[1] - b[1])
                 for a, b in zip(seen, seen[1:])]
        # The defining property: consecutive indices are always neighbours.
        check(f"order {order}: every step is one tile",
              all(s == 1 for s in steps) if len(steps) else True,
              f"max step {max(steps) if steps else 0}")
        check(f"order {order}: xy2d inverts d2xy",
              all(hilbert.xy2d(order, *xy) == d for d, xy in enumerate(seen)))
    order = 4
    ids = [zxy_to_tileid(order, *hilbert.d2xy(order, d))
           for d in range(1 << (2 * order))]
    check("index order matches the archive's tile-id order (so neighbours in "
          "index are contiguous in the file)", ids == sorted(ids))
    try:
        hilbert.xy2d(2, 4, 0)
        check("xy2d rejects out-of-grid coordinates", False)
    except ValueError:
        check("xy2d rejects out-of-grid coordinates", True)
    check("d2xy wraps rather than raising",
          hilbert.d2xy(2, 16) == hilbert.d2xy(2, 0))

    print("8. prompt metadata extraction")
    import promptmeta
    # A graph whose positive text arrives by wire (FormattedString, wildcards, a
    # list selector). The literal in the graph is the *negative* one, and the
    # fallback used to report it as the prompt -- plausible and entirely wrong.
    computed = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "zit/x.safetensors"}},
        "2": {"class_type": "FormattedString", "inputs": {"fstring": "{a}, best quality",
              "a": ["9", 0]}},
        "9": {"class_type": "StringOutputList", "inputs": {"values": "wolf\ngoat"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": ["2", 0], "clip": ["1", 1]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "worst quality, blurry",
              "clip": ["1", 1]}},
        "5": {"class_type": "KSampler", "inputs": {"seed": 42, "steps": 8, "cfg": 1.0,
              "model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0],
              "latent_image": ["1", 0]}},
    }
    m = promptmeta.summarize_prompt(computed)
    check("a computed positive prompt is not filled in from the negative",
          m.get("prompt") is None, repr(m.get("prompt"))[:60])
    check("the negative is still reported", m.get("negative") == "worst quality, blurry")
    check("and it says why the prompt is missing", bool(m.get("prompt_note")))

    zeroed = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat"}},
        "2": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["1", 0]}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["1", 0],
              "negative": ["2", 0]}},
    }
    m = promptmeta.summarize_prompt(zeroed)
    check("Z-Image's zeroed negative is labelled, not duplicated",
          (m.get("prompt"), m.get("negative")) == ("a cat", "(zeroed)"),
          f"{m.get('prompt')!r} / {m.get('negative')!r}")

    real = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry"}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["1", 0],
              "negative": ["2", 0]}},
    }
    m = promptmeta.summarize_prompt(real)
    check("a real negative prompt still comes through",
          (m.get("prompt"), m.get("negative")) == ("a cat", "blurry"),
          f"{m.get('prompt')!r} / {m.get('negative')!r}")

    print("9. change feed")
    feed_db = os.path.join(args.out, "feed.tiles.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(feed_db + suffix):
            os.remove(feed_db + suffix)
    feed_pmt = os.path.join(args.out, "feed.pmtiles")
    fstore = tilestore.TileStore(feed_db, ts)
    fstore.put_tile(2, 0, 0, numbered_tile(ts, (200, 40, 40), "a"),
                    meta={"kind": "leaf"})
    fstore.recompose_ancestors([(2, 0, 0)], 0)
    fstore.db.commit()
    seq0, dirty0, _ = tilestore.read_changes(feed_db, None)
    check("nothing is reported before the archive exists", seq0 == 0 and not dirty0,
          f"seq={seq0}, {len(dirty0)} changes")
    archive.build_archive(fstore, feed_pmt, embed_tile_metadata=True, name="feed")
    seq1, _, _ = tilestore.read_changes(feed_db, None)
    check("the archive build publishes a sequence", seq1 > 0, f"seq={seq1}")
    _, backlog, _ = tilestore.read_changes(feed_db, 0)
    check("a client at 0 is offered every published tile", len(backlog) == 3,
          f"{len(backlog)} changes (leaf + z1 + z0)")
    _, none_yet, _ = tilestore.read_changes(feed_db, seq1)
    check("a caught-up client is offered nothing", not none_yet)

    # A save whose archive has not been rebuilt must stay invisible: the tile is
    # in the store but not fetchable, so refreshing it would show stale bytes.
    fstore.put_tile(2, 1, 0, numbered_tile(ts, (40, 200, 40), "b"),
                    meta={"kind": "leaf"})
    fstore.recompose_ancestors([(2, 1, 0)], 0)
    fstore.db.commit()
    _, hidden, _ = tilestore.read_changes(feed_db, seq1)
    check("an unpublished write is withheld", not hidden, f"{len(hidden)} leaked")
    archive.build_archive(fstore, feed_pmt, embed_tile_metadata=True, name="feed")
    seq2, published, _ = tilestore.read_changes(feed_db, seq1)
    check("and appears once the archive is rebuilt", len(published) == 3,
          f"{len(published)} changes")
    check("the reported coords are the tiles that changed",
          sorted(published) == sorted([[0, 0, 0], [1, 0, 0], [2, 1, 0]]),
          str(sorted(published)))
    _, capped, truncated = tilestore.read_changes(feed_db, 0, limit=2)
    check("an over-long backlog is truncated, not silently clipped",
          truncated and len(capped) == 2, f"{len(capped)} changes, truncated={truncated}")
    fstore.close()

    print("10. opening a store that is not 256 px")
    # Every maintenance path (CLI, the build job, PMTilesMapInfo) opens a store
    # without stating a tile size. Defaulting to 256 there made all of them fail
    # on a 512 px map with TileSizeMismatch -- the guard is for the saver, whose
    # widget can disagree with the map, not for callers with no opinion.
    big_db = os.path.join(args.out, "size512.tiles.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(big_db + suffix):
            os.remove(big_db + suffix)
    with tilestore.TileStore(big_db, 512) as st:
        st.put_tile(2, 0, 0, numbered_tile(512, (200, 120, 40), "512"),
                    meta={"kind": "leaf"})
        st.db.commit()
    with tilestore.TileStore(big_db) as st:
        check("opening without a size adopts the store's own", st.tile_size == 512,
              f"got {st.tile_size}")
    try:
        tilestore.TileStore(big_db, 256).close()
        check("stating the wrong size still raises", False)
    except tilestore.TileSizeMismatch:
        check("stating the wrong size still raises", True)
    with tilestore.TileStore(big_db, 512) as st:
        check("stating the right size is accepted", st.tile_size == 512)
    fresh = os.path.join(args.out, "size_default.tiles.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(fresh + suffix):
            os.remove(fresh + suffix)
    with tilestore.TileStore(fresh) as st:
        check("a brand new store still defaults to 256", st.tile_size == 256,
              f"got {st.tile_size}")

    print("11. batching the archive rewrite")
    # The store costs ~40 KB per save whatever its size (SQLite writes changed
    # pages); the archive is rewritten whole. So the archive is the one to batch.
    batch_db = os.path.join(args.out, "batch.tiles.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(batch_db + suffix):
            os.remove(batch_db + suffix)
    with tilestore.TileStore(batch_db, ts) as st:
        check("a fresh store owes nothing", st.pending_tiles() == 0)
        st.put_tile(4, 0, 0, numbered_tile(ts, (80, 160, 80), ""), meta={"kind": "leaf"})
        st.bump_pending(1)
        st.bump_pending(3)
        check("pending accumulates across saves", st.pending_tiles() == 4,
              str(st.pending_tiles()))
        st.db.commit()
        info = archive.build_archive(st, os.path.join(args.out, "batch.pmtiles"),
                                     name="batch")
        check("serializing the archive clears the debt", st.pending_tiles() == 0,
              str(st.pending_tiles()))
    with tilestore.TileStore(batch_db) as st:
        st.bump_pending(7)
        st.db.commit()
    with tilestore.TileStore(batch_db) as st:
        check("the debt survives a reopen (a ComfyUI restart)",
              st.pending_tiles() == 7, str(st.pending_tiles()))
    pmt = os.path.join(args.out, "batch.pmtiles")
    check("archive_info reports the debt",
          archive.archive_info(pmt)["pending_tiles"] == 7,
          str(archive.archive_info(pmt)["pending_tiles"]))

    print("12. a store with no archive yet")
    # `write_archive` off (or a threshold not yet reached) leaves tiles in the
    # store and no .pmtiles at all. Such a map must still be listed and still be
    # buildable, or the viewer cannot reach it and only the CLI can help.
    only_db = os.path.join(args.out, "storeonly.tiles.db")
    only_pmt = os.path.join(args.out, "storeonly.pmtiles")
    for path in (only_pmt, *(only_db + s for s in ("", "-wal", "-shm"))):
        if os.path.exists(path):
            os.remove(path)
    with tilestore.TileStore(only_db, ts) as st:
        for i in range(4):
            st.put_tile(3, i, 0, numbered_tile(ts, colour(i, 4), ""),
                        meta={"kind": "leaf"})
        st.bump_pending(4)
        st.db.commit()
    listed = {m["name"]: m for m in archive.list_archives(args.out)}
    check("a store with no archive is listed", "storeonly" in listed)
    entry = listed.get("storeonly", {})
    check("and is marked unbuilt", entry.get("built") is False, str(entry.get("built")))
    check("with its zoom range and tile count from the store",
          (entry.get("min_zoom"), entry.get("max_zoom"), entry.get("tiles")) == (3, 3, 4),
          f"{entry.get('min_zoom')}-{entry.get('max_zoom')}, {entry.get('tiles')}")
    check("and its pending count", entry.get("pending_tiles") == 4,
          str(entry.get("pending_tiles")))
    check("bounds are a real envelope",
          entry.get("bounds") and entry["bounds"][2] > entry["bounds"][0])
    with tilestore.TileStore(only_db) as st:
        archive.build_archive(st, only_pmt, name="storeonly")
    after = {m["name"]: m for m in archive.list_archives(args.out)}["storeonly"]
    check("once serialized it is built and owes nothing",
          after.get("built") is True and after.get("pending_tiles") == 0,
          f"built={after.get('built')} pending={after.get('pending_tiles')}")
    check("and is listed once, not twice",
          [m["name"] for m in archive.list_archives(args.out)].count("storeonly") == 1)

    print("12. deferred pyramid")
    # Rendering with pyramid_to_zoom == z writes leaves only; one later pass must
    # produce exactly the same pyramid, for far less work.
    each_db = os.path.join(args.out, "pyr_each.tiles.db")
    defer_db = os.path.join(args.out, "pyr_defer.tiles.db")
    for db in (each_db, defer_db):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db + suffix):
                os.remove(db + suffix)
    side = 8
    counts = {}
    for db, defer in ((each_db, False), (defer_db, True)):
        st = tilestore.TileStore(db, ts)
        n = [0]
        original = st._recompose
        st._recompose = lambda z, x, y: (n.__setitem__(0, n[0] + 1), original(z, x, y))[1]
        for iy in range(side):
            for ix in range(side):
                st.put_tile(4, ix, iy, numbered_tile(ts, colour(iy * side + ix, 64), ""),
                            meta={"kind": "leaf"})
                if not defer:
                    st.recompose_ancestors([(4, ix, iy)], 0)
        if defer:
            st.rebuild_pyramid(0)
        st.db.commit()
        counts[defer] = (n[0], {(z, x, y) for z, x, y in st.all_coords()})
        st.close()
    check("deferred build produces the identical tile set",
          counts[True][1] == counts[False][1],
          f"{len(counts[True][1])} vs {len(counts[False][1])} tiles")
    check("and does far less work",
          counts[True][0] * 4 < counts[False][0],
          f"{counts[True][0]} recompositions vs {counts[False][0]}")
    with tilestore.TileStore(defer_db, ts) as st:
        check("a deferred build clears the stale flag",
              st.get_map_meta("pyramid_stale") == "0",
              str(st.get_map_meta("pyramid_stale")))

    if args.bench is not None:
        for n in (args.bench or [100, 1000]):
            bench(args.out, n, ts, args.quality)

    store.close()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

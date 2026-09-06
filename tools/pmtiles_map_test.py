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
...
20. bulk import: an odd-sized image is padded centred into its own nx x ny
    block, blocks are laid along the Hilbert curve without overlapping, and
    --resume writes nothing on a second pass
21. the bundle's own serve.py answers Range with byte-exact 206s -- the one
    thing `python -m http.server` cannot do, and without which a map is blank
Writes a contact sheet per zoom level to --out so the pyramid can be *looked at*
(CLAUDE.md rule 1: never judge an image pipeline by its exit code).
"""
import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import struct
import sys
import time

# realpath, not abspath: these are symlinked from the repo's tools/ directory, and
# the pack is the parent of wherever this file really lives.
HERE = os.path.dirname(os.path.realpath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, PACK)
sys.path.insert(0, HERE)          # the CLI tools are siblings, not pack modules

from PIL import Image   # noqa: E402

import archive          # noqa: E402
import paths            # noqa: E402
import hilbert          # noqa: E402
import imgimport        # noqa: E402
import routes            # noqa: E402
import tilestore        # noqa: E402

import pmtiles_export   # noqa: E402
import pmtiles_import   # noqa: E402

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


def odd_image(w, h, i):
    """A numbered gradient at a size that is a multiple of no tile size.

    The importer's whole reason to exist is that real images are shaped like
    this, so the fixture has to be too -- a square fixture would pass a padding
    bug straight through.
    """
    from PIL import ImageDraw
    img = Image.new("RGB", (w, h))
    base = colour(i, 8)
    for y in range(h):
        t = y / max(1, h - 1)
        img.paste(tuple(int(c * (0.35 + 0.65 * t)) for c in base), (0, y, w, y + 1))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], outline=(255, 255, 255))
    d.text((6, 4), f"image {i}  {w}x{h}", fill=(255, 255, 255))
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
    # Explicitly the classic averaging pyramid: sections 1, 4 and 5 assert the
    # four-children-in-quadrants layout, which is what `scale` means. `sample`,
    # now the default, is section 22's business.
    derived = store.recompose_ancestors(coords, min_zoom=0,
                                        mode=tilestore.PYRAMID_SCALE)
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

    print("12. live source: the store, not the archive")
    # The interactive viewer reads tiles from the store, so a run never has to
    # serialize the archive just to be watched -- and the change feed must not be
    # gated on archive_seq in that mode, or nothing would ever be announced.
    live_db = os.path.join(args.out, "live.tiles.db")
    live_pmt = os.path.join(args.out, "live.pmtiles")
    for path in (live_pmt, *(live_db + sfx for sfx in ("", "-wal", "-shm"))):
        if os.path.exists(path):
            os.remove(path)
    with tilestore.TileStore(live_db, ts) as st:
        st.put_tile(3, 0, 0, numbered_tile(ts, (200, 80, 80), "a"), meta={"kind": "leaf"})
        st.db.commit()
        archive.build_archive(st, live_pmt, name="live")     # publishes seq
        # a render that has NOT been serialized yet
        st.put_tile(3, 1, 0, numbered_tile(ts, (80, 200, 120), "b"), meta={"kind": "leaf"})
        st.db.commit()

    # Serving must not fight the archive over the encode cache. A map saved at a
    # non-default quality used to lose that fight both ways: every view
    # re-encoded the tile at 80 and overwrote the cache, then every build
    # re-encoded it back.
    q_db = os.path.join(args.out, "quality.tiles.db")
    q_pmt = os.path.join(args.out, "quality.pmtiles")
    for path in (q_pmt, *(q_db + sfx for sfx in ("", "-wal", "-shm"))):
        if os.path.exists(path):
            os.remove(path)
    with tilestore.TileStore(q_db, ts) as st:
        for i in range(4):
            st.put_tile(4, i, 0, numbered_tile(ts, colour(i, 4), ""), meta={"kind": "leaf"})
        st.set_map_meta("webp_quality", 90)
        st.db.commit()
        archive.build_archive(st, q_pmt, webp_quality=90, name="quality")
    _, again = tilestore.read_tile_for_serving(q_db, 4, 0, 0)
    check("a q90 tile is served from cache, not re-encoded at 80", again is False)
    with tilestore.TileStore(q_db) as st:
        info = archive.build_archive(st, q_pmt, webp_quality=90, name="quality")
    check("so the next build re-encodes nothing", info["encoded"] == 0,
          f"{info['encoded']} re-encoded")

    data, needs_encode = tilestore.read_tile_for_serving(live_db, 3, 1, 0)
    check("an unserialized tile is readable from the store", data is not None)
    check("and needs encoding the first time", needs_encode is True)
    blob = archive.encode_webp(tilestore.open_png(data), quality=80)
    tilestore.write_webp_cache(live_db, 3, 1, 0, blob, 80)
    again, again_encode = tilestore.read_tile_for_serving(live_db, 3, 1, 0)
    check("second read comes from the cache", again_encode is False and again == blob)
    check("that tile is absent from the archive",
          archive.open_archive(live_pmt).get(3, 1, 0) is None)

    _, gated, _ = tilestore.read_changes(live_db, 0, live=False)
    _, live_seen, _ = tilestore.read_changes(live_db, 0, live=True)
    check("the archive-gated feed withholds it", len(gated) == 1, f"{len(gated)} changes")
    check("the live feed announces it", len(live_seen) == 2, f"{len(live_seen)} changes")

    print("13. cached read connections")
    # Reads keep one connection per store, so a viewport of a hundred tiles does
    # not reopen SQLite a hundred times (0.030 ms per read against 0.139, and p95
    # 0.047 against 1.96). The hazard that buys is staleness, so the cache is
    # keyed by inode: a store deleted and recreated must not keep serving the old
    # one.
    ident_db = os.path.join(args.out, "identity.tiles.db")
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(ident_db + sfx):
            os.remove(ident_db + sfx)
    with tilestore.TileStore(ident_db, ts) as st:
        st.put_tile(3, 0, 0, numbered_tile(ts, (200, 40, 40), ""),
                    meta={"kind": "leaf", "v": "first"})
        st.db.commit()
    check("a read works", (tilestore.read_meta(ident_db, 3, 0, 0) or {}).get("v") == "first")
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(ident_db + sfx):
            os.remove(ident_db + sfx)
    with tilestore.TileStore(ident_db, ts) as st:
        st.put_tile(3, 0, 0, numbered_tile(ts, (40, 200, 40), ""),
                    meta={"kind": "leaf", "v": "second"})
        st.db.commit()
    check("a recreated store is not served from the old connection",
          (tilestore.read_meta(ident_db, 3, 0, 0) or {}).get("v") == "second",
          str((tilestore.read_meta(ident_db, 3, 0, 0) or {}).get("v")))
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(ident_db + sfx):
            os.remove(ident_db + sfx)
    check("a deleted store reads as absent", tilestore.read_meta(ident_db, 3, 0, 0) is None)
    tilestore.close_readers()

    print("14. static export index")
    import importlib.util as _il
    _spec = _il.spec_from_file_location("pmx", os.path.join(HERE, "pmtiles_export.py"))
    pmx = _il.module_from_spec(_spec)
    _spec.loader.exec_module(pmx)
    synth_db = os.path.join(args.out, "synthetic.tiles.db")
    # Expectation from the store itself, so this cannot drift with the fixture:
    # one entry per render == one per leaf that is the origin of its block.
    with tilestore.TileStore(synth_db) as st:
        origins = sum(1 for _, _, _, m in
                      st.db.execute("SELECT z, x, y, meta FROM tile_meta")
                      for meta in [json.loads(m)]
                      if meta.get("kind") == "leaf"
                      and not (isinstance(meta.get("grid"), list)
                               and (meta["grid"][0] or meta["grid"][1])))
    idx = pmx.search_index(synth_db, pmt)
    check("the sidecar has one entry per render, not per tile",
          len(idx) == origins, f"{len(idx)} entries for {origins} renders")
    check("entries carry coordinates and span",
          all({"z", "x", "y", "nx", "ny"} <= set(e) for e in idx))
    check("and the metadata a page can show",
          any(e.get("prompt") for e in idx))
    check("but never the whole graph",
          not any("full_prompt" in e for e in idx))

    print("15. a store with no archive yet")
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

    print("16. deferred pyramid")
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
        st._recompose = lambda *a, **k: (n.__setitem__(0, n[0] + 1), original(*a, **k))[1]
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

    print("17. the live path needs no archive")
    # Regression: every endpoint the live viewer calls used to start with
    # _archive_for(), so a map rendered with `write_archive` off -- the whole
    # point of the store-backed source -- answered 404 to its own change feed.
    # Only /file genuinely requires the .pmtiles.
    import asyncio
    from aiohttp import web as aioweb
    from aiohttp.test_utils import TestClient, TestServer

    live_db = os.path.join(args.out, "livewire.tiles.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(live_db + suffix):
            os.remove(live_db + suffix)
    with tilestore.TileStore(live_db, ts) as st:
        st.put_tile(3, 1, 1, numbered_tile(ts, (40, 160, 220), "L"),
                    meta={"kind": "leaf", "title": "wired", "tags": "live"})
        st.bump_pending(1)
        st.db.commit()
    check("the test map has no archive",
          not os.path.exists(os.path.join(args.out, "livewire.pmtiles")))

    async def exercise():
        app = aioweb.Application()
        app.add_routes(routes.build_routes(lambda: args.out))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for label, url, want in (
                    ("the map list", "/map/list", 200),
                    ("meta.json", "/map/livewire/meta.json", 200),
                    ("the change feed", "/map/livewire/changes?live=1", 200),
                    ("search", "/map/livewire/search?q=wired", 200),
                    ("per-tile metadata", "/map/livewire/tilemeta/3/1/1", 200),
                    ("the render preview", "/map/livewire/render/3/1/1", 200),
                    ("a tile", "/map/livewire/tiles/3/1/1.webp", 200),
                    ("a hole, as a placeholder", "/map/livewire/tiles/3/0/0.webp", 200),
                    ("but /file, which needs the archive", "/map/livewire/file", 404),
                    ("and an unknown map", "/map/nope/changes", 404)):
                res = await client.get(url)
                await res.read()
                check(f"store-only: {label} -> {want}", res.status == want,
                      f"got {res.status} from {url}")

            res = await client.get("/map/livewire/changes?live=1")
            seq = (await res.json())["seq"]
            stream = await client.get(f"/map/livewire/events?since={seq}&live=1")
            check("store-only: the event stream opens", stream.status == 200,
                  str(stream.status))
            # Write while subscribed: the frame must carry that exact tile.
            with tilestore.TileStore(live_db, ts) as st:
                st.put_tile(3, 2, 1, numbered_tile(ts, (220, 80, 40), "N"),
                            meta={"kind": "leaf"})
                st.bump_pending(1)
                st.db.commit()
            frame = None
            try:
                async with asyncio.timeout(15):
                    async for raw in stream.content:
                        if raw.startswith(b"data:"):
                            frame = json.loads(raw[5:])
                            break
            except (TimeoutError, asyncio.TimeoutError):
                pass
            stream.close()
            check("store-only: a tile written live is announced",
                  bool(frame) and [3, 2, 1] in frame.get("changes", []),
                  json.dumps(frame)[:120] if frame else "no frame in 15 s")
            check("store-only: the feed does not wait for an archive",
                  bool(frame) and frame.get("seq", 0) > seq,
                  f"seq {seq} -> {frame.get('seq') if frame else None}")
        finally:
            await client.close()

    asyncio.new_event_loop().run_until_complete(exercise())

    print("18. the encode cache does not rewrite the tile")
    # The cache used to be a column on `tiles`, beside the lossless PNG, and
    # SQLite rewrites a row's overflow pages when any column changes -- so
    # caching an 18 KB encode cost the whole 321 KB PNG, twice over WAL plus
    # checkpoint. Measured as WAL growth, which is exactly what SQLite writes.
    cache_db = os.path.join(args.out, "cache.tiles.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(cache_db + suffix):
            os.remove(cache_db + suffix)
    rnd = random.Random(11)
    noisy = Image.new("RGB", (ts, ts))
    noisy.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                   for _ in range(ts * ts)])
    blob = b"webp" + bytes(rnd.randrange(256) for _ in range(18 * 1024))
    n = 24
    with tilestore.TileStore(cache_db, ts) as st:
        for i in range(n):
            st.put_tile(3, i % 8, i // 8, noisy, meta={"kind": "leaf"})
        st.db.commit()
        st.db.execute("PRAGMA wal_autocheckpoint=0")
        st.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        png_bytes_each = len(st.get_png(3, 0, 0))
        base = os.path.getsize(cache_db + "-wal")
        for i in range(n):
            st.set_webp_cached(3, i % 8, i // 8, blob, 80)
            st.db.commit()
        grew = os.path.getsize(cache_db + "-wal") - base
        check("caching an encode costs the encode, not the tile",
              grew < 3 * n * len(blob),
              f"{grew/1e6:.1f} MB for {n} x {len(blob)/1024:.0f} KB "
              f"(PNG in the row is {png_bytes_each/1024:.0f} KB; "
              f"{grew/n/1024:.0f} KB per tile)")
        check("and it reads back", st.get_webp_cached(3, 0, 0, 80) == blob)
        check("at that quality only", st.get_webp_cached(3, 0, 0, 60) is None)
        st.put_tile(3, 0, 0, noisy, meta={"kind": "leaf"})
        check("new pixels invalidate it",
              st.get_webp_cached(3, 0, 0, 80) is None)
        st.db.commit()

    served, needs = tilestore.read_tile_for_serving(cache_db, 3, 1, 0)
    check("serving uses the cache", served == blob and needs is False, str(needs))
    tilestore.close_readers()

    # A map last written before the move has its encodes in the old column and
    # no tile_webp at all. Serving opens the store read-only, so it cannot
    # create one -- it has to fall back rather than report a hole.
    legacy_db = os.path.join(args.out, "legacy.tiles.db")
    shutil.copyfile(cache_db, legacy_db)
    legacy = sqlite3.connect(legacy_db)
    legacy.execute("UPDATE tiles SET webp = ?, webp_q = 80 WHERE z=3 AND x=2 AND y=0",
                   (blob,))
    legacy.execute("DROP TABLE tile_webp")
    legacy.commit()
    legacy.close()
    served, needs = tilestore.read_tile_for_serving(legacy_db, 3, 2, 0)
    check("a pre-move store still serves its cache",
          served == blob and needs is False, str(needs))
    served, needs = tilestore.read_tile_for_serving(legacy_db, 3, 3, 0)
    check("and an uncached tile there still asks for an encode",
          served is not None and needs is True, str(needs))
    with tilestore.TileStore(legacy_db) as st:
        check("reopening it recreates the table and keeps the old cache",
              st.get_webp_cached(3, 2, 0, 80) == blob)
    tilestore.close_readers()

    # Serving is what touches an upgraded map first, and it must be able to
    # *fill* the cache, not just read it -- swallowing "no such table" there
    # left every view re-encoding a tile it had already encoded.
    fresh = os.path.join(args.out, "legacy2.tiles.db")
    shutil.copyfile(cache_db, fresh)
    con = sqlite3.connect(fresh)
    con.execute("DROP TABLE tile_webp")
    con.commit()
    con.close()
    served, needs = tilestore.read_tile_for_serving(fresh, 3, 4, 0)
    check("a pre-move store reports its uncached tile", needs is True, str(needs))
    tilestore.write_webp_cache(fresh, 3, 4, 0, blob, 80)
    tilestore.close_readers()
    served, needs = tilestore.read_tile_for_serving(fresh, 3, 4, 0)
    check("and serving can create the cache table to fill it",
          served == blob and needs is False, str(needs))
    tilestore.close_readers()

    print("19. store format is a choice, and lossless webp is a free one")
    import numpy as np
    fmt_src = Image.new("RGB", (ts, ts))
    rnd = random.Random(5)
    fmt_src.putdata([colour(rnd.randrange(64), 64) for _ in range(ts * ts)])
    fmt_src = fmt_src.filter(__import__("PIL.ImageFilter", fromlist=["x"]).BoxBlur(2))
    ref = np.asarray(fmt_src, dtype=np.int16)

    sizes, pyramids = {}, {}
    for fmt, q in ((tilestore.PNG, 92), (tilestore.WEBP_LOSSLESS, 92),
                   (tilestore.WEBP_LOSSY, 92)):
        db = os.path.join(args.out, f"fmt_{fmt}.tiles.db")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db + suffix):
                os.remove(db + suffix)
        with tilestore.TileStore(db, ts, store_format=fmt, store_quality=q) as st:
            coords = []
            for iy in range(2):
                for ix in range(2):
                    st.put_tile(1, ix, iy, fmt_src, meta={"kind": "leaf"})
                    coords.append((1, ix, iy))
            st.recompose_ancestors(coords, 0)
            st.db.commit()
            sizes[fmt] = len(st.get_png(1, 0, 0))
            pyramids[fmt] = np.asarray(st.get_image(0, 0, 0).convert("RGB"),
                                       dtype=np.int16)
            check(f"{fmt}: a tile decodes back at the right size",
                  st.get_image(1, 0, 0).size == (ts, ts))
        served, needs = tilestore.read_tile_for_serving(db, 1, 0, 0)
        want_encode = fmt == tilestore.PNG
        check(f"{fmt}: serving {'needs' if want_encode else 'skips'} an encode",
              needs is want_encode, str(needs))
        with tilestore.TileStore(db) as st:      # no format stated
            check(f"{fmt}: reopening adopts it", st.store_format == fmt,
                  st.store_format)
        tilestore.close_readers()

    lossless_px = np.asarray(
        tilestore.open_png(tilestore.encode_tile(fmt_src, tilestore.WEBP_LOSSLESS)
                           ).convert("RGB"), dtype=np.int16)
    check("webp_lossless really is lossless",
          int(np.abs(lossless_px - ref).max()) == 0,
          f"max channel delta {int(np.abs(lossless_px - ref).max())}")
    check("and it is smaller than PNG",
          sizes[tilestore.WEBP_LOSSLESS] < sizes[tilestore.PNG],
          f"{sizes[tilestore.WEBP_LOSSLESS]/1024:.0f} KB vs "
          f"{sizes[tilestore.PNG]/1024:.0f} KB")
    check("so the derived pyramid is bit-identical too",
          int(np.abs(pyramids[tilestore.WEBP_LOSSLESS]
                     - pyramids[tilestore.PNG]).max()) == 0)
    check("webp_lossy is much smaller and does lose",
          sizes[tilestore.WEBP_LOSSY] < sizes[tilestore.WEBP_LOSSLESS] / 2
          and int(np.abs(pyramids[tilestore.WEBP_LOSSY]
                         - pyramids[tilestore.PNG]).max()) > 0,
          f"{sizes[tilestore.WEBP_LOSSY]/1024:.0f} KB")

    # A parent is recomposed from its children, never from itself, so saving the
    # same tile again must not add another generation of loss.
    again = os.path.join(args.out, "fmt_resave.tiles.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(again + suffix):
            os.remove(again + suffix)
    with tilestore.TileStore(again, ts, store_format=tilestore.WEBP_LOSSY,
                             store_quality=92) as st:
        for _ in range(5):
            coords = []
            for iy in range(2):
                for ix in range(2):
                    st.put_tile(1, ix, iy, fmt_src, meta={"kind": "leaf"})
                    coords.append((1, ix, iy))
            st.recompose_ancestors(coords, 0)
        st.db.commit()
        five = np.asarray(st.get_image(0, 0, 0).convert("RGB"), dtype=np.int16)
    check("re-saving a lossy tile adds no further loss",
          int(np.abs(five - pyramids[tilestore.WEBP_LOSSY]).max()) == 0,
          f"max delta {int(np.abs(five - pyramids[tilestore.WEBP_LOSSY]).max())}")

    mixed = os.path.join(args.out, "fmt_mixed.tiles.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(mixed + suffix):
            os.remove(mixed + suffix)
    with tilestore.TileStore(mixed, ts, store_format=tilestore.PNG) as st:
        st.put_tile(1, 0, 0, fmt_src, meta={"kind": "leaf"})
        st.db.commit()
    with tilestore.TileStore(mixed, ts, store_format=tilestore.WEBP_LOSSLESS) as st:
        st.put_tile(1, 1, 0, fmt_src, meta={"kind": "leaf"})
        st.db.commit()
        check("formats may be mixed in one map -- the decoder sniffs",
              st.get_image(1, 0, 0).size == (ts, ts)
              and st.get_image(1, 1, 0).size == (ts, ts))
    try:
        tilestore.TileStore(mixed, ts, store_format="jpeg2000").close()
        check("an unknown format is refused", False, "no error raised")
    except ValueError:
        check("an unknown format is refused", True)

    # ------------------------------------------------------------------ 20
    print("20. bulk import: odd-sized images padded into Hilbert blocks")

    ITS = 128                       # small tiles: this section is geometry, not bytes
    src_dir = os.path.join(args.out, "import_src")
    shutil.rmtree(src_dir, ignore_errors=True)
    os.makedirs(os.path.join(src_dir, "cat"))
    N_IMPORT = 20
    for i in range(N_IMPORT):
        odd_image(300, 100, i).save(
            os.path.join(src_dir, "cat", f"image {i}.png"))

    check("block_tiles rounds up", imgimport.block_tiles(960, 1408, 512) == (2, 3),
          str(imgimport.block_tiles(960, 1408, 512)))
    check("an exact multiple is not rounded further",
          imgimport.block_tiles(1024, 1536, 512) == (2, 3))
    check("one pixel over costs a whole tile",
          imgimport.block_tiles(1025, 1536, 512) == (3, 3))
    check("and the same image is 4x6 at 256",
          imgimport.block_tiles(960, 1408, 256) == (4, 6))

    odd = odd_image(300, 100, 3)
    tiles, off, resized = imgimport.cut_padded(odd, ITS, 3, 1)
    check("padding centres the image", off == ((3 * ITS - 300) // 2, (ITS - 100) // 2),
          str(off))
    check("nothing was resampled", not resized)
    check("the block is exactly nx*ny tiles of tile_size",
          len(tiles) == 3 and all(t.size == (ITS, ITS) for _, t in tiles))
    canvas = Image.new("RGBA", (3 * ITS, ITS), (0, 0, 0, 0))
    for (ix, iy), tile in tiles:
        canvas.paste(tile, (ix * ITS, iy * ITS))
    check("the padding is transparent, not black",
          canvas.getpixel((0, 0))[3] == 0 and canvas.getpixel((3 * ITS - 1, 0))[3] == 0)
    check("and reassembling the tiles is bit-exact",
          canvas.crop((off[0], off[1], off[0] + 300, off[1] + 100)).tobytes()
          == odd.convert("RGBA").tobytes())

    # An image bigger than its block is contained, not cropped and not squashed.
    big = odd_image(900, 300, 0)
    _, _, was_resized = imgimport.cut_padded(big, ITS, 3, 1)
    check("an oversized image is contain-fit rather than refused", was_resized)

    plan = imgimport.plan(N_IMPORT, 300, 100, ITS)
    order, pz = plan["block_order"], plan["z"]
    check("block_order_for(3249) is 6", imgimport.block_order_for(3249) == 6)
    check("a 2x3 block at order 6 needs z=8", imgimport.zoom_for(6, 2, 3) == 8)
    origins = [imgimport.block_origin(order, i, plan["nx"], plan["ny"])
               for i in range(N_IMPORT)]
    cells = {(x + ix, y + iy) for x, y in origins
             for iy in range(plan["ny"]) for ix in range(plan["nx"])}
    check("blocks never overlap", len(cells) == N_IMPORT * plan["nx"] * plan["ny"],
          f"{len(cells)} of {N_IMPORT * plan['nx'] * plan['ny']}")
    check("every tile is inside the zoom's extent",
          all(max(x, y) < (1 << pz) for x, y in cells))
    steps = [(abs(origins[i + 1][0] // plan["nx"] - origins[i][0] // plan["nx"])
              + abs(origins[i + 1][1] // plan["ny"] - origins[i][1] // plan["ny"]))
             for i in range(N_IMPORT - 1)]
    check("consecutive indices are adjacent blocks", set(steps) == {1}, str(sorted(set(steps))))
    try:
        imgimport.block_origin(order, 1 << (2 * order), plan["nx"], plan["ny"])
        check("an index past the curve is refused, not wrapped", False, "no error")
    except ValueError:
        check("an index past the curve is refused, not wrapped", True)

    # put_tile_encoded has to be put_tile, or the importer writes a different map
    # than the node does.
    enc_a = os.path.join(args.out, "enc_a.tiles.db")
    enc_b = os.path.join(args.out, "enc_b.tiles.db")
    for path in (enc_a, enc_b):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
    sample = numbered_tile(ITS, (10, 120, 200), "enc")
    with tilestore.TileStore(enc_a, ITS) as sa, tilestore.TileStore(enc_b, ITS) as sb:
        sa.put_tile(0, 0, 0, sample, meta={"kind": "leaf", "n": 1})
        sb.put_tile_encoded(
            0, 0, 0, tilestore.encode_tile(sample, sb.store_format, sb.store_quality),
            sample.width, sample.height, meta={"kind": "leaf", "n": 1})
        sa.db.commit(); sb.db.commit()
        check("put_tile_encoded stores what put_tile stores",
              sa.get_png(0, 0, 0) == sb.get_png(0, 0, 0)
              and sa.get_meta(0, 0, 0) == sb.get_meta(0, 0, 0))

    imp_db = os.path.join(args.out, "import.tiles.db")
    imp_pmt = os.path.join(args.out, "import.pmtiles")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(imp_db + suffix):
            os.remove(imp_db + suffix)
    rc = pmtiles_import.main([
        src_dir, "--map-name", "import", "--maps-dir", args.out,
        "--tile-size", str(ITS), "--jobs", "2", "--store-format", "png",
        "--webp-quality", str(args.quality), "--quiet",
    ])
    check("the importer runs to completion", rc == 0, f"rc={rc}")

    with tilestore.TileStore(imp_db) as st:
        stats = st.stats()
        leaves = stats["per_zoom"].get(stats["max_zoom"], {}).get("leaf", 0)
        check("every image became a full block of leaves",
              leaves == N_IMPORT * plan["nx"] * plan["ny"], str(leaves))
        check("the pyramid reaches z=0", stats["min_zoom"] == 0, str(stats["min_zoom"]))
        check("and every level between is populated",
              all(zz in stats["per_zoom"]
                  for zz in range(stats["min_zoom"], stats["max_zoom"] + 1)))
        origin_rows = st.db.execute(
            "SELECT count(*) FROM tile_meta "
            "WHERE json_extract(meta,'$.grid[0]') = 0 "
            "AND json_extract(meta,'$.grid[1]') = 0").fetchone()[0]
        check("grid is per image, so search sees one row per render",
              origin_rows == N_IMPORT, str(origin_rows))
        before_seq = st.current_seq()
        before_tiles = stats["tiles"]

    hits = tilestore.search(imp_db, "image 13")["results"]
    check("search finds an imported title", len(hits) == 1, f"{len(hits)} hits")
    if hits:
        check("and reports the render's own footprint",
              (hits[0]["nx"], hits[0]["ny"]) == (plan["nx"], plan["ny"]))
        stitched = archive.stitch_render(
            imp_db, imp_pmt, hits[0]["z"], hits[0]["x"] + plan["nx"] - 1, hits[0]["y"])
        check("a non-origin tile stitches back to the whole render",
              stitched is not None
              and stitched[0].size == (plan["nx"] * ITS, plan["ny"] * ITS)
              and stitched[1] == (hits[0]["z"], hits[0]["x"], hits[0]["y"]),
              "none" if stitched is None else f"{stitched[0].size} from {stitched[1]}")

    rc = pmtiles_import.main([
        src_dir, "--map-name", "import", "--maps-dir", args.out,
        "--tile-size", str(ITS), "--jobs", "2", "--store-format", "png",
        "--webp-quality", str(args.quality), "--no-archive", "--quiet",
    ])
    with tilestore.TileStore(imp_db) as st:
        # current_seq, not the tile count: an idempotent rewrite would leave the
        # count alone while still doing all the work.
        check("--resume writes nothing the second time",
              rc == 0 and st.current_seq() == before_seq
              and st.stats()["tiles"] == before_tiles,
              f"seq {before_seq} -> {st.current_seq()}")
        cached = st.db.execute(
            "SELECT count(*) FROM tiles t LEFT JOIN tile_webp c "
            "ON c.z=t.z AND c.x=t.x AND c.y=t.y AND c.q=? "
            "WHERE c.blob IS NULL", (int(args.quality),)).fetchone()[0]
        check("the prewarm left no tile for build_archive to encode", cached == 0,
              f"{cached} uncached")

    # An image's Hilbert index is its position in the manifest, so dropping one
    # would relocate every image after it on top of the tiles already written.
    os.remove(os.path.join(src_dir, "cat", "image 0.png"))
    try:
        pmtiles_import.main([src_dir, "--map-name", "import", "--maps-dir", args.out,
                             "--tile-size", str(ITS), "--jobs", "2",
                             "--no-archive", "--quiet"])
        check("a changed manifest is refused, not silently relocated", False, "no exit")
    except SystemExit as exc:
        check("a changed manifest is refused, not silently relocated",
              exc.code not in (0, None), f"exit {exc.code}")

    # ------------------------------------------------------------------ 21
    print("21. the bundled server answers Range, which is the whole job")

    import http.server
    import threading
    import urllib.error
    import urllib.request

    serve_mod = pmtiles_export.bundle_server()
    check("parse_range: a plain range", serve_mod.parse_range("bytes=0-99", 500)
          == (0, 99))
    check("parse_range: open-ended", serve_mod.parse_range("bytes=400-", 500)
          == (400, 499))
    check("parse_range: a suffix", serve_mod.parse_range("bytes=-100", 500)
          == (400, 499))
    check("parse_range: past the end is clamped",
          serve_mod.parse_range("bytes=100-99999", 500) == (100, 499))
    check("parse_range: junk is ignored, not an error",
          serve_mod.parse_range("bytes=abc", 500) is None
          and serve_mod.parse_range("bytes=0-9,20-29", 500) is None)
    check("parse_range: wholly past the end is unsatisfiable",
          serve_mod.parse_range("bytes=600-700", 500) == "unsatisfiable")

    srv_dir = os.path.join(args.out, "serve_fixture")
    os.makedirs(srv_dir, exist_ok=True)
    payload = bytes(random.Random(11).randrange(256) for _ in range(200_000))
    with open(os.path.join(srv_dir, "fixture.pmtiles"), "wb") as fh:
        fh.write(payload)
    with open(os.path.join(srv_dir, "index.html"), "w") as fh:
        fh.write("<h1>ok</h1>")

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        lambda *a, **k: serve_mod.RangeHandler(*a, directory=srv_dir, **k))
    serve_mod.QUIET = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def fetch(path, rng=None):
        req = urllib.request.Request(base + path)
        if rng:
            req.add_header("Range", rng)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    try:
        status, headers, body = fetch("/fixture.pmtiles", "bytes=1000-1999")
        check("a range comes back as 206 with the exact bytes",
              status == 206 and body == payload[1000:2000]
              and headers.get("Content-Range") == f"bytes 1000-1999/{len(payload)}",
              f"{status} {headers.get('Content-Range')} {len(body)} bytes")
        status, headers, body = fetch("/fixture.pmtiles", "bytes=-64")
        check("a suffix range reads from the end",
              status == 206 and body == payload[-64:])
        status, headers, _ = fetch("/fixture.pmtiles", "bytes=999999-")
        check("a range past the end is 416, not a truncated 206",
              status == 416 and headers.get("Content-Range") == f"bytes */{len(payload)}",
              f"{status} {headers.get('Content-Range')}")
        status, _, body = fetch("/fixture.pmtiles", "bytes=nonsense")
        check("a malformed range serves the whole file, per RFC 9110",
              status == 200 and len(body) == len(payload), f"{status} {len(body)}")
        status, headers, body = fetch("/fixture.pmtiles")
        check("no range at all still serves the file",
              status == 200 and body == payload)
        check("and Accept-Ranges advertises the support",
              headers.get("Accept-Ranges") == "bytes")
        # The failure this file exists to prevent: an archive reassembled from
        # many small reads has to be identical, or pmtiles.js reads garbage
        # offsets and the map draws nothing.
        step = 4096
        rebuilt = b"".join(
            fetch("/fixture.pmtiles", f"bytes={off}-{off + step - 1}")[2]
            for off in range(0, len(payload), step))
        check("49 sequential ranges reassemble the file exactly",
              rebuilt == payload, f"{len(rebuilt)} of {len(payload)}")
        status, _, body = fetch("/")
        check("and an ordinary page is still served", status == 200 and b"ok" in body)
    finally:
        httpd.shutdown()
        httpd.server_close()

    # ------------------------------------------------------------------ 22
    print("22. pyramid: sample instead of scale, once the content is unreadable")

    PTS = 64                       # so the floor lands at a level we can reach
    pyr = os.path.join(args.out, "pyramid_mode.tiles.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(pyr + suffix):
            os.remove(pyr + suffix)
    LEAFZ = 4
    with tilestore.TileStore(pyr, PTS) as st:
        side = 1 << LEAFZ
        for iy in range(side):
            for ix in range(side):
                st.put_tile(LEAFZ, ix, iy,
                            numbered_tile(PTS, colour(iy * side + ix, side * side),
                                          f"{ix},{iy}"),
                            kind=tilestore.LEAF, meta={"kind": "leaf"})
        st.db.commit()

        # One leaf tile's content is PTS >> (LEAFZ - z) px at level z: 64 at z4,
        # 32 at z3, 16 at z2... so with a floor of 32 only z3 averages.
        check("the floor decides which levels average",
              [st._scales_at(z, LEAFZ, tilestore.PYRAMID_SAMPLE, 32)
               for z in (3, 2, 1, 0)] == [True, False, False, False],
              str([st._scales_at(z, LEAFZ, tilestore.PYRAMID_SAMPLE, 32)
                   for z in (3, 2, 1, 0)]))
        check("scale mode always averages",
              all(st._scales_at(z, LEAFZ, tilestore.PYRAMID_SCALE, 32)
                  for z in (3, 2, 1, 0)))

        st.rebuild_pyramid(mode=tilestore.PYRAMID_SAMPLE, min_px=32)
        st.db.commit()
        # z2 is assembled from a quarter of each z3 child, cropped at 1:1.
        parent = st.get_image(2, 0, 0).convert("RGBA")
        meta = st.get_meta(2, 0, 0)
        check("a sampled tile says so, and counts all four children",
              meta.get("sampled") is True and meta.get("children") == 4, str(meta))
        half = PTS // 2
        exact = 0
        for dx in (0, 1):
            for dy in (0, 1):
                child = st.get_image(3, dx, dy).convert("RGBA")
                box = (dx * half, dy * half, dx * half + half, dy * half + half)
                got = parent.crop(box)
                if got.tobytes() == child.crop(box).tobytes():
                    exact += 1
        check("each quadrant is that child's own quadrant, pixel for pixel",
              exact == 4, f"{exact} of 4 -- nothing was resampled")
        sampled_z0 = st.get_png(0, 0, 0)

        st.rebuild_pyramid(mode=tilestore.PYRAMID_SCALE, min_px=32)
        st.db.commit()
        check("scale mode gives a different z=0 tile", st.get_png(0, 0, 0) != sampled_z0)
        check("and averages four children there",
              (st.get_meta(0, 0, 0) or {}).get("children") == 4,
              str((st.get_meta(0, 0, 0) or {}).get("children")))
        check("both modes still produce the same tile SET",
              st.stats()["tiles"] > 0 and st.stats()["min_zoom"] == 0)

        # The settings stick to the map, so a later rebuild agrees without being told.
        check("the mode is remembered", st.get_map_meta("pyramid_mode") == "scale"
              and st.get_map_meta("pyramid_min_px") == "32",
              f"{st.get_map_meta('pyramid_mode')}/{st.get_map_meta('pyramid_min_px')}")
        st.rebuild_pyramid()
        st.db.commit()
        check("and a rebuild with no arguments reuses it",
              (st.get_meta(0, 0, 0) or {}).get("children") == 4)
        try:
            st.pyramid_settings(mode="nonsense")
            check("an unknown mode is refused", False, "no error")
        except ValueError:
            check("an unknown mode is refused", True)

    # ------------------------------------------------------------------ 23
    print("23. Hilbert blocks with a different step per axis")

    # 2x3-tile renders on a z=8 level: 128 fit across but only 85 down, so the
    # square block grid is 64 -- which is what the importer independently picks
    # for 3249 renders.
    check("the tighter axis decides the block grid",
          imgimport.block_grid_order(8, 2, 3) == 6,
          str(imgimport.block_grid_order(8, 2, 3)))
    check("a square block is unchanged by the addition",
          [imgimport.block_grid_order(5, n, n) for n in (1, 2, 4)] == [5, 4, 3],
          str([imgimport.block_grid_order(5, n, n) for n in (1, 2, 4)]))
    check("a block larger than the level still yields a 1x1 grid",
          imgimport.block_grid_order(2, 8, 8) == 0)

    ORDER, BW, BH = 8, 2, 3
    border = imgimport.block_grid_order(ORDER, BW, BH)
    origins = [imgimport.block_origin(border, i, BW, BH) for i in range(1 << (2 * border))]
    check("every block is distinct", len(set(origins)) == len(origins),
          f"{len(set(origins))} of {len(origins)}")
    check("and the steps really differ per axis",
          sorted({x for x, _ in origins})[:3] == [0, 2, 4]
          and sorted({y for _, y in origins})[:3] == [0, 3, 6],
          f"x {sorted({x for x, _ in origins})[:3]} y {sorted({y for _, y in origins})[:3]}")
    check("nothing lands outside the level",
          max(x for x, _ in origins) + BW <= (1 << ORDER)
          and max(y for _, y in origins) + BH <= (1 << ORDER),
          f"spans {max(x for x, _ in origins) + BW}x{max(y for _, y in origins) + BH} "
          f"of {1 << ORDER}")
    steps = [(abs(origins[i + 1][0] // BW - origins[i][0] // BW)
              + abs(origins[i + 1][1] // BH - origins[i][1] // BH))
             for i in range(len(origins) - 1)]
    check("consecutive indices are still adjacent blocks", set(steps) == {1},
          str(sorted(set(steps))))

    # ------------------------------------------------------------------ 24
    print("24. the pack loads the way ComfyUI loads it")

    # Every other check here imports the modules FLAT, with the pack directory on
    # sys.path -- which is how tools/ use them and is exactly the mode in which a
    # bare `import hilbert` inside a pack module works fine. ComfyUI imports the
    # directory as a *package*, where the same line raises and takes the whole
    # pack down: no nodes, no /map/. That is a one-line mistake with a total
    # failure mode and nothing was catching it.
    import importlib.util
    import types

    saved_fp = sys.modules.get("folder_paths")
    stub = types.ModuleType("folder_paths")
    stub.get_output_directory = lambda: args.out
    sys.modules["folder_paths"] = stub
    try:
        spec = importlib.util.spec_from_file_location(
            "pmtiles_pack_undertest", os.path.join(PACK, "__init__.py"),
            submodule_search_locations=[PACK])
        pack = importlib.util.module_from_spec(spec)
        sys.modules["pmtiles_pack_undertest"] = pack
        error = None
        try:
            spec.loader.exec_module(pack)
        except Exception as exc:                      # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        check("the pack imports as a package, not just off sys.path",
              error is None, error or "")
        check("and registers its nodes",
              sorted(getattr(pack, "NODE_CLASS_MAPPINGS", {})) ==
              ["HilbertXY", "PMTilesMapInfo", "SavePMTilesMap"],
              str(sorted(getattr(pack, "NODE_CLASS_MAPPINGS", {}))))
        if error is None:
            node = pack.NODE_CLASS_MAPPINGS["HilbertXY"]()
            check("HilbertXY still steps square by default",
                  [node.convert(i, 5, 4)[:2] for i in range(4)]
                  == [(0, 0), (0, 4), (4, 4), (4, 0)],
                  str([node.convert(i, 5, 4)[:2] for i in range(4)]))
            check("and rectangular when asked",
                  [node.convert(i, 8, 2, 3)[:2] for i in range(3)]
                  == [(0, 0), (2, 0), (2, 3)],
                  str([node.convert(i, 8, 2, 3)[:2] for i in range(3)]))
    finally:
        sys.modules.pop("pmtiles_pack_undertest", None)
        if saved_fp is None:
            sys.modules.pop("folder_paths", None)
        else:
            sys.modules["folder_paths"] = saved_fp

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

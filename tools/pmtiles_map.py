#!/usr/bin/env python3
"""Inspect and maintain a PMTiles results map from the shell.

usage:
  ./venv/bin/python tools/pmtiles_map.py --list
  ./venv/bin/python tools/pmtiles_map.py --info results
  ./venv/bin/python tools/pmtiles_map.py --rebuild results [--quality 80]
  ./venv/bin/python tools/pmtiles_map.py --rebuild-pyramid results
  ./venv/bin/python tools/pmtiles_map.py --dump results 4 0 0 out.webp

The SQLite store (output/maps/<name>.tiles.db) is the source of truth; the
.pmtiles archive is a build product.  --rebuild re-serializes it, which is what
you want after saving with write_archive off, or after editing the store.
"""
import argparse
import json
import os
import sys

# realpath, not abspath: these are symlinked from the repo's tools/ directory, and
# the pack is the parent of wherever this file really lives.
HERE = os.path.dirname(os.path.realpath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, PACK)

import archive          # noqa: E402
import paths            # noqa: E402
import promptmeta       # noqa: E402
import tilestore        # noqa: E402

# Written by the saver, not derived from the graph — must survive a re-derive.
_NODE_KEYS = ("kind", "batch_index", "source_size", "placement", "grid",
              "full_prompt", "time", "title", "tags")

MAPS = paths.default_maps_dir()


def map_files(name, maps_dir):
    """(store, archive) paths for a map name."""
    return (os.path.join(maps_dir, f"{name}.tiles.db"),
            os.path.join(maps_dir, f"{name}.pmtiles"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--maps-dir", default=MAPS)
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--no-tile-metadata", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--info", metavar="MAP")
    ap.add_argument("--rebuild", metavar="MAP")
    ap.add_argument("--rebuild-pyramid", metavar="MAP")
    ap.add_argument("--pyramid-mode", choices=list(tilestore.PYRAMID_MODES),
                    default=None,
                    help="scale: average four children into one tile. sample "
                         "(the default): do that only while the content stays "
                         "above --pyramid-min-px, then keep one child whole. "
                         "Remembered on the map once given.")
    ap.add_argument("--pyramid-min-px", type=int, default=None,
                    help=f"floor for `sample` (default {tilestore.MIN_CONTENT_PX})")
    ap.add_argument("--dump", nargs=5, metavar=("MAP", "Z", "X", "Y", "OUT"))
    ap.add_argument("--refresh-meta", metavar="MAP",
                    help="re-derive each tile's metadata from its stored "
                         "full_prompt (needs store_full_prompt to have been on)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --refresh-meta: report what would change only")
    args = ap.parse_args()

    if args.list:
        for info in archive.list_archives(args.maps_dir):
            print(json.dumps(info, indent=2))
        stores = [f for f in sorted(os.listdir(args.maps_dir))
                  if f.endswith(".tiles.db")] if os.path.isdir(args.maps_dir) else []
        print(f"stores: {', '.join(stores) or 'none'}")
        return

    if args.info:
        db, pmt = map_files(args.info, args.maps_dir)
        out = {}
        if os.path.exists(db):
            with tilestore.TileStore(db) as store:
                out["store"] = store.stats()
        if os.path.exists(pmt):
            out["archive"] = archive.archive_info(pmt)
        print(json.dumps(out, indent=2))
        return

    if args.rebuild_pyramid:
        db, _ = map_files(args.rebuild_pyramid, args.maps_dir)
        with tilestore.TileStore(db) as store:
            written = store.rebuild_pyramid(mode=args.pyramid_mode,
                                            min_px=args.pyramid_min_px)
            store.db.commit()
        print(f"recomposed {len(written)} derived tiles")
        return

    if args.refresh_meta:
        db, _ = map_files(args.refresh_meta, args.maps_dir)
        if not os.path.exists(db):
            sys.exit(f"no store at {db}")
        changed = skipped = no_prompt = 0
        with tilestore.TileStore(db) as store:
            rows = store.db.execute(
                "SELECT z, x, y, meta FROM tile_meta ORDER BY z, x, y").fetchall()
            for z, x, y, blob in rows:
                old = json.loads(blob)
                graph = old.get("full_prompt")
                if not isinstance(graph, dict):
                    no_prompt += 1
                    continue
                new = promptmeta.summarize_prompt(
                    graph, extra={k: old[k] for k in ("title", "tags")
                                  if old.get(k)})
                for key in _NODE_KEYS:          # keep what the saver recorded
                    if key in old:
                        new[key] = old[key]
                if new == old:
                    skipped += 1
                    continue
                changed += 1
                if changed <= 3:
                    print(f"  {z}/{x}/{y}")
                    for key in ("prompt", "negative", "prompt_note"):
                        if old.get(key) != new.get(key):
                            print(f"    {key}: {str(old.get(key))[:48]!r}"
                                  f" -> {str(new.get(key))[:48]!r}")
                if not args.dry_run:
                    store.db.execute(
                        "UPDATE tile_meta SET meta = ? WHERE z=? AND x=? AND y=?",
                        (json.dumps(new, ensure_ascii=False), z, x, y))
            if not args.dry_run:
                store.db.commit()
        verb = "would change" if args.dry_run else "updated"
        print(f"{verb} {changed}, unchanged {skipped}, "
              f"no stored graph {no_prompt} (of {changed + skipped + no_prompt})")
        if changed and not args.dry_run:
            print(f"the viewer reads the store directly, so this is already live; "
                  f"run --rebuild {args.refresh_meta} to put it in the archive too")
        return

    if args.rebuild:
        db, pmt = map_files(args.rebuild, args.maps_dir)
        if not os.path.exists(db):
            sys.exit(f"no store at {db}")
        with tilestore.TileStore(db) as store:
            info = archive.build_archive(
                store, pmt, webp_quality=args.quality,
                embed_tile_metadata=not args.no_tile_metadata,
                name=args.rebuild)
        print(json.dumps(info, indent=2))
        return

    if args.dump:
        name, z, x, y, out = args.dump
        _, pmt = map_files(name, args.maps_dir)
        data = archive.open_archive(pmt).get(int(z), int(x), int(y))
        if data is None:
            sys.exit(f"no tile {z}/{x}/{y} in {pmt}")
        with open(out, "wb") as fh:
            fh.write(data)
        print(f"wrote {out} ({len(data)} bytes)")
        return

    ap.print_help()


if __name__ == "__main__":
    main()

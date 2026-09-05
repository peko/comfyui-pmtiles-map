#!/usr/bin/env python3
"""Import a folder (or a .7z) of ready-made images into a PMTiles map.

usage:
  ./venv/bin/python tools/pmtiles_import.py IMAGES/ --map-name my-map --dry-run
  ./venv/bin/python tools/pmtiles_import.py IMAGES/ --map-name my-map --jobs 10
  ./venv/bin/python tools/pmtiles_import.py pack.7z --subdir "root/Booru" --map-name booru

The saver node only ever ingests an IMAGE tensor out of a running graph, and the
slicing it uses needs the render to be an exact multiple of the tile size --
otherwise it squashes the whole thing into one square tile.  Existing images are
rarely either, so this pads instead: one image becomes the smallest nx x ny block
of tiles that holds it, centred, with the remainder transparent.  Blocks are laid
along the Hilbert curve, one per index, so consecutive images stay adjacent on the
map *and* contiguous in the archive.

Three phases, in this order and for a reason:

  leaves    parallel; the workers decode, pad, crop and encode, the parent does
            nothing but insert, because SQLite has to stay single-threaded
  pyramid   one rebuild_pyramid at the end -- recomposing ancestors per image is
            the O(N*depth) cost the pack already measured at 7x
  archive   the WebP encode is 345 ms a 512 px tile, so the cache is warmed in
            parallel first and build_archive then just copies bytes

Placement is a pure function of the manifest index, so --resume is exact and
`next_free_block` (O(all leaves) per call) is never used.
"""
import argparse
import hashlib
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import time

# realpath, not abspath: these are symlinked from the repo's tools/ directory, and
# the pack is the parent of wherever this file really lives.
HERE = os.path.dirname(os.path.realpath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, PACK)

import archive          # noqa: E402
import imgimport        # noqa: E402
import paths            # noqa: E402
import tilestore        # noqa: E402

MAPS = paths.default_maps_dir()

QUIET = False


def say(*parts):
    # flush: a run of this length is normally redirected to a log, and block
    # buffering would hide every progress line until the job finished.
    if not QUIET:
        print(*parts, flush=True)


# Measured on this box with real 512 px tiles cut from a 960x1408 JPEG.  Used
# only for the up-front estimate; nothing depends on them being right, but a
# wrong estimate is worse than none, so re-measure if they stop matching.
STORE_BYTES = {"png": 395_000, "webp_lossless": 225_000, "webp_lossy": 48_000}
STORE_SECONDS = {"png": 0.019, "webp_lossless": 0.080, "webp_lossy": 0.021}
DECODE_SECONDS = 0.025
ARCHIVE_BYTES = 26_700
ARCHIVE_SECONDS = 0.345
RECOMPOSE_SECONDS = 0.070

# build_archive drops *all* per-tile metadata once the JSON exceeds its budget,
# so estimate the JSON rather than discover it in the log.
META_BYTES = {"full": 700, "origin": 190}


def map_files(name, maps_dir):
    """(store, archive) paths for a map name."""
    return (os.path.join(maps_dir, f"{name}.tiles.db"),
            os.path.join(maps_dir, f"{name}.pmtiles"))


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


# ------------------------------------------------------------------ source

def stage_archive(src, staging_dir, image_count_hint=0):
    """Unpack a .7z once, sequentially, and return the directory.

    A solid archive decompresses a whole block (~1 GiB here) to reach any member,
    so per-file extraction is O(N * block) -- minutes per image.  Sequential
    extraction touches each block once instead.  The staging directory is kept by
    default: a resumed run must not pay for it twice.
    """
    for exe in ("7z", "7za", "7zr"):
        binary = shutil.which(exe)
        if binary:
            break
    else:
        sys.exit(f"no 7z binary found; extract {src} yourself and pass the folder")

    os.makedirs(staging_dir, exist_ok=True)
    # A sentinel, not "the directory is non-empty": an interrupted extraction
    # leaves a plausible-looking partial tree, and a short manifest silently
    # relocates every image after the gap.
    done_marker = os.path.join(staging_dir, ".extracted")
    if os.path.exists(done_marker):
        say(f"staging     reusing {staging_dir} "
        f"({len(imgimport.iter_images(staging_dir))} images already there)")
        return staging_dir
    say(f"staging     unpacking {src}\n         -> {staging_dir}")
    t0 = time.time()
    subprocess.run([binary, "x", "-y", "-bb0", "-bd", f"-o{staging_dir}", src],
                   check=True, stdout=subprocess.DEVNULL)
    with open(done_marker, "w") as fh:
        fh.write(os.path.basename(src))
    say(f"staging     done in {clock(time.time() - t0)}")
    return staging_dir


def resolve_root(root, subdir):
    """Descend into --subdir, or into a lone top-level directory."""
    if subdir:
        path = os.path.join(root, subdir)
        if not os.path.isdir(path):
            sys.exit(f"--subdir {subdir!r} is not a directory under {root}")
        return path
    entries = [e for e in os.listdir(root) if not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(root, entries[0])):
        return os.path.join(root, entries[0])
    return root


# ---------------------------------------------------------------- metadata

def load_templates(root, arg):
    """Prompt templates, by lower-cased collection name.

    A literal string given on the command line is used for everything; a path is
    parsed as a readme, which is where this dataset keeps its real prompts.
    """
    if arg and not os.path.exists(arg):
        return {"": {"prompt": arg, "negative": "", "settings": {}}}
    path = arg
    if path is None:
        for candidate in (os.path.join(root, "readme.txt"),
                          os.path.join(os.path.dirname(root), "readme.txt")):
            if os.path.isfile(candidate):
                path = candidate
                break
    if not path:
        return {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return imgimport.parse_readme_templates(fh.read())


def pick_template(templates, rel, root_name):
    """The template for one image: match on its top directory, then the import
    root's own name (that is the collection when --subdir points inside one),
    then fall back to the only one there is."""
    if not templates:
        return None
    head = rel.split("/", 1)[0].lower() if "/" in rel else ""
    for key in (head, (root_name or "").lower()):
        for name, tpl in templates.items():
            if key and (key == name or key in name or name in key):
                return tpl
    return next(iter(templates.values())) if len(templates) == 1 else None


def base_meta_for(rel, abs_path, args, templates, root_name, extra_meta):
    """The per-image metadata, built once in the parent.

    Key names are deliberately the ones the rest of the pack already reads:
    `title`/`tags`/`prompt` are what search() concatenates, and
    `seed`/`steps`/`cfg`/`sampler_name`/`models`/`latent_size`/`time` are
    promptmeta's spellings, so the viewer's field table shows them unchanged.
    """
    import datetime

    stem = os.path.splitext(os.path.basename(rel))[0]
    parts = rel.split("/")[:-1]
    if args.title_from == "path":
        title = os.path.splitext(rel)[0]
    elif args.title_from == "name":
        title = os.path.basename(rel)
    else:
        title = stem

    tags = list(parts)
    if root_name and args.tag_root:
        tags.insert(0, root_name)
    if args.tags:
        tags.extend(t.strip() for t in args.tags.split(",") if t.strip())

    meta = {
        "title": title,
        "tags": ", ".join(tags),
        "time": datetime.datetime.fromtimestamp(os.path.getmtime(abs_path))
                .astimezone().isoformat(timespec="seconds"),
        "source": args.source_label,
        "source_path": rel,
        "batch_index": 0,
    }
    tpl = pick_template(templates, rel, root_name)
    if tpl:
        prompt = imgimport.render_prompt(tpl["prompt"], stem, args.artist_token)
        if prompt:
            meta["prompt"] = prompt
        negative = args.negative if args.negative is not None else tpl["negative"]
        if negative:
            meta["negative"] = negative
        meta.update(tpl["settings"])
    elif args.negative:
        meta["negative"] = args.negative
    meta.update(extra_meta)
    return meta


# ------------------------------------------------------------------- phases

def prewarm_derived(store, db_path, quality, jobs, ctx):
    """Fill tile_webp for every derived tile that has no cache row.

    build_archive spends 345 ms a 512 px tile in encode_webp(method=6); on a map
    this size that is hours, and it is the whole cost of the archive step.  With
    the cache warm it becomes a copy and reports `encoded 0`.  Leaves are warmed
    by render_job while the image is still decoded, so only the pyramid is left.
    """
    store.db.commit()                       # workers read over WAL
    todo = [
        (db_path, z, x, y, quality)
        for z, x, y in store.db.execute(
            "SELECT t.z, t.x, t.y FROM tiles t "
            "LEFT JOIN tile_webp c ON c.z = t.z AND c.x = t.x AND c.y = t.y "
            "                     AND c.q = ? "
            "WHERE c.blob IS NULL AND t.kind = ? ORDER BY t.z, t.x, t.y",
            (int(quality), tilestore.DERIVED),
        )
    ]
    if not todo:
        return 0
    done = 0
    t0 = time.time()
    with ctx.Pool(jobs) as pool:
        for result in pool.imap_unordered(imgimport.webp_job, todo, chunksize=8):
            done += 1
            if result is not None:
                z, x, y, blob = result
                store.set_webp_cached(z, x, y, blob, quality)
            if done % 512 == 0:
                store.db.commit()
                progress("prewarm", done, len(todo), t0)
    store.db.commit()
    progress("prewarm", done, len(todo), t0, final=True)
    return done


def progress(label, done, total, t0, final=False, extra=""):
    if QUIET:
        return
    rate = done / max(1e-6, time.time() - t0)
    eta = (total - done) / rate if rate else 0
    line = (f"{label:<9} {done}/{total}  {100.0 * done / max(1, total):5.1f}%  "
            f"{rate:6.1f}/s  eta {clock(eta)}{extra}")
    if sys.stdout.isatty() and not final:
        sys.stdout.write("\r" + line + "   ")
        sys.stdout.flush()
    else:
        print(line, flush=True)
    if final and sys.stdout.isatty():
        sys.stdout.write("\n")


# ---------------------------------------------------------------------- cli

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="a directory of images, or a .7z archive")
    ap.add_argument("--subdir", default=None,
                    help="import only this subtree of the source")
    ap.add_argument("--map-name", default=None,
                    help="map name (default: the source's basename)")
    ap.add_argument("--maps-dir", default=MAPS)

    ap.add_argument("--tile-size", type=int, default=512)
    ap.add_argument("--tiles-per-image", default=None, metavar="WxH",
                    help="override the derived block shape, e.g. 2x3")
    ap.add_argument("--zoom", type=int, default=None,
                    help="override the derived zoom level")
    ap.add_argument("--pad-color", default="0,0,0,0",
                    help="R,G,B,A for the padding around each image")

    ap.add_argument("--store-format", default=tilestore.WEBP_LOSSY,
                    choices=list(tilestore.STORE_FORMATS))
    ap.add_argument("--store-quality", type=int, default=80)
    ap.add_argument("--webp-quality", type=int, default=80,
                    help="archive quality (and the cache the viewer shares)")
    ap.add_argument("--tile-meta", default="origin", choices=("full", "origin"),
                    help="'origin' keeps the heavy fields on the render's "
                         "top-left tile only, which is all search() reads and "
                         "is what keeps the archive under its metadata budget")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--pyramid-mode", default=None,
                    choices=list(tilestore.PYRAMID_MODES),
                    help="scale: average four children into one tile. sample "
                         "(the default): only while the content stays above "
                         "--pyramid-min-px, then keep one child whole")
    ap.add_argument("--pyramid-min-px", type=int, default=None,
                    help=f"floor for `sample` (default {tilestore.MIN_CONTENT_PX})")

    ap.add_argument("--title-from", default="stem", choices=("stem", "name", "path"))
    ap.add_argument("--tags", default=None, help="extra tags, comma separated")
    ap.add_argument("--no-tag-root", dest="tag_root", action="store_false",
                    help="do not tag every image with the import root's name")
    ap.add_argument("--prompt-template", default=None,
                    help="a readme to parse, or a literal template string")
    ap.add_argument("--artist-token", default="xxx",
                    help="placeholder in the template replaced by the title")
    ap.add_argument("--negative", default=None)
    ap.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE",
                    help="extra metadata, repeatable")
    ap.add_argument("--source-label", default=None)
    ap.add_argument("--index-manifest", default=None,
                    help="write 'index<TAB>path' so two maps can be diffed")

    ap.add_argument("--limit", type=int, default=0, help="first N images only")
    ap.add_argument("--sample", type=int, default=0,
                    help="N evenly spaced images -- a better smoke test than "
                         "--limit, which takes them all from one directory")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-pyramid", dest="pyramid", action="store_false")
    ap.add_argument("--no-archive", dest="build", action="store_false")
    ap.add_argument("--no-prewarm", dest="prewarm", action="store_false")
    ap.add_argument("--no-resume", dest="resume", action="store_false")
    ap.add_argument("--force", action="store_true",
                    help="import even when the manifest no longer matches")
    ap.add_argument("--staging-dir", default=None)
    ap.add_argument("--clean-staging", action="store_true")
    ap.add_argument("--commit-every", type=int, default=64)
    ap.add_argument("--quiet", action="store_true",
                    help="no plan block and no progress lines")
    return ap.parse_args(argv)


def main(argv=None):
    global QUIET
    args = parse_args(argv)
    QUIET = args.quiet
    src = os.path.abspath(args.source)
    if not os.path.exists(src):
        sys.exit(f"no such source: {src}")

    staging = None
    if os.path.isfile(src):
        stem = os.path.splitext(os.path.basename(src))[0]
        staging = args.staging_dir or os.path.join(
            os.path.dirname(src), ".pmtiles-import", stem)
        root = stage_archive(src, staging)
    else:
        root = src
    root = resolve_root(root, args.subdir)
    root_name = os.path.basename(root.rstrip("/"))

    name = args.map_name or root_name
    if args.source_label is None:
        args.source_label = os.path.basename(src)
    extra_meta = {}
    for item in args.meta:
        key, _, value = item.partition("=")
        extra_meta[key.strip()] = value.strip()

    rels = imgimport.iter_images(root)
    if not rels:
        sys.exit(f"no images under {root}")
    if args.sample and args.sample < len(rels):
        step = len(rels) / args.sample
        rels = [rels[int(i * step)] for i in range(args.sample)]
    elif args.limit:
        rels = rels[: args.limit]

    from PIL import Image
    with Image.open(os.path.join(root, rels[0])) as probe:
        first_size = (probe.width, probe.height)

    tpi = None
    if args.tiles_per_image:
        tpi = tuple(int(v) for v in args.tiles_per_image.lower().split("x"))
    layout = imgimport.plan(len(rels), first_size[0], first_size[1],
                            args.tile_size, tiles_per_image=tpi, zoom=args.zoom)
    nx, ny, z = layout["nx"], layout["ny"], layout["z"]
    pad_color = tuple(int(v) for v in args.pad_color.split(","))
    templates = load_templates(root, args.prompt_template)

    fingerprint = hashlib.sha256(
        ("\n".join(rels) + "\0" +
         repr((args.tile_size, nx, ny, layout["block_order"], z))).encode()
    ).hexdigest()

    # ------------------------------------------------------------ estimate
    derived = estimate_derived(layout)
    store_b = STORE_BYTES[args.store_format]
    meta_json = len(rels) * (META_BYTES["full"] +
                             (nx * ny - 1) * META_BYTES[args.tile_meta])
    peak = ((layout["leaves"] + derived) * (store_b + ARCHIVE_BYTES)
            + layout["leaves"] * META_BYTES["full"]
            + (layout["leaves"] + derived) * ARCHIVE_BYTES)
    leaf_s = len(rels) * (DECODE_SECONDS + nx * ny *
                          (STORE_SECONDS[args.store_format] + ARCHIVE_SECONDS))
    free = shutil.disk_usage(args.maps_dir if os.path.isdir(args.maps_dir)
                             else os.path.dirname(os.path.abspath(args.maps_dir))).free

    say(f"source      {src}")
    if staging:
        say(f"staging     {staging}  ({'kept' if not args.clean_staging else 'temporary'})")
    say(f"root        {root}")
    say(f"images      {len(rels)}   first is {first_size[0]}x{first_size[1]}")
    say(f"prompts     {len(templates)} template(s): "
        f"{', '.join(sorted(templates)) or 'none -- search will only see title/tags'}")
    say(f"tile size   {args.tile_size}   block {nx}x{ny} tiles = "
        f"{layout['block_px'][0]}x{layout['block_px'][1]}, pad "
        f"{(nx * args.tile_size - first_size[0]) // 2} px l/r, "
        f"{(ny * args.tile_size - first_size[1]) // 2} px t/b")
    say(f"hilbert     order {layout['block_order']}   "
        f"{layout['blocks_side']}x{layout['blocks_side']} blocks -> "
        f"{layout['extent'][0]}x{layout['extent'][1]} tiles   "
        f"{100.0 * len(rels) / (1 << 2 * layout['block_order']):.1f}% of the curve")
    say(f"zoom        z = {z}   (extent {1 << z} tiles a side)")
    say(f"leaves      {layout['leaves']} tiles   {args.store_format} "
        f"q{args.store_quality}   ~{human(layout['leaves'] * store_b)}")
    say(f"pyramid     ~{derived} tiles                     "
        f"~{human(derived * store_b)}")
    say(f"webp cache  {layout['leaves'] + derived} tiles   q{args.webp_quality}"
        f"           ~{human((layout['leaves'] + derived) * ARCHIVE_BYTES)}")
    say(f"archive     ~{human((layout['leaves'] + derived) * ARCHIVE_BYTES)}")
    budget = "fits" if meta_json <= 8 * 1024 * 1024 else "OVER -- archive will carry none"
    say(f"tile meta   {args.tile_meta}: ~{human(meta_json)} of JSON, {budget}")
    say(f"            peak ~{human(peak)} against {human(free)} free")
    say(f"time        leaves {clock(leaf_s / args.jobs)} at --jobs {args.jobs} "
        f"({clock(leaf_s)} at 1) | pyramid ~{clock(derived * RECOMPOSE_SECONDS)} "
        f"| prewarm ~{clock(derived * ARCHIVE_SECONDS / args.jobs)}")

    if peak > free:
        sys.exit("refusing to start: the estimate does not fit in the free space")
    if args.dry_run:
        say("--dry-run: nothing written.")
        return 0

    if args.index_manifest:
        with open(args.index_manifest, "w", encoding="utf-8") as fh:
            for i, rel in enumerate(rels):
                fh.write(f"{i}\t{rel}\n")

    os.makedirs(args.maps_dir, exist_ok=True)
    db_path, pmt_path = map_files(name, args.maps_dir)
    fresh = not os.path.exists(db_path)
    store = tilestore.TileStore(
        db_path,
        tile_size=args.tile_size if fresh else None,
        store_format=args.store_format,
        store_quality=args.store_quality,
    )
    if store.tile_size != args.tile_size:
        sys.exit(f"map {name} has tile_size {store.tile_size}, not {args.tile_size}")

    previous = store.get_map_meta("import_manifest_sha")
    if previous and previous != fingerprint and not args.force:
        store.close()
        sys.exit(
            f"map {name} was built from a different manifest "
            f"({previous[:12]} != {fingerprint[:12]}).\n"
            "An image's Hilbert index is its position in the manifest, so a "
            "changed list relocates everything after it.  Re-run with --force "
            "only if you mean to overwrite, or use a new --map-name."
        )

    try:
        run_import(store, db_path, pmt_path, name, root, rels, layout, pad_color,
                   templates, root_name, extra_meta, fingerprint, args)
    finally:
        store.close()
    if staging and args.clean_staging:
        shutil.rmtree(staging, ignore_errors=True)
    return 0


def estimate_derived(layout):
    """Roughly how many pyramid tiles a leaf level of this shape produces."""
    total, w, h = 0, layout["extent"][0], layout["extent"][1]
    while w > 1 or h > 1:
        w, h = max(1, -(-w // 2)), max(1, -(-h // 2))
        total += w * h
    return total


def run_import(store, db_path, pmt_path, name, root, rels, layout, pad_color,
               templates, root_name, extra_meta, fingerprint, args):
    nx, ny, z = layout["nx"], layout["ny"], layout["z"]
    store.set_map_meta("import_manifest_sha", fingerprint)
    store.set_map_meta("import_source", args.source_label)
    store.set_map_meta("import_z", str(z))
    store.set_map_meta("import_block", f"{nx}x{ny}")
    store.set_map_meta("import_order", str(layout["block_order"]))
    # The viewer encodes on demand at this quality; keep it in step with the
    # cache we are about to fill, or the two fight each other.
    store.set_map_meta("webp_quality", str(args.webp_quality))
    # Hundreds of MB of -wal otherwise: the default 4 MB autocheckpoint fires
    # every few tiles under a bulk insert.
    store.db.execute("PRAGMA wal_autocheckpoint = 4000")

    occupied = store.leaf_cells(z) if args.resume else set()

    jobs = []
    for index, rel in enumerate(rels):
        x0, y0 = imgimport.block_origin(layout["block_order"], index, nx, ny)
        if args.resume and all((x0 + ix, y0 + iy) in occupied
                               for iy in range(ny) for ix in range(nx)):
            continue
        abs_path = os.path.join(root, rel)
        jobs.append({
            "index": index, "path": abs_path, "x0": x0, "y0": y0,
            "nx": nx, "ny": ny, "tile_size": args.tile_size,
            "pad_color": pad_color, "store_format": args.store_format,
            "store_quality": args.store_quality,
            "webp_quality": args.webp_quality if args.prewarm else None,
            "tile_meta": args.tile_meta,
            "base_meta": base_meta_for(rel, abs_path, args, templates,
                                       root_name, extra_meta),
        })
    skipped = len(rels) - len(jobs)
    say(f"leaves      {len(jobs)} to render, {skipped} already present")

    # forkserver, never fork: a forked worker inherits this process's open
    # SQLite connection and WAL descriptors, which is the classic way to corrupt
    # a store even when the child never touches them.
    try:
        ctx = multiprocessing.get_context("forkserver")
    except ValueError:
        ctx = multiprocessing.get_context("spawn")

    written = pending = failed = resized = 0
    t0 = time.time()
    if jobs:
        with ctx.Pool(args.jobs) as pool:
            for done, result in enumerate(
                    pool.imap(imgimport.render_job, jobs, chunksize=2), 1):
                if not result["ok"]:
                    failed += 1
                    print(f"\n  skipped {rels[result['index']]}: {result['error']}")
                    continue
                for x, y, blob, w, h, meta, webp in result["tiles"]:
                    store.put_tile_encoded(z, x, y, blob, w, h,
                                           kind=tilestore.LEAF, meta=meta)
                    if webp is not None:
                        # After put_tile_encoded, never before: it deletes the
                        # cache row for the coordinate it just rewrote.
                        store.set_webp_cached(z, x, y, webp, args.webp_quality)
                    written += 1
                    pending += 1
                if done % args.commit_every == 0:
                    store.bump_pending(pending)
                    pending = 0
                    store.db.commit()
                    progress("leaves", done, len(jobs), t0)
        if pending:
            store.bump_pending(pending)
        store.db.commit()
        progress("leaves", len(jobs), len(jobs), t0, final=True)
    say(f"leaves      {written} tiles written, {failed} files failed")

    stale = store.get_map_meta("pyramid_stale") == "1"
    if args.pyramid and (written or stale):
        t0 = time.time()

        def on_level(level, done, total):
            progress(f"pyramid z{level}", done, total, t0)

        made = store.rebuild_pyramid(progress=on_level, mode=args.pyramid_mode,
                                     min_px=args.pyramid_min_px)
        store.db.commit()
        say(f"\npyramid     {len(made)} tiles in {clock(time.time() - t0)}")
    elif args.pyramid:
        # rebuild_pyramid drops every derived tile and recomposes it, so running
        # it when nothing was written is not a no-op -- it is the whole cost of
        # the pyramid for an identical result, and it churns the change feed.
        say("pyramid     up to date, nothing was written")
    else:
        store.set_map_meta("pyramid_stale", "1")
        store.db.commit()

    if args.prewarm:
        try:
            ctx2 = multiprocessing.get_context("forkserver")
        except ValueError:
            ctx2 = multiprocessing.get_context("spawn")
        warmed = prewarm_derived(store, db_path, args.webp_quality, args.jobs, ctx2)
        say(f"prewarm     {warmed} derived tiles cached")

    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.db.commit()

    if args.build:
        t0 = time.time()
        # Above the budget build_archive silently omits *every* per-tile record,
        # so decide it here instead of finding out in the log.
        meta_json = len(rels) * (META_BYTES["full"] +
                                 (nx * ny - 1) * META_BYTES[args.tile_meta])
        embed = meta_json <= 8 * 1024 * 1024
        if not embed:
            say("archive     per-tile metadata left out (over the 8 MB budget); "
              "the store still has it, and that is what the viewer reads")
        info = archive.build_archive(
            store, pmt_path, webp_quality=args.webp_quality,
            embed_tile_metadata=embed, name=name,
            description=f"imported from {args.source_label}",
            progress=lambda done, total: progress("archive", done, total, t0),
        )
        say(f"\narchive     {info['tiles']} tiles, {human(info['file_bytes'])}, "
          f"{info['encoded']} encoded on the fly, "
            f"z{info['min_zoom']}-{info['max_zoom']}, {clock(info['seconds'])}")
    say(f"map         {name}   {db_path}")


if __name__ == "__main__":
    sys.exit(main())

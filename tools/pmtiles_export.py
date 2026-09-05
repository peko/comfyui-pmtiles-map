#!/usr/bin/env python3
"""Export a map as a static bundle: upload the directory, done.

usage:
  ./venv/bin/python tools/pmtiles_export.py <map> <outdir> [--build] [--force]
                                            [--maps-dir DIR] [--limit N]

The result needs no server code. index.html reads the .pmtiles by HTTP range
request through the vendored pmtiles.js, and search runs over a sidecar index
baked at export time -- there is nothing to query once the map is on a CDN.

Two things the host must get right, and both are easy to get wrong:

  * serve the .pmtiles **untransformed**. A CDN that gzips or otherwise rewrites
    the body breaks the byte offsets the format is built on.
  * allow `Range` (and CORS, if the page is served from another origin).
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, PACK)

import archive          # noqa: E402
import paths            # noqa: E402
import tilestore        # noqa: E402

WEB = os.path.join(PACK, "web")
# Metadata worth carrying to a CDN. `full_prompt` is a whole graph per tile and
# never belongs in a page's sidecar.
_FIELDS = ("title", "tags", "prompt", "negative", "seed", "steps", "cfg",
           "sampler_name", "models", "time")

BUNDLE_README = """\
# {name}

A zoomable map of {renders} renders: {tiles} tiles, z{min_zoom}-{max_zoom}, \
{tile_size} px.

## Look at it

```
python3 serve.py            # then open http://127.0.0.1:8000/
python3 serve.py 8080 --host 0.0.0.0     # and from other machines on the LAN
```

`serve.py` is standard library only -- no pip install, no dependencies, any
Python 3.8+.

**Do not use `python -m http.server`.** It has no `Range` support, so
`pmtiles.js` gets the entire archive back for each of the ~16 KB reads it makes
per tile: the page loads, the map stays blank, and nothing says why. That single
missing feature is why `serve.py` is here.

## Put it on a real host

Upload the whole directory. `index.html` reads `{archive}` by HTTP range
request, and search runs over `search.js`, baked at export time -- there is
nothing server-side to run. Two things the host must get right:

* **`Range` must be answered with `206`.** Everything hangs on this.
* **The `.pmtiles` must be served untransformed.** A CDN that gzips or otherwise
  rewrites the body breaks the byte offsets the format is built on.

If the page and the archive live on different origins, the archive's host also
needs permissive CORS.

Static hosts that work: any S3-compatible bucket, Cloudflare R2, Netlify,
Hugging Face (`resolve/main/...` answers 206 and reflects the request Origin).
GitHub Pages serves static files but caps a file at 100 MB and a site at 1 GB,
and cannot serve Git LFS objects at all -- fine for a small map, not for a large
archive.

## What is in here

| | |
|---|---|
| `index.html`, `static.js`, `config.js` | the viewer |
| `search.js` | one entry per render, baked at export |
| `{archive}` | the tiles |
| `leaflet/`, `pmtiles/` | vendored, no CDN needed |
| `serve.py` | the local server described above |
"""


def search_index(db_path, archive_path, limit=None):
    """One entry per render: the origin tile of each block, plus its metadata."""
    entries = []
    records = {}
    if os.path.isfile(db_path):
        import sqlite3
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT z, x, y, meta FROM tile_meta").fetchall()
        finally:
            db.close()
        records = {(z, x, y): json.loads(meta) for z, x, y, meta in rows}
    else:                       # no store beside the archive: use its own blob
        md = archive.open_archive(archive_path).metadata.get("tiles", {})
        for key, meta in md.items():
            z, x, y = (int(v) for v in key.split("/"))
            records[(z, x, y)] = meta

    for (z, x, y), meta in sorted(records.items()):
        if meta.get("kind") != "leaf":
            continue
        grid = meta.get("grid")
        if isinstance(grid, list) and len(grid) == 4 and (grid[0] or grid[1]):
            continue            # not the origin tile of its block
        entry = {"z": z, "x": x, "y": y,
                 "nx": grid[2] if grid else 1, "ny": grid[3] if grid else 1}
        entry.update({k: meta[k] for k in _FIELDS if meta.get(k) not in (None, "")})
        entries.append(entry)
        if limit and len(entries) >= limit:
            break
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("map")
    ap.add_argument("outdir")
    ap.add_argument("--maps-dir", default=paths.default_maps_dir())
    ap.add_argument("--build", action="store_true",
                    help="rebuild the pyramid and the archive before exporting")
    ap.add_argument("--force", action="store_true",
                    help="export even when the archive is behind the store")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the search index (0 = every render)")
    ap.add_argument("--serve", type=int, metavar="PORT", default=0,
                    help="after exporting, serve the bundle on this port for a look")
    ap.add_argument("--host", default="127.0.0.1",
                    help="--serve bind address; 0.0.0.0 to allow the LAN in")
    args = ap.parse_args()

    db = os.path.join(args.maps_dir, f"{args.map}.tiles.db")
    pmt = os.path.join(args.maps_dir, f"{args.map}.pmtiles")

    if args.build:
        if not os.path.isfile(db):
            sys.exit(f"no store at {db}; nothing to build from")
        with tilestore.TileStore(db) as store:
            written = store.rebuild_pyramid(0)
            store.db.commit()
            print(f"pyramid: {len(written)} derived tiles")
            info = archive.build_archive(store, pmt, name=args.map)
            print(f"archive: {info['tiles']} tiles, {info['file_bytes']/1e6:.1f} MB")

    if not os.path.isfile(pmt):
        sys.exit(f"no archive at {pmt} -- run with --build, or "
                 f"tools/pmtiles_map.py --rebuild {args.map}")

    info = archive.archive_info(pmt)
    # Exporting a stale archive means uploading a map that is missing renders or
    # zoom levels. Say which, and refuse unless told otherwise.
    problems = []
    if info.get("pending_tiles"):
        problems.append(f"{info['pending_tiles']} tile(s) are in the store but not "
                        f"in the archive")
    if info.get("pyramid_stale"):
        problems.append("the zoom pyramid has not been built, so the map will only "
                        "be viewable near its deepest level")
    if problems and not args.force:
        sys.exit("refusing to export:\n  - " + "\n  - ".join(problems)
                 + "\nre-run with --build to fix, or --force to export as is")
    for problem in problems:
        print(f"warning: {problem}")

    out = os.path.abspath(args.outdir)
    os.makedirs(out, exist_ok=True)
    shutil.copy2(pmt, os.path.join(out, os.path.basename(pmt)))
    for src, dst in (("vendor/leaflet", "leaflet"), ("vendor/pmtiles", "pmtiles")):
        shutil.copytree(os.path.join(WEB, src), os.path.join(out, dst),
                        dirs_exist_ok=True)

    entries = search_index(db, pmt, args.limit or None)
    with open(os.path.join(out, "search.js"), "w", encoding="utf-8") as fh:
        # A .js file rather than JSON so the bundle also works from file:// ,
        # where fetch() of a local JSON is blocked by CORS.
        fh.write("window.SEARCH = ")
        json.dump(entries, fh, ensure_ascii=False)
        fh.write(";\n")

    config = {
        "name": args.map,
        "archive": os.path.basename(pmt),
        "tile_size": info["tile_size"],
        "min_zoom": info["min_zoom"],
        "max_zoom": info["max_zoom"],
        "bounds": info["bounds"],
        "attribution": info.get("attribution") or f"{args.map}.pmtiles",
    }
    with open(os.path.join(out, "config.js"), "w", encoding="utf-8") as fh:
        fh.write("const CONFIG = ")
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.write(";\n")

    template = open(os.path.join(WEB, "static", "index.html"), encoding="utf-8").read()
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(template.replace("__TITLE__", args.map))
    shutil.copy2(os.path.join(WEB, "static", "static.js"),
                 os.path.join(out, "static.js"))
    # The bundle carries its own server, so a recipient with no web server at
    # all -- and no pip -- can still look at the map.  `python -m http.server`
    # cannot substitute: no Range, so every tile request returns the whole
    # archive and the map stays blank.
    shutil.copy2(os.path.join(WEB, "static", "serve.py"),
                 os.path.join(out, "serve.py"))
    os.chmod(os.path.join(out, "serve.py"), 0o755)
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(BUNDLE_README.format(
            name=args.map, archive=os.path.basename(pmt),
            tiles=info["tiles"], min_zoom=info["min_zoom"],
            max_zoom=info["max_zoom"], tile_size=info["tile_size"],
            renders=len(entries)))
    # search.js has to load before static.js reads window.SEARCH.
    with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    html = html.replace('<script src="config.js"></script>',
                        '<script src="config.js"></script>\n'
                        '<script src="search.js"></script>')
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)

    total = sum(os.path.getsize(os.path.join(root, f))
                for root, _, files in os.walk(out) for f in files)
    print(f"\nexported to {out}")
    print(f"  {info['tiles']} tiles, z{info['min_zoom']}-{info['max_zoom']}, "
          f"{info['tile_size']}px")
    print(f"  {len(entries)} renders in the search index")
    print(f"  {total / 1e6:.1f} MB total")
    print("\ncheck it locally, or hand the directory to someone who has no web")
    print("server at all -- serve.py inside it needs nothing but python3:")
    print(f"  cd {out} && python3 serve.py")
    print("  (NOT `python -m http.server`: it ignores Range, and pmtiles.js reads")
    print("   the archive by range -- the map would simply stay blank)")
    print("\nuploading:")
    print("  * serve the .pmtiles untransformed -- a CDN that gzips it breaks the")
    print("    byte offsets the format relies on")
    print("  * allow Range requests, and CORS if the page lives on another origin")

    if args.serve:
        serve(out, args.serve, args.host)


def bundle_server():
    """The `serve.py` that ships inside the bundle, imported as a module.

    Deliberately the same file the recipient runs: checking an export with a
    different server than the one distributed with it is how a Range bug ships.
    It is standard-library only, so `--serve` no longer needs aiohttp either.
    """
    import importlib.util
    path = os.path.join(WEB, "static", "serve.py")
    spec = importlib.util.spec_from_file_location("pmtiles_bundle_serve", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def serve(directory, port, host="127.0.0.1"):
    bundle_server().serve(directory, port, host)


if __name__ == "__main__":
    main()

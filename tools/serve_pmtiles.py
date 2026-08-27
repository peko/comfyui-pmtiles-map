#!/usr/bin/env python3
"""Serve the PMTiles map viewer without ComfyUI running.

usage: ./venv/bin/python tools/serve_pmtiles.py [--port 8899] [--listen 0.0.0.0]
                                                [--maps-dir ComfyUI/output/maps]

Same route table and same viewer as the in-ComfyUI version (the custom node
registers it on ComfyUI's own aiohttp app); this is just a second host for it,
so a long render and map-browsing do not have to share a process.  Binds
0.0.0.0 by default, matching run_comfyui.sh's deliberate LAN choice.
"""
import argparse
import os
import sys

from aiohttp import web

# realpath, not abspath: these are symlinked from the repo's tools/ directory, and
# the pack is the parent of wherever this file really lives.
HERE = os.path.dirname(os.path.realpath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, PACK)

import archive          # noqa: E402
import paths            # noqa: E402  (needs PACK on sys.path first)
import routes           # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--maps-dir", default=paths.default_maps_dir(),
                    help=f"default: first output/maps found near the pack, or "
                         f"${paths.ENV_VAR}")
    args = ap.parse_args()

    maps_dir = os.path.abspath(args.maps_dir)
    found = archive.list_archives(maps_dir)
    print(f"maps dir: {maps_dir}")
    if not found:
        print("  (no .pmtiles archives yet -- the viewer will say so)")
    for info in found:
        if "error" in info:
            print(f"  {info['name']}: BROKEN {info['error']}")
        else:
            print(f"  {info['name']}: {info['tiles']} tiles, "
                  f"z{info['min_zoom']}-{info['max_zoom']}, "
                  f"{info['bytes'] / 1e6:.2f} MB")

    app = web.Application()
    app.add_routes(routes.build_routes(lambda: maps_dir))
    print(f"\nviewer: http://{'127.0.0.1' if args.listen == '0.0.0.0' else args.listen}"
          f":{args.port}/pmtiles/")
    web.run_app(app, host=args.listen, port=args.port, print=None)


if __name__ == "__main__":
    main()

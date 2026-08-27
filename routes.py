"""HTTP endpoints for the map viewer.

The same route table serves two hosts: ComfyUI's own aiohttp app (registered
from __init__.py) and tools/serve_pmtiles.py for viewing with ComfyUI stopped.
Hence `build_routes(maps_dir_fn)` rather than module-level decorators.

Tiles are extracted from the archive *server-side*, so the browser fetches plain
image URLs -- no client-side range reads, no pmtiles.js, and nothing to go wrong
if a host mishandles `Range`.  `/pmtiles/{name}/file` still hands out the raw
archive (via FileResponse, which aiohttp serves ranged) for anyone who wants to
point MapLibre or pmtiles.js at it instead.
"""
import asyncio
import hashlib
import io
import json
import os
import re

from aiohttp import web

try:
    from . import archive, tilestore
except ImportError:
    import archive
    import tilestore

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


_EMPTY = None


def _empty_tile():
    """A 1x1 fully transparent WebP. Leaflet scales it to the tile size."""
    global _EMPTY
    if _EMPTY is None:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(buf, format="WEBP",
                                                     lossless=True)
        _EMPTY = buf.getvalue()
    return _EMPTY


def _archive_for(maps_dir, name):
    if not _SAFE_NAME.match(name or "") or name.startswith("."):
        raise web.HTTPBadRequest(reason="bad map name")
    path = os.path.join(maps_dir, f"{name}.pmtiles")
    if not os.path.isfile(path):
        raise web.HTTPNotFound(reason=f"no such map: {name}")
    return path


def build_routes(maps_dir_fn):
    routes = web.RouteTableDef()

    @routes.get("/pmtiles")
    async def index_redirect(request):
        raise web.HTTPFound("/pmtiles/")

    @routes.get("/pmtiles/")
    async def index(request):
        return web.FileResponse(
            os.path.join(WEB_DIR, "index.html"),
            headers={"Content-Type": _CONTENT_TYPES[".html"],
                     "Cache-Control": "no-cache"},
        )

    @routes.get("/pmtiles/assets/{path:.*}")
    async def asset(request):
        rel = request.match_info["path"]
        full = os.path.realpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) \
                or not os.path.isfile(full):
            raise web.HTTPNotFound()
        ctype = _CONTENT_TYPES.get(os.path.splitext(full)[1].lower(),
                                   "application/octet-stream")
        return web.FileResponse(full, headers={"Content-Type": ctype})

    @routes.get("/pmtiles/maps")
    async def maps(request):
        return web.json_response({"maps": archive.list_archives(maps_dir_fn())})

    @routes.get("/pmtiles/{name}/meta.json")
    async def meta(request):
        name = request.match_info["name"]
        path = _archive_for(maps_dir_fn(), name)
        payload = archive.archive_info(path)
        # Deliberately NOT the per-tile records: on a 5000-tile map that blob is
        # megabytes, and the viewer only ever needs the tile you clicked. Ask
        # /tilemeta/{z}/{x}/{y} for those.
        payload["has_store"] = os.path.isfile(
            os.path.join(maps_dir_fn(), f"{name}.tiles.db"))
        return web.json_response(payload, headers={"Cache-Control": "no-cache"})

    @routes.get("/pmtiles/{name}/tilemeta/{z}/{x}/{y}")
    async def tilemeta(request):
        name = request.match_info["name"]
        maps = maps_dir_fn()
        path = _archive_for(maps, name)
        try:
            z = int(request.match_info["z"])
            x = int(request.match_info["x"])
            y = int(request.match_info["y"])
        except ValueError:
            raise web.HTTPBadRequest(reason="z/x/y must be integers")
        # The store first: it is authoritative and complete even when the
        # archive's metadata blob was skipped for size. The archive is the
        # fallback, so a .pmtiles copied somewhere on its own still works.
        found = tilestore.read_meta(os.path.join(maps, f"{name}.tiles.db"), z, x, y)
        source = "store"
        if found is None:
            found = archive.open_archive(path).metadata.get(
                "tiles", {}).get(f"{z}/{x}/{y}")
            source = "archive"
        if found is None:
            raise web.HTTPNotFound(reason=f"no metadata for {z}/{x}/{y}")
        return web.json_response({"coord": [z, x, y], "source": source,
                                  "meta": found},
                                 headers={"Cache-Control": "no-cache"})

    @routes.get("/pmtiles/{name}/file")
    async def raw(request):
        path = _archive_for(maps_dir_fn(), request.match_info["name"])
        return web.FileResponse(path, headers={
            "Content-Type": "application/octet-stream",
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
        })

    @routes.get("/pmtiles/{name}/tiles/{z}/{x}/{y}.webp")
    async def tile(request):
        path = _archive_for(maps_dir_fn(), request.match_info["name"])
        try:
            z = int(request.match_info["z"])
            x = int(request.match_info["x"])
            y = int(request.match_info["y"])
        except ValueError:
            raise web.HTTPBadRequest(reason="z/x/y must be integers")
        handle = archive.open_archive(path)
        data = handle.get(z, x, y)
        if data is None:
            # A hole is served as a transparent tile with 200, not 404.
            #
            # A 404 makes Leaflet treat the tile as an *error*: it swaps in
            # errorTileUrl and the tile stops behaving like a tile, so the change
            # feed has nothing whose src it can bust when a render finally fills
            # that spot -- it stayed blank until the page was reloaded. Worse, 404
            # is heuristically cacheable (RFC 9111 4.2.2), so the browser
            # remembered the hole and answered later requests from cache.
            #
            # A 200 placeholder is an ordinary tile: it lives in Leaflet's tile
            # registry, it can be refreshed in place, and `no-cache` plus an ETag
            # that differs from any real tile means the next revalidation returns
            # the render as soon as one exists. `?missing=404` restores strict
            # semantics for programmatic callers.
            if request.query.get("missing") == "404":
                raise web.HTTPNotFound(
                    reason=f"no tile {z}/{x}/{y}",
                    headers={"Cache-Control": "no-store, must-revalidate"})
            return web.Response(body=_empty_tile(), headers={
                "Content-Type": "image/webp",
                "Cache-Control": "no-cache",
                "ETag": '"empty"',
                "X-Tile-Missing": "1",
            })
        data = bytes(data)
        # ETag over the *bytes*, not the archive mtime: every rebuild changes the
        # mtime, so an mtime ETag would turn each revalidation into a full 200
        # even for the overwhelming majority of tiles that did not change.
        etag = f'"{hashlib.blake2b(data, digest_size=8).hexdigest()}"'
        headers = {
            "Content-Type": "image/webp",
            # The archive is rewritten under the viewer, so a tile URL is not
            # immutable; revalidation is cheap and staleness is confusing.
            "Cache-Control": "no-cache",
            "ETag": etag,
        }
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        return web.Response(body=data, headers=headers)

    @routes.get("/pmtiles/{name}/render/{z}/{x}/{y}")
    async def render(request):
        """The original image a tile came from, stitched from its render block."""
        name = request.match_info["name"]
        maps = maps_dir_fn()
        path = _archive_for(maps, name)
        try:
            z = int(request.match_info["z"])
            x = int(request.match_info["x"])
            y = int(request.match_info["y"])
        except ValueError:
            raise web.HTTPBadRequest(reason="z/x/y must be integers")
        fmt = request.query.get("format", "png").lower()
        if fmt not in ("png", "webp"):
            raise web.HTTPBadRequest(reason="format must be png or webp")

        stitched = archive.stitch_render(
            os.path.join(maps, f"{name}.tiles.db"), path, z, x, y)
        if stitched is None:
            raise web.HTTPNotFound(reason=f"nothing to stitch at {z}/{x}/{y}")
        image, origin, span, source = stitched
        # A zoomed-out click can only reach a derived pyramid tile; the preview
        # then shows an aggregate, not a render, and should not pretend otherwise.
        kind = (tilestore.read_meta(os.path.join(maps, f"{name}.tiles.db"),
                                    *origin) or {}).get("kind", "unknown")
        buf = io.BytesIO()
        if fmt == "webp":
            image.save(buf, format="WEBP", quality=95, method=4)
        else:
            image.save(buf, format="PNG", compress_level=3)
        data = buf.getvalue()
        return web.Response(body=data, headers={
            "Content-Type": f"image/{fmt}",
            "Cache-Control": "no-cache",
            "ETag": f'"{hashlib.blake2b(data, digest_size=8).hexdigest()}"',
            # So the viewer can caption the preview without a second request.
            "X-Render-Origin": "/".join(str(v) for v in origin),
            "X-Render-Span": f"{span[0]}x{span[1]}",
            "X-Render-Source": source,
            "X-Render-Kind": kind,
            "X-Render-Size": f"{image.width}x{image.height}",
        })

    @routes.get("/pmtiles/{name}/search")
    async def search(request):
        name = request.match_info["name"]
        _archive_for(maps_dir_fn(), name)
        try:
            limit = min(int(request.query.get("limit", 60)), 500)
        except ValueError:
            raise web.HTTPBadRequest(reason="limit must be an integer")
        found = tilestore.search(
            os.path.join(maps_dir_fn(), f"{name}.tiles.db"),
            request.query.get("q", ""), limit)
        if not found:
            found = {"results": [], "truncated": False}
        return web.json_response(found, headers={"Cache-Control": "no-cache"})

    @routes.get("/pmtiles/{name}/changes")
    async def changes(request):
        """Which tiles changed since `since`. Omit `since` to just get the seq."""
        name = request.match_info["name"]
        _archive_for(maps_dir_fn(), name)
        raw = request.query.get("since")
        try:
            since = None if raw in (None, "") else int(raw)
        except ValueError:
            raise web.HTTPBadRequest(reason="since must be an integer")
        seq, dirty, truncated = tilestore.read_changes(
            os.path.join(maps_dir_fn(), f"{name}.tiles.db"), since)
        if seq is None:
            raise web.HTTPNotFound(reason="no change feed for this map (no store)")
        return web.json_response({"seq": seq, "changes": dirty,
                                  "truncated": truncated},
                                 headers={"Cache-Control": "no-cache"})

    @routes.get("/pmtiles/{name}/events")
    async def events(request):
        """The same feed as a server-sent event stream."""
        name = request.match_info["name"]
        _archive_for(maps_dir_fn(), name)
        db_path = os.path.join(maps_dir_fn(), f"{name}.tiles.db")
        raw = request.query.get("since")
        try:
            since = None if raw in (None, "") else int(raw)
        except ValueError:
            raise web.HTTPBadRequest(reason="since must be an integer")

        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",      # in case anything proxies this
        })
        await response.prepare(request)
        seq, _, _ = tilestore.read_changes(db_path, None)
        if since is not None:
            seq = since
        idle = 0
        failures = 0
        try:
            while True:
                new_seq, dirty, truncated = tilestore.read_changes(db_path, seq)
                if new_seq is None:
                    # Usually a transient lock while a batch is writing. Ending
                    # the stream here used to stop updates until the page was
                    # reloaded, so ride it out instead.
                    failures += 1
                    if failures > 60:
                        break
                    await asyncio.sleep(1)
                    continue
                failures = 0
                if dirty:
                    await response.write(
                        b"data: " + json.dumps({
                            "seq": new_seq, "changes": dirty,
                            "truncated": truncated,
                        }).encode() + b"\n\n")
                    seq, idle = new_seq, 0
                else:
                    seq = new_seq
                    idle += 1
                    if idle >= 15:          # comment frame: detects a dead peer
                        await response.write(b": ping\n\n")
                        idle = 0
                await asyncio.sleep(1)
        except (ConnectionResetError, asyncio.CancelledError):
            pass                            # the tab went away
        return response

    return routes


def register():
    """Attach to ComfyUI's server if we are running inside it."""
    try:
        from server import PromptServer
        import folder_paths
    except ImportError:
        return False
    instance = getattr(PromptServer, "instance", None)
    if instance is None:
        # Headless import (tools/make_workflow.load_node_defs) -- node classes
        # still register, we just have no server to hang routes on.
        return False
    if instance.app.router.frozen:
        print("[pmtiles-map] router already frozen; viewer routes not registered")
        return False

    def maps_dir():
        return os.path.join(folder_paths.get_output_directory(), "maps")

    # Straight onto the app rather than into instance.routes: custom-node import
    # happens before ComfyUI adds its catch-all web.static('/', ...)
    # (ComfyUI/server.py:1279), and aiohttp resolves in registration order, so
    # these win over the static handler.
    instance.app.add_routes(build_routes(maps_dir))
    print("[pmtiles-map] viewer at /pmtiles/")
    return True

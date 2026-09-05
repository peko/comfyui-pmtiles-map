"""HTTP endpoints for the map viewer.

The same route table serves two hosts: ComfyUI's own aiohttp app (registered
from __init__.py) and tools/serve_pmtiles.py for viewing with ComfyUI stopped.
Hence `build_routes(maps_dir_fn)` rather than module-level decorators.

Tiles are extracted from the archive *server-side*, so the browser fetches plain
image URLs -- no client-side range reads, no pmtiles.js, and nothing to go wrong
if a host mishandles `Range`.  `/map/{name}/file` still hands out the raw
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
# One build job per map, in-process: state for the viewer to poll.
_JOBS = {}


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


def _pick_source(maps_dir, name, requested=None):
    """Which backing store to read tiles from.

    The live viewer reads the SQLite store, so a run never has to serialize the
    archive just to be watched -- and it is faster besides (0.36 ms vs 2.11 ms
    per tile, same bytes). The archive is for what will be uploaded: reading it
    is how you check the file itself. Auto = store when there is one.
    """
    has_store = os.path.isfile(os.path.join(maps_dir, f"{name}.tiles.db"))
    has_archive = os.path.isfile(os.path.join(maps_dir, f"{name}.pmtiles"))
    if requested == "store" and has_store:
        return "store"
    if requested == "archive" and has_archive:
        return "archive"
    if requested in ("store", "archive"):
        raise web.HTTPNotFound(reason=f"{name} has no {requested}")
    if has_store:
        return "store"
    if has_archive:
        return "archive"
    raise web.HTTPNotFound(reason=f"no such map: {name}")


def _store_tile(db_path, z, x, y):
    """Tile bytes from the store, encoding to WebP the first time one is asked for.

    The encode uses the *map's own* quality, recorded by the saver, so a tile the
    viewer warms is exactly the one build_archive will reuse instead of redoing.
    """
    data, needs_encode = tilestore.read_tile_for_serving(db_path, z, x, y)
    if data is None:
        return None
    if not needs_encode:
        return data
    quality = int(tilestore.read_map_meta(db_path, "webp_quality", 80) or 80)
    blob = archive.encode_webp(tilestore.open_png(data), quality=quality, method=4)
    tilestore.write_webp_cache(db_path, z, x, y, blob, quality)
    return blob


def _require_map(maps_dir, name):
    """404 only when a map exists in neither form; returns the archive path or None.

    Everything except /file works off the store when there is one -- tiles,
    metadata, search, the change feed, the render preview. Gating those on the
    .pmtiles made a map with `write_archive` off answer 404 to its own live feed,
    which is precisely the workflow the store-backed source exists for.
    """
    if not _SAFE_NAME.match(name or "") or name.startswith("."):
        raise web.HTTPBadRequest(reason="bad map name")
    pmt = os.path.join(maps_dir, f"{name}.pmtiles")
    if os.path.isfile(pmt):
        return pmt
    if os.path.isfile(os.path.join(maps_dir, f"{name}.tiles.db")):
        return None
    raise web.HTTPNotFound(reason=f"no such map: {name}")


def _archive_for(maps_dir, name):
    if not _SAFE_NAME.match(name or "") or name.startswith("."):
        raise web.HTTPBadRequest(reason="bad map name")
    path = os.path.join(maps_dir, f"{name}.pmtiles")
    if not os.path.isfile(path):
        raise web.HTTPNotFound(reason=f"no such map: {name}")
    return path


def build_routes(maps_dir_fn):
    routes = web.RouteTableDef()

    @routes.get("/map")
    async def index_redirect(request):
        raise web.HTTPFound("/map/")

    @routes.get("/map/")
    async def index(request):
        return web.FileResponse(
            os.path.join(WEB_DIR, "index.html"),
            headers={"Content-Type": _CONTENT_TYPES[".html"],
                     "Cache-Control": "no-cache"},
        )

    @routes.get("/map/assets/{path:.*}")
    async def asset(request):
        rel = request.match_info["path"]
        full = os.path.realpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) \
                or not os.path.isfile(full):
            raise web.HTTPNotFound()
        ctype = _CONTENT_TYPES.get(os.path.splitext(full)[1].lower(),
                                   "application/octet-stream")
        return web.FileResponse(full, headers={"Content-Type": ctype})

    @routes.get("/map/list")
    async def maps(request):
        return web.json_response({"maps": archive.list_archives(maps_dir_fn())})

    @routes.get("/map/{name}/meta.json")
    async def meta(request):
        name = request.match_info["name"]
        maps = maps_dir_fn()
        if not _SAFE_NAME.match(name or "") or name.startswith("."):
            raise web.HTTPBadRequest(reason="bad map name")
        path = os.path.join(maps, f"{name}.pmtiles")
        if not os.path.isfile(path):
            # Store-only: describable, servable, buildable -- see store_info.
            payload = archive.store_info(os.path.join(maps, f"{name}.tiles.db"))
            if payload is None:
                raise web.HTTPNotFound(reason=f"no such map: {name}")
            return web.json_response(payload, headers={"Cache-Control": "no-cache"})
        payload = archive.archive_info(path)
        payload["built"] = True
        # Deliberately NOT the per-tile records: on a 5000-tile map that blob is
        # megabytes, and the viewer only ever needs the tile you clicked. Ask
        # /tilemeta/{z}/{x}/{y} for those.
        payload["has_store"] = os.path.isfile(
            os.path.join(maps_dir_fn(), f"{name}.tiles.db"))
        return web.json_response(payload, headers={"Cache-Control": "no-cache"})

    @routes.get("/map/{name}/tilemeta/{z}/{x}/{y}")
    async def tilemeta(request):
        name = request.match_info["name"]
        maps = maps_dir_fn()
        path = _require_map(maps, name)
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
        if found is None and path:
            found = archive.open_archive(path).metadata.get(
                "tiles", {}).get(f"{z}/{x}/{y}")
            source = "archive"
        if found is None:
            raise web.HTTPNotFound(reason=f"no metadata for {z}/{x}/{y}")
        return web.json_response({"coord": [z, x, y], "source": source,
                                  "meta": found},
                                 headers={"Cache-Control": "no-cache"})

    @routes.get("/map/{name}/file")
    async def raw(request):
        path = _archive_for(maps_dir_fn(), request.match_info["name"])
        return web.FileResponse(path, headers={
            "Content-Type": "application/octet-stream",
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
        })

    @routes.get("/map/{name}/tiles/{z}/{x}/{y}.webp")
    async def tile(request):
        name = request.match_info["name"]
        maps = maps_dir_fn()
        if not _SAFE_NAME.match(name or "") or name.startswith("."):
            raise web.HTTPBadRequest(reason="bad map name")
        source = _pick_source(maps, name, request.query.get("source"))
        try:
            z = int(request.match_info["z"])
            x = int(request.match_info["x"])
            y = int(request.match_info["y"])
        except ValueError:
            raise web.HTTPBadRequest(reason="z/x/y must be integers")
        if source == "store":
            data = _store_tile(os.path.join(maps, f"{name}.tiles.db"), z, x, y)
        else:
            data = archive.open_archive(
                os.path.join(maps, f"{name}.pmtiles")).get(z, x, y)
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
            "X-Tile-Source": source,
        }
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        return web.Response(body=data, headers=headers)

    @routes.get("/map/{name}/render/{z}/{x}/{y}")
    async def render(request):
        """The original image a tile came from, stitched from its render block."""
        name = request.match_info["name"]
        maps = maps_dir_fn()
        path = _require_map(maps, name)
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
            os.path.join(maps, f"{name}.tiles.db"),
            path or os.path.join(maps, f"{name}.pmtiles"), z, x, y)
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

    @routes.get("/map/{name}/build")
    async def build_status(request):
        # No archive check: a map being built for the first time has none yet.
        return web.json_response(_JOBS.get(request.match_info["name"])
                                 or {"state": "idle"},
                                 headers={"Cache-Control": "no-cache"})

    @routes.post("/map/{name}/build")
    async def build(request):
        """Recompose the pyramid in one pass, then re-serialize the archive.

        Both are O(whole map), so this runs in a worker thread and reports
        progress -- on a 65k-tile map it is minutes, and the event loop still has
        a viewer (and possibly a running ComfyUI) to serve.
        """
        name = request.match_info["name"]
        maps = maps_dir_fn()
        # The *store* is what a build needs; requiring the archive here made a map
        # that has never been serialized (write_archive off from the first save)
        # impossible to build from the viewer.
        if not _SAFE_NAME.match(name or "") or name.startswith("."):
            raise web.HTTPBadRequest(reason="bad map name")
        if not os.path.isfile(os.path.join(maps, f"{name}.tiles.db")):
            raise web.HTTPNotFound(reason=f"no store for {name}; nothing to build")
        job = _JOBS.get(name)
        if job and job.get("state") == "running":
            raise web.HTTPConflict(reason="a build is already running for this map")

        want_pyramid = request.query.get("pyramid", "1") != "0"
        want_archive = request.query.get("archive", "1") != "0"
        try:
            min_zoom = int(request.query.get("min_zoom", 0))
            quality = int(request.query.get("quality", 80))
        except ValueError:
            raise web.HTTPBadRequest(reason="min_zoom and quality must be integers")

        state = {"state": "running", "phase": "starting", "done": 0, "total": 0,
                 "derived": 0, "seconds": 0.0}
        _JOBS[name] = state

        def work():
            import time as _time
            start = _time.perf_counter()
            db_path = os.path.join(maps, f"{name}.tiles.db")
            with tilestore.TileStore(db_path) as store:
                if want_pyramid:
                    state["phase"] = "pyramid"

                    def on_level(level, done, total):
                        state.update(level=level, done=done, total=total)

                    written = store.rebuild_pyramid(min_zoom, progress=on_level)
                    state["derived"] = len(written)
                    store.db.commit()
                if want_archive:
                    state["phase"] = "archive"
                    info = archive.build_archive(
                        store, os.path.join(maps, f"{name}.pmtiles"),
                        webp_quality=quality, name=name,
                        progress=lambda done, total: state.update(done=done,
                                                                  total=total))
                    state["tiles"] = info["tiles"]
                    state["encoded"] = info["encoded"]
            state["seconds"] = _time.perf_counter() - start
            state["phase"] = "done"
            state["state"] = "done"

        async def run():
            try:
                await asyncio.to_thread(work)
            except Exception as exc:                      # report, never crash
                state.update(state="error", phase="error",
                             error=f"{type(exc).__name__}: {exc}")

        asyncio.create_task(run())
        return web.json_response({"started": True, "map": name,
                                  "pyramid": want_pyramid, "archive": want_archive})

    @routes.get("/map/{name}/search")
    async def search(request):
        name = request.match_info["name"]
        _require_map(maps_dir_fn(), name)
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

    @routes.get("/map/{name}/leaves")
    async def leaves(request):
        """A bitmap of which cells hold a render, for "mark everything else".

        Raw bits rather than JSON: a z=8 map is 8 KB against ~1 MB of
        coordinate triples, and the client wants a lookup table, not a list.
        """
        name = request.match_info["name"]
        _require_map(maps_dir_fn(), name)
        raw = request.query.get("z")
        try:
            zoom = None if raw in (None, "") else int(raw)
        except ValueError:
            raise web.HTTPBadRequest(reason="z must be an integer")
        found = tilestore.read_occupancy(
            os.path.join(maps_dir_fn(), f"{name}.tiles.db"), zoom)
        if found is None:
            raise web.HTTPNotFound(reason="no store to read leaf coverage from")
        z, side, bits = found
        return web.Response(
            body=bits,
            headers={"Content-Type": "application/octet-stream",
                     "X-Zoom": str(z), "X-Side": str(side),
                     "Cache-Control": "no-cache"},
        )

    @routes.get("/map/{name}/changes")
    async def changes(request):
        """Which tiles changed since `since`. Omit `since` to just get the seq."""
        name = request.match_info["name"]
        _require_map(maps_dir_fn(), name)
        raw = request.query.get("since")
        try:
            since = None if raw in (None, "") else int(raw)
        except ValueError:
            raise web.HTTPBadRequest(reason="since must be an integer")
        seq, dirty, truncated = tilestore.read_changes(
            os.path.join(maps_dir_fn(), f"{name}.tiles.db"), since,
            live=request.query.get("live") == "1")
        if seq is None:
            raise web.HTTPNotFound(reason="no change feed for this map (no store)")
        return web.json_response({"seq": seq, "changes": dirty,
                                  "truncated": truncated},
                                 headers={"Cache-Control": "no-cache"})

    @routes.get("/map/{name}/events")
    async def events(request):
        """The same feed as a server-sent event stream."""
        name = request.match_info["name"]
        _require_map(maps_dir_fn(), name)
        db_path = os.path.join(maps_dir_fn(), f"{name}.tiles.db")
        raw = request.query.get("since")
        try:
            since = None if raw in (None, "") else int(raw)
        except ValueError:
            raise web.HTTPBadRequest(reason="since must be an integer")

        live = request.query.get("live") == "1"
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",      # in case anything proxies this
        })
        await response.prepare(request)
        seq, _, _ = tilestore.read_changes(db_path, None, live=live)
        if since is not None:
            seq = since
        idle = 0
        failures = 0
        try:
            while True:
                new_seq, dirty, truncated = tilestore.read_changes(
                    db_path, seq, live=live)
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

    # The viewer used to live under /pmtiles/. Temporary, not permanent: a 308
    # would be cached hard by browsers and painful to undo.
    @routes.get("/pmtiles")
    async def legacy_root(request):
        raise web.HTTPTemporaryRedirect("/map/")

    @routes.get("/pmtiles/{tail:.*}")
    async def legacy(request):
        tail = request.match_info["tail"]
        query = f"?{request.query_string}" if request.query_string else ""
        raise web.HTTPTemporaryRedirect(f"/map/{tail}{query}")

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
    print("[pmtiles-map] viewer at /map/")
    return True

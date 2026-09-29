"""ComfyUI nodes: save a render into a PMTiles map, and report on one.

Modelled on core SaveImage (ComfyUI/nodes.py:1643) for the tensor->PIL
conversion, the hidden prompt inputs and the {"ui": {"images": ...}} return, so
the tile previews in the graph like any other saver.
"""
import json
import os
import random
import re

import numpy as np
from PIL import Image

import folder_paths

try:
    from . import archive, hilbert, imgimport, promptmeta, tilestore
except ImportError:                     # imported off the pack dir by tools/
    import archive
    import hilbert
    import imgimport
    import promptmeta
    import tilestore

MAX_ZOOM = 22                           # 2^22 tiles a side; well under the 31 the format allows
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def maps_dir():
    return os.path.join(folder_paths.get_output_directory(), "maps")


def safe_map_name(name):
    name = _SAFE_NAME.sub("_", (name or "").strip()).strip("._-")
    return name or "results"


def store_path(name):
    return os.path.join(maps_dir(), f"{name}.tiles.db")


def archive_path(name):
    return os.path.join(maps_dir(), f"{name}.pmtiles")


def tensor_to_pil(image):
    arr = 255.0 * image.cpu().numpy()
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 3 and arr.shape[2] == 4:
        return Image.fromarray(arr, "RGBA")
    if arr.ndim == 3 and arr.shape[2] == 1:
        return Image.fromarray(arr[:, :, 0], "L").convert("RGB")
    return Image.fromarray(arr[:, :, :3], "RGB")


class SavePMTilesMap:
    """Write an image into the map's tile store at z/x/y. Leaves only.

    The node does not build the pyramid and does not serialize the archive.
    Both are O(whole map) and neither gets cheaper for being done per render:
    recomposing ancestors costs one pass per level per image and rewrites the
    shallow tiles once per image (65536 times for z=0 on a full z=8 map), and
    the .pmtiles is re-serialized *whole* every time -- 135 MB per render on a
    20k-tile map. Doing either N images at a time only changes how often the
    waste happens.

    So the saver writes leaves, marks the map `pyramid_stale`, and the client
    builds both in one pass when asked: the viewer's build button, or
    `tools/pmtiles_map.py --rebuild-pyramid`. Nothing is lost by waiting -- the
    store is the source of truth and the viewer reads tiles from it, so a render
    is on the map the moment it is saved, archive or no archive.
    """

    def __init__(self):
        self.temp_dir = folder_paths.get_temp_directory()
        self.prefix_append = "_pmtiles_" + "".join(
            random.choice("abcdefghijklmnopqrstupvxyz") for _ in range(5)
        )

    # `"advanced": True` hides an input until the node's Advanced toggle is on,
    # and outlines it differently when shown -- a frontend feature, not ours:
    # `isWidgetVisible()` is `!(collapsed || hidden || advanced && !showAdvanced)`,
    # and `Comfy.Node.AlwaysShowAdvancedWidgets` forces them all open. Used by
    # core (nodes_hooks, nodes_model_advanced, nodes_video_model) too.
    #
    # Basic is what changes per render: which map, where on it, what to call it,
    # and what the node draws in the graph. Everything else describes the *map*
    # -- set once when it is created and then left alone -- or is wired rather
    # than typed (prompt_text/negative_text), so none of it belongs in the way.
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "renders to place on the map"}),
                "map_name": ("STRING", {"default": "results", "tooltip":
                             "output/maps/<name>.pmtiles (+ .tiles.db source of truth)"}),
                # 10 gives a 1024x1024 tile extent. The old default of 4 is 16
                # tiles a side: a 2x3-tile cell overflows it after five rows, and
                # the failure is a ValueError mid-sweep rather than anything the
                # graph could have warned about. Depth is free here -- empty
                # levels cost nothing, and the pyramid is built once at the end.
                "z": ("INT", {"default": 10, "min": 0, "max": MAX_ZOOM, "advanced": True,
                      "tooltip":
                      "zoom level the render is placed on; the map is 2^z tiles a "
                      "side, so a block at x,y must satisfy x+nx <= 2^z. Coarser "
                      "levels are derived later, by the viewer's build button"}),
                "x": ("INT", {"default": 0, "min": 0, "max": (1 << MAX_ZOOM) - 1}),
                "y": ("INT", {"default": 0, "min": 0, "max": (1 << MAX_ZOOM) - 1}),
                "placement": (["slice", "single_tile"], {"default": "slice",
                              "advanced": True,
                              "tooltip": "slice: cut the render into a tile grid at "
                                         "native pixels. single_tile: resize the whole "
                                         "render into one tile."}),
                "coords_mode": (["manual", "auto_grid", "hilbert"],
                                {"default": "manual", "advanced": True,
                                 "tooltip":
                                 "manual: place at x/y. auto_grid: ignore x/y and "
                                 "take the next free block, scanning in growing "
                                 "squares. hilbert: the same, but walking the "
                                 "Hilbert curve -- neighbours on the map are then "
                                 "also neighbours inside the .pmtiles, which is the "
                                 "order the format itself stores tiles in. Both "
                                 "automatic modes keep no counter; they look at "
                                 "what the map already holds."}),
                "tile_size": (["256", "512"], {"default": "256", "advanced": True}),
                "store_format": (list(tilestore.STORE_FORMATS), {"default":
                                 tilestore.WEBP_LOSSY, "advanced": True,
                                 "tooltip":
                                 "how tiles are kept in the .tiles.db behind the "
                                 "archive. Measured on 512 px render tiles: png "
                                 "341 KB (17 ms), webp_lossless 238 KB (84 ms) and "
                                 "BIT-IDENTICAL, webp_lossy 34 KB at q80 -- a tenth "
                                 "of png, which is why it is the default: a big map "
                                 "is mostly store. Lossy costs ~3.5 dB PSNR per "
                                 "pyramid level, because a parent is recomposed from "
                                 "its children and so encodes an already-encoded "
                                 "image; re-saving the same tile does NOT add loss. "
                                 "webp_lossless when the store has to be exact."}),
                "store_quality": ("INT", {"default": 80, "min": 1, "max": 100,
                                  "advanced": True,
                                  "tooltip": "quality for store_format=webp_lossy "
                                             "only; ignored otherwise. Serving reads "
                                             "these bytes straight through, so this "
                                             "is the quality you actually look at -- "
                                             "and the archive build re-encodes at the "
                                             "same number, so it is the map's one "
                                             "quality setting rather than the first "
                                             "of two."}),
                "store_full_prompt": ("BOOLEAN", {"default": False, "advanced": True,
                                      "tooltip":
                                      "keep the whole graph per tile in the SQLite store"}),
                "title": ("STRING", {"default": ""}),
                "tags": ("STRING", {"default": "", "advanced": True}),
            },
            # Optional, not required: appending a *required* input would break
            # every already-saved API prompt ("Required input is missing"), and
            # optional inputs still render as widgets and can still be wired.
            #
            # Anything that belongs *beside* a required widget has to be required
            # too: ComfyUI renders required inputs before optional ones, so an
            # optional input can only ever land at the end of the node. That is
            # why store_format/store_quality sit up with the image settings and
            # not here. Moving an input across that boundary shifts every later
            # value in a saved graph's positional `widgets_values` --
            # tools/migrate_widgets.py rewrites them by name when it happens.
            #
            # prompt_text/negative_text are single-line on purpose: the frontend's
            # addMultilineWidget hardcodes `minNodeSize = [400, 200]` and exposes
            # no height option, so one multiline field sets the whole node's
            # minimum size. Both are meant to be *wired* from whatever builds the
            # string, and a wired widget draws no editor at all.
            "optional": {
                "prompt_text": ("STRING", {"default": "", "multiline": False,
                                "advanced": True,
                                "tooltip": "the prompt to record, when the graph "
                                           "builds it at runtime (FormattedString, "
                                           "wildcards, a list selector) and it "
                                           "therefore cannot be read off the graph. "
                                           "Wire the same string that feeds "
                                           "CLIPTextEncode.text here."}),
                "negative_text": ("STRING", {"default": "", "multiline": False,
                                  "advanced": True,
                                  "tooltip": "same, for the negative prompt"}),
                # Last on purpose: it is the one widget that changes nothing
                # about the map, only what this node draws in the graph.
                "preview": (["thumbnail", "full", "off"], {"default": "thumbnail",
                            "tooltip": "the image the node shows in the graph is a "
                                       "file in ComfyUI/temp. `full` writes the whole "
                                       "render (~2.8 MB each, measured); `thumbnail` "
                                       "writes a 384 px WebP (~42 KB); `off` writes "
                                       "nothing -- the map itself is the preview."}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "info")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "image/pmtiles"
    DESCRIPTION = ("Saves renders as tiles in the map's store at the given z/x/y. "
                   "Leaves only: the coarser pyramid levels and the .pmtiles "
                   "archive are built in one pass afterwards, from the viewer's "
                   "build button or tools/pmtiles_map.py.")

    def save(self, images, map_name, z, x, y, placement, coords_mode, tile_size,
             store_format, store_quality, store_full_prompt,
             title, tags, preview="thumbnail", prompt_text="", negative_text="",
             prompt=None, extra_pnginfo=None, **_retired):
        # **_retired swallows pyramid_to_zoom / pyramid_mode / pyramid_min_px /
        # archive_every / y_scheme / write_archive / embed_tile_metadata /
        # webp_quality.
        # ComfyUI only passes declared inputs, so this is for anything calling
        # save() directly -- a graph that still carries the old widgets is not
        # this function's problem.
        name = safe_map_name(map_name)
        ts = int(tile_size)
        os.makedirs(maps_dir(), exist_ok=True)

        lines = []
        placed = []
        summary = promptmeta.summarize_prompt(
            prompt, extra_pnginfo,
            extra={"title": title.strip(), "tags": tags.strip()},
        )
        # An explicitly supplied prompt wins: it is the runtime value, whereas
        # anything read off the graph is at best the literal that was typed there.
        if prompt_text.strip():
            summary["prompt"] = prompt_text.strip()
            summary.pop("prompt_note", None)
        if negative_text.strip():
            summary["negative"] = negative_text.strip()

        with tilestore.TileStore(store_path(name), ts,
                                 store_format=store_format or None,
                                 store_quality=store_quality or None) as store:
            for index, image in enumerate(images):
                pil = tensor_to_pil(image)
                tiles, nx, ny, how = self._cut(pil, ts, placement)

                if coords_mode != "manual" or index > 0:
                    # index > 0: manual coordinates describe one spot, so the rest
                    # of a batch would overwrite each other. Advance instead --
                    # along whichever traversal the mode asked for, and along the
                    # shells when the mode had no opinion because it is `manual`.
                    place = (store.next_free_hilbert_block
                             if coords_mode == "hilbert" else store.next_free_block)
                    x0, y0 = place(z, nx, ny)
                    if coords_mode == "manual" and index > 0:
                        lines.append(f"image {index}: manual x/y already used by "
                                     f"image 0, auto-placed at {x0},{y0}")
                else:
                    # XYZ: y counts from the top, and a multi-tile block is
                    # addressed by its top-left corner. TMS (y from the bottom)
                    # used to be selectable here and was never once selected.
                    x0, y0 = int(x), int(y)

                if x0 + nx > (1 << z) or y0 + ny > (1 << z) or min(x0, y0) < 0:
                    raise ValueError(
                        f"a {nx}x{ny} tile block at {x0},{y0} does not fit inside "
                        f"the {1 << z}x{1 << z} extent of z={z}"
                    )

                for (ix, iy), tile in tiles:
                    meta = dict(summary)
                    meta.update({
                        "kind": tilestore.LEAF,
                        "batch_index": index,
                        "source_size": [pil.width, pil.height],
                        "placement": how,
                    })
                    if nx * ny > 1:
                        meta["grid"] = [ix, iy, nx, ny]
                    if store_full_prompt and prompt is not None:
                        meta["full_prompt"] = prompt
                    store.put_tile(z, x0 + ix, y0 + iy, tile,
                                   kind=tilestore.LEAF, meta=meta)
                    placed.append((z, x0 + ix, y0 + iy))
                lines.append(f"image {index}: {how} -> {nx}x{ny} tile(s) at "
                             f"z={z} x={x0} y={y0}")

            # Leaves only, always -- the coarse levels and the archive are both
            # O(whole map) and belong to the build step, never to a render.
            # The flag is what carries that: the viewer reads it to light its
            # build button, so a map missing its pyramid cannot look complete.
            # It is not logged. Announcing "not built" every render would imply
            # the node had a choice, and this node has never had one.
            store.set_map_meta("pyramid_stale", "1")
            # No separate archive quality is recorded: TileStore already stamps
            # `store_quality`, and the archive build and the on-demand encode
            # both read that one number, so they fill the same cache.
            # The shape of one render, in tiles. The viewer needs it to know
            # which tiles form one image; deriving that per tile from `grid`
            # costs a request per render hovered -- unaffordable against a busy
            # server, and unnecessary, since the shape is a property of the map
            # rather than of the tile.
            store.set_map_meta("render_block", f"{nx}x{ny}")
            # Counts how far the archive has fallen behind; the viewer shows it
            # on the build button. Not logged for the same reason as above --
            # the archive is re-serialized whole (135 MB per render on a 20k
            # tile map), so building it here was never on the table.
            store.bump_pending(len(placed))
            store.db.commit()

        text = "\n".join(lines)
        print(f"[pmtiles-map] {name}\n  " + "\n  ".join(lines))
        return {
            "ui": {"images": self._previews(images, preview), "text": [text]},
            "result": (images, text),
        }

    def _cut(self, pil, ts, placement):
        """-> ([((ix, iy), image)], nx, ny, description)"""
        if placement == "slice" and pil.width % ts == 0 and pil.height % ts == 0 \
                and pil.width >= ts and pil.height >= ts:
            nx, ny = pil.width // ts, pil.height // ts
            tiles = [((ix, iy), pil.crop((ix * ts, iy * ts,
                                          (ix + 1) * ts, (iy + 1) * ts)))
                     for iy in range(ny) for ix in range(nx)]
            return tiles, nx, ny, f"slice {pil.width}x{pil.height}"
        how = "resize"
        if placement == "slice":
            # Not a whole number of tiles -- say so rather than silently resizing.
            how = (f"resize (slice needs {pil.width}x{pil.height} divisible "
                   f"by {ts})")
        return [((0, 0), pil.resize((ts, ts), Image.LANCZOS))], 1, 1, how

    PREVIEW_PX = 384

    def _previews(self, images, mode="thumbnail"):
        """The image the node shows in the graph, as a file in ComfyUI/temp.

        ComfyUI renders a node's preview from a file it can fetch through /view,
        so there is no way to show one without writing something. What there is a
        way to avoid is writing the *whole render*: measured 290 KB per preview on
        a real run, for a thumbnail nobody zooms into -- the map is the preview
        that matters. `thumbnail` writes a 384 px WebP instead (~40 KB), `off`
        writes nothing at all.
        """
        if mode == "off":
            return []
        results = []
        try:
            full_output_folder, filename, counter, subfolder, _ = \
                folder_paths.get_save_image_path(
                    "pmtiles" + self.prefix_append, self.temp_dir,
                    images[0].shape[1], images[0].shape[0])
            for index, image in enumerate(images):
                img = tensor_to_pil(image)
                if mode == "thumbnail":
                    img.thumbnail((self.PREVIEW_PX, self.PREVIEW_PX), Image.LANCZOS)
                    file = f"{filename}_{counter + index:05}_.webp"
                    img.save(os.path.join(full_output_folder, file),
                             format="WEBP", quality=85, method=4)
                else:
                    file = f"{filename}_{counter + index:05}_.png"
                    img.save(os.path.join(full_output_folder, file), compress_level=4)
                results.append({"filename": file, "subfolder": subfolder,
                                "type": "temp"})
        except Exception as exc:                # a preview is never worth failing on
            print(f"[pmtiles-map] preview skipped: {type(exc).__name__}: {exc}")
        return results


class HilbertXY:
    """One integer -> tile x/y along a Hilbert curve.

    Wire `x`/`y` into SavePMTilesMap (with coords_mode manual) and drive `index`
    from a counter, and successive renders fill the level compactly instead of
    marching along one row.  The curve is PMTiles' own, so neighbours in index
    are neighbours on the map *and* contiguous in the archive.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "index": ("INT", {"default": 0, "min": 0, "max": 1 << 40,
                                  "tooltip": "position along the curve; wraps at "
                                             "4^order"}),
                "order": ("INT", {"default": 5, "min": 0, "max": MAX_ZOOM,
                                  "tooltip": "grid is 2^order tiles a side -- use the "
                                             "same value as the saver's z"}),
                "block_size": ("INT", {"default": 1, "min": 1, "max": 64,
                                       "tooltip": "tiles per render along x (a 1024px "
                                                  "render at 256px tiles is 4); x/y "
                                                  "are scaled so blocks never "
                                                  "overlap"}),
            },
            "optional": {
                "block_size_y": ("INT", {"default": 0, "min": 0, "max": 64,
                                         "tooltip": "tiles per render along y, when a "
                                                    "render is not square -- 960x1408 "
                                                    "at 512px tiles is 2 wide by 3 "
                                                    "tall. 0 = same as block_size"}),
            },
        }

    RETURN_TYPES = ("INT", "INT", "STRING")
    RETURN_NAMES = ("x", "y", "info")
    FUNCTION = "convert"
    CATEGORY = "image/pmtiles"
    DESCRIPTION = ("Converts an integer index into x/y tile coordinates along a "
                   "Hilbert curve (the same curve PMTiles orders tiles by).")

    def convert(self, index, order, block_size, block_size_y=0):
        # With a block per render, the curve runs over blocks, not tiles, so the
        # usable grid shrinks accordingly -- otherwise blocks would overlap. The
        # steps differ per axis when the render does: the curve still walks a
        # square grid of blocks, each block just covers bw x bh tiles.
        bw = max(1, int(block_size))
        bh = max(1, int(block_size_y) or bw)
        block_order = imgimport.block_grid_order(order, bw, bh)
        bx, by = hilbert.d2xy(block_order, index)
        x, y = bx * bw, by * bh
        side = 1 << block_order
        info = (f"index {index} -> {x},{y} (order {order}, {bw}x{bh} blocks on a "
                f"{side}x{side} block grid spanning {side * bw}x{side * bh} tiles)")
        return (x, y, info)


class PMTilesMapInfo:
    """Report what is currently in a map -- tile counts per zoom, bounds, size."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "map_name": ("STRING", {"default": "results"}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("info",)
    FUNCTION = "info"
    OUTPUT_NODE = True
    CATEGORY = "image/pmtiles"

    def info(self, map_name):
        name = safe_map_name(map_name)
        out = {"map": name}
        db = store_path(name)
        if os.path.exists(db):
            with tilestore.TileStore(db) as store:
                out["store"] = store.stats()
        pmt = archive_path(name)
        if os.path.exists(pmt):
            try:
                out["archive"] = archive.archive_info(pmt)
            except Exception as exc:
                out["archive"] = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            out["archive"] = None
        text = json.dumps(out, indent=2, ensure_ascii=False)
        return {"ui": {"text": [text]}, "result": (text,)}


NODE_CLASS_MAPPINGS = {
    "SavePMTilesMap": SavePMTilesMap,
    "PMTilesMapInfo": PMTilesMapInfo,
    "HilbertXY": HilbertXY,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "SavePMTilesMap": "Save PMTiles Map Tile",
    "PMTilesMapInfo": "PMTiles Map Info",
    "HilbertXY": "Hilbert Index → Tile X/Y",
}

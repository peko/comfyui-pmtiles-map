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
    from . import archive, hilbert, promptmeta, tilestore
except ImportError:                     # imported off the pack dir by tools/
    import archive
    import hilbert
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
    """Write an image into a PMTiles archive at z/x/y and keep the pyramid current."""

    def __init__(self):
        self.temp_dir = folder_paths.get_temp_directory()
        self.prefix_append = "_pmtiles_" + "".join(
            random.choice("abcdefghijklmnopqrstupvxyz") for _ in range(5)
        )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "renders to place on the map"}),
                "map_name": ("STRING", {"default": "results", "tooltip":
                             "output/maps/<name>.pmtiles (+ .tiles.db source of truth)"}),
                "z": ("INT", {"default": 4, "min": 0, "max": MAX_ZOOM, "tooltip":
                      "zoom level the render is placed on; coarser levels are derived"}),
                "x": ("INT", {"default": 0, "min": 0, "max": (1 << MAX_ZOOM) - 1}),
                "y": ("INT", {"default": 0, "min": 0, "max": (1 << MAX_ZOOM) - 1}),
                "placement": (["slice", "single_tile"], {"default": "slice",
                              "tooltip": "slice: cut the render into a tile grid at "
                                         "native pixels. single_tile: resize the whole "
                                         "render into one tile."}),
                "coords_mode": (["manual", "auto_grid"], {"default": "manual",
                                "tooltip": "auto_grid ignores x/y and takes the next "
                                           "free block at zoom z"}),
                "tile_size": (["256", "512"], {"default": "256"}),
                "webp_quality": ("INT", {"default": 80, "min": 1, "max": 100}),
                "pyramid_to_zoom": ("INT", {"default": 0, "min": 0, "max": MAX_ZOOM,
                                    "tooltip": "build derived levels down to this zoom"}),
                "y_scheme": (["xyz", "tms"], {"default": "xyz", "tooltip":
                             "interpretation of the y input; tiles are always stored XYZ"}),
                "write_archive": ("BOOLEAN", {"default": True, "tooltip":
                                  "re-serialize the .pmtiles file after this save"}),
                "embed_tile_metadata": ("BOOLEAN", {"default": True}),
                "store_full_prompt": ("BOOLEAN", {"default": False, "tooltip":
                                      "keep the whole graph per tile in the SQLite store"}),
                "title": ("STRING", {"default": ""}),
                "tags": ("STRING", {"default": ""}),
            },
            # Optional, not required: appending a *required* input would break
            # every already-saved API prompt ("Required input is missing"), and
            # optional inputs still render as widgets and can still be wired.
            #
            # Order within this block is grouped by subject -- the two write knobs
            # first, then the metadata text -- but it cannot be merged with the
            # `write_archive` group above: ComfyUI renders required inputs before
            # optional ones, and `widgets_values` in a saved workflow is
            # positional, so moving anything across that boundary would feed old
            # graphs' values into the wrong fields.
            "optional": {
                "archive_every": ("INT", {"default": 0, "min": 0, "max": 100000,
                                  "tooltip": "batch the archive rewrite: serialize "
                                             "only once at least this many tiles are "
                                             "waiting. 0 = every save (fine for a "
                                             "small map). The .pmtiles is rewritten "
                                             "whole every time -- 135 MB per render on "
                                             "a 20k-tile map -- while the store costs "
                                             "~40 KB, so this is the knob that saves "
                                             "the disk."}),
                "preview": (["thumbnail", "full", "off"], {"default": "thumbnail",
                            "tooltip": "the image the node shows in the graph is a "
                                       "file in ComfyUI/temp. `full` writes the whole "
                                       "render (~2.8 MB each, measured); `thumbnail` "
                                       "writes a 384 px WebP (~42 KB); `off` writes "
                                       "nothing -- the map itself is the preview."}),
                "prompt_text": ("STRING", {"default": "", "multiline": True,
                                "tooltip": "the prompt to record, when the graph "
                                           "builds it at runtime (FormattedString, "
                                           "wildcards, a list selector) and it "
                                           "therefore cannot be read off the graph. "
                                           "Wire the same string that feeds "
                                           "CLIPTextEncode.text here."}),
                "negative_text": ("STRING", {"default": "", "multiline": True,
                                  "tooltip": "same, for the negative prompt"}),
                "store_format": (list(tilestore.STORE_FORMATS), {"default":
                                 tilestore.PNG, "tooltip":
                                 "how tiles are kept in the .tiles.db behind the "
                                 "archive. Measured on 512 px render tiles: png "
                                 "341 KB (17 ms), webp_lossless 238 KB (84 ms) and "
                                 "BIT-IDENTICAL, webp_lossy 34 KB at q80. Lossy "
                                 "costs ~3.5 dB PSNR per pyramid level, because a "
                                 "parent is recomposed from its children and so "
                                 "encodes an already-encoded image; re-saving the "
                                 "same tile does NOT add loss. webp_lossless is the "
                                 "free win -- a third smaller for the same pixels."}),
                "store_quality": ("INT", {"default": 92, "min": 1, "max": 100,
                                  "tooltip": "quality for store_format=webp_lossy "
                                             "only; ignored otherwise. This is the "
                                             "source of truth, so keep it well above "
                                             "the archive's webp_quality."}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "info")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "image/pmtiles"
    DESCRIPTION = ("Saves renders as WebP tiles in a PMTiles v3 archive at the given "
                   "z/x/y, rebuilding the coarser pyramid levels down to z=0.")

    def save(self, images, map_name, z, x, y, placement, coords_mode, tile_size,
             webp_quality, pyramid_to_zoom, y_scheme, write_archive,
             embed_tile_metadata, store_full_prompt, title, tags,
             archive_every=0, preview="thumbnail", prompt_text="", negative_text="",
             store_format="", store_quality=0,
             prompt=None, extra_pnginfo=None):
        name = safe_map_name(map_name)
        ts = int(tile_size)
        pyramid_to_zoom = min(int(pyramid_to_zoom), int(z))
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

                if coords_mode == "auto_grid" or index > 0:
                    # index > 0: manual coordinates describe one spot, so the rest
                    # of a batch would overwrite each other. Advance instead.
                    x0, y0 = store.next_free_block(z, nx, ny)
                    if coords_mode == "manual" and index > 0:
                        lines.append(f"image {index}: manual x/y already used by "
                                     f"image 0, auto-placed at {x0},{y0}")
                else:
                    x0, y0 = int(x), int(y)
                    if y_scheme == "tms":
                        y0 = (1 << z) - 1 - y0 - (ny - 1)

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

            derived = store.recompose_ancestors(placed, min_zoom=pyramid_to_zoom)
            if pyramid_to_zoom >= z:
                # Nothing was recomposed: the coarse levels are missing, and the
                # viewer should offer to build them rather than pretend the map is
                # complete. Doing it per save costs `depth` recompositions per
                # render and rewrites the shallow tiles once per render -- 65536
                # times for z=0 on a full z=8 map.
                store.set_map_meta("pyramid_stale", "1")
                lines.append("pyramid: skipped (build it from the viewer or "
                             "tools/pmtiles_map.py --rebuild-pyramid)")
            else:
                lines.append(f"pyramid: {len(derived)} derived tile(s) down to "
                             f"z={pyramid_to_zoom}")
            # So a tile the viewer encodes on demand matches what the archive
            # build wants, instead of the two overwriting each other's cache.
            store.set_map_meta("webp_quality", int(webp_quality))
            pending = store.bump_pending(len(placed) + len(derived))
            store.db.commit()
            stats = store.stats()

            # The store costs ~40 KB of writes per save regardless of map size
            # (SQLite writes changed pages), but the archive is rewritten *whole*
            # -- 135 MB per render on a 20k-tile map. So batch that one.
            threshold = max(0, int(archive_every))
            due = threshold == 0 or pending >= threshold
            if write_archive and not due:
                lines.append(f"archive: deferred, {pending} tile(s) pending "
                             f"(archive_every={threshold})")
            if write_archive and due:
                info = archive.build_archive(
                    store, archive_path(name),
                    webp_quality=webp_quality,
                    embed_tile_metadata=embed_tile_metadata,
                    name=name,
                )
                lines.append(
                    "archive: {tiles} tiles ({unique} unique), {mb:.2f} MB, "
                    "z{minz}-{maxz}, {secs:.2f}s".format(
                        tiles=info["tiles"], unique=info["unique_tiles"],
                        mb=info["file_bytes"] / 1e6, minz=info["min_zoom"],
                        maxz=info["max_zoom"], secs=info["seconds"])
                )
            elif not write_archive:
                lines.append(f"archive: off, {pending} tile(s) pending of "
                             f"{stats['tiles']} in the store — build from the viewer")

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
                                       "tooltip": "tiles per render (a 1024px render "
                                                  "at 256px tiles is 4); x/y are "
                                                  "scaled so blocks never overlap"}),
            }
        }

    RETURN_TYPES = ("INT", "INT", "STRING")
    RETURN_NAMES = ("x", "y", "info")
    FUNCTION = "convert"
    CATEGORY = "image/pmtiles"
    DESCRIPTION = ("Converts an integer index into x/y tile coordinates along a "
                   "Hilbert curve (the same curve PMTiles orders tiles by).")

    def convert(self, index, order, block_size):
        # With a block per render, the curve runs over blocks, not tiles, so the
        # usable grid shrinks accordingly -- otherwise blocks would overlap.
        side_blocks = max(1, (1 << order) // block_size)
        block_order = max(0, side_blocks.bit_length() - 1)
        bx, by = hilbert.d2xy(block_order, index)
        x, y = bx * block_size, by * block_size
        info = (f"index {index} -> {x},{y} (order {order}, {block_size}x{block_size} "
                f"blocks on a {1 << block_order}x{1 << block_order} block grid)")
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

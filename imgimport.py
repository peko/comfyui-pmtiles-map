"""Geometry for importing ready-made images into a map.

The saver node slices a render that is an exact multiple of the tile size
(`SavePMTilesMap._cut`).  A folder of existing images is not that: a 960x1408
JPEG is a multiple of nothing useful, and squashing it into one square tile --
which is what `_cut` falls back to -- destroys the aspect ratio.

So the unit here is a **block**: the smallest nx x ny group of tiles that holds
one image, with the image centred in it and the remainder padded.  Blocks are
laid out along the Hilbert curve, one block per index, which is the same trick
the `HilbertXY` node uses except that nx and ny may differ -- the curve runs
over blocks, the block is a rectangle of tiles, and blocks tile the plane
without ever overlapping.

Pure geometry and PIL on purpose: no SQLite, no ComfyUI, so the test suite can
exercise it without a store.
"""

import os
import re

import hilbert

from PIL import Image

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".avif")

# Keys kept on a tile that is not its render's origin.  build_archive refuses to
# embed per-tile metadata at all once the JSON passes its budget (8 MB), so a
# full ~700-byte record on all six tiles of 3249 renders -- 13 MB -- would cost
# the archive *every* record rather than the redundant five sixths.  search() and
# pmtiles_export only ever read the origin tile, so the rest can be a stub.
SLIM_KEYS = ("kind", "grid", "title", "tags", "source_path")


def block_tiles(width, height, tile_size):
    """Tiles needed to hold one image: (ceil(w/ts), ceil(h/ts)), at least 1x1."""
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")
    nx = max(1, -(-int(width) // int(tile_size)))
    ny = max(1, -(-int(height) // int(tile_size)))
    return nx, ny


def paste_offset(width, height, tile_size, nx, ny):
    """Top-left corner that centres a w x h image in an nx x ny block."""
    return ((nx * tile_size - width) // 2, (ny * tile_size - height) // 2)


def fit_into_block(img, tile_size, nx, ny):
    """Downscale `img` to fit inside the block, preserving aspect.  No-op if it
    already fits -- an import must never quietly resample the common case."""
    bw, bh = nx * tile_size, ny * tile_size
    if img.width <= bw and img.height <= bh:
        return img, False
    scale = min(bw / img.width, bh / img.height)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.LANCZOS), True


def cut_padded(img, tile_size, nx, ny, pad_color=(0, 0, 0, 0)):
    """One image -> the nx*ny tiles of its block.

    Returns ``(tiles, offset, resized)`` where `tiles` is
    ``[((ix, iy), tile_image), ...]`` in row-major order, `offset` is where the
    image was pasted, and `resized` says whether it had to be scaled down to
    fit.  The canvas is RGBA so the padding can be transparent, which is what
    the rest of the map already uses for a hole.
    """
    img, resized = fit_into_block(img, tile_size, nx, ny)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    off = paste_offset(img.width, img.height, tile_size, nx, ny)
    canvas = Image.new("RGBA", (nx * tile_size, ny * tile_size), tuple(pad_color))
    canvas.paste(img, off)
    tiles = [
        ((ix, iy), canvas.crop((ix * tile_size, iy * tile_size,
                                (ix + 1) * tile_size, (iy + 1) * tile_size)))
        for iy in range(ny)
        for ix in range(nx)
    ]
    return tiles, off, resized


def block_order_for(n_blocks):
    """Smallest Hilbert order whose 4**order cells hold `n_blocks`."""
    n = max(1, int(n_blocks))
    order = 0
    while (1 << (2 * order)) < n:
        order += 1
    return order


def block_grid_order(order, nx, ny):
    """Largest square block grid that fits nx x ny blocks inside 2^order tiles.

    The inverse of `zoom_for`: there the block count is known and the zoom
    follows, here the zoom is fixed (it is the saver's `z`) and the question is
    how many blocks fit.  The grid is square because the Hilbert curve is, and
    the tighter axis decides -- 2x3 blocks on a z=8 level fit 128 across but
    only 85 down, so 64x64 blocks it is, spanning 128x192 tiles.
    """
    side = 1 << int(order)
    fit = min(side // max(1, int(nx)), side // max(1, int(ny)))
    return max(0, max(1, fit).bit_length() - 1)


def zoom_for(block_order, nx, ny):
    """Smallest zoom whose 2**z x 2**z tile grid holds the whole block grid.

    A block grid of 2**order blocks a side spans ``2**order * nx`` tiles across
    and ``2**order * ny`` down; the map is square, so the taller side decides.
    """
    side = (1 << block_order) * max(int(nx), int(ny))
    z = 0
    while (1 << z) < side:
        z += 1
    return z


def block_origin(block_order, index, nx, ny):
    """Index -> the top-left tile of its block.

    `hilbert.d2xy` is PMTiles' own curve, so consecutive indices are adjacent on
    the map *and* contiguous in the archive; scaling by (nx, ny) keeps that
    while letting the step differ per axis.

    Note `d2xy` **wraps** (`index %= side*side`), so an order too small for the
    image count silently stacks two images on one block instead of raising.
    `plan()` is what stops that happening; do not bypass it.
    """
    if index >= (1 << (2 * block_order)):
        raise ValueError(
            f"index {index} is past the {1 << (2 * block_order)} cells of "
            f"order {block_order}; d2xy would wrap and overwrite an earlier image"
        )
    bx, by = hilbert.d2xy(block_order, index)
    return bx * int(nx), by * int(ny)


def plan(n_images, width, height, tile_size, tiles_per_image=None, zoom=None):
    """Everything the placement needs, decided once, up front.

    Returns a dict with nx, ny, block_order, z, and the extent in tiles, so the
    caller can print it and stop before writing gigabytes.
    """
    if tiles_per_image is not None:
        nx, ny = tiles_per_image
    else:
        nx, ny = block_tiles(width, height, tile_size)
    order = block_order_for(n_images)
    z = zoom_for(order, nx, ny) if zoom is None else int(zoom)
    side = 1 << order
    extent = (side * nx, side * ny)
    if max(extent) > (1 << z):
        raise ValueError(
            f"z={z} holds {1 << z} tiles a side but the layout needs "
            f"{max(extent)} ({side} blocks of {nx}x{ny})"
        )
    return {
        "images": int(n_images),
        "nx": nx,
        "ny": ny,
        "tile_size": int(tile_size),
        "block_order": order,
        "blocks_side": side,
        "z": z,
        "extent": extent,
        "leaves": int(n_images) * nx * ny,
        "block_px": (nx * tile_size, ny * tile_size),
    }


def iter_images(root, exts=IMAGE_EXTS):
    """Every image under `root`, as paths relative to it, POSIX-separated."""
    lower = tuple(e.lower() for e in exts)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.lower().endswith(lower):
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                out.append(rel.replace(os.sep, "/"))
    return out


# ------------------------------------------------------------------ workers
#
# These are module-level on purpose: the pool is started with "forkserver" (or
# "spawn"), never "fork", so a worker cannot inherit the parent's open SQLite
# connection and WAL descriptors -- and that requires the callable to be
# importable rather than defined in a script's __main__.


def render_job(job):
    """One source image -> its already-encoded tiles.  Runs in a pool process.

    Everything expensive happens here: decode, pad, crop, the store encode and
    (when `webp_quality` is set) the archive's WebP encode, which at 345 ms a
    512 px tile is the single largest cost of a bulk import.  Only bytes go back
    to the parent, which owns the one SQLite connection and does nothing but
    insert.
    """
    import tilestore

    index = job["index"]
    try:
        with Image.open(job["path"]) as src:
            src.load()
            source_size = [src.width, src.height]
            tiles, off, resized = cut_padded(
                src, job["tile_size"], job["nx"], job["ny"], job["pad_color"]
            )
    except Exception as exc:  # a broken file must not take the whole run down
        return {"index": index, "ok": False,
                "error": f"{type(exc).__name__}: {exc}", "tiles": []}

    nx, ny, ts = job["nx"], job["ny"], job["tile_size"]
    base = dict(job["base_meta"])
    base["kind"] = tilestore.LEAF
    base["source_size"] = source_size
    base["placement"] = (
        f"{'contain' if resized else 'pad'} {source_size[0]}x{source_size[1]}"
        f" -> {nx}x{ny} @ {ts} (+{off[0]},+{off[1]})"
    )

    quality = job["webp_quality"]
    if quality is not None:
        import archive as _archive

    out = []
    for (ix, iy), tile in tiles:
        origin = ix == 0 and iy == 0
        if origin or job["tile_meta"] == "full":
            meta = dict(base)
        else:
            meta = {k: base[k] for k in SLIM_KEYS if k in base}
            meta["kind"] = tilestore.LEAF
        meta["grid"] = [ix, iy, nx, ny]
        blob = tilestore.encode_tile(tile, job["store_format"], job["store_quality"])
        webp = None
        if quality is not None:
            # Exactly what build_archive would encode: method=6, and the XMP
            # packet is attached because a leaf is never DERIVED.
            webp = _archive.encode_webp(tile, quality, xmp=_archive.xmp_packet(meta))
        out.append((job["x0"] + ix, job["y0"] + iy, blob,
                    tile.width, tile.height, meta, webp))
    return {"index": index, "ok": True, "error": None, "tiles": out}


def webp_job(spec):
    """(db_path, z, x, y, quality) -> (z, x, y, blob) for the archive cache.

    The worker reads the tile out of the store itself rather than being handed
    pixels: a stored 512 px tile is tens to hundreds of KB and the encode
    returns ~27 KB, so pulling is far cheaper than pushing.  WAL makes the
    concurrent readers free -- provided the parent committed first.

    Used only for `derived` tiles; a leaf's WebP is produced by `render_job`
    while the image is already decoded.
    """
    import archive as _archive
    import tilestore

    db_path, z, x, y, quality = spec
    with tilestore.reading(db_path) as db:
        if db is None:
            return None
        row = db.execute(
            "SELECT png FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)
        ).fetchone()
    if row is None:
        return None
    img = tilestore.open_png(row[0])
    # DERIVED tiles carry no XMP -- build_archive:158-162 makes the same call.
    return (z, x, y, _archive.encode_webp(img, quality, xmp=None))


# -------------------------------------------------------- readme / prompts
#
# The dataset this was written for ships a readme.txt holding the real prompt
# per collection, with the artist as the literal token `xxx`.  Recovering it is
# what makes the map searchable by anything other than the filename.

_RULE = re.compile(r"^=+\s*$", re.M)
_SETTINGS_START = re.compile(r"^\s*Steps:", re.M)


def parse_a1111_settings(text):
    """"Steps: 17, Sampler: ..., Size: 960x1408, Model: X" -> a meta dict.

    Key names are promptmeta's (`steps`, `cfg`, `seed`, `sampler_name`,
    `models`, `latent_size`), so the viewer's field table and pmtiles_export
    pick them up with no special-casing.  Best-effort throughout: an unfamiliar
    settings line yields fewer keys, never an exception.
    """
    flat = " ".join(line.strip() for line in text.splitlines() if line.strip())
    fields = {}
    for part in flat.split(","):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        fields[key.strip().lower()] = value.strip()
    out = {}
    for src, dst, cast in (
        ("steps", "steps", int),
        ("cfg scale", "cfg", float),
        ("seed", "seed", int),
        ("clip skip", "clip_skip", int),
    ):
        if src in fields:
            try:
                out[dst] = cast(fields[src])
            except ValueError:
                pass
    if isinstance(out.get("cfg"), float) and out["cfg"].is_integer():
        out["cfg"] = int(out["cfg"])
    if "sampler" in fields:
        out["sampler_name"] = fields["sampler"]
    if "model" in fields:
        out["models"] = [fields["model"]]
    if "size" in fields:
        m = re.match(r"(\d+)\s*x\s*(\d+)", fields["size"])
        if m:
            out["latent_size"] = [int(m.group(1)), int(m.group(2))]
    return out


def parse_readme_templates(text):
    """`prompt <Name> Sample` sections of a readme -> {name.lower(): {...}}.

    Each entry holds `prompt`, `negative` and the parsed `settings`.  Sections
    are delimited by rules of '='; anything that does not look like a prompt
    section (the readme's `meta` block, for one) is ignored.
    """
    parts = _RULE.split(text)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        header = parts[i].strip()
        body = parts[i + 1]
        m = re.match(r"prompt\s+(.+?)\s+sample\s*$", header, re.I)
        if not m:
            continue
        name = m.group(1).strip().lower()
        prompt = negative = ""
        settings = {}
        if "Prompt:" in body:
            after = body.split("Prompt:", 1)[1]
            head, _, tail = after.partition("Negative prompt:")
            prompt = head.strip()
            if tail:
                cut = _SETTINGS_START.search(tail)
                negative = (tail[: cut.start()] if cut else tail).strip()
                if cut:
                    settings = parse_a1111_settings(tail[cut.start():])
        out[name] = {"prompt": prompt, "negative": negative, "settings": settings}
    return out


def render_prompt(template, artist, token="xxx"):
    """Put the artist into a template.

    Accepts the dataset's bare `xxx` placeholder as well as the more usual
    `{artist}` / `{title}` / `__artist__` spellings, so a hand-written
    --prompt-template does not have to imitate this one readme.
    """
    if not template:
        return ""
    out = template
    for placeholder in ("{artist}", "{title}", "__artist__"):
        out = out.replace(placeholder, artist)
    if token:
        out = re.sub(rf"\b{re.escape(token)}\b", artist, out)
    return out

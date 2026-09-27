"""Hilbert curve index <-> tile coordinates.

Deliberately built on `pmtiles.tile`'s own curve rather than a fresh d2xy
implementation.  The Hilbert curve has several valid orientations and this one
has to match the archive exactly: PMTiles orders tiles by Hilbert id, so
consecutive indices then land not only adjacent *on the map* but contiguous
*in the file*.  Rolling our own would give a curve that looks equally correct
and quietly breaks that.

The library indexes tiles globally across zoom levels; a single level z starts
at `((1 << 2z) - 1) // 3`, so a per-level index is just that offset away.
"""
from pmtiles.tile import tileid_to_zxy, zxy_to_tileid


def level_offset(order):
    """Tile id of index 0 at this order (= zoom level)."""
    return ((1 << (2 * order)) - 1) // 3


def d2xy(order, index):
    """Index along the curve -> (x, y) in a 2^order square.  Wraps."""
    if order < 0:
        raise ValueError("order must be >= 0")
    side = 1 << order
    index %= side * side                    # wrap rather than raise: a counter
    z, x, y = tileid_to_zxy(level_offset(order) + index)
    if z != order:                          # cannot happen; catches an upstream change
        raise AssertionError(f"tileid_to_zxy returned z={z} for order={order}")
    return x, y


def block_grid_order(order, nx, ny):
    """Largest square block grid that fits nx x ny blocks inside 2^order tiles.

    The grid is square because the Hilbert curve is, and the tighter axis
    decides -- 2x3 blocks on a z=8 level fit 128 across but only 85 down, so
    64x64 blocks it is, spanning 128x192 tiles.

    Lives here rather than in imgimport because the saver's placement needs it
    too, and tilestore should not have to import the image importer to place a
    tile. `imgimport.block_grid_order` is an alias.
    """
    side = 1 << int(order)
    fit = min(side // max(1, int(nx)), side // max(1, int(ny)))
    return max(0, max(1, fit).bit_length() - 1)


def xy2d(order, x, y):
    """(x, y) -> index along the curve.  The inverse of d2xy."""
    side = 1 << order
    if not (0 <= x < side and 0 <= y < side):
        raise ValueError(f"{x},{y} outside a {side}x{side} grid (order {order})")
    return zxy_to_tileid(order, x, y) - level_offset(order)

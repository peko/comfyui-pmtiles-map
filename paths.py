"""Where the maps live, when ComfyUI is not the one asking.

Inside ComfyUI the saver uses `folder_paths.get_output_directory()`. The CLI and
the standalone server have no ComfyUI to ask, so they have to find
`output/maps` themselves -- and the pack may sit either in
`ComfyUI/custom_nodes/` (a normal install) or outside it with a symlink (this
repo's convention, so a ComfyUI update cannot clobber it).
"""
import os

ENV_VAR = "PMTILES_MAPS_DIR"
_HERE = os.path.dirname(os.path.abspath(__file__))


def candidates(start=None):
    base = start or _HERE
    for _ in range(6):
        yield os.path.join(base, "ComfyUI", "output", "maps")
        yield os.path.join(base, "output", "maps")
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent


def default_maps_dir(start=None):
    """First existing output/maps at or above the pack, else a cwd-relative one."""
    env = os.environ.get(ENV_VAR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    for path in candidates(start):
        if os.path.isdir(path):
            return os.path.abspath(path)
    return os.path.abspath(os.path.join(os.getcwd(), "output", "maps"))

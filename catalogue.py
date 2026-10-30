"""Where a model came from, for the viewer's info pane.

A render's metadata names its models the way the graph did --
`illustrious/amanatsuIllustrious_v2.safetensors` -- which identifies a file on
this box and says nothing about what the model *is* or where to get it again.
That is exactly what a model catalogue knows, so if there is one, ask it.

The catalogue is an **optional** SQLite index, not part of this pack: one row
per weights file, with the Civitai / HuggingFace origin read out of the
sidecars. The schema this reads is the one written by `model_catalog.py` in
peko/civitai -- table `files`, columns `comfy_name`, `file`, `kind`, `source`,
`model_name`, `version_name`, `model_id`, `version_id`, `base_model`,
`model_type`, `creator`, `hf_repo`, `hf_file`, `url`, `trained_words`,
`precision`, `size`. Nothing here requires it: with no catalogue the lookup
returns nothing and the viewer lists the file names exactly as before.

**The identity of a model here is its sha256, not its file name.** Files get
renamed locally -- `tools/rename_loras.py` exists precisely to do that in bulk
-- and ComfyUI names a model by its path relative to `models/<kind>`, so moving
one between subfolders renames it too. A name recorded in a map months ago may
name nothing today, or, worse, name a different file. So the saver resolves the
name *while it is still current* and records the hash with the tile
(`model_hashes`), and this lookup resolves the hash first.

Name matching is the fallback, for maps saved before that existed: full ComfyUI
name first, then basename, recursively.
"""
import json
import os
import sqlite3

ENV_VAR = "PMTILES_MODEL_CATALOG"
# Both spellings, because the thing gets called both.
DB_NAMES = ("catalog.sqlite", "catalogue.sqlite")
_HERE = os.path.dirname(os.path.abspath(__file__))

# (path, mtime, size) -> {key: entry}. One catalogue is ~750 rows / 600 KB, so
# the whole index is cheaper to hold than to re-query per tile click.
_INDEX = None
_STAMP = None


def _yaml_roots(path):
    """`base_path` of every section in an extra_model_paths.yaml.

    That is where the catalogue would sit -- beside the per-kind folders, not
    inside one. PyYAML ships with ComfyUI but is not a dependency of this pack,
    so its absence is just one fewer place to look.
    """
    if not os.path.isfile(path):
        return []
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as handle:
            conf = yaml.safe_load(handle) or {}
    except Exception:
        return []
    out = []
    for section in conf.values():
        if isinstance(section, dict) and section.get("base_path"):
            out.append(os.path.abspath(
                os.path.expanduser(str(section["base_path"]))))
    return out


def _models_roots():
    """Every directory ComfyUI treats as a models root, plus the obvious ones.

    `folder_names_and_paths` holds the per-kind directories
    (`/mnt/netac/models/loras`, ...), and the catalogue sits beside them at the
    root, so take each parent. This is also what picks up the extra roots from
    `extra_model_paths.yaml` -- the whole point, since on a box with a second
    drive that is where the weights (and the catalogue) actually live.
    """
    roots = []
    try:
        import folder_paths
        for _, entry in folder_paths.folder_names_and_paths.items():
            for path in entry[0]:
                roots.append(os.path.dirname(os.path.abspath(path)))
        roots.append(os.path.abspath(folder_paths.models_dir))
    except Exception:                    # not inside ComfyUI, or an odd version
        pass
    # The standalone server and the CLI tools have no ComfyUI to ask: walk up
    # from the pack looking for a models/ the same way paths.py looks for
    # output/maps, and read `extra_model_paths.yaml` ourselves. Without that
    # second half a box whose weights live on another drive -- the normal case
    # for anyone with a catalogue -- finds nothing from the shell while the
    # same code inside ComfyUI finds it, which reads as the script being broken.
    base = _HERE
    for _ in range(6):
        roots.append(os.path.join(base, "models"))
        roots.append(os.path.join(base, "ComfyUI", "models"))
        for conf in (os.path.join(base, "extra_model_paths.yaml"),
                     os.path.join(base, "ComfyUI", "extra_model_paths.yaml")):
            roots.extend(_yaml_roots(conf))
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent
    seen = set()
    for root in roots:
        if root not in seen:
            seen.add(root)
            yield root


def find_db():
    """Path to the catalogue, or None. `PMTILES_MODEL_CATALOG` wins."""
    env = os.environ.get(ENV_VAR)
    if env:
        path = os.path.abspath(os.path.expanduser(env))
        return path if os.path.isfile(path) else None
    for root in _models_roots():
        for db_name in DB_NAMES:
            path = os.path.join(root, db_name)
            if os.path.isfile(path):
                return path
    return None


def _civitai_host(rows):
    """The host the catalogue itself uses for Civitai, for the rows that have
    no stored URL.

    Not hardcoded to civitai.com: a box whose sidecars were written against a
    mirror has every *stored* link pointing there, and constructing the handful
    of missing ones against a different host would produce two kinds of link in
    one panel -- one of which may be the kind this network cannot reach.
    """
    counts = {}
    for row in rows:
        url = row["url"]
        if not url or "://" not in url:
            continue
        host = url.split("/", 3)[:3]
        if len(host) == 3 and "civitai" in host[2]:
            key = f"{host[0]}//{host[2]}"
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "https://civitai.com"
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _url_for(row, civitai_host):
    """A page a human can open, or None.

    The stored `url` is used verbatim when there is one -- it is what the
    sidecar recorded. Otherwise it is reconstructed from the ids, which is what
    covers the `lora-manager` rows (Civitai ids, no URL) and the Civitai rows
    whose sidecar predates the URL field.
    """
    url = (row["url"] or "").strip()
    if url:
        return url
    model_id = row["model_id"]
    if model_id:
        version = f"?modelVersionId={row['version_id']}" if row["version_id"] else ""
        return f"{civitai_host}/models/{model_id}{version}"
    repo = (row["hf_repo"] or "").strip()
    if repo:
        hf_file = (row["hf_file"] or "").strip()
        # Link the file itself when the sidecar names one: on a repackaged repo
        # (Comfy-Org/Wan_2.1_ComfyUI_repackaged holds dozens of variants) the
        # repo root does not say which file this render used.
        return (f"https://huggingface.co/{repo}/blob/main/{hf_file}" if hf_file
                else f"https://huggingface.co/{repo}")
    return None


def _entry(row, civitai_host):
    try:
        words = json.loads(row["trained_words"]) if row["trained_words"] else None
    except (ValueError, TypeError):
        words = None
    return {
        "file": row["file"],
        "comfy_name": row["comfy_name"],
        "sha256": (row["sha256"] or "").lower() or None,
        "kind": row["kind"],
        "source": row["source"],
        "url": _url_for(row, civitai_host),
        "model": row["model_name"] or None,
        "version": row["version_name"] or None,
        "base": row["base_model"] or None,
        "type": row["model_type"] or None,
        "creator": row["creator"] or None,
        "precision": row["precision"] or None,
        "size": row["size"],
        "trained_words": words or None,
    }


def _rank(entry):
    """How much a row is worth when two files share a basename.

    A link beats no link, and a named model beats a bare filename -- the copy
    on the second drive and the copy in ComfyUI/models are the same weights, so
    the only thing to choose between them is which row knows more.
    """
    return (1 if entry["url"] else 0) + (1 if entry["model"] else 0)


def _build_index(path):
    """Two indexes over the same rows: by hash, and by name as a fallback."""
    by_sha, by_name = {}, {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT root, path, kind, comfy_name, file, size, precision, "
                "sha256, source, model_name, version_name, model_id, "
                "version_id, base_model, model_type, creator, hf_repo, "
                "hf_file, url, trained_words FROM files").fetchall()
        except sqlite3.Error:
            return {"sha": {}, "name": {}}
    civitai_host = _civitai_host(rows)
    for row in rows:
        entry = _entry(row, civitai_host)
        if entry["sha256"]:
            key = entry["sha256"]
            if key not in by_sha or _rank(entry) > _rank(by_sha[key]):
                by_sha[key] = entry
        for key in (row["comfy_name"], row["file"]):
            if not key:
                continue
            key = key.lower().replace("\\", "/")
            if key not in by_name or _rank(entry) > _rank(by_name[key]):
                by_name[key] = entry
    return {"sha": by_sha, "name": by_name}


def _index():
    """The catalogue, reloaded when the file changes under us.

    A rebuild (`model_catalog.py` after a download) has to be visible without
    restarting ComfyUI, and the server is long-lived here -- so the stamp, not
    a once-only load.
    """
    global _INDEX, _STAMP
    path = find_db()
    if path is None:
        _INDEX, _STAMP = None, None
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    stamp = (path, stat.st_mtime, stat.st_size)
    if _INDEX is None or _STAMP != stamp:
        try:
            _INDEX = _build_index(path)
        except sqlite3.Error:
            return None
        _STAMP = stamp
    return _INDEX


def lookup(items):
    """{name: entry} for whatever the catalogue recognises.

    `items` are names, or `(name, sha256)` pairs / `{"name", "sha256"}` dicts.
    A hash resolves whatever the file is called today; a name only resolves a
    file that still has that name. Unknown entries are simply absent -- the
    viewer prints those as plain text.

    The entry carries `matched_by`, because "the catalogue found this by hash
    and it is now called something else" is the one case worth saying out loud.
    """
    index = _index()
    if not index:
        return {}
    found = {}
    for item in items:
        name, sha = _split_item(item)
        if not name and not sha:
            continue
        entry, how = None, None
        if sha:
            entry = index["sha"].get(sha.lower())
            how = "sha256" if entry is not None else None
        if entry is None and name:
            key = name.strip().lower().replace("\\", "/")
            entry = index["name"].get(key)
            # The graph may hold a bare basename where the catalogue has the
            # subfolder, or the other way round after a reorganisation.
            if entry is None:
                entry = index["name"].get(key.rsplit("/", 1)[-1])
            how = "name" if entry is not None else None
        if entry is None:
            continue
        found[name or sha] = dict(entry, matched_by=how,
                                  renamed=bool(how == "sha256" and name
                                               and entry["comfy_name"]
                                               and entry["comfy_name"].lower()
                                               != name.strip().lower()))
    return found


def _split_item(item):
    if isinstance(item, str):
        return item.strip(), None
    if isinstance(item, dict):
        return (str(item.get("name") or "").strip(),
                str(item.get("sha256") or "").strip() or None)
    if isinstance(item, (list, tuple)) and len(item) == 2:
        return (str(item[0] or "").strip(), str(item[1] or "").strip() or None)
    return "", None


def status():
    """What the viewer needs to know: is there a catalogue, and how big."""
    path = find_db()
    index = _index() if path else None
    return {"catalog": path is not None,
            "path": path,
            "files": len(index["name"]) if index else 0}

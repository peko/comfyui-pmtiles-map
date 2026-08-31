#!/usr/bin/env python3
"""Rewrite a saved workflow's `widgets_values` after the node's inputs moved.

usage:
  migrate_widgets.py <workflow.json|dir> [...] [--apply] [--server URL]
                     [--node SavePMTilesMap]

A saved graph stores widget values **positionally**, so inserting an input in
the middle of a node feeds every later value into the wrong field -- silently,
because the types often still fit (a zoom lands in `y_scheme`, a boolean in a
string). Reordering by *name* is the only safe fix, and both orders are
available without guessing:

  * the new one comes from a running ComfyUI's /object_info, so this tool never
    carries a stale copy of the node definition;
  * **the old one is in the file itself.** The frontend writes an `inputs` entry
    per widget carrying `{"widget": {"name": ...}}`, in widget order, so a saved
    graph states the layout it was saved against. Do not try to infer it from
    the value count: the node gained and lost widgets over time, and two
    different orders shared a length -- an early attempt here mapped a tags
    string onto `store_full_prompt` and would have corrupted 19 workflows.

`inputs` itself is left alone: its indices are what `links` refer to. Values
whose name survives are carried across, anything new gets its default. Dry run
unless --apply.
"""
import argparse
import glob
import json
import os
import shutil
import sys
import urllib.request

# Widget order as saved by earlier versions of SavePMTilesMap. The node only
# ever *grew*, so a file's length says which of these it was written against.
# (`images` is a socket, not a widget, and never appears here.)
HISTORY = {
    "SavePMTilesMap": [
        "map_name", "z", "x", "y", "placement", "coords_mode", "tile_size",
        "webp_quality", "pyramid_to_zoom", "y_scheme", "write_archive",
        "embed_tile_metadata", "store_full_prompt", "title", "tags",   # 15
        "archive_every", "preview",                                    # 17
        "prompt_text", "negative_text",                                # 19
        "store_format", "store_quality",                               # 21
    ],
}


def widget_order(defs, node_type):
    """Widget names in the order the frontend lays them out, plus defaults.

    Mirrors make_workflow.ui_widgets: required before optional, sockets skipped,
    and `control_after_generate` inserted right after any input that asks for it
    -- miss that and every later widget shifts by one.
    """
    spec = defs[node_type]["input"]
    names, defaults = [], {}
    for section in ("required", "optional"):
        for name, entry in (spec.get(section) or {}).items():
            if not isinstance(entry, (list, tuple)) or not entry:
                continue
            kind, opts = entry[0], (entry[1] if len(entry) > 1 else {})
            if not isinstance(opts, dict):
                opts = {}
            is_combo = isinstance(kind, list) or (
                isinstance(kind, str) and "COMBO" in kind)
            if not is_combo and isinstance(kind, str) and kind.isupper() \
                    and kind not in ("INT", "FLOAT", "STRING", "BOOLEAN"):
                continue                        # a link socket, not a widget
            if opts.get("forceInput"):
                continue
            names.append(name)
            defaults[name] = opts.get("default", "" if kind == "STRING" else 0)
            if opts.get("control_after_generate"):
                names.append(f"{name}::control")
                defaults[f"{name}::control"] = "fixed"
    return names, defaults


def saved_order(node, fallback):
    """The widget order this file was written against, from the file."""
    names = [inp["widget"]["name"] for inp in (node.get("inputs") or [])
             if isinstance(inp.get("widget"), dict) and inp["widget"].get("name")]
    values = node.get("widgets_values") or []
    if len(names) == len(values):
        return names
    return fallback[:len(values)]               # pre-dates the per-widget entries


def migrate_node(node, new_names, defaults, old_names):
    values = node.get("widgets_values")
    if not isinstance(values, list):
        return None                             # dict-form or absent: nothing to do
    old = saved_order(node, old_names)
    if len(old) != len(values):                 # cannot say what these values are
        return {"skipped": f"{len(values)} values but {len(old)} known names"}
    have = dict(zip(old, values))
    if [have.get(n) for n in new_names] == values and new_names[:len(values)] == old:
        return None                             # already in the new order
    node["widgets_values"] = [
        have[n] if n in have else defaults.get(n) for n in new_names
    ]
    return {"before": values, "after": node["widgets_values"], "names": have,
            "kept": sum(1 for n in new_names if n in have)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="workflow .json files or directories")
    ap.add_argument("--node", default="SavePMTilesMap")
    ap.add_argument("--server", default="http://127.0.0.1:8188")
    ap.add_argument("--apply", action="store_true",
                    help="write the files (default is a dry run)")
    args = ap.parse_args()

    url = f"{args.server}/object_info/{args.node}"
    try:
        defs = json.load(urllib.request.urlopen(url, timeout=15))
    except Exception as exc:
        sys.exit(f"could not read {url}: {exc}\n"
                 f"start ComfyUI first -- the new widget order comes from it")
    if args.node not in defs:
        sys.exit(f"{args.node} is not registered on that server")
    new_names, defaults = widget_order(defs, args.node)
    old_names = HISTORY.get(args.node)
    if not old_names:
        sys.exit(f"no historical widget order recorded for {args.node}")

    files = []
    for path in args.paths:
        if os.path.isdir(path):
            files += sorted(glob.glob(os.path.join(path, "**", "*.json"),
                                      recursive=True))
        else:
            files.append(path)

    changed = 0
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                wf = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(wf, dict) or "nodes" not in wf:
            continue
        edits = []
        for node in wf["nodes"]:
            if node.get("type") != args.node:
                continue
            result = migrate_node(node, new_names, defaults, old_names)
            if result and result.get("skipped"):
                print(f"{os.path.basename(path)}: node {node.get('id')} SKIPPED "
                      f"-- {result['skipped']}")
                continue
            if result:
                edits.append((node.get("id"), result))
        if not edits:
            continue
        changed += 1
        print(f"\n{os.path.basename(path)}")
        for node_id, result in edits:
            print(f"  node {node_id}: {len(result['before'])} -> "
                  f"{len(result['after'])} values, {result['kept']} carried over")
            for name, value in zip(new_names, result["after"]):
                print(f"    {name:<22} {value!r}"
                      + ("" if name in result["names"] else "   (new, default)"))
        if args.apply:
            backup = path + ".bak"
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(wf, fh, ensure_ascii=False, indent=2)

    print(f"\n{changed} file(s) {'rewritten' if args.apply else 'would change'}"
          f"{'' if args.apply else ' -- re-run with --apply'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

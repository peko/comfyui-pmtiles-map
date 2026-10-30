#!/usr/bin/env python3
"""Put model links into maps that were saved before the saver recorded them.

usage:
  backfill_model_links.py --all [--dry-run]
  backfill_model_links.py MAP [MAP ...] [--maps-dir DIR] [--catalog PATH]
                          [--renames TSV] [--force] [--report] [--dry-run]

A tile records the models it used as **file names**, which is what the graph
had. The viewer turns those into Civitai / HuggingFace links through the model
catalogue, and resolves them by **sha256** when the tile carries one
(`model_hashes`, written by the saver since this feature landed). Maps saved
before that carry names only -- so they still link, but only for as long as
every file keeps the name it had, and `tools/rename_loras.py` exists precisely
to break that.

This walks an existing map, resolves each model name against the catalogue
*now*, and writes the hashes into the tile metadata. After it has run, those
maps resolve by hash like a fresh one: rename, move between subfolders, reorganise
the whole tree -- the links still work.

**It can only recover what the catalogue still recognises.** A model already
renamed before this runs has nothing left to match on, and is reported as
unknown rather than guessed at. That is the argument for running it sooner
rather than later, and `--report` shows exactly which names are in that state.

The exception is a rename somebody wrote down: `--renames tools/lora_rename.tsv`
(repeatable) takes any table with an old and a new column, tries it on every
name the catalogue does not know, and so recovers links that were already lost.

Only the SQLite store is touched -- `UPDATE tile_meta`, no pixels, no tile
events, so a viewer open on the map keeps its tiles and picks the new metadata
up on the next click. The .pmtiles archive is a build product: run
`pmtiles_map.py --rebuild MAP` (or press build) to carry it across.

Do not run it in the middle of a sweep. Nothing corrupts -- SQLite waits -- but
a saver holding the write lock makes it sit there.
"""
import argparse
import csv
import json
import os
import sys

# realpath, not abspath: these are symlinked from the repo's tools/ directory, and
# the pack is the parent of wherever this file really lives.
HERE = os.path.dirname(os.path.realpath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, PACK)

import catalogue        # noqa: E402
import paths            # noqa: E402
import tilestore        # noqa: E402

MAPS = paths.default_maps_dir()


def map_names(maps_dir):
    """Every map with a store, which is the only thing this can edit."""
    if not os.path.isdir(maps_dir):
        return []
    return sorted(f[:-len(".tiles.db")] for f in os.listdir(maps_dir)
                  if f.endswith(".tiles.db"))


_OLD_COLUMNS = ("path", "old_path", "old_name", "old", "original_name", "from")
_NEW_COLUMNS = ("new_path", "new_name", "new", "to")


def load_renames(paths):
    """{old name (lower): new name} from a rename table.

    The one case this script cannot otherwise help with: a model renamed
    *before* it ran leaves a name that resolves to nothing. If whatever did the
    renaming wrote down what it did -- `tools/rename_loras.py` keeps
    `lora_rename.tsv` -- that table is the missing link, and the old maps can
    be repaired after all.

    Any TSV/CSV with a header naming an old and a new column will do. A new
    value with no folder and no extension is read as a stem beside the old
    file, which is how a table of `path / original_name / new_name` works.
    """
    out = {}
    for path in paths or ():
        with open(path, newline="", encoding="utf-8-sig") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            delim = "\t" if "\t" in sample.splitlines()[0] else ","
            reader = csv.DictReader(handle, delimiter=delim)
            fields = [f for f in (reader.fieldnames or [])]
            old_col = next((c for c in _OLD_COLUMNS if c in fields), None)
            new_col = next((c for c in _NEW_COLUMNS if c in fields), None)
            if not old_col or not new_col:
                sys.exit(f"{path}: need an old and a new column, "
                         f"got {fields}")
            for row in reader:
                old = (row.get(old_col) or "").strip().replace("\\", "/")
                new = (row.get(new_col) or "").strip().replace("\\", "/")
                if not old or not new or old == new:
                    continue
                if "/" not in new and not os.path.splitext(new)[1]:
                    folder, tail = os.path.split(old)
                    new = os.path.join(folder, new + os.path.splitext(tail)[1])
                out[old.lower()] = new
    return out


def resolve_names(names, renames):
    """{name as the tile has it: catalogue entry}, renames applied on a miss."""
    entries = catalogue.lookup(sorted(names))
    missing = [n for n in names if n not in entries]
    alias = {n: renames[n.lower()] for n in missing if n.lower() in renames}
    if alias:
        found = catalogue.lookup(sorted(set(alias.values())))
        for old, new in alias.items():
            if new in found:
                # Keyed by the name the *tile* holds -- that is what the viewer
                # will ask about -- while the row describes the file today.
                entries[old] = dict(found[new], matched_by="rename")
    return entries


def wanted_sets(rows, force):
    """Distinct model lists still needing work.

    A sweep writes the same list into every tile of every cell: 4150 tiles over
    21 distinct sets on one map here. Resolving per tile would be thousands of
    lookups for twenty-one answers.
    """
    wanted = set()
    for _, _, _, meta in rows:
        names = meta.get("models")
        if not isinstance(names, list) or not names:
            continue
        if meta.get("model_hashes") and not force:
            continue
        wanted.add(tuple(n for n in names if isinstance(n, str)))
    return wanted


def backfill(name, maps_dir, force=False, dry_run=False, report=False,
             renames=None):
    db_path = os.path.join(maps_dir, f"{name}.tiles.db")
    print(f"\n{name}")
    if not os.path.exists(db_path):
        archive_only = os.path.exists(os.path.join(maps_dir, f"{name}.pmtiles"))
        print("  no store" + (" -- archive only, and its metadata is a build "
                              "product with nothing left to build it from"
                              if archive_only else ""))
        return 0

    with tilestore.TileStore(db_path) as store:
        rows = [(z, x, y, json.loads(blob)) for z, x, y, blob in
                store.db.execute("SELECT z, x, y, meta FROM tile_meta "
                                 "ORDER BY z, x, y")]
        sets = wanted_sets(rows, force)
        seen = {name for names in sets for name in names}
        # Three outcomes, and conflating them hides the only one that is
        # actionable. A file the catalogue knows but has no sha256 for (no
        # sidecar) still *links* -- by name, as before -- it just cannot be
        # made rename-proof. A name the catalogue does not know at all is the
        # one worth printing.
        entries = resolve_names(seen, renames or {})
        resolved = {name for name, entry in entries.items() if entry.get("sha256")}
        no_hash = set(entries) - resolved
        unknown = seen - set(entries)
        recovered = {name for name, entry in entries.items()
                     if entry.get("matched_by") == "rename"}
        hashes_for = {names: {n: entries[n]["sha256"] for n in names
                              if n in resolved}
                      for names in sets}

        changed = already = no_models = 0
        for z, x, y, meta in rows:
            names = meta.get("models")
            if not isinstance(names, list) or not names:
                no_models += 1
                continue
            if meta.get("model_hashes") and not force:
                already += 1
                continue
            found = hashes_for.get(tuple(n for n in names if isinstance(n, str)))
            if not found:
                continue
            # Merge rather than replace: --force re-resolves, but a hash this
            # run could not confirm is still better evidence than nothing.
            merged = dict(meta.get("model_hashes") or {}, **found)
            if merged == meta.get("model_hashes"):
                already += 1
                continue
            meta["model_hashes"] = merged
            changed += 1
            if not dry_run:
                store.db.execute(
                    "UPDATE tile_meta SET meta = ? WHERE z=? AND x=? AND y=?",
                    (json.dumps(meta, ensure_ascii=False), z, x, y))
        if not dry_run:
            store.db.commit()

    print(f"  {len(rows)} tile(s) with metadata · {no_models} without models"
          f" · {already} already resolved")
    print(f"  {len(seen)} model name(s) over {len(hashes_for)} distinct set(s)"
          f" -> {len(resolved)} hashed"
          + (f" ({len(recovered)} of them via the rename table)" if recovered else "")
          + f", {len(no_hash)} linked by name only, {len(unknown)} unknown")
    for name_ in sorted(recovered):
        print(f"    recovered by rename: {name_} -> {entries[name_]['comfy_name']}")
    for name_ in sorted(no_hash):
        print(f"    no sha256 in the catalogue (no sidecar), stays "
              f"name-matched: {name_}")
    for name_ in sorted(unknown):
        # The actionable line: the catalogue does not know this file under this
        # name. Either it was never indexed -- rebuild it -- or it was renamed
        # before this ran, and then nothing here can recover it.
        print(f"    not in the catalogue: {name_}")
    if report and entries:
        for name_, entry in sorted(entries.items()):
            link = entry["url"] or "(known, but with no recorded origin)"
            print(f"    {name_}\n      {link}")
    print(f"  {'would update' if dry_run else 'updated'} {changed} tile(s)")
    return changed


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("maps", nargs="*", metavar="MAP")
    ap.add_argument("--all", action="store_true", help="every map with a store")
    ap.add_argument("--maps-dir", default=MAPS)
    ap.add_argument("--catalog", metavar="PATH",
                    help="the model catalogue to resolve against; without it "
                         f"${catalogue.ENV_VAR} and the models roots are tried")
    ap.add_argument("--renames", metavar="TSV", action="append",
                    help="a table of old -> new model names (repeatable), to "
                         "recover links for files renamed before this ran; "
                         "e.g. tools/lora_rename.tsv")
    ap.add_argument("--force", action="store_true",
                    help="re-resolve tiles that already carry hashes")
    ap.add_argument("--report", action="store_true",
                    help="print the link each resolved model will show")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    args = ap.parse_args()

    if args.catalog:
        os.environ[catalogue.ENV_VAR] = os.path.abspath(
            os.path.expanduser(args.catalog))

    status = catalogue.status()
    if not status["catalog"]:
        sys.exit("no model catalogue found -- nothing to resolve against. "
                 f"Point --catalog at one, or set ${catalogue.ENV_VAR}.")
    print(f"catalogue: {status['path']} ({status['files']} names)")

    names = args.maps or (map_names(args.maps_dir) if args.all else [])
    if not names:
        sys.exit("name a map, or pass --all "
                 f"(maps dir: {args.maps_dir})")

    renames = load_renames(args.renames)
    if renames:
        print(f"rename table: {len(renames)} entries from "
              f"{', '.join(args.renames)}")

    total = sum(backfill(name, args.maps_dir, force=args.force,
                         dry_run=args.dry_run, report=args.report,
                         renames=renames)
                for name in names)
    if total and not args.dry_run:
        print(f"\n{total} tile(s) updated. The viewer reads the store directly, "
              "so this is live now; rebuild a map to put it in the .pmtiles too:"
              f"\n  pmtiles_map.py --rebuild {names[0]}")
    elif args.dry_run:
        print(f"\n{total} tile(s) would change (dry run)")


if __name__ == "__main__":
    main()

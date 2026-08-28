import hashlib
import os
import re
import shutil
from collections import defaultdict

import action_log
import funscript_utils

_JUNK_NAMES = {'thumbs.db', 'desktop.ini'}
_FUNSCRIPT_EXT = funscript_utils.FUNSCRIPT_EXT

# Patreon post IDs run 6-9 digits in every folder name observed in this
# project (e.g. "[167073603] 2026-08-18 title"). Matching folders by this
# embedded ID rather than the literal name is what lets folder-pairing
# survive whatever decoration wraps it -- the raw Patreon downloader's own
# prefix, prefixFix.py stripping it, fix_garbled_names.py's mojibake/rename
# pass, or a manually retitled folder -- none of which change the post ID.
# Anchored at the start first (every real example seen leads with the ID,
# e.g. "[167073603] ..." or a bare/underscored "167073603_...") so a long
# number elsewhere in a title (resolution, a count, ...) doesn't get picked
# up instead; only falls back to searching anywhere if that finds nothing.
_LEADING_POST_ID_RE = re.compile(r'^\W*(\d{6,9})\b')
_POST_ID_RE = re.compile(r'\d{6,9}')


def _sha256_file(path, chunk_size=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _content_key(path):
    """Fingerprint used to decide whether two files hold the same data.

    A parseable .funscript is compared by its point data (see
    funscript_utils.funscript_data), not its bytes -- format/whitespace/
    metadata changes don't matter, only whether the actual points, inverted
    flag, and range match. Everything else -- a non-funscript file, or a
    .funscript that fails to parse -- falls back to a raw SHA256 of its
    bytes.

    Note this only ever compares one *specific* file against another --
    a source video's .pitch.funscript missing from the destination while
    its .twist.funscript is already there (or vice versa) is a per-file
    question, not a folder-wide one, so a script simply lacking an axis
    another one has is expected and never flagged as some kind of
    inconsistency here.
    """
    if path.lower().endswith(_FUNSCRIPT_EXT):
        data = funscript_utils.funscript_data(path)
        if data is not None:
            return ('funscript', data)
    return ('bytes', _sha256_file(path))


def _scoped_files(root, funscripts_only):
    """Yield paths (relative to root) of files under root, recursively.

    Skips dotfiles and common OS junk, and never descends into '.trash' --
    a dedupe/extraction pass's soft-deletes live there, and a file only
    reachable through .trash isn't actually present as far as any of this
    module's callers should be concerned (see find_missing_files). When
    funscripts_only is set, only yields .funscript files.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        if action_log.TRASH_DIRNAME in dirnames:
            dirnames.remove(action_log.TRASH_DIRNAME)
        for fn in filenames:
            if fn.startswith('.') or fn.lower() in _JUNK_NAMES:
                continue
            if funscripts_only and not fn.lower().endswith(_FUNSCRIPT_EXT):
                continue
            full = os.path.join(dirpath, fn)
            yield os.path.relpath(full, root)


def _folder_key(name):
    """The token folders are matched on: the embedded Patreon post ID if one
    can be found, else the literal name as a fallback (e.g. the hand-made
    "Weekly voted(...)" folders that carry no post ID)."""
    m = _LEADING_POST_ID_RE.match(name)
    if m:
        return m.group(1)
    m = _POST_ID_RE.search(name)
    return m.group(0) if m else name


def _folder_key_map(folders):
    """Map each folder's matching key -> folder name.

    A key claimed by more than one folder on the same side (should be
    essentially impossible for real, distinct posts, but never silently
    drop a folder over it) falls back to using each of those folders' own
    literal name as its key instead, same as a folder with no ID at all.
    """
    by_key = defaultdict(list)
    for f in folders:
        by_key[_folder_key(f)].append(f)
    result = {}
    for key, names in by_key.items():
        if len(names) == 1:
            result[key] = names[0]
        else:
            for name in names:
                result[name] = name
    return result


def _backup_existing_file(dest_path):
    """If dest_path exists, move it aside to '<name>.bak' so the incoming file
    can be copied in under its real name instead of spawning a ' (synced N)'
    sibling. Only one backup generation is kept -- a prior .bak from an
    earlier sync is overwritten, not stacked into .bak2, .bak3, ...

    Returns the backup path, or None if there was nothing to back up.

    Only called for a file find_missing_files already proved differs by
    content from anything in the destination -- never based on the name
    alone -- so a same-name/same-content file is left untouched and simply
    not re-copied.
    """
    if not os.path.exists(dest_path):
        return None
    backup = f"{dest_path}.bak"
    os.replace(dest_path, backup)
    action_log.record('rename', old_path=dest_path, new_path=backup)
    return backup


def build_dest_content_index(destination_root, funscripts_only):
    """Index of every file already present anywhere under destination_root
    (.trash excluded -- see _scoped_files), for find_missing_files to check
    a source file against.

    Deliberately whole-tree, not per-post-folder: some creators (confirmed:
    Pize) repost an already-downloaded script inside a later "collection"
    post, and the destination's own dedupe pass (downloadContent._dedup_existing)
    already collapses that to a single on-disk copy, wherever it happens to
    be oldest -- which is not necessarily the file's own post's folder. A
    folder-scoped search would see that folder as missing the file and copy
    it right back in, only for the next dedupe run to remove it again. This
    also means two byte-identical files that legitimately belong to two
    different posts collapse to one entry here, same as dedupe already
    treats them.

    Returns (funscript_keys, size_map): funscript_keys is a set of
    _content_key() results for every .funscript found; size_map maps
    byte size -> list of full paths, for byte-hash matching everything
    else (skipped entirely when funscripts_only is set).
    """
    funscript_keys = set()
    size_map = defaultdict(list)
    for rel in _scoped_files(destination_root, funscripts_only):
        full = os.path.join(destination_root, rel)
        if rel.lower().endswith(_FUNSCRIPT_EXT):
            funscript_keys.add(_content_key(full))
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        size_map[size].append(full)
    return funscript_keys, size_map


def find_missing_files(src_root, dest_index, funscripts_only):
    """Return a list of (src_full_path, relpath) present in src_root but not,
    by content, anywhere in dest_index (built once for the whole
    destination tree by build_dest_content_index -- see its docstring for
    why this isn't scoped to just this one folder).

    A file counts as already present if a matching file exists anywhere in
    the destination, regardless of filename or folder — a file that was
    renamed after copying (e.g. by fix_garbled_names), or that already
    lives in a different post's folder because a creator reposted it, is
    not re-copied. For a .funscript, "matching" means the same points (see
    _content_key), not the same bytes, so a copy the downloader
    re-touched/re-saved with reformatted JSON isn't flagged as missing just
    because its byte size changed — which also means funscripts aren't
    sized-bucketed the way other files are below (two byte-different files
    can hold identical points). Everything else still matches on exact
    byte content.
    """
    funscript_keys, size_map = dest_index
    dest_hash_cache = {}

    def dest_hash(full):
        if full not in dest_hash_cache:
            dest_hash_cache[full] = _sha256_file(full)
        return dest_hash_cache[full]

    missing = []
    for rel in _scoped_files(src_root, funscripts_only):
        src_full = os.path.join(src_root, rel)

        if rel.lower().endswith(_FUNSCRIPT_EXT):
            if _content_key(src_full) not in funscript_keys:
                missing.append((src_full, rel))
            continue

        try:
            size = os.path.getsize(src_full)
        except OSError:
            continue

        candidates = size_map.get(size, [])
        if not candidates:
            missing.append((src_full, rel))
            continue

        src_hash = _sha256_file(src_full)
        if not any(dest_hash(cand) == src_hash for cand in candidates):
            missing.append((src_full, rel))

    return missing


def sync_new_folders(source, destination):
    source_folders = {
        f for f in os.listdir(source)
        if os.path.isdir(os.path.join(source, f))
    }
    dest_folders = {
        f for f in os.listdir(destination)
        if os.path.isdir(os.path.join(destination, f))
    }

    # Match by embedded post ID, not literal name -- see _folder_key. A folder
    # already present under a differently-decorated name (prefix stripped,
    # mojibake fixed, retitled, ...) is "common", not "new", so the per-file
    # symmetry check below actually gets to run on it instead of being skipped
    # entirely because the literal names never matched.
    source_by_key = _folder_key_map(source_folders)
    dest_by_key = _folder_key_map(dest_folders)

    new_keys = sorted(set(source_by_key) - set(dest_by_key))
    common_keys = sorted(set(source_by_key) & set(dest_by_key))

    new_folders = [source_by_key[k] for k in new_keys]
    # (src_name, dest_name) -- may differ even though they're the same post.
    common_folders = [(source_by_key[k], dest_by_key[k]) for k in common_keys]

    if not new_folders:
        print("\nNo new folders found — destination is already up to date.")
    else:
        print(f"\nFound {len(new_folders)} new folder(s) to copy:")
        preview_limit = 20
        for folder in new_folders[:preview_limit]:
            print(f"  {folder}")
        if len(new_folders) > preview_limit:
            print(f"  ... and {len(new_folders) - preview_limit} more")

        print()
        confirm = input(f"Copy {len(new_folders)} folder(s) to destination? (y/n): ").strip().lower()
        if confirm != 'y':
            print("Skipped.")
        else:
            print()
            copied = 0
            errors = 0
            for i, folder in enumerate(new_folders, 1):
                src_path = os.path.join(source, folder)
                dst_path = os.path.join(destination, folder)
                print(f"  [{i}/{len(new_folders)}] {folder}")
                try:
                    shutil.copytree(src_path, dst_path)
                    action_log.record('copytree', dst=dst_path)
                    copied += 1
                except OSError as e:
                    print(f"    ERROR: {e}")
                    errors += 1
            print()
            print(f"Done — copied: {copied}, errors: {errors}")

    return common_folders


def sync_existing_folders(source, destination, common_folders):
    print()
    print("========================================")
    print("  Symmetry Check (existing folders)")
    print("========================================")
    print()
    print("Checks folders that already exist in both source and destination")
    print("for files present in source but missing from the destination —")
    print("compared by content (a funscript by its actual points, not raw")
    print("bytes), not just filename, and against the whole destination")
    print("tree, not just each file's own folder — so a script a creator")
    print("reposted elsewhere (and your dedupe pass already collapsed to one")
    print("copy) isn't flagged as missing and copied right back in. If a")
    print("same-named file exists in the file's own folder but its content")
    print("differs, the old one is renamed to <filename>.bak (one generation")
    print("kept) instead of copying the new one in as a ' (synced 2)' sibling.")
    print()

    if not common_folders:
        print("No folders exist in both source and destination — nothing to check.")
        return

    run_it = input("Run symmetry check on existing folders? (y/n): ").strip().lower()
    if run_it != 'y':
        print("Skipped.")
        return

    scope = input("Check funscripts only, or all files? [F/a] (default: funscripts only): ").strip().lower()
    funscripts_only = scope != 'a'
    print(f"Scope: {'funscripts only' if funscripts_only else 'all files'}")

    print()
    print("Indexing destination content...")
    dest_index = build_dest_content_index(destination, funscripts_only)

    print(f"Scanning {len(common_folders)} folder(s)...")
    all_missing = []  # (dest_folder, src_full, relpath)
    for i, (src_folder, dest_folder) in enumerate(common_folders, 1):
        label = dest_folder if src_folder == dest_folder else f"{dest_folder}  (source: {src_folder})"
        print(f"  [{i}/{len(common_folders)}] {label}", end='\r')
        src_root = os.path.join(source, src_folder)
        for src_full, rel in find_missing_files(src_root, dest_index, funscripts_only):
            all_missing.append((dest_folder, src_full, rel))
    print()

    if not all_missing:
        print("\nNo missing files found — destination is in sync.")
        return

    print(f"\nFound {len(all_missing)} file(s) missing from the destination:")
    preview_limit = 30
    for folder, _src_full, rel in all_missing[:preview_limit]:
        print(f"  {folder}/{rel}")
    if len(all_missing) > preview_limit:
        print(f"  ... and {len(all_missing) - preview_limit} more")

    print()
    confirm = input(f"Copy {len(all_missing)} file(s) to destination? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Cancelled.")
        return

    print()
    copied = 0
    errors = 0
    for i, (folder, src_full, rel) in enumerate(all_missing, 1):
        dest_root = os.path.join(destination, folder)
        dest_path = os.path.join(dest_root, rel)
        print(f"  [{i}/{len(all_missing)}] {folder}/{rel}")
        try:
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            backup = _backup_existing_file(dest_path)
            if backup:
                print(f"    (existing file differs -- backed up to {os.path.basename(backup)})")
            shutil.copy2(src_full, dest_path)
            action_log.record('copy', dst=dest_path)
            copied += 1
        except OSError as e:
            print(f"    ERROR: {e}")
            errors += 1

    print()
    print(f"Done — copied: {copied}, errors: {errors}")


def main():
    print()
    print("========================================")
    print("  Sync New Folders")
    print("========================================")
    print()
    print("Copies folders that exist in the source (Patreon downloader")
    print("output) but not yet in the destination (post-processor working")
    print("directory). Existing folders are never touched by this step.")
    print()

    source = input("Source folder (Patreon downloader output): ").strip().strip('"\'')
    if not os.path.isdir(source):
        print(f"Directory not found: {source}")
        return

    destination = input("Destination folder (post-processor working dir): ").strip().strip('"\'')
    if not os.path.isdir(destination):
        print(f"Directory not found: {destination}")
        return

    source = os.path.abspath(source)
    destination = os.path.abspath(destination)

    if source == destination:
        print("Source and destination are the same directory — nothing to do.")
        return

    action_log.start('sync_new_folders', destination)
    common_folders = sync_new_folders(source, destination)
    sync_existing_folders(source, destination, common_folders)
    action_log.finish()


if __name__ == "__main__":
    main()
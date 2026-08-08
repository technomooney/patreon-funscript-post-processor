import hashlib
import os
import shutil
from collections import defaultdict

import action_log

_JUNK_NAMES = {'thumbs.db', 'desktop.ini'}
_FUNSCRIPT_EXT = '.funscript'


def _sha256_file(path, chunk_size=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _scoped_files(root, funscripts_only):
    """Yield paths (relative to root) of files under root, recursively.

    Skips dotfiles and common OS junk. When funscripts_only is set, only
    yields .funscript files.
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.startswith('.') or fn.lower() in _JUNK_NAMES:
                continue
            if funscripts_only and not fn.lower().endswith(_FUNSCRIPT_EXT):
                continue
            full = os.path.join(dirpath, fn)
            yield os.path.relpath(full, root)


def _unique_dest_path(dest_path):
    """If dest_path exists, find a free ' (synced N)' variant."""
    if not os.path.exists(dest_path):
        return dest_path
    base, ext = os.path.splitext(dest_path)
    n = 2
    while True:
        candidate = f"{base} (synced {n}){ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def find_missing_files(src_root, dest_root, funscripts_only):
    """Return a list of (src_full_path, relpath) present in src_root but not,
    by content, anywhere in dest_root (scoped to funscripts_only if set).

    A file counts as already present if a byte-identical file exists in the
    destination, regardless of filename — a file that was renamed after
    copying (e.g. by fix_garbled_names) is not re-copied.
    """
    dest_rel_files = list(_scoped_files(dest_root, funscripts_only))
    dest_size_map = defaultdict(list)
    for rel in dest_rel_files:
        try:
            size = os.path.getsize(os.path.join(dest_root, rel))
        except OSError:
            continue
        dest_size_map[size].append(rel)

    dest_hash_cache = {}

    def dest_hash(rel):
        if rel not in dest_hash_cache:
            dest_hash_cache[rel] = _sha256_file(os.path.join(dest_root, rel))
        return dest_hash_cache[rel]

    missing = []
    for rel in _scoped_files(src_root, funscripts_only):
        src_full = os.path.join(src_root, rel)
        try:
            size = os.path.getsize(src_full)
        except OSError:
            continue

        candidates = dest_size_map.get(size, [])
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

    new_folders = sorted(source_folders - dest_folders)
    common_folders = sorted(source_folders & dest_folders)

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
    print("compared by content, not just filename, so a file already copied")
    print("under a different name won't be re-copied.")
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
    print(f"Scanning {len(common_folders)} folder(s)...")
    all_missing = []  # (folder, src_full, relpath)
    for i, folder in enumerate(common_folders, 1):
        print(f"  [{i}/{len(common_folders)}] {folder}", end='\r')
        src_root = os.path.join(source, folder)
        dest_root = os.path.join(destination, folder)
        for src_full, rel in find_missing_files(src_root, dest_root, funscripts_only):
            all_missing.append((folder, src_full, rel))
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
        dest_path = _unique_dest_path(os.path.join(dest_root, rel))
        print(f"  [{i}/{len(all_missing)}] {folder}/{rel}")
        try:
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
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
#!/usr/bin/env python3
"""
Undo the most recent run of a change-making script (renames, sync copies,
dedupe deletions). Only one level deep — running any of those scripts again
replaces what "last action" means, and a soft-deleting script permanently
purges its own previous run's trash the moment it starts a new run.

Usage
-----
  python undo_last_action.py
"""

import os
import shutil

import action_log


def _undo_rename(entry: dict) -> str:
    old_path, new_path = entry['old_path'], entry['new_path']
    if not os.path.exists(new_path):
        return f'SKIP — renamed file no longer exists: {new_path}'
    if os.path.exists(old_path):
        return f'SKIP — original path is occupied again: {old_path}'
    os.rename(new_path, old_path)
    return f'restored: {os.path.basename(new_path)} -> {os.path.basename(old_path)}'


def _undo_copy(entry: dict) -> str:
    dst = entry['dst']
    if not os.path.exists(dst):
        return f'SKIP — copied file no longer exists: {dst}'
    os.remove(dst)
    return f'removed copy: {dst}'


def _undo_copytree(entry: dict) -> str:
    dst = entry['dst']
    if not os.path.isdir(dst):
        return f'SKIP — copied folder no longer exists: {dst}'
    shutil.rmtree(dst)
    return f'removed copied folder: {dst}'


def _undo_soft_delete(entry: dict) -> str:
    orig_path, trash_path = entry['orig_path'], entry['trash_path']
    if not os.path.exists(trash_path):
        return f'SKIP — trashed file no longer exists: {trash_path}'
    if os.path.exists(orig_path):
        return f'SKIP — original path is occupied again: {orig_path}'
    os.makedirs(os.path.dirname(orig_path), exist_ok=True)
    os.rename(trash_path, orig_path)
    return f'restored from trash: {os.path.basename(orig_path)}'


_HANDLERS = {
    'rename': _undo_rename,
    'copy': _undo_copy,
    'copytree': _undo_copytree,
    'soft_delete': _undo_soft_delete,
}


def main():
    print()
    print("========================================")
    print("  Undo Last Action")
    print("========================================")
    print()

    journal = action_log.read_last()
    if not journal:
        print("Nothing to undo — no undoable run recorded (or it was already undone).")
        return

    entries = journal['entries']
    counts: dict[str, int] = {}
    for e in entries:
        counts[e['op']] = counts.get(e['op'], 0) + 1
    summary = ', '.join(f'{n} {op}' for op, n in counts.items())

    print(f"Last action: {journal.get('script')}  ({journal.get('timestamp')})")
    print(f"Root: {journal.get('root_dir')}")
    print(f"Changes: {summary}")
    print()
    confirm = input(f"Undo {len(entries)} change(s)? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Cancelled.")
        return

    print()
    ok = 0
    skipped = 0
    for i, entry in enumerate(reversed(entries), 1):
        handler = _HANDLERS.get(entry['op'])
        if handler is None:
            print(f"  [{i}/{len(entries)}] SKIP — unknown op: {entry['op']}")
            skipped += 1
            continue
        try:
            result = handler(entry)
        except OSError as e:
            result = f'ERROR — {e}'
        print(f"  [{i}/{len(entries)}] {result}")
        if result.startswith('SKIP') or result.startswith('ERROR'):
            skipped += 1
        else:
            ok += 1

    print()
    print(f"Done — undone: {ok}, skipped: {skipped}")
    if skipped == 0:
        action_log.clear_last()
    else:
        print("Some changes couldn't be undone (see above) — journal kept in case you want to retry.")


if __name__ == "__main__":
    main()

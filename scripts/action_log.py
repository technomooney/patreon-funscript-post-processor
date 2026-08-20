"""Shared journal backing the menu's "Undo last action" feature.

Any script that performs reversible filesystem changes (renames, copies,
soft-deletes) calls start() once, record() for each change, and finish()
when done. The journal for a run lives *inside the folder that run acted
on* (root_dir/.last_action.json — the same per-folder-state convention
folder_log.py already uses), not in one global file. That means running
this toolchain against a scratch/test folder no longer clobbers the real
undo target for whatever folder you actually work in day to day — each
folder keeps its own undo history, one level deep ("undo the last thing
this toolchain did *to that folder*", not a multi-level history). A run
that made no changes leaves that folder's previous journal (if any) in
place as the undo target.

read_last()/clear_last() take an optional root_dir. Pass one to target that
folder's journal specifically; omit it to fall back to "the last menu
action" system-wide — resolved via a small pointer file (_POINTER_PATH,
fixed at the repo root) that finish() updates with whichever root_dir it
just wrote to. Nothing is ever destroyed by the pointer moving on: an
older run's journal is still sitting in its own folder, just no longer the
no-argument default — pass its root_dir explicitly to reach it.

Soft-deletes (used by dedupe, where a file has to disappear but should stay
recoverable) are moved into a '.trash' folder next to the files being
scanned rather than actually deleted. Trash is age-based, not "one run
deep": purge_old_trash() removes only files older than TRASH_RETENTION_DAYS
(default 14) and is safe to call at the start of every soft-deleting run —
undo (the journal above) only ever covers the *last* run anyway, so a
15-day-old trashed file was never reachable through undo by the time it's
actually removed. This is deliberately a longer, decoupled safety net: you
can still recover something from .trash by hand within the retention
window even after several newer runs have moved on.
"""

import json
import os
import shutil
import time

JOURNAL_FILENAME = '.last_action.json'
_POINTER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              '.last_action_pointer.json')
TRASH_DIRNAME = '.trash'
DEFAULT_TRASH_RETENTION_DAYS = 14

_entries: list[dict] = []
_script_name: str | None = None
_root_dir: str | None = None


def start(script: str, root_dir: str) -> None:
    """Begin journaling a new run. Call once before making any changes."""
    global _entries, _script_name, _root_dir
    _entries = []
    _script_name = script
    _root_dir = root_dir


def record(op: str, **fields) -> None:
    """Record one reversible change. op is 'rename', 'copy', 'copytree', or 'soft_delete'."""
    entry = {'op': op}
    entry.update(fields)
    _entries.append(entry)


def _journal_path(root_dir: str) -> str:
    return os.path.join(os.path.normpath(root_dir), JOURNAL_FILENAME)


def _write_json(path: str, payload: dict) -> None:
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        print(f'  [action_log] could not write {os.path.basename(path)}: {e}')


def _resolve_root_dir(root_dir: str | None) -> str | None:
    """*root_dir* itself if given, else the folder the pointer file says was
    acted on most recently. None if there's nothing to resolve to."""
    if root_dir is not None:
        return root_dir
    if not os.path.exists(_POINTER_PATH):
        return None
    try:
        with open(_POINTER_PATH, 'r', encoding='utf-8') as f:
            return json.load(f).get('root_dir')
    except (OSError, json.JSONDecodeError):
        return None


def finish() -> None:
    """Persist the journal into root_dir, and point the no-argument default
    (read_last()/clear_last() called with no root_dir) at it.

    No-op if nothing was recorded this run — an empty run shouldn't erase a
    previous run's undo target, in that folder or as the default.
    """
    if not _entries:
        return
    payload = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'script': _script_name,
        'root_dir': _root_dir,
        'entries': _entries,
    }
    _write_json(_journal_path(_root_dir), payload)
    _write_json(_POINTER_PATH, {'root_dir': _root_dir})


def read_last(root_dir: str | None = None) -> dict | None:
    """Return the persisted journal for *root_dir*, or (if omitted) for
    whichever folder was acted on most recently. None if there's nothing to
    undo."""
    resolved = _resolve_root_dir(root_dir)
    if resolved is None:
        return None
    path = _journal_path(resolved)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('entries'):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def clear_last(root_dir: str | None = None) -> None:
    """Delete the journal for *root_dir* (or, if omitted, whichever folder
    was acted on most recently) — call after a successful undo so it can't
    be re-applied. Also clears the pointer if it was pointing at the
    journal just cleared, so a later no-argument call doesn't chase a
    now-missing file."""
    resolved = _resolve_root_dir(root_dir)
    if resolved is None:
        return
    try:
        os.remove(_journal_path(resolved))
    except OSError:
        pass
    if _resolve_root_dir(None) == resolved:
        try:
            os.remove(_POINTER_PATH)
        except OSError:
            pass


def trash_path(root_dir: str, original_path: str) -> str:
    """Return the quarantine path for *original_path*, preserving its structure
    relative to *root_dir* under '<root_dir>/.trash/'."""
    rel = os.path.relpath(original_path, root_dir)
    return os.path.join(root_dir, TRASH_DIRNAME, rel)


def purge_old_trash(root_dir: str, max_age_days: float | None = None) -> int:
    """Permanently delete trashed files under *root_dir* older than *max_age_days*.

    Defaults to the TRASH_RETENTION_DAYS env var, falling back to
    DEFAULT_TRASH_RETENTION_DAYS. Safe to call unconditionally at the start of
    any soft-deleting run — it never touches anything young enough to still be
    a plausible undo target. Prunes directories left empty behind it too.
    Returns the number of files removed.
    """
    if max_age_days is None:
        env = os.getenv('TRASH_RETENTION_DAYS', '').strip()
        try:
            max_age_days = float(env) if env else DEFAULT_TRASH_RETENTION_DAYS
        except ValueError:
            max_age_days = DEFAULT_TRASH_RETENTION_DAYS

    trash_dir = os.path.join(root_dir, TRASH_DIRNAME)
    if not os.path.isdir(trash_dir):
        return 0

    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for root, _dirs, files in os.walk(trash_dir, topdown=False):
        for f in files:
            fpath = os.path.join(root, f)
            try:
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    removed += 1
            except OSError as e:
                print(f'  [action_log] could not purge trashed file {fpath}: {e}')
        if root != trash_dir:
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass
    return removed


def soft_delete(root_dir: str, path: str) -> str:
    """Move *path* into the trash instead of deleting it outright. Returns the
    trash path it was moved to, for record()."""
    dest = trash_path(root_dir, path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.rename(path, dest)
    return dest

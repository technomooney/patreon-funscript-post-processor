"""Shared journal backing the menu's "Undo last action" feature.

Any script that performs reversible filesystem changes (renames, copies,
soft-deletes) calls start() once, record() for each change, and finish()
when done. The journal always holds only the most recently *completed* run
that actually changed something — "undo" means "undo the last thing this
toolchain did", not a multi-level history. A run that made no changes
leaves the previous journal (if any) in place as the undo target.

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

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.last_action.json')
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


def finish() -> None:
    """Persist the journal, replacing whatever run was previously the 'last action'.

    No-op if nothing was recorded this run — an empty run shouldn't erase a
    previous run's undo target.
    """
    if not _entries:
        return
    payload = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'script': _script_name,
        'root_dir': _root_dir,
        'entries': _entries,
    }
    tmp = _PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _PATH)
    except OSError as e:
        print(f'  [action_log] could not write undo journal: {e}')


def read_last() -> dict | None:
    """Return the persisted journal, or None if there's nothing to undo."""
    if not os.path.exists(_PATH):
        return None
    try:
        with open(_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('entries'):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def clear_last() -> None:
    """Delete the journal — call after a successful undo so it can't be re-applied."""
    try:
        os.remove(_PATH)
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

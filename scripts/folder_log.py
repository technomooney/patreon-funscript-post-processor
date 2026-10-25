"""Per-folder run history ("has script X already processed this folder?").

Storage: the shared library database (library_db: <library root>/.library-data/library.db, table `runs`) so the
downloader, the AV1 compressor and future tools all see one history. There is no per-folder file any more.

Every tool calls ensure_library(<the folder it works on>) first: that declares the folder a library when none
exists above it and imports any old .folder_log.json / compressor logs (migrate_library_data), which are only
ever read by that migration. Using read/append_run on a folder with no library is a bug and raises NoLibrary.
"""
import os
import time

import library_db

FILENAME = '.folder_log.json'     # the retired per-folder file; kept so scans can still skip it as bookkeeping
_UPDATED_BY = 'downloader'


class ParentOfLibraries(RuntimeError):
    """The folder holds other libraries; tools work on one creator's folder, never a folder of creators."""


class NoLibrary(RuntimeError):
    """A folder was used without ensure_library() having made it part of a library."""


def _force_rerun() -> bool:
    return os.getenv('FORCE_RERUN', 'false').strip().lower() not in ('false', '0', 'no')


def _declared_root(folder: str):
    """The library root owning *folder*, or None when no .library-data folder exists above it."""
    root = library_db.find_root(folder)
    return root if os.path.isdir(os.path.join(root, library_db.DATA_DIRNAME)) else None


def _root(folder: str) -> str:
    root = _declared_root(folder)
    if root is None:
        raise NoLibrary(f'{folder} is not inside a library; call folder_log.ensure_library() on the folder first')
    return root


_migrated: set[str] = set()      # library roots already checked for legacy logs in this process


def _migrate_legacy(root: str) -> None:
    """Import old .folder_log.json / compressor logs into the library DB before anything else uses it."""
    if root in _migrated:
        return
    _migrated.add(root)
    import migrate_library_data
    done = migrate_library_data.migrate_if_needed(root)
    if done:
        print(f'[library] migrated old logs in {root}: {done["runs"]} run record(s), {done["links"]} pixeldrain '
              f'link(s), {done["files"]} file identity row(s)')


_checked_parents: set[str] = set()


def _refuse_parent_of_libraries(base_path: str) -> None:
    base_path = os.path.abspath(base_path)
    if base_path in _checked_parents:
        return
    children = sorted(d for d in os.listdir(base_path)
                      if d not in library_db.SKIP_DIRNAMES and os.path.isdir(os.path.join(base_path, d, library_db.DATA_DIRNAME)))
    if children:
        raise ParentOfLibraries(
            f'{base_path} contains other libraries ({", ".join(children[:5])}{"..." if len(children) > 5 else ""}). '
            'Run on one creator folder, not a folder of creators.')
    _checked_parents.add(base_path)


def ensure_library(base_path: str) -> str:
    """Make sure *base_path* belongs to a library database, then bring it up to date. Run before any tool's work.

    Tools only coordinate through the shared DB, so: declare the folder a library when none exists above it, and
    import any old per-folder logs into it (idempotent; legacy files are filed under .library-data/legacy, never
    deleted). Returns the library root. A folder that cannot be written raises: nothing could be downloaded into
    it anyway. Always give it the folder one creator's content lives in: a folder with a library directly inside it
    raises ParentOfLibraries before anything is created or run."""
    _refuse_parent_of_libraries(base_path)
    root = _declared_root(base_path)
    if root is None:
        root = library_db.use_root(base_path)
        print(f'[library] created {library_db.db_path(root)} (this folder is now a library shared by the tools)')
    _migrate_legacy(root)
    return root


def read(folder: str) -> list:
    root = _root(folder)
    return library_db.runs_for(root, library_db.rel_folder(root, folder))


def has_run(folder: str, script: str) -> bool:
    """Return True if *script* has a completed run recorded for *folder*.

    Always returns False when FORCE_RERUN=true is set, so callers re-run
    and stamp the new log record with force_rerun=true.
    """
    if _force_rerun():
        return False
    return any(r.get('script') == script for r in read(folder))


def append_run(folder: str, script: str, **data) -> None:
    """Append a completed-run record for *folder*."""
    record: dict = {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'), 'script': script}
    if _force_rerun():
        record['force_rerun'] = True
    record.update(data)
    root = _root(folder)
    try:
        library_db.add_run(root, library_db.rel_folder(root, folder), script, record, _UPDATED_BY)
    except (OSError, library_db.sqlite3.Error) as e:
        print(f'  [folder_log] could not write log for {os.path.basename(folder)}: {e}')


def remove_records(folder: str, predicate) -> int:
    """Drop this folder's records for which predicate(record) is true. Returns how many were removed."""
    root = _root(folder)
    return library_db.remove_runs(root, library_db.rel_folder(root, folder), predicate)

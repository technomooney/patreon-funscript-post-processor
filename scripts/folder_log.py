"""Per-folder run history ("has script X already processed this folder?").

Storage: the shared library database (library_db: <library root>/.library-data/library.db, table `runs`) so the
downloader, the AV1 compressor and future tools all see one history instead of thousands of tiny files.

A "library root" is the nearest parent holding a .library-data folder; entry points / migrate_library_data.py
declare one with library_db.use_root(). A folder under NO declared root keeps the old behaviour (a
.folder_log.json inside the folder) so ordinary folders do not each grow their own database.

Records still sitting in legacy files are merged into read(): the folder's .folder_log.json and, for libraries the
AV1 compressor already used, its .av1-compressor/library_log.jsonl and the per-folder logs an earlier
compressor parked under .av1-compressor/legacy_folder_logs/<folder>/. Legacy files are never moved or deleted here
(migrate_library_data.py imports them and then files them under .library-data/legacy/); duplicates already imported into the DB are ignored.
"""
import json
import os
import time

import library_db

FILENAME = '.folder_log.json'                       # legacy per-folder file
COMPRESSOR_LOG = os.path.join('.av1-compressor', 'library_log.jsonl')
# Per-folder logs an earlier AV1 compressor parked here instead of leaving them in place.
PARKED_DIR = os.path.join('.av1-compressor', 'legacy_folder_logs')
_UPDATED_BY = 'downloader'


def _force_rerun() -> bool:
    return os.getenv('FORCE_RERUN', 'false').strip().lower() not in ('false', '0', 'no')


def _declared_root(folder: str):
    """The library root owning *folder*, or None when no .library-data folder exists above it."""
    root = library_db.find_root(folder)
    return root if os.path.isdir(os.path.join(root, library_db.DATA_DIRNAME)) else None


def _legacy_file(folder: str) -> list:
    path = os.path.join(folder, FILENAME)
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _compressor_log(root: str, rel: str) -> list:
    path = os.path.join(root, COMPRESSOR_LOG)
    out = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict) and r.get('folder', '') == rel:
                    out.append({k: v for k, v in r.items() if k != 'folder'})
    except OSError:
        pass
    return out


def _key(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True, ensure_ascii=False)


def read(folder: str) -> list:
    root = _declared_root(folder)
    if root is None:
        return _legacy_file(folder)
    rel = library_db.rel_folder(root, folder)
    records = library_db.runs_for(root, rel)
    seen = {_key(r) for r in records}
    parked = _legacy_file(os.path.join(root, PARKED_DIR, rel))
    for r in parked + _legacy_file(folder) + _compressor_log(root, rel):
        if _key(r) not in seen:
            seen.add(_key(r))
            records.append(r)
    records.sort(key=lambda r: r.get('timestamp', ''))   # stable: DB order kept for equal timestamps
    return records


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
    root = _declared_root(folder)
    try:
        if root is None:
            _append_legacy(folder, record)
        else:
            library_db.add_run(root, library_db.rel_folder(root, folder), script, record, _UPDATED_BY)
    except (OSError, library_db.sqlite3.Error) as e:
        print(f'  [folder_log] could not write log for {os.path.basename(folder)}: {e}')


def remove_records(folder: str, predicate) -> int:
    """Drop this folder's DB records for which predicate(record) is true. Returns how many were removed.

    Legacy files are left alone (they are read-only history awaiting migration)."""
    root = _declared_root(folder)
    if root is None:
        records = _legacy_file(folder)
        kept = [r for r in records if not predicate(r)]
        if len(kept) != len(records):
            _write_legacy(folder, kept)
        return len(records) - len(kept)
    return library_db.remove_runs(root, library_db.rel_folder(root, folder), predicate)


def _write_legacy(folder: str, records: list) -> None:
    path = os.path.join(folder, FILENAME)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _append_legacy(folder: str, record: dict) -> None:
    _write_legacy(folder, _legacy_file(folder) + [record])

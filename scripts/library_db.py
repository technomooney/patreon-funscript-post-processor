"""Shared per-library SQLite store: <library root>/.library-data/library.db

One database per library root replaces the thousands of tiny per-folder history files and the separate
manifests. It travels with the library (libraries are not git repos, so their history has to live inside
them). Machine-local state (profiles, passwords, ...) lives in app_paths.app_home() instead.

Several tools share this file (the downloader, the AV1 compressor, future tools), so the rules matter:
  * SHARED SCHEMA is versioned by PRAGMA user_version (SCHEMA_VERSION). It only ever grows: add tables or
    columns, never rename or drop. A tool that finds a NEWER user_version than it knows refuses to open the
    database for writing (SchemaTooNew) rather than risk corrupting data it does not understand.
  * Each tool owns a private migration track in schema_migrations(tool, version): use register_migration()
    to add its own '<tool>_*' tables without touching anyone else's.
  * Free-form per-tool data goes in tool_data(tool, key, json).
  * Every row says who wrote it last (updated_by) and when.
  * WAL mode and a long busy_timeout let two tools use the file at once; keep transactions short.
Contract and ownership of each table: docs/LIBRARY_DB.md.

Standard library only (the compressor also runs under the system Python); this file is kept byte-identical in
the sibling AV1 compressor repo -- a test compares them.
"""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

DATA_DIRNAME = '.library-data'
DB_NAME = 'library.db'
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 30000

# Directory names no scan may descend into: soft-delete folder + this store's own folder.
SKIP_DIRNAMES = frozenset({'.trash', DATA_DIRNAME})


class SchemaTooNew(RuntimeError):
    """The database was written by a newer tool than this one; refuse rather than corrupt it."""


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    tool TEXT NOT NULL, version INTEGER NOT NULL, applied_at TEXT NOT NULL,
    PRIMARY KEY (tool, version)
);
CREATE TABLE IF NOT EXISTS tool_data (
    tool TEXT NOT NULL, key TEXT NOT NULL, value_json TEXT, updated_by TEXT, updated_at TEXT,
    PRIMARY KEY (tool, key)
);
-- One row per completed script run on a folder (replaces .folder_log.json / library_log.jsonl).
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,             -- relative to the library root, '' = the root itself
    script TEXT NOT NULL,
    ts TEXT NOT NULL,
    data_json TEXT NOT NULL,          -- the whole record as the caller wrote it
    updated_by TEXT
);
CREATE INDEX IF NOT EXISTS runs_folder ON runs(folder, script);
-- File identity: survives rename, extension change and recompression.
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    orig_name TEXT, cur_name TEXT,
    orig_size INTEGER, cur_size INTEGER,
    cur_mtime_ns INTEGER,
    sha256 TEXT,                      -- of the ORIGINAL download when known
    pixeldrain_id TEXT,
    duration REAL, codec TEXT,
    state TEXT,                       -- downloaded | compressed | manual | missing | ...
    extra_json TEXT,
    updated_by TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS files_cur ON files(folder, cur_name);
CREATE INDEX IF NOT EXISTS files_orig ON files(orig_name);
CREATE INDEX IF NOT EXISTS files_pd ON files(pixeldrain_id);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE TABLE IF NOT EXISTS pixeldrain_links (
    link_id TEXT PRIMARY KEY,
    source_url TEXT,
    status TEXT,                      -- done | broken | gone | locked | failed | pending
    remote_sha256 TEXT, remote_name TEXT, remote_size INTEGER,
    first_attempt TEXT, last_attempt TEXT,
    file_id INTEGER,
    extra_json TEXT,                  -- tool-specific fields (pending_path, info_checked, ...)
    updated_by TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS discord_messages (
    creator TEXT NOT NULL, message_id TEXT NOT NULL,
    ts TEXT, author TEXT, text TEXT, hrefs_json TEXT,
    processed INTEGER NOT NULL DEFAULT 0,
    decoded_links_json TEXT,
    updated_by TEXT, updated_at TEXT,
    PRIMARY KEY (creator, message_id)
);
-- Marker files such as .manual that tell tools "hands off / the user decided this".
CREATE TABLE IF NOT EXISTS markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL, kind TEXT NOT NULL,
    first_seen TEXT, seen_at TEXT, cleared_at TEXT,
    UNIQUE (path, kind)
);
-- Differences between what is on disk and what the database expects.
CREATE TABLE IF NOT EXISTS drift (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, folder TEXT, path TEXT, kind TEXT NOT NULL,
    detail_json TEXT,
    status TEXT NOT NULL DEFAULT 'open'   -- open | accepted | resolved
);
CREATE INDEX IF NOT EXISTS drift_status ON drift(status, kind);
"""


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


# ---------------------------------------------------------------------------
# Locating a library root
# ---------------------------------------------------------------------------

_roots: dict[str, str] = {}   # abspath(folder) -> library root, resolved once


def db_path(root: str) -> str:
    return os.path.join(root, DATA_DIRNAME, DB_NAME)


def data_dir(root: str, sub: str = '') -> str:
    """<root>/.library-data[/sub], created if missing (reports, legacy backups, ...)."""
    path = os.path.join(root, DATA_DIRNAME, sub) if sub else os.path.join(root, DATA_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def use_root(root: str) -> str:
    """Declare *root* a library root (entry points call this first). Creates the data folder and database."""
    root = os.path.abspath(root)
    os.makedirs(os.path.join(root, DATA_DIRNAME), exist_ok=True)
    _roots.clear()
    connect(root).close()
    return root


def find_root(folder: str) -> str:
    """Library root owning *folder*: the nearest parent holding a data folder, else the folder itself."""
    folder = os.path.abspath(folder)
    if folder in _roots:
        return _roots[folder]
    cur = folder
    while True:
        if os.path.isdir(os.path.join(cur, DATA_DIRNAME)):
            _roots[folder] = cur
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            _roots[folder] = folder
            return folder
        cur = parent


def rel_folder(root: str, folder: str) -> str:
    rel = os.path.relpath(os.path.abspath(folder), root)
    return '' if rel == '.' else rel


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def connect(root: str) -> sqlite3.Connection:
    """Open (creating/upgrading as needed) the library database. Caller closes it, or use db()."""
    path = db_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=NORMAL')
        version = con.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(
                f'{path} uses schema version {version}, this tool only understands up to {SCHEMA_VERSION}. '
                f'Update this tool before touching the library.')
        if version < SCHEMA_VERSION:
            con.execute('BEGIN IMMEDIATE')
            try:
                for stmt in _statements(_SCHEMA_V1):
                    con.execute(stmt)
                if not con.execute("SELECT 1 FROM meta WHERE key='library_id'").fetchone():
                    con.execute("INSERT INTO meta(key, value) VALUES('library_id', ?)", (str(uuid.uuid4()),))
                    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)", (_now(),))
                con.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                con.execute('COMMIT')
            except BaseException:
                con.execute('ROLLBACK')
                raise
    except BaseException:
        con.close()
        raise
    return con


def _statements(script: str) -> list[str]:
    """Split a schema script into single statements (no semicolons inside our DDL strings)."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith('--')]
    cleaned = '\n'.join(ln.split('--')[0] if '--' in ln else ln for ln in lines)
    return [s.strip() for s in cleaned.split(';') if s.strip()]


@contextmanager
def db(root: str, *, write: bool = False):
    """with db(root) as con: ...   write=True wraps the block in one IMMEDIATE transaction (commit/rollback)."""
    con = connect(root)
    try:
        if write:
            con.execute('BEGIN IMMEDIATE')
            try:
                yield con
                con.execute('COMMIT')
            except BaseException:
                con.execute('ROLLBACK')
                raise
        else:
            yield con
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Meta, per-tool migrations, free-form tool data
# ---------------------------------------------------------------------------

def get_meta(root: str, key: str, default=None):
    with db(root) as con:
        row = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_meta(root: str, key: str, value: str) -> None:
    with db(root, write=True) as con:
        con.execute('INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (key, value))


def library_id(root: str) -> str:
    return get_meta(root, 'library_id')


def register_migration(root: str, tool: str, version: int, statements: list[str]) -> bool:
    """Apply one numbered step of *tool*'s private migration track, once. Returns True if it ran now.

    Statements should create/alter only tables named '<tool>_*'. Never touch another tool's or the shared tables.
    """
    with db(root, write=True) as con:
        if con.execute('SELECT 1 FROM schema_migrations WHERE tool=? AND version=?', (tool, version)).fetchone():
            return False
        for stmt in statements:
            con.execute(stmt)
        con.execute('INSERT INTO schema_migrations(tool, version, applied_at) VALUES(?,?,?)', (tool, version, _now()))
    return True


def tool_data_get(root: str, tool: str, key: str, default=None):
    with db(root) as con:
        row = con.execute('SELECT value_json FROM tool_data WHERE tool=? AND key=?', (tool, key)).fetchone()
    return json.loads(row['value_json']) if row else default


def tool_data_set(root: str, tool: str, key: str, value, updated_by: str = '') -> None:
    with db(root, write=True) as con:
        con.execute(
            'INSERT INTO tool_data(tool, key, value_json, updated_by, updated_at) VALUES(?,?,?,?,?) '
            'ON CONFLICT(tool, key) DO UPDATE SET value_json=excluded.value_json, '
            'updated_by=excluded.updated_by, updated_at=excluded.updated_at',
            (tool, key, json.dumps(value, ensure_ascii=False), updated_by or tool, _now()))


# ---------------------------------------------------------------------------
# runs: the folder-log history
# ---------------------------------------------------------------------------

def runs_for(root: str, folder_rel: str) -> list[dict]:
    """Every run record of one folder, oldest first, as the dicts callers originally wrote."""
    with db(root) as con:
        rows = con.execute('SELECT data_json FROM runs WHERE folder=? ORDER BY id', (folder_rel,)).fetchall()
    out = []
    for r in rows:
        try:
            rec = json.loads(r['data_json'])
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def add_run(root: str, folder_rel: str, script: str, record: dict, updated_by: str = '') -> None:
    ts = record.get('timestamp') or _now()
    with db(root, write=True) as con:
        con.execute('INSERT INTO runs(folder, script, ts, data_json, updated_by) VALUES(?,?,?,?,?)',
                    (folder_rel, script, ts, json.dumps(record, ensure_ascii=False), updated_by or script))


def remove_runs(root: str, folder_rel: str, predicate) -> int:
    """Delete this folder's run records for which predicate(record) is true. Returns how many were removed."""
    with db(root, write=True) as con:
        rows = con.execute('SELECT id, data_json FROM runs WHERE folder=?', (folder_rel,)).fetchall()
        doomed = []
        for r in rows:
            try:
                rec = json.loads(r['data_json'])
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and predicate(rec):
                doomed.append(r['id'])
        for rid in doomed:
            con.execute('DELETE FROM runs WHERE id=?', (rid,))
    return len(doomed)

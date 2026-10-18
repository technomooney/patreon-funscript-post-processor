"""Machine-local app database: <app home>/app.db (see app_paths for where that is).

Holds per-user/per-machine state that must NOT travel with a library or live in a repo checkout:
creator profiles (Discord guild/channel, unattended-run configs), the password history that creator_db keeps,
and a registry of the libraries this machine has seen. Per-library history lives in library_db instead.

Tables are created on demand (IF NOT EXISTS) and only ever grow. creator_db adds its own tables to the same file.
Standard library only.
"""
import json
import sqlite3
import time

import app_paths

BUSY_TIMEOUT_MS = 30000

_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)',
    # one row per creator; profile_json is that creator's whole profile dict (see creator_profiles)
    'CREATE TABLE IF NOT EXISTS creator_profiles (creator_key TEXT PRIMARY KEY, profile_json TEXT NOT NULL)',
    # libraries this machine has seen: library_id (UUID stored inside each library DB) -> where it lives now
    'CREATE TABLE IF NOT EXISTS libraries (library_id TEXT PRIMARY KEY, path TEXT NOT NULL, '
    'nickname TEXT, first_seen TEXT, last_seen TEXT)',
)


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the app database (or *path*, for tests/overrides), creating the shared tables if missing."""
    con = sqlite3.connect(path or app_paths.app_db_path(), timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    con.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
    con.execute('PRAGMA journal_mode=WAL')
    for stmt in _SCHEMA:
        con.execute(stmt)
    con.commit()
    return con


def get_meta(con: sqlite3.Connection, key: str, default=None):
    row = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute('INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, value))


def load_profiles(con: sqlite3.Connection) -> dict:
    out = {}
    for r in con.execute('SELECT creator_key, profile_json FROM creator_profiles'):
        try:
            out[r['creator_key']] = json.loads(r['profile_json'])
        except json.JSONDecodeError:
            continue
    return out


def save_profiles(con: sqlite3.Connection, profiles: dict) -> None:
    """Replace every creator profile with *profiles* (same whole-dict semantics the old .creators.json had)."""
    with con:
        con.execute('DELETE FROM creator_profiles')
        con.executemany('INSERT INTO creator_profiles(creator_key, profile_json) VALUES(?, ?)',
                        [(k, json.dumps(v, ensure_ascii=False)) for k, v in profiles.items()])


def register_library(library_id: str, path: str, nickname: str = '') -> None:
    """Remember where a library lives (updates the path if the library was moved)."""
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    con = connect()
    try:
        with con:
            con.execute(
                'INSERT INTO libraries(library_id, path, nickname, first_seen, last_seen) VALUES(?,?,?,?,?) '
                'ON CONFLICT(library_id) DO UPDATE SET path=excluded.path, last_seen=excluded.last_seen, '
                'nickname=CASE WHEN excluded.nickname != \'\' THEN excluded.nickname ELSE libraries.nickname END',
                (library_id, path, nickname, now, now))
    finally:
        con.close()


def libraries() -> list[dict]:
    con = connect()
    try:
        return [dict(r) for r in con.execute('SELECT * FROM libraries ORDER BY last_seen DESC')]
    finally:
        con.close()

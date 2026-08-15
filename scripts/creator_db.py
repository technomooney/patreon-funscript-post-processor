"""SQLite-backed per-creator data store for password-protected script archives.

Kept separate from creator_profiles.py's .creators.json (Discord guild/channel
config, flat and rarely-changing) because this is genuinely historical,
queryable data: creators like Pize rotate the password on their archives over
time, sometimes re-encrypting *old* archives under the newest password and
sometimes not — so a later extraction attempt may need to try several
passwords, oldest-known to newest, not just "whatever we last saw". A single
JSON blob doesn't fit that as naturally as a real table does.

DB file: .creator_data.db at repo root, gitignored (mirrors .creators.json).

Used by extract_variant_archives.py; discord_passwords.py persists what it
finds here so a later run doesn't need to re-scan Discord for a password
that's already on file and still works.
"""
import datetime
import os
import sqlite3

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.creator_data.db')


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec='seconds')


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS passwords (
            creator_key       TEXT NOT NULL,
            password          TEXT NOT NULL,
            source            TEXT NOT NULL DEFAULT 'discord',
            first_seen        TEXT NOT NULL,
            last_seen         TEXT NOT NULL,
            confirmed_working INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (creator_key, password)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS extractions (
            archive_path  TEXT PRIMARY KEY,
            status        TEXT NOT NULL,
            password_used TEXT,
            updated_at    TEXT NOT NULL
        )
    ''')
    return conn


def record_passwords(creator_key: str, passwords: list[str], source: str = 'discord') -> None:
    """Upsert *passwords* for *creator_key*. A password already on file just gets its
    last_seen bumped — it isn't dropped, since an older password can still be the
    right one for an archive the creator never re-encrypted."""
    if not passwords:
        return
    now = _now()
    creator_key = creator_key.strip().lower()
    with _connect() as conn:
        for pw in passwords:
            conn.execute('''
                INSERT INTO passwords (creator_key, password, source, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (creator_key, password) DO UPDATE SET last_seen = excluded.last_seen
            ''', (creator_key, pw, source, now, now))


def mark_confirmed(creator_key: str, password: str) -> None:
    """Flag a password as proven to work (an archive actually extracted with it), so
    it sorts ahead of newer-but-unconfirmed candidates on the next lookup."""
    creator_key = creator_key.strip().lower()
    with _connect() as conn:
        conn.execute('''
            UPDATE passwords SET confirmed_working = 1, last_seen = ?
            WHERE creator_key = ? AND password = ?
        ''', (_now(), creator_key, password))


def get_password_history(creator_key: str) -> list[str]:
    """Every known password for *creator_key*, most-likely-current first:
    confirmed-working ones first, then most-recently-seen."""
    creator_key = creator_key.strip().lower()
    with _connect() as conn:
        rows = conn.execute('''
            SELECT password FROM passwords WHERE creator_key = ?
            ORDER BY confirmed_working DESC, last_seen DESC
        ''', (creator_key,)).fetchall()
    return [r[0] for r in rows]


def extraction_status(archive_path: str) -> str | None:
    """'extracted', 'failed', or None if this archive has never been attempted."""
    with _connect() as conn:
        row = conn.execute(
            'SELECT status FROM extractions WHERE archive_path = ?', (archive_path,)
        ).fetchone()
    return row[0] if row else None


def record_extraction(archive_path: str, status: str, password_used: str | None) -> None:
    with _connect() as conn:
        conn.execute('''
            INSERT INTO extractions (archive_path, status, password_used, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (archive_path) DO UPDATE SET
                status = excluded.status, password_used = excluded.password_used, updated_at = excluded.updated_at
        ''', (archive_path, status, password_used, _now()))

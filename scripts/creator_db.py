"""SQLite-backed per-creator data store for password-protected script archives.

Kept separate from creator_profiles.py's .creators.json (Discord guild/channel
config, flat and rarely-changing) because this is genuinely historical,
queryable data: creators like Pize rotate the password on their archives over
time, sometimes re-encrypting *old* archives under the newest password and
sometimes not — so a later extraction attempt may need to try several
passwords, oldest-known to newest, not just "whatever we last saw". A single
JSON blob doesn't fit that as naturally as a real table does.

DB file: the machine-local app database (app_db: <app home>/app.db), alongside creator_profiles. It used to be
.creator_data.db at the repo root; that file's rows are copied in once and the file is left untouched.

Used by extract_variant_archives.py; discord_passwords.py persists what it
finds here so a later run doesn't need to re-scan Discord for a password
that's already on file and still works.
"""
import calendar
import datetime
import os
import re
import sqlite3

import app_db
import app_paths
import legacy_app_data

# CREATOR_DB_PATH overrides the location -- lets a test run against a
# throwaway folder use its own DB instead of the real password history.
# (Unset: the shared app database, resolved lazily so importing has no side effects.)
_DB_OVERRIDE = os.getenv('CREATOR_DB_PATH')
_LEGACY_NAME = '.creator_data.db'
_LEGACY_TABLES = ('passwords', 'extractions', 'password_labels')
_IMPORTED_KEY = 'creator_data_db_imported'


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec='seconds')


def _db_path() -> str:
    return _DB_OVERRIDE or app_paths.app_db_path()


def _import_legacy(conn: sqlite3.Connection) -> None:
    """Copy the rows of the old repo-root .creator_data.db into this database, once (INSERT OR IGNORE, so a
    password already on file wins). The old file is only read."""
    if _DB_OVERRIDE:
        return
    conn.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    if conn.execute('SELECT 1 FROM meta WHERE key=?', (_IMPORTED_KEY,)).fetchone():
        return
    src_path = legacy_app_data.legacy_path(_LEGACY_NAME)
    if os.path.isfile(src_path):
        src = None
        try:
            src = sqlite3.connect(f'file:{src_path}?mode=ro', uri=True)
            for table in _LEGACY_TABLES:
                try:
                    rows = src.execute(f'SELECT * FROM {table}').fetchall()
                except sqlite3.Error:
                    continue        # the old file never had this table
                if rows:
                    marks = ','.join('?' * len(rows[0]))
                    conn.executemany(f'INSERT OR IGNORE INTO {table} VALUES ({marks})', rows)
        except sqlite3.Error as e:
            print(f'  [creator_db] could not read {src_path}: {e} -- will retry next time')
            return
        finally:
            if src is not None:
                src.close()
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES(?, '1')", (_IMPORTED_KEY,))
    conn.commit()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
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
    # Some creators (illustrative examples given by the user, not yet
    # confirmed against a real post — see discord_passwords.parse_labeled_
    # passwords) label a Discord password with the post date range/month and
    # post type ("Collection" vs "standalone post") it applies to, e.g.
    # "2026.08.16-2026.08.31 standalone post PW  <value>". Stored separately
    # from `passwords` itself (many labels can point at the same password,
    # and a label with no parseable date is just dropped, never blocking
    # anything) purely to let get_password_history() try the right-looking
    # password first for an archive whose own post date it knows.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS password_labels (
            creator_key TEXT NOT NULL,
            password    TEXT NOT NULL,
            date_label  TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (creator_key, password, date_label, kind)
        )
    ''')
    _import_legacy(conn)
    return conn


def record_password_labels(creator_key: str, entries: list[dict]) -> None:
    """Persist (password, date_label, kind) hints parsed from a Discord post
    (see discord_passwords.parse_labeled_passwords). Purely an optimization —
    get_password_history() uses these to try the best-guess password first
    when it's given the archive's own post date, but a label that fails to
    parse or match never excludes anything; every known password still gets
    tried eventually."""
    if not entries:
        return
    creator_key = creator_key.strip().lower()
    with _connect() as conn:
        for e in entries:
            pw = (e.get('password') or '').strip()
            date_label = (e.get('date_label') or '').strip()
            if not pw or not date_label:
                continue
            conn.execute('''
                INSERT OR IGNORE INTO password_labels (creator_key, password, date_label, kind)
                VALUES (?, ?, ?, ?)
            ''', (creator_key, pw, date_label, (e.get('kind') or '').strip().lower()))


def _month_end(year: int, month: int) -> datetime.date:
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def _parse_date_label(label: str) -> tuple[datetime.date, datetime.date] | None:
    """Best-effort parse of a password label's date portion into an inclusive
    (start, end) range, for the containment check get_password_history() uses.
    Handles every shape seen in the (illustrative, unconfirmed-live) examples:
        '2026.09.01'              -> that single day
        '2026.08.01-2026.08.15'   -> that full date range
        '2026.02-07'              -> Feb-Jul 2026 (a bare month range)
        '2026.08'                 -> the whole month
    Returns None on anything else — callers treat that as "no date hint",
    never as a reason to skip a password.
    """
    label = label.strip()
    sides = re.split(r'\s*[-~]\s*', label, maxsplit=1)

    def _nums(side: str) -> list[int] | None:
        try:
            return [int(n) for n in re.split(r'[.\-]', side) if n]
        except ValueError:
            return None

    left = _nums(sides[0])
    if not left or left[0] < 1000:  # must start with a plausible 4-digit year
        return None
    year, month = left[0], (left[1] if len(left) > 1 else 1)
    day = left[2] if len(left) > 2 else None
    try:
        if len(sides) == 1:
            if day is not None:
                d = datetime.date(year, month, day)
                return d, d
            return datetime.date(year, month, 1), _month_end(year, month)

        right = _nums(sides[1])
        if not right:
            return None
        if len(right) >= 3:
            r_year, r_month, r_day = right[0], right[1], right[2]
        elif len(right) == 2:
            r_year, r_month, r_day = year, right[0], right[1]
        elif right[0] > 31:  # bare 4-digit year on the right side
            r_year, r_month, r_day = right[0], 1, None
        elif day is not None:  # e.g. '2026.08.01-15' -> day 15 of the same month
            r_year, r_month, r_day = year, month, right[0]
        else:  # e.g. '2026.02-07' -> month 7 of the same year
            r_year, r_month, r_day = year, right[0], None

        start = datetime.date(year, month, day or 1)
        end = datetime.date(r_year, r_month, r_day) if r_day is not None else _month_end(r_year, r_month)
        return start, end
    except ValueError:
        return None  # an out-of-range date component (e.g. month 13) -- not a real date label


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


def get_password_history(
        creator_key: str, post_date: datetime.date | None = None,
        is_collection: bool | None = None) -> list[str]:
    """Every known password for *creator_key*, most-likely-current first:
    confirmed-working ones first, then most-recently-seen.

    *post_date*/*is_collection* (the archive's own containing post's date and
    whether that post looks like a "collection"/pack post — see
    extract_variant_archives._post_context) move a password whose recorded
    label (see record_password_labels) matches ahead of that base ordering —
    a date-range match ahead of no match, and a date+kind match ahead of a
    date-only match. This only ever *reorders* what's tried first; a password
    with no matching (or no) label still gets tried, just later.
    """
    creator_key = creator_key.strip().lower()
    with _connect() as conn:
        rows = conn.execute('''
            SELECT password FROM passwords WHERE creator_key = ?
            ORDER BY confirmed_working DESC, last_seen DESC
        ''', (creator_key,)).fetchall()
        passwords = [r[0] for r in rows]
        if post_date is None or not passwords:
            return passwords
        label_rows = conn.execute('''
            SELECT password, date_label, kind FROM password_labels WHERE creator_key = ?
        ''', (creator_key,)).fetchall()

    if not label_rows:
        return passwords

    scores: dict[str, int] = {}
    for password, date_label, kind in label_rows:
        date_range = _parse_date_label(date_label)
        if date_range is None:
            continue
        start, end = date_range
        if not (start <= post_date <= end):
            continue
        kind_matches = is_collection is not None and kind and (kind == 'collection') == is_collection
        score = 2 if kind_matches else 1
        scores[password] = max(scores.get(password, 0), score)

    if not scores:
        return passwords
    # sorted() is stable, so passwords tied on relevance keep the base
    # confirmed/most-recent-first ordering from the query above.
    return sorted(passwords, key=lambda p: -scores.get(p, 0))


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

"""Download outcomes and report tables, stored in the library database instead of loose CSV files.

Two downloader-owned tables (migration track 'downloader' v2, see library_db.register_migration):

* downloader_link_outcomes: one row per (folder, url) that failed or needs a human look, with WHY it failed (the
  HTTP status when we got one, a short reason, and whether the link is permanently dead so no run retries it).
  A link that later downloads is removed again, so the table only ever lists what still needs attention.
* downloader_report_rows: every other report a tool used to write as a CSV (funscript_check, many_funscripts,
  garbled_names, ...). A report is replaced as a whole each time its tool runs, exactly like the CSV was
  rewritten; the HTML report (generate_audit_report) reads these rows.

Both are keyed relative to the library root, so a library keeps working when it is moved.
"""
import json
import os

import library_db

_TOOL = 'downloader'
_VERSION = 2
_MIGRATION = [
    """CREATE TABLE IF NOT EXISTS downloader_link_outcomes (
        folder TEXT NOT NULL,             -- relative to the library root
        url TEXT NOT NULL,
        kind TEXT NOT NULL,               -- failed | uncertain
        domain TEXT,
        funscript_name TEXT,
        filename TEXT,
        reason TEXT,                      -- short machine-ish cause: http_404, mega_taken_down, timeout, ...
        http_status INTEGER,              -- status code of the page/API call when there was one
        permanent INTEGER NOT NULL DEFAULT 0,   -- 1 = taken down / will never work: never retry
        matched_video TEXT,               -- uncertain only: the existing video that may already be this link
        match_score REAL,
        attempts INTEGER NOT NULL DEFAULT 1,
        first_seen TEXT, last_attempt TEXT,
        PRIMARY KEY (folder, url)
    )""",
    "CREATE INDEX IF NOT EXISTS downloader_link_outcomes_permanent ON downloader_link_outcomes(permanent)",
    """CREATE TABLE IF NOT EXISTS downloader_report_rows (
        report TEXT NOT NULL,             -- the old CSV's name without .csv, plus any run suffix
        seq INTEGER NOT NULL,
        data_json TEXT NOT NULL,
        created_at TEXT,
        PRIMARY KEY (report, seq)
    )""",
]

KINDS = ('failed', 'uncertain')


def _locate(folder: str):
    """Library root owning *folder* with the migration applied, or None when it is not inside a declared library."""
    root = library_db.find_root(folder)
    if not os.path.isdir(os.path.join(root, library_db.DATA_DIRNAME)):
        return None
    library_db.register_migration(root, _TOOL, _VERSION, _MIGRATION)
    return root


def _rel(root: str, folder: str) -> str:
    return library_db.rel_folder(root, folder) if folder else ''


# ---------------------------------------------------------------------------
# Link outcomes
# ---------------------------------------------------------------------------

def record_outcome(base: str, url: str, folder: str, kind: str = 'failed', *, domain: str = '',
                   funscript_name: str = '', filename: str = '', reason: str = '', http_status: int | None = None,
                   permanent: bool = False, matched_video: str = '', match_score: float | None = None) -> bool:
    """Upsert the outcome of one link. A repeat bumps `attempts`; `permanent` never goes back to 0 on its own
    (a link once proven dead stays dead until forget() is called). False when *base* has no library database."""
    if kind not in KINDS:
        raise ValueError(f'kind must be one of {KINDS}, not {kind!r}')
    root = _locate(base)
    if root is None:
        return False
    now = library_db._now()
    with library_db.db(root, write=True) as con:
        con.execute(
            'INSERT INTO downloader_link_outcomes(folder, url, kind, domain, funscript_name, filename, reason, '
            'http_status, permanent, matched_video, match_score, attempts, first_seen, last_attempt) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?) '
            'ON CONFLICT(folder, url) DO UPDATE SET kind=excluded.kind, domain=excluded.domain, '
            'funscript_name=excluded.funscript_name, filename=excluded.filename, reason=excluded.reason, '
            'http_status=excluded.http_status, permanent=MAX(permanent, excluded.permanent), '
            'matched_video=excluded.matched_video, match_score=excluded.match_score, '
            'attempts=attempts+1, last_attempt=excluded.last_attempt',
            (_rel(root, folder), url, kind, domain, funscript_name, filename, reason, http_status,
             1 if permanent else 0, matched_video, match_score, now, now))
    return True


def forget(base: str, url: str, folder: str) -> None:
    """Drop a link's outcome (it downloaded, or the user decided to retry it)."""
    root = _locate(base)
    if root is None:
        return
    with library_db.db(root, write=True) as con:
        con.execute('DELETE FROM downloader_link_outcomes WHERE folder=? AND url=?', (_rel(root, folder), url))


def outcomes(base: str, kind: str | None = None, *, permanent: bool | None = None) -> list[dict]:
    """Outcome rows with absolute `save_directory` (the column the old CSVs used), oldest first."""
    root = _locate(base)
    if root is None:
        return []
    sql, args = 'SELECT * FROM downloader_link_outcomes WHERE 1=1', []
    if kind is not None:
        sql += ' AND kind=?'
        args.append(kind)
    if permanent is not None:
        sql += ' AND permanent=?'
        args.append(1 if permanent else 0)
    with library_db.db(root) as con:
        rows = [dict(r) for r in con.execute(sql + ' ORDER BY first_seen, rowid', args)]
    for r in rows:
        r['save_directory'] = os.path.normpath(os.path.join(root, r.pop('folder')))
    return rows


def known_dead_links(base: str) -> set[str]:
    """URLs proven permanently dead: skipped on every run, whatever SKIP_KNOWN_FAILURES says."""
    root = _locate(base)
    if root is None:
        return set()
    with library_db.db(root) as con:
        return {r[0] for r in con.execute('SELECT url FROM downloader_link_outcomes WHERE permanent=1')}


# ---------------------------------------------------------------------------
# Report rows (the former CSV files)
# ---------------------------------------------------------------------------

def replace_report(base: str, report: str, rows: list[dict]) -> bool:
    """Make *rows* the whole content of *report*. An empty list clears it (the old CSV was simply not written)."""
    root = _locate(base)
    if root is None:
        return False
    now = library_db._now()
    with library_db.db(root, write=True) as con:
        con.execute('DELETE FROM downloader_report_rows WHERE report=?', (report,))
        con.executemany(
            'INSERT INTO downloader_report_rows(report, seq, data_json, created_at) VALUES(?,?,?,?)',
            [(report, i, json.dumps(r, ensure_ascii=False), now) for i, r in enumerate(rows)])
    return True


def read_report(base: str, report: str) -> list[dict]:
    root = _locate(base)
    if root is None:
        return []
    with library_db.db(root) as con:
        return [json.loads(r[0]) for r in con.execute(
            'SELECT data_json FROM downloader_report_rows WHERE report=? ORDER BY seq', (report,))]


def report_names(base: str) -> list[str]:
    root = _locate(base)
    if root is None:
        return []
    with library_db.db(root) as con:
        return [r[0] for r in con.execute('SELECT DISTINCT report FROM downloader_report_rows ORDER BY report')]

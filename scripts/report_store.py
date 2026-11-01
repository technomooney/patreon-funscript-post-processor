"""Download outcomes and report tables, stored in the library database instead of loose CSV files.

Downloader-owned tables (migration track 'downloader' v2 to v5, see library_db.register_migration):

* downloader_link_outcomes: one row per (folder, url) that failed or needs a human look, with WHY it failed (the
  HTTP status when we got one, a short reason, and whether the link is permanently dead so no run retries it).
  A link that later downloads is removed again, so the table only ever lists what still needs attention.
* downloader_report_rows: every other report a tool used to write as a CSV (funscript_check, many_funscripts,
  garbled_names, ...). A report is replaced as a whole each time its tool runs, exactly like the CSV was
  rewritten; the HTML report (generate_audit_report) reads these rows.
* downloader_progress: the resume state of an interrupted download run (was .download_progress.json).

They are keyed relative to the library root, so a library keeps working when it is moved.
"""
import json
import os

import library_db

_TOOL = 'downloader'
_MIGRATION_V2 = [
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

# Resume state of an interrupted download run: a (folder, url) row per finished link, and a row with url '' per post
# whose links were all attempted. Cleared when a run completes cleanly.
_MIGRATION_V3 = [
    """CREATE TABLE IF NOT EXISTS downloader_progress (
        folder TEXT NOT NULL,             -- relative to the library root
        url TEXT NOT NULL,                -- '' = the whole post was attempted
        PRIMARY KEY (folder, url)
    )""",
]
# When a retryable failure (a rate limit) is worth another try; later runs leave the link alone until then.
_MIGRATION_V4 = [
    "ALTER TABLE downloader_link_outcomes ADD COLUMN retry_after TEXT",
]
# Run history and what each run asked of the network (see run_stats): one row per run, per host in a run, and per link.
_MIGRATION_V5 = [
    """CREATE TABLE IF NOT EXISTS downloader_run_stats (
        run_id TEXT PRIMARY KEY,
        script TEXT, started_at TEXT, ended_at TEXT,
        status TEXT,                     -- running | completed | stopped | interrupted
        duration_s REAL,
        tasks INTEGER, links_seen INTEGER, downloaded INTEGER, duplicates INTEGER, failed INTEGER, deferred INTEGER,
        skipped INTEGER, bytes_saved INTEGER,
        requests INTEGER, bytes_transferred INTEGER, request_seconds REAL,
        code_version TEXT, settings_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS downloader_host_stats (
        run_id TEXT NOT NULL, host TEXT NOT NULL,
        requests INTEGER, bytes INTEGER, seconds REAL, ok INTEGER, redirects INTEGER, client_errors INTEGER,
        forbidden INTEGER, rate_limited INTEGER, server_errors INTEGER, errors INTEGER, slowest_seconds REAL,
        PRIMARY KEY (run_id, host)
    )""",
    """CREATE TABLE IF NOT EXISTS downloader_link_stats (
        run_id TEXT NOT NULL, folder TEXT NOT NULL, url TEXT NOT NULL,
        host TEXT, started_at TEXT, requests INTEGER, bytes INTEGER, seconds REAL,
        precheck_seconds REAL, download_seconds REAL,
        precheck_result TEXT,            -- what settled the pre-check: name match, same-size shortcut, audio, visual, ...
        outcome TEXT,
        PRIMARY KEY (run_id, folder, url)
    )""",
    "CREATE INDEX IF NOT EXISTS downloader_link_stats_host ON downloader_link_stats(host)",
]
_MIGRATIONS = ((2, _MIGRATION_V2), (3, _MIGRATION_V3), (4, _MIGRATION_V4), (5, _MIGRATION_V5))

KINDS = ('failed', 'uncertain')


def _locate(folder: str):
    """Library root owning *folder* with the migration applied, or None when it is not inside a declared library."""
    root = library_db.find_root(folder)
    if not os.path.isdir(os.path.join(root, library_db.DATA_DIRNAME)):
        return None
    for version, statements in _MIGRATIONS:
        library_db.register_migration(root, _TOOL, version, statements)
    return root


def root_for(base: str) -> str | None:
    """The library root owning *base* with every downloader table in place, or None when it is not in a library."""
    return _locate(base)


def _rel(root: str, folder: str) -> str:
    return library_db.rel_folder(root, folder) if folder else ''


# ---------------------------------------------------------------------------
# Link outcomes
# ---------------------------------------------------------------------------

def record_outcome(base: str, url: str, folder: str, kind: str = 'failed', *, domain: str = '',
                   funscript_name: str = '', filename: str = '', reason: str = '', http_status: int | None = None,
                   permanent: bool = False, matched_video: str = '', match_score: float | None = None,
                   retry_after: str | None = None) -> bool:
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
            'http_status, permanent, matched_video, match_score, attempts, first_seen, last_attempt, retry_after) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?) '
            'ON CONFLICT(folder, url) DO UPDATE SET kind=excluded.kind, domain=excluded.domain, '
            'funscript_name=excluded.funscript_name, filename=excluded.filename, reason=excluded.reason, '
            'http_status=excluded.http_status, permanent=MAX(permanent, excluded.permanent), '
            'matched_video=excluded.matched_video, match_score=excluded.match_score, '
            'attempts=attempts+1, last_attempt=excluded.last_attempt, retry_after=excluded.retry_after',
            (_rel(root, folder), url, kind, domain, funscript_name, filename, reason, http_status,
             1 if permanent else 0, matched_video, match_score, now, now, retry_after))
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


def cooling_down_links(base: str) -> set[str]:
    """URLs whose retry_after time has not come yet (rate-limited): later runs leave them alone until it has."""
    root = _locate(base)
    if root is None:
        return set()
    with library_db.db(root) as con:
        return {r[0] for r in con.execute(
            'SELECT url FROM downloader_link_outcomes WHERE retry_after IS NOT NULL AND retry_after > ?',
            (library_db._now(),))}


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


# ---------------------------------------------------------------------------
# Row helpers for the download run
# ---------------------------------------------------------------------------

def record_rows(base: str, rows: list[dict], kind: str = 'failed') -> int:
    """Record failure/uncertain rows in the shape the downloader builds them (link, save_directory, ...)."""
    n = 0
    for row in rows:
        if not row.get('link'):
            continue
        ok = record_outcome(
            base, row['link'], row.get('save_directory') or base, kind,
            domain=row.get('domain') or '', funscript_name=row.get('funscript_name') or '',
            filename=row.get('filename') or '', reason=row.get('reason') or '',
            http_status=row.get('http_status'), permanent=bool(row.get('permanent')),
            matched_video=row.get('matched_video') or '',
            match_score=float(row['match_score']) if row.get('match_score') not in (None, '') else None,
            retry_after=row.get('retry_after') or None)
        n += ok
    return n


def forget_many(base: str, pairs) -> None:
    """Drop the outcomes of many (url, folder) pairs in one transaction."""
    root = _locate(base)
    if root is None:
        return
    with library_db.db(root, write=True) as con:
        con.executemany('DELETE FROM downloader_link_outcomes WHERE folder=? AND url=?',
                        [(_rel(root, f), u) for u, f in pairs])


def failed_links(base: str) -> set[str]:
    """Every URL currently listed as failed (the old failed_downloads.csv 'link' column)."""
    return {r['url'] for r in outcomes(base, 'failed')}


def replace_table(base: str, name: str, header: list[str], rows: list[list]) -> str:
    """replace_report() for a report built as a header plus plain row lists (what csv.writer was given).
    Returns a short label for a "written to ..." message."""
    replace_report(base, name, [dict(zip(header, r)) for r in rows])
    return f"report '{name}' (library database)"


# ---------------------------------------------------------------------------
# Resume state of a download run
# ---------------------------------------------------------------------------

def progress_load(base: str) -> tuple[dict[str, set[str]], set[str]]:
    """({folder: finished link urls}, {folders whose links were all attempted}) with absolute folder paths."""
    root = _locate(base)
    if root is None:
        return {}, set()
    links: dict[str, set[str]] = {}
    folders: set[str] = set()
    with library_db.db(root) as con:
        for folder, url in con.execute('SELECT folder, url FROM downloader_progress'):
            path = os.path.normpath(os.path.join(root, folder))
            if url:
                links.setdefault(path, set()).add(url)
            else:
                folders.add(path)
    return links, folders


def progress_mark(base: str, folder: str, url: str = '') -> None:
    """Record a finished link, or (url '') a post whose links were all attempted."""
    root = _locate(base)
    if root is None:
        return
    with library_db.db(root, write=True) as con:
        con.execute('INSERT OR IGNORE INTO downloader_progress(folder, url) VALUES(?,?)', (_rel(root, folder), url))


def progress_clear(base: str) -> None:
    root = _locate(base)
    if root is None:
        return
    with library_db.db(root, write=True) as con:
        con.execute('DELETE FROM downloader_progress')

"""Links discovered *outside* the Patreon post, such as the video links inside an eroscripts thread.

Some creators publish their videos only through those links, so they are a first-class source of links like the
post's own. They live in the library database (library_db, table downloader_external_links, owned by the
downloader) so a later run can tell what a source already yielded. Keyed by folder + url: a folder is its own
scope, the same link in another post's folder is a separate row.

The downloader declares its folder a library before it runs (folder_log.ensure_library); a folder under no
declared root simply has nothing recorded.
"""
import json
import os

import library_db

_TOOL = 'downloader'
_UPDATED_BY = 'downloader'
_MIGRATION = [
    """CREATE TABLE IF NOT EXISTS downloader_external_links (
        folder TEXT NOT NULL,            -- relative to the library root
        url TEXT NOT NULL,
        source_kind TEXT NOT NULL,       -- eroscripts | ...
        source_url TEXT,                 -- the page the link was found on
        status TEXT NOT NULL,            -- downloaded | failed
        first_seen TEXT, last_attempt TEXT,
        files_json TEXT,
        PRIMARY KEY (folder, url)
    )""",
]


def _locate(folder: str):
    """(library root, relative folder) for *folder*, or None when it is not inside a declared library."""
    root = library_db.find_root(folder)
    if not os.path.isdir(os.path.join(root, library_db.DATA_DIRNAME)):
        return None
    library_db.register_migration(root, _TOOL, 1, _MIGRATION)
    return root, library_db.rel_folder(root, folder)


def record(folder: str, url: str, source_kind: str, source_url: str, status: str, files: list[str] | None = None) -> bool:
    """Upsert one discovered link. Returns False (and stores nothing) when *folder* has no library database."""
    loc = _locate(folder)
    if loc is None:
        return False
    root, rel = loc
    now = library_db._now()
    with library_db.db(root, write=True) as con:
        con.execute(
            'INSERT INTO downloader_external_links(folder, url, source_kind, source_url, status, first_seen, '
            'last_attempt, files_json) VALUES(?,?,?,?,?,?,?,?) '
            'ON CONFLICT(folder, url) DO UPDATE SET source_kind=excluded.source_kind, source_url=excluded.source_url, '
            'status=excluded.status, last_attempt=excluded.last_attempt, '
            'files_json=COALESCE(excluded.files_json, files_json)',
            (rel, url, source_kind, source_url, status, now, now, json.dumps(files) if files else None))
    return True


def downloaded_urls(folder: str) -> set[str]:
    """URLs already fetched successfully for *folder* from any external source (failed ones stay retryable)."""
    loc = _locate(folder)
    if loc is None:
        return set()
    root, rel = loc
    with library_db.db(root) as con:
        rows = con.execute("SELECT url FROM downloader_external_links WHERE folder=? AND status='downloaded'",
                           (rel,)).fetchall()
    return {r[0] for r in rows}


def source_settled(folder: str, source_url: str) -> bool:
    """True when everything found on *source_url* (e.g. a thread) for *folder* downloaded and nothing there failed."""
    loc = _locate(folder)
    if loc is None:
        return False
    root, rel = loc
    with library_db.db(root) as con:
        statuses = {r[0] for r in con.execute(
            'SELECT status FROM downloader_external_links WHERE folder=? AND source_url=?', (rel, source_url))}
    return statuses == {'downloaded'}

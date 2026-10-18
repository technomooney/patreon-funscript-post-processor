"""Drift detection: where does the library on disk differ from what the shared database expects?

Anyone can change a library behind the tools' backs -- rename a file, replace it with their own edit, delete it,
drop a `.manual` marker into a folder. This module notices, records each difference in the `drift` table and every
marker file in the `markers` table, and never changes a single file. Tools use it to avoid acting on something the
user touched (see `needs_attention`), and the user reviews/accepts the differences with check_library_drift.py.

Findings (drift.kind):
  missing            a `files` row's current file is not on disk (detail says if it is in .trash)
  changed            the file's size differs from the recorded current size (replaced or edited by hand)
  renamed_suspected  a file is missing and an unrecorded file of exactly the same size sits in the same folder
  marker_added / marker_removed   a `.manual` / `.consolidated` marker appeared / disappeared since the last scan
Not drift: a file that is simply not in the database yet (counted in the report only -- a freshly migrated library
has thousands), and mtime-only differences (a copied library would flag everything).

Lifecycle: a finding is 'open' while it holds, becomes 'resolved' on its own when the disk matches again, and
'accepted' when the user says "this is the new normal" (which also updates the files row to match the disk).

Standard library only; kept byte-identical in the sibling AV1 compressor repo.
"""
import json
import os
import time

import library_db

MARKER_NAMES = ('.manual', '.consolidated')
_SKIP = set(library_db.SKIP_DIRNAMES) | {'.av1-compressor'}
_UPDATED_BY = 'library_drift'


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


# ---------------------------------------------------------------------------
# Scanning (read-only with respect to the library's files)
# ---------------------------------------------------------------------------

def _scan_markers(root: str) -> set[tuple[str, str]]:
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP]
        for name in MARKER_NAMES:
            if name in filenames:
                rel = os.path.relpath(dirpath, root)
                found.add(('' if rel == '.' else rel, name))
    return found


def _folder_listing(root: str, rel: str) -> dict[str, int]:
    """{file name: size} of the regular files directly in <root>/<rel>; {} if the folder is gone."""
    out = {}
    try:
        with os.scandir(os.path.join(root, rel)) as it:
            for e in it:
                if e.is_file(follow_symlinks=False):
                    out[e.name] = e.stat(follow_symlinks=False).st_size
    except OSError:
        pass
    return out


def _file_findings(root: str) -> tuple[list[dict], int]:
    """(findings, count of files in tracked folders that have no row). Pure reading."""
    with library_db.db(root) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM files WHERE cur_name IS NOT NULL AND COALESCE(state,'') != 'missing'")]
    by_folder: dict[str, list[dict]] = {}
    for r in rows:
        by_folder.setdefault(r['folder'], []).append(r)
    findings: list[dict] = []
    untracked = 0
    for folder, frows in by_folder.items():
        listing = _folder_listing(root, folder)
        tracked_names = {r['cur_name'] for r in frows}
        unrecorded = {n: s for n, s in listing.items() if n not in tracked_names and not n.startswith('.')}
        untracked += len(unrecorded)
        for r in frows:
            path = os.path.join(folder, r['cur_name'])
            size = listing.get(r['cur_name'])
            if size is None:
                trash = os.path.exists(os.path.join(root, '.trash', folder, r['cur_name']))
                same_size = sorted(n for n, s in unrecorded.items() if r['cur_size'] is not None and s == r['cur_size'])
                if same_size:
                    findings.append({'kind': 'renamed_suspected', 'folder': folder, 'path': path, 'file_id': r['id'],
                                     'detail': {'candidates': same_size, 'recorded_size': r['cur_size']}})
                else:
                    findings.append({'kind': 'missing', 'folder': folder, 'path': path, 'file_id': r['id'],
                                     'detail': {'in_trash': trash, 'recorded_size': r['cur_size']}})
            elif r['cur_size'] is not None and size != r['cur_size']:
                findings.append({'kind': 'changed', 'folder': folder, 'path': path, 'file_id': r['id'],
                                 'detail': {'recorded_size': r['cur_size'], 'actual_size': size}})
    return findings, untracked


def scan(root: str) -> dict:
    """Compare disk and database, update the markers/drift tables, return a report. Never touches library files."""
    root = os.path.abspath(root)
    library_db.use_root(root)
    now = _now()
    markers_now = _scan_markers(root)
    findings, untracked = _file_findings(root)

    with library_db.db(root, write=True) as con:
        known = {(r['path'], r['kind']): dict(r) for r in con.execute('SELECT * FROM markers')}
        first_scan = not known and not con.execute("SELECT 1 FROM meta WHERE key='drift_first_scan'").fetchone()
        con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('drift_first_scan', ?)", (now,))
        for path, kind in markers_now:
            row = known.get((path, kind))
            if row is None:
                con.execute('INSERT INTO markers(path, kind, first_seen, seen_at) VALUES(?,?,?,?)', (path, kind, now, now))
                if not first_scan:
                    findings.append({'kind': 'marker_added', 'folder': path, 'path': os.path.join(path, kind), 'detail': {}})
            else:
                if row['cleared_at']:       # it came back
                    findings.append({'kind': 'marker_added', 'folder': path, 'path': os.path.join(path, kind), 'detail': {}})
                con.execute('UPDATE markers SET seen_at=?, cleared_at=NULL WHERE id=?', (now, row['id']))
        for (path, kind), row in known.items():
            if (path, kind) not in markers_now and not row['cleared_at']:
                con.execute('UPDATE markers SET cleared_at=? WHERE id=?', (now, row['id']))
                findings.append({'kind': 'marker_removed', 'folder': path, 'path': os.path.join(path, kind), 'detail': {}})

        open_rows = {(r['kind'], r['path']): dict(r) for r in con.execute("SELECT * FROM drift WHERE status='open'")}
        current = {(f['kind'], f['path']) for f in findings}
        for key, row in open_rows.items():           # no longer true -> resolved by itself
            if key not in current and not key[0].startswith('marker_'):
                con.execute("UPDATE drift SET status='resolved' WHERE id=?", (row['id'],))
        for f in findings:
            detail = dict(f['detail'], **({'file_id': f['file_id']} if 'file_id' in f else {}))
            existing = open_rows.get((f['kind'], f['path']))
            if existing:
                con.execute('UPDATE drift SET detail_json=? WHERE id=?', (json.dumps(detail), existing['id']))
            else:
                con.execute('INSERT INTO drift(ts, folder, path, kind, detail_json) VALUES(?,?,?,?,?)',
                            (now, f['folder'], f['path'], f['kind'], json.dumps(detail)))
        active = [dict(r) for r in con.execute("SELECT path, kind FROM markers WHERE cleared_at IS NULL ORDER BY path")]
    return {'findings': findings, 'untracked_in_tracked_folders': untracked, 'markers': active}


def open_findings(root: str, kind: str | None = None) -> list[dict]:
    with library_db.db(root) as con:
        q = "SELECT * FROM drift WHERE status='open'" + (' AND kind=?' if kind else '') + ' ORDER BY kind, path'
        rows = con.execute(q, (kind,) if kind else ()).fetchall()
    return [dict(r, detail=json.loads(r['detail_json'] or '{}')) for r in rows]


# ---------------------------------------------------------------------------
# Accepting ("this is the new normal")
# ---------------------------------------------------------------------------

def accept(root: str, drift_id: int) -> str:
    """Adopt the disk's state for one open finding. Returns what was done. Marker findings are just acknowledged."""
    with library_db.db(root, write=True) as con:
        row = con.execute("SELECT * FROM drift WHERE id=? AND status='open'", (drift_id,)).fetchone()
        if not row:
            return 'no such open finding'
        detail = json.loads(row['detail_json'] or '{}')
        fid, now = detail.get('file_id'), _now()
        what = 'acknowledged'
        if row['kind'] == 'changed' and fid:
            full = os.path.join(root, row['path'])
            st = os.stat(full)
            con.execute('UPDATE files SET cur_size=?, cur_mtime_ns=?, state=?, updated_by=?, updated_at=? WHERE id=?',
                        (st.st_size, st.st_mtime_ns, 'manual', _UPDATED_BY, now, fid))
            what = f'recorded the new size ({st.st_size} bytes)'
        elif row['kind'] == 'missing' and fid:
            con.execute("UPDATE files SET state='missing', updated_by=?, updated_at=? WHERE id=?", (_UPDATED_BY, now, fid))
            what = 'recorded as intentionally gone'
        elif row['kind'] == 'renamed_suspected' and fid:
            cands = detail.get('candidates', [])
            if len(cands) != 1:
                return f'ambiguous ({len(cands)} candidates); not changed'
            con.execute('UPDATE files SET cur_name=?, updated_by=?, updated_at=? WHERE id=?',
                        (cands[0], _UPDATED_BY, now, fid))
            what = f'recorded the rename to {cands[0]}'
        con.execute("UPDATE drift SET status='accepted' WHERE id=?", (drift_id,))
    return what


# ---------------------------------------------------------------------------
# For tools that are about to act on a file
# ---------------------------------------------------------------------------

def has_manual_marker(root: str, path: str) -> bool:
    """True if *path* (a file or folder, absolute) sits in or under a folder holding a `.manual` marker. Looks at
    the filesystem directly, so it is correct even before any scan."""
    root = os.path.abspath(root)
    cur = os.path.abspath(path)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    while True:
        if os.path.isfile(os.path.join(cur, '.manual')):
            return True
        if cur == root or os.path.dirname(cur) == cur:
            return False
        cur = os.path.dirname(cur)


def needs_attention(root: str, folder_rel: str, name: str) -> str | None:
    """Why a tool should NOT silently act on <root>/<folder_rel>/<name>, or None if it is fine to proceed:
    the user marked the folder `.manual`, or the file no longer matches what was recorded (changed/renamed)."""
    full = os.path.join(root, folder_rel, name)
    if has_manual_marker(root, full):
        return 'its folder is marked .manual'
    if not os.path.isfile(library_db.db_path(root)):
        return None
    with library_db.db(root) as con:
        row = con.execute("SELECT cur_size FROM files WHERE folder=? AND cur_name=? AND cur_size IS NOT NULL",
                          (folder_rel, name)).fetchone()
    if row is None:
        return None
    try:
        actual = os.path.getsize(full)
    except OSError:
        return 'the file recorded for it is gone from disk'
    if row['cur_size'] != actual:
        return f'it changed since it was recorded ({row["cur_size"]} -> {actual} bytes)'
    return None

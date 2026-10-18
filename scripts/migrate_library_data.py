"""Convert existing libraries to the shared library database (<library root>/.library-data/library.db).

Run it by hand, outside the menu:
    ./migrate.sh /path/to/Patreon            (dry run: prints a report, writes NOTHING)
    ./migrate.sh /path/to/Patreon --apply    (imports)
    migrate.bat D:\\Patreon [--apply]

Give it a LIBRARY ROOT -- the folder above all the creator folders you want in one database. It imports:
  * every per-folder .folder_log.json                                -> runs
  * the AV1 compressor's .av1-compressor/library_log.jsonl            -> runs
  * logs an older compressor parked in .av1-compressor/legacy_folder_logs/  -> runs (history that would otherwise
    be invisible to the downloader)
  * Pize's _pixeldrain/.manifest.json links                          -> pixeldrain_links (its packages/posts are
    kept verbatim in tool_data until that plugin moves over)
  * compressor 'compressed' records                                  -> files (original name/size -> current
    name/size), so a transcoded file is recognised as already owned
  * the library is registered in the machine-local app database

Safe by design: dry run by default; COPY, never move or delete (every legacy file stays exactly where it is);
idempotent (running twice adds nothing); one transaction per library (an interrupt leaves it untouched); anything
odd is listed in the report instead of guessed at. Ctrl+C prints "Cancelled.".
"""
import argparse
import json
import os
import sys
import time

import app_db
import library_db

LEGACY_LOG = '.folder_log.json'
COMPRESSOR_DIR = '.av1-compressor'
COMPRESSOR_LOG = 'library_log.jsonl'
PARKED_DIR = 'legacy_folder_logs'
PIZE_DIR = '_pixeldrain'
PIZE_MANIFEST = '.manifest.json'
_UPDATED_BY = 'migrate_library_data'


# ---------------------------------------------------------------------------
# Reading legacy data (never writes)
# ---------------------------------------------------------------------------

def _rel(root: str, path: str) -> str:
    rel = os.path.relpath(path, root)
    return '' if rel == '.' else rel


def _read_json(path: str, problems: list):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        problems.append({'kind': 'unreadable', 'path': path, 'detail': str(e)})
        return None


def _key(folder: str, rec: dict) -> str:
    return folder + '\x00' + json.dumps(rec, sort_keys=True, ensure_ascii=False)


def scan(root: str) -> dict:
    """Everything importable under *root*, plus problems found. Read-only."""
    problems: list = []
    runs: list[tuple[str, dict]] = []                       # (folder rel, record)
    sources = {'folder_logs': 0, 'compressor_log_records': 0, 'parked_logs': 0, 'pize_manifests': 0}
    pize: list[tuple[str, dict]] = []                       # (creator folder rel, manifest dict)
    nested: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.normpath(dirpath) != os.path.normpath(root) and library_db.DATA_DIRNAME in dirnames:
            nested.append(os.path.join(dirpath, library_db.DATA_DIRNAME))
        dirnames[:] = sorted(d for d in dirnames if d not in ('.trash', library_db.DATA_DIRNAME, COMPRESSOR_DIR))
        if LEGACY_LOG in filenames:
            data = _read_json(os.path.join(dirpath, LEGACY_LOG), problems)
            if isinstance(data, list):
                sources['folder_logs'] += 1
                runs += [(_rel(root, dirpath), r) for r in data if isinstance(r, dict)]
        if os.path.basename(dirpath) == PIZE_DIR and PIZE_MANIFEST in filenames:
            data = _read_json(os.path.join(dirpath, PIZE_MANIFEST), problems)
            if isinstance(data, dict):
                sources['pize_manifests'] += 1
                pize.append((_rel(root, os.path.dirname(dirpath)), data))

    cdir = os.path.join(root, COMPRESSOR_DIR)
    jsonl = os.path.join(cdir, COMPRESSOR_LOG)
    if os.path.isfile(jsonl):
        try:
            with open(jsonl, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(r, dict):
                        sources['compressor_log_records'] += 1
                        runs.append((r.get('folder', ''), {k: v for k, v in r.items() if k != 'folder'}))
        except OSError as e:
            problems.append({'kind': 'unreadable', 'path': jsonl, 'detail': str(e)})
    parked_root = os.path.join(cdir, PARKED_DIR)
    for dirpath, _dirs, filenames in os.walk(parked_root):
        if LEGACY_LOG in filenames:
            data = _read_json(os.path.join(dirpath, LEGACY_LOG), problems)
            if isinstance(data, list):
                sources['parked_logs'] += 1
                runs += [(_rel(parked_root, dirpath), r) for r in data if isinstance(r, dict)]

    for path in nested:
        problems.append({'kind': 'nested_library_data', 'path': path,
                         'detail': 'a second .library-data inside the library would shadow this root for everything '
                                   'below it; decide which one to keep before relying on either'})
    return {'runs': runs, 'pize': pize, 'sources': sources, 'problems': problems}


def build_plan(root: str) -> dict:
    """Scan and subtract what the database already holds, so the plan is exactly what --apply would add."""
    found = scan(root)
    existing_runs: set[str] = set()
    existing_links: set[str] = set()
    existing_files: set[tuple] = set()
    if os.path.isfile(library_db.db_path(root)):
        with library_db.db(root) as con:
            for r in con.execute('SELECT folder, data_json FROM runs'):
                try:
                    existing_runs.add(_key(r['folder'], json.loads(r['data_json'])))
                except json.JSONDecodeError:
                    continue
            existing_links = {r['link_id'] for r in con.execute('SELECT link_id FROM pixeldrain_links')}
            existing_files = {(r['folder'], r['orig_name']) for r in con.execute('SELECT folder, orig_name FROM files')}

    seen = set(existing_runs)
    runs_new: list[tuple[str, dict]] = []
    for folder, rec in found['runs']:
        k = _key(folder, rec)
        if k not in seen:
            seen.add(k)
            runs_new.append((folder, rec))
    runs_new.sort(key=lambda fr: (fr[0], fr[1].get('timestamp', '')))

    links_new: list[dict] = []
    for creator_rel, manifest in found['pize']:
        for url, e in (manifest.get('links') or {}).items():
            if not isinstance(e, dict):
                continue
            link_id = e.get('file_id') or url.rstrip('/').split('/')[-1]
            if link_id in existing_links:
                continue
            existing_links.add(link_id)
            extra = {k: v for k, v in e.items() if k not in ('status', 'file_id', 'first_attempt', 'last_attempt',
                                                              'remote_sha256', 'archive_name')}
            extra['creator_folder'] = creator_rel
            links_new.append({'link_id': link_id, 'source_url': url, 'status': e.get('status'),
                              'remote_sha256': e.get('remote_sha256'), 'remote_name': e.get('archive_name'),
                              'first_attempt': e.get('first_attempt'), 'last_attempt': e.get('last_attempt'),
                              'extra_json': json.dumps(extra, ensure_ascii=False)})

    files_new: list[dict] = []
    problems = list(found['problems'])
    for folder, rec in runs_new + [fr for fr in found['runs'] if _key(*fr) in existing_runs]:
        if rec.get('script') != 'compress_library' or rec.get('status') != 'compressed' or not rec.get('file'):
            continue
        ident = (folder, rec['file'])
        if ident in existing_files:
            continue
        existing_files.add(ident)
        final = rec.get('final_file') or rec['file']
        if not os.path.exists(os.path.join(root, folder, final)):
            problems.append({'kind': 'compressed_file_missing', 'path': os.path.join(root, folder, final),
                             'detail': f'the compressor recorded {rec["file"]} -> {final} but {final} is not on disk'})
        files_new.append({'folder': folder, 'orig_name': rec['file'], 'cur_name': final,
                          'orig_size': rec.get('original_size'), 'cur_size': rec.get('compressed_size'),
                          'codec': 'av1', 'state': 'compressed'})

    extras = [(creator_rel, {'packages': m.get('packages') or {}, 'posts': m.get('posts') or {}})
              for creator_rel, m in found['pize']]
    return {'root': root, 'runs': runs_new, 'links': links_new, 'files': files_new, 'pize_extra': extras,
            'sources': found['sources'], 'problems': problems}


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def apply_plan(plan: dict) -> dict:
    """Write the plan in ONE transaction (all or nothing). Returns the counts written."""
    root = plan['root']
    library_db.use_root(root)
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    with library_db.db(root, write=True) as con:
        for folder, rec in plan['runs']:
            con.execute('INSERT INTO runs(folder, script, ts, data_json, updated_by) VALUES(?,?,?,?,?)',
                        (folder, rec.get('script', ''), rec.get('timestamp', ''),
                         json.dumps(rec, ensure_ascii=False), _UPDATED_BY))
        for l in plan['links']:
            con.execute(
                'INSERT OR IGNORE INTO pixeldrain_links(link_id, source_url, status, remote_sha256, remote_name, '
                'first_attempt, last_attempt, extra_json, updated_by, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (l['link_id'], l['source_url'], l['status'], l['remote_sha256'], l['remote_name'],
                 l['first_attempt'], l['last_attempt'], l['extra_json'], _UPDATED_BY, now))
        for f in plan['files']:
            con.execute(
                'INSERT INTO files(folder, orig_name, cur_name, orig_size, cur_size, codec, state, updated_by, '
                'updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (f['folder'], f['orig_name'], f['cur_name'], f['orig_size'], f['cur_size'], f['codec'], f['state'],
                 _UPDATED_BY, now))
        for creator_rel, extra in plan['pize_extra']:
            con.execute(
                'INSERT INTO tool_data(tool, key, value_json, updated_by, updated_at) VALUES(?,?,?,?,?) '
                'ON CONFLICT(tool, key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at',
                ('pize_pixeldrain_archives', f'legacy_manifest_extra:{creator_rel}',
                 json.dumps(extra, ensure_ascii=False), _UPDATED_BY, now))
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_migration', ?)", (now,))
    app_db.register_library(library_db.library_id(root), root)
    return {'runs': len(plan['runs']), 'links': len(plan['links']), 'files': len(plan['files'])}


def format_report(plan: dict, applied: dict | None) -> str:
    s = plan['sources']
    lines = [
        f'Library: {plan["root"]}',
        f'  found: {s["folder_logs"]} per-folder log file(s), {s["compressor_log_records"]} compressor log record(s), '
        f'{s["parked_logs"]} parked log file(s), {s["pize_manifests"]} Pize manifest(s)',
        f'  {"imported" if applied else "would import"}: {len(plan["runs"])} run record(s), '
        f'{len(plan["links"])} pixeldrain link(s), {len(plan["files"])} compressed-file identity row(s)',
    ]
    if not (plan['runs'] or plan['links'] or plan['files']):
        lines.append('  nothing new to import (already up to date).')
    by_kind: dict[str, list] = {}
    for p in plan['problems']:
        by_kind.setdefault(p['kind'], []).append(p)
    for kind, items in by_kind.items():
        lines.append(f'  ! {kind}: {len(items)}')
        for p in items[:10]:
            lines.append(f'      {p["path"]}  ({p["detail"]})')
        if len(items) > 10:
            lines.append(f'      ... and {len(items) - 10} more (see --report)')
    lines.append('  Legacy files were left exactly where they are.')
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Convert libraries to the shared library database '
                                                 '(dry run unless --apply)')
    parser.add_argument('roots', nargs='+', help='library root folder(s): the folder above the creator folders')
    parser.add_argument('--apply', action='store_true', help='actually import (default: dry run, writes nothing)')
    parser.add_argument('--report', help='also write the full plan and problems to this JSON file')
    args = parser.parse_args(argv)

    full = []
    for root in args.roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            print(f'Not a folder: {root}')
            return 2
        plan = build_plan(root)
        applied = apply_plan(plan) if args.apply else None
        print(format_report(plan, applied))
        full.append({'root': root, 'applied': bool(applied), 'plan': {k: v for k, v in plan.items() if k != 'root'}})
    if not args.apply:
        print('\nDry run: nothing was written. Re-run with --apply to import.')
    if args.report:
        with open(args.report, 'w', encoding='utf-8') as f:
            json.dump(full, f, indent=2, ensure_ascii=False)
        print(f'Full report written to {args.report}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nCancelled.')
        sys.exit(130)

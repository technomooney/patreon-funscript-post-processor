#!/usr/bin/env python3
"""Machine-wide view of every recorded run and what each host cost, across all libraries.

Reads the rollup the downloader keeps in the app database (run_rollup / host_rollup) and prints it; with --html it also
writes a page with copy buttons like the audit report. Per-library detail (every link) is in the audit report and the
library database itself.

    python run_stats_report.py [--days N] [--library NAME] [--html FILE]
"""
import argparse
import html as htmllib
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_db
import app_paths


def _mb(n) -> str:
    return f'{(n or 0) / 1048576:.1f}'


def _dur(seconds) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h}h{m:02d}m{s:02d}s' if h else (f'{m}m{s:02d}s' if m else f'{s}s')


def load(days: int | None = None, library: str | None = None, path: str | None = None):
    """(runs, hosts): rows for the selected window, newest first / busiest host first. Empty when nothing was recorded."""
    db = path or app_paths.app_db_path()
    if not os.path.isfile(db):
        return [], []
    con = app_db.connect(db)
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'run_rollup' not in tables:
            return [], []
        where, args = [], []
        if days:
            where.append('started_at >= ?')
            args.append(time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - days * 86400)))
        if library:
            where.append('lower(library_path) LIKE ?')
            args.append(f'%{library.lower()}%')
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        runs = [dict(r) for r in con.execute(f'SELECT * FROM run_rollup {clause} ORDER BY started_at DESC', args)]
        ids = [r['run_id'] for r in runs]
        hosts = []
        if ids and 'host_rollup' in tables:
            marks = ','.join('?' * len(ids))
            hosts = [dict(r) for r in con.execute(
                f'SELECT host, SUM(requests) requests, SUM(bytes) bytes, SUM(seconds) seconds, SUM(ok) ok, '
                f'SUM(client_errors) client_errors, SUM(forbidden) forbidden, SUM(rate_limited) rate_limited, '
                f'SUM(server_errors) server_errors, SUM(errors) errors FROM host_rollup WHERE run_id IN ({marks}) '
                f'GROUP BY host ORDER BY requests DESC', ids)]
        return runs, hosts
    finally:
        con.close()


def run_rows(runs: list[dict], limit: int = 30) -> list[dict]:
    return [{'started': r['started_at'], 'library': os.path.basename(r['library_path'] or '') or '?', 'script': r['script'],
             'status': r['status'], 'took': _dur(r['duration_s']), 'tasks': r['tasks'] or '', 'links': r['links_seen'] or '',
             'downloaded': r['downloaded'] or 0, 'failed': r['failed'] or 0, 'saved MB': _mb(r['bytes_saved']),
             'requests': r['requests'] or 0, 'transferred MB': _mb(r['bytes_transferred'])} for r in runs[:limit]]


def host_rows(hosts: list[dict]) -> list[dict]:
    rows = []
    for h in hosts:
        seconds = h['seconds'] or 0
        rows.append({'host': h['host'], 'requests': h['requests'] or 0, 'MB': _mb(h['bytes']), 'time': _dur(seconds),
                     'avg KB/s': f"{(h['bytes'] or 0) / 1024 / seconds:.0f}" if seconds else '', 'ok': h['ok'] or 0,
                     '4xx': h['client_errors'] or 0, '403': h['forbidden'] or 0, '429/exit 6': h['rate_limited'] or 0,
                     '5xx': h['server_errors'] or 0, 'no answer': h['errors'] or 0})
    return rows


def text_table(rows: list[dict]) -> str:
    if not rows:
        return '  (nothing recorded)'
    cols = list(rows[0])
    widths = {c: max(len(str(c)), *(len(str(r[c])) for r in rows)) for c in cols}
    line = lambda r: '  ' + '  '.join(str(r[c]).ljust(widths[c]) for c in cols)   # noqa: E731
    return '\n'.join([line({c: c for c in cols}), '  ' + '  '.join('-' * widths[c] for c in cols), *(line(r) for r in rows)])


def html_page(runs: list[dict], hosts: list[dict]) -> str:
    import generate_audit_report as g
    sections = []
    for title, rows in (('Runs', run_rows(runs, 200)), ('Hosts (selected runs added up)', host_rows(hosts))):
        sections.append(g._render_csv_section(title.lower().replace(' ', '_') + '.csv', [{k: str(v) for k, v in r.items()} for r in rows],
                                              '', f'{len(rows)} rows') if rows else '')
    body = '\n'.join(s for s in sections if s) or '<p>Nothing recorded yet.</p>'
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Run statistics</title>'
            f'<style>{g._CSS}</style></head><body><header class="page-header"><div class="page-title">Run statistics</div>'
            f'<div class="page-meta">{htmllib.escape(app_paths.app_db_path())}<span class="sep">·</span>'
            f'Generated {time.strftime("%Y-%m-%d %H:%M:%S")}</div></header>'
            f'<section class="reports-section">{body}</section><script>{g._JS}</script></body></html>')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--days', type=int, help='only runs started in the last N days')
    ap.add_argument('--library', help='only libraries whose path contains this text')
    ap.add_argument('--html', help='also write an HTML page to this file')
    args = ap.parse_args(argv)
    runs, hosts = load(args.days, args.library)
    print(f'{len(runs)} run(s) recorded' + (f' in the last {args.days} day(s)' if args.days else '') + '\n')
    print('Runs (newest first):')
    print(text_table(run_rows(runs)))
    print('\nHosts (all selected runs added up):')
    print(text_table(host_rows(hosts)))
    if args.html:
        with open(args.html, 'w', encoding='utf-8') as f:
            f.write(html_page(runs, hosts))
        print(f'\nWrote {args.html}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nCancelled.')

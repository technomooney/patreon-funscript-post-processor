#!/usr/bin/env python3
"""
Generate an HTML audit report from .folder_log.json files.

Reads every .folder_log.json under the given base path, builds one HTML file
with an overall summary and per-folder detail, and writes it to
_reports/audit_report.html.

Usage
-----
  python generate_audit_report.py [directory]
"""

import csv
import html
import json
import os
import sys
import time
from pathlib import Path

import folder_log as _flog


_SCRIPTS = ('prefixFix', 'downloadContent', 'fix_garbled_names', 'check_funscripts', 'generate_html')
_LABELS  = {
    'prefixFix':        'Prefix Fix',
    'downloadContent':  'Download',
    'fix_garbled_names':'Name Fix',
    'check_funscripts': 'FS Check',
    'generate_html':    'HTML',
}
_LINK_STATUS_CLASS = {
    'downloaded':          'ok',
    'skipped_duplicate':   'skip',
    'skipped_av_similar':  'skip',
    'previously_done':     'skip',
    'deferred_mega':       'skip',
    'skipped_known_failure': 'skip',
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _e(s) -> str:
    return html.escape(str(s), quote=False)

def _ea(s) -> str:
    return html.escape(str(s), quote=True)

def _reports_dir(base: str) -> str:
    path = os.path.join(base, '_reports')
    os.makedirs(path, exist_ok=True)
    return path

def _collect(base: str) -> list[dict]:
    """Return one entry per direct subfolder of *base*, sorted by name."""
    entries = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return entries
    for name in names:
        if name.startswith('_') or name.startswith('.'):
            continue
        folder = os.path.join(base, name)
        if not os.path.isdir(folder):
            continue
        entries.append({'name': name, 'folder': folder, 'log': _flog.read(folder)})
    return entries


# ---------------------------------------------------------------------------
# CSV report helpers
# ---------------------------------------------------------------------------

# Friendly names and special column treatment for known CSVs.
# path_cols: columns whose values are absolute paths — shown relative to base.
# order: preferred display order (lower = earlier in the Reports section).
_CSV_META: dict[str, dict] = {
    'funscript_check.csv':        {'title': 'Missing Funscripts',          'path_cols': {'folder'},                        'order': 1},
    'funscript_video_matches.csv':{'title': 'Funscript-to-Video Matches',  'path_cols': {'funscript', 'suggested'},        'order': 2},
    'garbled_names.csv':          {'title': 'Garbled Name Fixes',          'path_cols': {'old_path', 'new_path'},          'order': 3},
    'funscript_renames.csv':      {'title': 'Funscript Extension Fixes',   'path_cols': {'old_path', 'new_path'},          'order': 4},
    'media_renames.csv':          {'title': 'Media Extension Fixes',       'path_cols': {'old_path', 'new_path'},          'order': 5},
    'failed_downloads.csv':       {'title': 'Failed Downloads',            'path_cols': {'save_directory'},                'order': 6},
    'uncertain_downloads.csv':    {'title': 'Uncertain Downloads',         'path_cols': {'save_directory'},                'order': 7},
    'many_funscripts.csv':        {'title': 'Folders with Many Funscripts','path_cols': {'folder'},                        'order': 8},
    'mega_error6.csv':            {'title': 'Mega Error Log',              'path_cols': set(),                             'order': 9},
}


def _csv_title(fname: str) -> str:
    meta = _CSV_META.get(fname)
    if meta:
        return meta['title']
    return fname.replace('_', ' ').replace('.csv', '').title()


def _read_csv_report(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error):
        return []


def _rel(path: str, base: str) -> str:
    """Return *path* relative to *base* for compact display."""
    try:
        r = os.path.relpath(path, base)
        return r if not r.startswith('..') else os.path.basename(path)
    except ValueError:
        return os.path.basename(path)


def _looks_like_path(val: str) -> bool:
    return val.startswith('/') or (len(val) > 2 and val[1] == ':' and val[2] in '/\\')

def _looks_like_url(val: str) -> bool:
    return val.startswith(('http://', 'https://'))


def _status_cls(status: str) -> str:
    s = status.lower()
    if s in ('renamed', 'downloaded', 'would rename'):
        return 'st-ok'
    if s.startswith('skipped') or s == 'previously_done':
        return 'st-skip'
    if s.startswith('error'):
        return 'st-fail'
    if s.startswith('uncertain'):
        return 'st-warn'
    return 'st-other'


def _render_csv_section(fname: str, rows: list[dict], base: str) -> str:
    if not rows:
        return ''

    title     = _csv_title(fname)
    meta      = _CSV_META.get(fname, {})
    path_cols: set[str] = meta.get('path_cols', set())

    cols = list(rows[0].keys()) if rows else []

    # Count by status for the summary badge
    has_status = 'status' in cols
    status_counts: dict[str, int] = {}
    if has_status:
        for row in rows:
            s = row.get('status', '')
            status_counts[s] = status_counts.get(s, 0) + 1
        badge_parts = [f'<span class="rpt-badge {_status_cls(s)}">{n} {_e(s)}</span>'
                       for s, n in sorted(status_counts.items(), key=lambda x: -x[1])]
    else:
        badge_parts = [f'<span class="rpt-badge st-other">{len(rows)} rows</span>']
    badge_html = ' '.join(badge_parts)

    # Table header
    th = ''.join(f'<th>{_e(c.replace("_", " "))}</th>' for c in cols)

    # Table rows — auto-detect path and URL columns if not in _CSV_META
    trs = []
    for row in rows:
        cells = []
        for col in cols:
            val = row.get(col, '')
            is_path = col in path_cols or (not path_cols and _looks_like_path(val))
            is_url  = not is_path and _looks_like_url(val)
            is_long = is_path or is_url or len(val) > 80
            display = _rel(val, base) if is_path and val else val
            if col == 'status' and val:
                cls = _status_cls(val)
                cells.append(f'<td class="st {cls}">{_e(val)}</td>')
            elif is_long and val:
                extra = 'rpt-path' if is_path else ('rpt-url' if is_url else '')
                cells.append(
                    f'<td>'
                    f'<div class="cell-clip {extra}" title="{_ea(val)}">{_e(display)}</div>'
                    f'</td>'
                )
            else:
                cells.append(f'<td>{_e(display)}</td>')
        trs.append('<tr>' + ''.join(cells) + '</tr>')

    rows_html = '\n'.join(trs)

    return (
        f'<details class="rpt">'
        f'<summary class="rpt-sum">'
        f'<span class="rpt-title">{_e(title)}</span>'
        f'<span class="rpt-badges">{badge_html}</span>'
        f'<span class="rpt-fname">{_e(fname)}</span>'
        f'</summary>'
        f'<div class="rpt-body">'
        f'<table class="rpt-tbl"><thead><tr>{th}</tr></thead>'
        f'<tbody>{rows_html}</tbody></table>'
        f'</div>'
        f'</details>'
    )


# ---------------------------------------------------------------------------
# Per-run HTML
# ---------------------------------------------------------------------------

def _render_link_row(lnk: dict) -> str:
    url    = lnk.get('url', '')
    status = lnk.get('status', '')
    cls    = _LINK_STATUS_CLASS.get(status, 'fail' if 'fail' in status else 'other')
    return (f'<div class="link-row">'
            f'<span class="ls ls-{cls}">{_e(status)}</span>'
            f'<span class="link-url">{_e(url)}</span>'
            f'</div>')

def _render_rename_row(frm: str, to: str, note: str = '') -> str:
    note_html = f' <span class="rename-note">[{_e(note)}]</span>' if note else ''
    return (f'<div class="rename-row">'
            f'<span class="rf">{_e(frm)}</span>'
            f'<span class="rarrow">→</span>'
            f'<span class="rt">{_e(to)}</span>'
            f'{note_html}'
            f'</div>')

def _render_runs(log: list[dict]) -> str:
    """Render all runs, collapsing consecutive same-script groups to latest + hidden earlier."""
    if not log:
        return '<div class="no-log">No history recorded yet.</div>'

    # Group consecutive same-script runs
    groups: list[tuple[str, list[dict]]] = []
    for run in log:
        script = run.get('script', '?')
        if groups and groups[-1][0] == script:
            groups[-1][1].append(run)
        else:
            groups.append((script, [run]))

    parts: list[str] = []
    for script, runs in groups:
        if len(runs) == 1:
            parts.append(_render_run(runs[0]))
        else:
            label   = _LABELS.get(script, script)
            n       = len(runs) - 1
            earlier = ''.join(_render_run(r) for r in runs[:-1])
            noun    = 'run' if n == 1 else 'runs'
            parts.append(
                f'<details class="older-runs">'
                f'<summary class="older-runs-sum">{n} earlier {_e(label)} {noun}</summary>'
                f'<div class="older-runs-body">{earlier}</div>'
                f'</details>'
            )
            parts.append(_render_run(runs[-1]))

    return ''.join(parts)


def _render_run(run: dict) -> str:
    script  = run.get('script', '?')
    ts      = run.get('timestamp', '')
    forced  = run.get('force_rerun', False)
    label   = _LABELS.get(script, script)
    parts: list[str] = []

    if script == 'downloadContent':
        links = run.get('links', [])
        files = run.get('files_saved', [])
        if links:
            parts.append('<div class="run-links">')
            parts.extend(_render_link_row(l) for l in links)
            parts.append('</div>')
        if files:
            file_spans = ' '.join(f'<span class="fname">{_e(f)}</span>' for f in files)
            parts.append(f'<div class="run-files"><span class="sec-label">Saved:</span> {file_spans}</div>')
        if not links and not files:
            parts.append('<span class="empty-note">No links or files recorded.</span>')

    elif script == 'prefixFix':
        renames = run.get('renames', [])
        if renames:
            parts.append('<div class="run-renames">')
            parts.extend(_render_rename_row(r.get('from', ''), r.get('to', '')) for r in renames)
            parts.append('</div>')
        else:
            parts.append('<span class="empty-note">No renames needed.</span>')

    elif script == 'fix_garbled_names':
        changes = run.get('changes', [])
        actual  = [c for c in changes if c.get('status') in ('renamed', 'would rename')]
        if actual:
            parts.append('<div class="run-renames">')
            for c in actual:
                frm      = c.get('old_path') or c.get('funscript', '')
                to       = c.get('new_path') or c.get('suggested', '')
                strategy = c.get('strategy') or c.get('score', '')
                parts.append(_render_rename_row(frm, to, strategy))
            parts.append('</div>')
        else:
            parts.append('<span class="empty-note">No renames needed.</span>')

    elif script == 'check_funscripts':
        missing = run.get('missing', [])
        total_v = run.get('total_videos', 0)
        if missing:
            parts.append('<div class="run-fs-check">')
            parts.append(f'<span class="sec-label">{len(missing)} of {total_v} video(s) missing funscript:</span>')
            parts.append('<ul class="missing-list">')
            parts.extend(f'<li class="missing-video">{_e(v)}</li>' for v in missing)
            parts.append('</ul>')
            parts.append('</div>')
        else:
            parts.append(f'<span class="empty-note">All {total_v} video(s) have matching funscripts.</span>')

    elif script == 'generate_html':
        parts.append('<span class="empty-note">description.html written.</span>')

    forced_badge = ' <span class="badge-forced">FORCED</span>' if forced else ''

    return (f'<div class="run-entry">'
            f'<div class="run-hdr">'
            f'<span class="run-script s-{script}">{_e(label)}</span>'
            f'{forced_badge}'
            f'<span class="run-ts">{_e(ts)}</span>'
            f'</div>'
            f'<div class="run-body">{"".join(parts)}</div>'
            f'</div>')


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def generate(base: str) -> str:
    base    = os.path.abspath(base)
    entries = _collect(base)
    total   = len(entries)

    # --- aggregate stats ---
    script_done: dict[str, int] = {s: 0 for s in _SCRIPTS}
    n_downloaded       = 0
    n_files_saved      = 0
    n_renames          = 0
    n_forced           = 0
    n_with_log         = 0
    n_missing_scripts  = 0  # videos missing funscripts (from most recent check_funscripts run)

    for e in entries:
        log = e['log']
        if log:
            n_with_log += 1
        scripts_seen: set[str] = set()
        last_fs_check: dict | None = None
        for run in log:
            s = run.get('script', '')
            scripts_seen.add(s)
            if run.get('force_rerun'):
                n_forced += 1
            if s == 'downloadContent':
                n_downloaded  += sum(1 for l in run.get('links', []) if l.get('status') == 'downloaded')
                n_files_saved += len(run.get('files_saved', []))
            elif s == 'prefixFix':
                n_renames += len(run.get('renames', []))
            elif s == 'fix_garbled_names':
                n_renames += sum(1 for c in run.get('changes', []) if c.get('status') == 'renamed')
            elif s == 'check_funscripts':
                last_fs_check = run
        if last_fs_check is not None:
            n_missing_scripts += len(last_fs_check.get('missing', []))
        for s in scripts_seen:
            if s in script_done:
                script_done[s] += 1

    n_unlogged = total - n_with_log
    now = time.strftime('%Y-%m-%d %H:%M:%S')

    # --- stat cards ---
    def card(value, label, extra_cls=''):
        return (f'<div class="stat-card {extra_cls}">'
                f'<div class="stat-val">{_e(value)}</div>'
                f'<div class="stat-lbl">{_e(label)}</div>'
                f'</div>')

    fs_csv_exists = os.path.exists(os.path.join(_reports_dir(base), 'funscript_check.csv'))

    cards_html = ''.join([
        card(total,             'Total folders'),
        card(n_with_log,        'Folders logged'),
        card(n_unlogged,        'Not yet processed', 'warn' if n_unlogged else ''),
        card(script_done['downloadContent'], 'Downloads complete'),
        card(n_downloaded,      'Files downloaded'),
        card(n_files_saved,     'Files saved'),
        card(n_renames,         'Files renamed'),
        card(n_missing_scripts, 'Missing funscripts', 'warn' if n_missing_scripts else ''),
        card(n_forced,          'Force-rerun entries', 'warn' if n_forced else ''),
    ])

    # --- progress bars ---
    bars: list[str] = []
    for s in _SCRIPTS:
        count = script_done[s]
        pct   = int(count / total * 100) if total else 0
        lbl   = _LABELS[s]
        bars.append(
            f'<div class="prog-row">'
            f'<span class="prog-lbl">{_e(lbl)}</span>'
            f'<div class="prog-wrap"><div class="prog-bar" style="width:{pct}%"></div></div>'
            f'<span class="prog-count">{count} / {total}</span>'
            f'</div>'
        )
    bars_html = '\n'.join(bars)

    # --- per-folder items ---
    folder_items: list[str] = []
    for e in entries:
        name        = e['name']
        log         = e['log']
        folder_path = e['folder']
        scripts_done_set = {run.get('script') for run in log}

        # Status tags (space-separated) used by the JS filter
        status_tags: list[str] = []
        if not log:
            status_tags.append('no-log')
        else:
            if all(s in scripts_done_set for s in _SCRIPTS):
                status_tags.append('complete')
            else:
                status_tags.append('partial')
        last_fs_run = next((r for r in reversed(log) if r.get('script') == 'check_funscripts'), None)
        if last_fs_run and last_fs_run.get('missing'):
            status_tags.append('missing-fs')
        status_str = ' '.join(status_tags)

        file_uri = Path(folder_path).as_uri()

        badges = ''.join(
            f'<span class="fbadge {"fb-done" if s in scripts_done_set else "fb-pend"}" title="{_ea(s)}">'
            f'{_e(_LABELS[s])}</span>'
            for s in _SCRIPTS
        )

        runs_html = _render_runs(log)

        folder_items.append(
            f'<details class="fi" data-name="{_ea(name.lower())}" data-status="{_ea(status_str)}" data-path="{_ea(folder_path)}">'
            f'<summary class="fi-sum">'
            f'<span class="fi-name">{_e(name)}</span>'
            f'<span class="fi-actions">'
            f'<a class="btn-open" href="{_ea(file_uri)}" title="Open folder" target="_blank">open</a>'
            f'<button class="btn-copy" onclick="copyPath(this)" title="Copy path">copy</button>'
            f'</span>'
            f'<span class="fi-badges">{badges}</span>'
            f'</summary>'
            f'<div class="fi-body">{runs_html}</div>'
            f'</details>'
        )

    folders_html = '\n'.join(folder_items)

    fs_csv_link = ('<a class="csv-link" href="funscript_check.csv">funscript_check.csv</a>'
                   if fs_csv_exists else '')

    # --- report tables: scan all *.csv files in _reports/ ---
    rdir = _reports_dir(base)
    try:
        csv_files = sorted(
            (f for f in os.listdir(rdir) if f.endswith('.csv')),
            key=lambda f: (_CSV_META.get(f, {}).get('order', 99), f)
        )
    except OSError:
        csv_files = []

    report_parts: list[str] = []
    for fname in csv_files:
        rows = _read_csv_report(os.path.join(rdir, fname))
        chunk = _render_csv_section(fname, rows, base)
        if chunk:
            report_parts.append(chunk)

    reports_html = '\n'.join(report_parts)

    return _build_page(base=_e(base), generated=_e(now),
                       cards=cards_html, bars=bars_html, folders=folders_html,
                       fs_csv_link=fs_csv_link, reports=reports_html)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: system-ui, sans-serif;
    background: #1a1a1e;
    color: #e0e0e8;
    padding: 2rem;
    line-height: 1.5;
    max-width: 1100px;
    margin: 0 auto;
    font-size: .9rem;
}

/* ---- page header ---- */
.page-header {
    margin-bottom: 2rem;
    padding-bottom: .75rem;
    border-bottom: 1px solid #38384a;
}
.page-title { font-size: 1.4rem; font-weight: 700; color: #c8c8f0; }
.page-meta  { font-size: .75rem; color: #606080; margin-top: .3rem; word-break: break-all; }
.page-meta .sep { margin: 0 .4rem; }
.jump-link { color: #6070a0; text-decoration: none; }
.jump-link:hover { color: #9090c8; text-decoration: underline; }

/* ---- tips box ---- */
.tips-box {
    background: #1c1c28; border: 1px solid #30304a;
    border-radius: 5px; margin-bottom: 1.5rem;
}
.tips-sum {
    padding: .45rem .8rem; cursor: pointer; list-style: none;
    font-size: .78rem; color: #707090; user-select: none;
}
.tips-sum::-webkit-details-marker { display: none; }
.tips-sum::before { content: '▶  '; font-size: .6rem; color: #505070; }
.tips-box[open] > .tips-sum::before { content: '▼  '; }
.tips-body { padding: .6rem .9rem 1rem; border-top: 1px solid #2a2a40; }
.tips-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: .45rem .8rem; }
.tip { display: flex; flex-direction: column; font-size: .76rem; gap: .1rem; }
.tip-key { color: #8080b0; font-weight: 600; }
.tip-val { color: #7070a0; line-height: 1.4; }
.tip-val b { color: #a0a0c8; font-weight: 600; }

/* ---- summary section ---- */
.summary-section { margin-bottom: 2.5rem; }

.stat-cards {
    display: flex; flex-wrap: wrap; gap: .75rem;
    margin-bottom: 1.5rem;
}
.stat-card {
    background: #22222e; border: 1px solid #33334a;
    border-radius: 6px; padding: .75rem 1rem;
    min-width: 110px; text-align: center;
}
.stat-card.warn { border-color: #5a4a20; background: #26221a; }
.stat-val { font-size: 1.6rem; font-weight: 700; color: #b0b0e0; }
.stat-lbl { font-size: .7rem; color: #606080; margin-top: .2rem; }

/* ---- progress bars ---- */
.prog-section-title {
    font-size: .72rem; text-transform: uppercase; letter-spacing: .08em;
    color: #505070; margin-bottom: .6rem;
}
.prog-row {
    display: flex; align-items: center; gap: .75rem; margin-bottom: .4rem;
}
.prog-lbl   { width: 90px; font-size: .78rem; color: #8080a8; text-align: right; flex-shrink: 0; }
.prog-wrap  { flex: 1; background: #2a2a3a; border-radius: 3px; height: 8px; }
.prog-bar   { background: #5050c0; height: 8px; border-radius: 3px; transition: width .3s; }
.prog-count { width: 60px; font-size: .75rem; color: #606080; }

/* ---- folders section ---- */
.folders-section { }
.folders-hdr {
    display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
    margin-bottom: .6rem;
}
.folders-title { font-size: 1rem; font-weight: 600; color: #a0a0c8; }
.folder-search {
    flex: 1; min-width: 160px; max-width: 320px;
    background: #22222e; border: 1px solid #38384a; border-radius: 4px;
    padding: .35rem .65rem; color: #d0d0e8; font-size: .82rem;
    outline: none;
}
.folder-search:focus { border-color: #5050a0; }

/* ---- status filter buttons ---- */
.status-filters {
    display: flex; gap: .35rem; flex-wrap: wrap;
    margin-bottom: 1rem;
}
.sf-btn {
    font-size: .72rem; padding: .22rem .6rem; border-radius: 3px;
    border: 1px solid #38384a; background: #1e1e28; color: #707090;
    cursor: pointer; font-family: inherit; white-space: nowrap;
}
.sf-btn:hover { color: #a0a0c8; border-color: #50508a; }
.sf-btn.sf-active { background: #2a2a4a; color: #c0c0e8; border-color: #6060b0; }

/* ---- folder item ---- */
.fi {
    background: #1e1e28; border: 1px solid #2e2e40;
    border-radius: 5px; margin-bottom: .45rem;
}
.fi[open] { border-color: #44446a; }
.fi-sum {
    display: flex; align-items: center; gap: .5rem;
    padding: .55rem .8rem; cursor: pointer; list-style: none;
    user-select: none;
}

.fi-actions {
    display: flex; gap: .25rem; align-items: center;
    flex-shrink: 0; margin-left: .25rem;
}
.btn-open, .btn-copy {
    font-size: .63rem; padding: .1rem .38rem; border-radius: 3px;
    border: 1px solid #35354a; background: #1e1e2c; color: #60607a;
    cursor: pointer; text-decoration: none; white-space: nowrap;
    font-family: inherit; line-height: 1.4;
}
.btn-open:hover, .btn-copy:hover { color: #9090c0; border-color: #5050a0; }
.fi-sum::-webkit-details-marker { display: none; }
.fi-sum::before {
    content: '▶'; font-size: .65rem; color: #505070;
    transition: transform .15s; flex-shrink: 0;
}
.fi[open] > .fi-sum::before { transform: rotate(90deg); }
.fi-name { flex: 1; font-size: .82rem; color: #c0c0e0; word-break: break-all; }
.fi-badges { display: flex; gap: .3rem; flex-wrap: wrap; flex-shrink: 0; }

.fbadge {
    font-size: .65rem; padding: .1rem .4rem; border-radius: 3px;
    white-space: nowrap;
}
.fb-done { background: #1e3a1e; color: #70c070; border: 1px solid #2a5a2a; }
.fb-pend { background: #2a2a2a; color: #505060; border: 1px solid #363636; }

.fi-body { padding: .75rem .8rem 1rem; border-top: 1px solid #2e2e40; }

.no-log { font-size: .78rem; color: #505060; font-style: italic; }

/* ---- collapsed older runs ---- */
.older-runs { margin-bottom: .5rem; }
.older-runs-sum {
    display: inline-block; list-style: none;
    font-size: .68rem; color: #404060; cursor: pointer;
    padding: .15rem .5rem; border-radius: 3px;
    border: 1px solid #2a2a3a; background: #1c1c26;
    user-select: none;
}
.older-runs-sum::-webkit-details-marker { display: none; }
.older-runs-sum::before { content: '▶  '; font-size: .6rem; }
.older-runs[open] > .older-runs-sum::before { content: '▼  '; }
.older-runs-sum:hover { color: #7070a0; border-color: #40406a; }
.older-runs-body { margin-top: .35rem; opacity: .7; }

/* ---- run entry ---- */
.run-entry {
    background: #22222e; border: 1px solid #2e2e40;
    border-radius: 4px; padding: .6rem .75rem;
    margin-bottom: .5rem;
}
.run-hdr {
    display: flex; align-items: center; gap: .6rem;
    margin-bottom: .45rem;
}
.run-script {
    font-size: .72rem; font-weight: 600; padding: .1rem .45rem;
    border-radius: 3px; white-space: nowrap;
}
.s-downloadContent  { background: #1a2e4a; color: #70a0d8; border: 1px solid #264070; }
.s-prefixFix        { background: #2a1e40; color: #9070c8; border: 1px solid #40306a; }
.s-fix_garbled_names{ background: #1e2e1e; color: #70b070; border: 1px solid #2a4a2a; }
.s-check_funscripts { background: #1a2e3a; color: #60b8c8; border: 1px solid #264860; }
.s-generate_html    { background: #2e2a1a; color: #c0a040; border: 1px solid #4a4020; }

.run-fs-check { }
.missing-list { margin: .3rem 0 0 1.2rem; }
.missing-video { color: #c07060; font-size: .76rem; margin-bottom: .1rem; word-break: break-all; }

.csv-link {
    font-size: .76rem; color: #6090b0; text-decoration: none;
    border: 1px solid #2a4060; border-radius: 3px; padding: .15rem .45rem;
    margin-left: .5rem;
}
.csv-link:hover { color: #90c0e0; border-color: #4a6080; }

.badge-forced {
    font-size: .65rem; background: #4a2a1a; color: #d07040;
    border: 1px solid #6a3a20; border-radius: 3px; padding: .1rem .4rem;
}
.run-ts { font-size: .7rem; color: #505070; margin-left: auto; white-space: nowrap; }

/* ---- run body ---- */
.run-body { font-size: .78rem; }
.empty-note { color: #505060; font-style: italic; }
.sec-label  { color: #606080; font-size: .72rem; }

/* links */
.run-links { display: flex; flex-direction: column; gap: .2rem; }
.link-row  { display: flex; align-items: baseline; gap: .5rem; }
.ls {
    font-size: .68rem; padding: .05rem .35rem; border-radius: 2px;
    white-space: nowrap; flex-shrink: 0;
}
.ls-ok   { background: #1a3a1a; color: #60c060; border: 1px solid #2a5a2a; }
.ls-skip { background: #2a2a1e; color: #909060; border: 1px solid #3a3a28; }
.ls-fail { background: #3a1a1a; color: #c06060; border: 1px solid #5a2a2a; }
.ls-other{ background: #1e2a2e; color: #6090a0; border: 1px solid #2a3e46; }
.link-url { color: #6080a0; word-break: break-all; font-size: .72rem; }

/* renames */
.run-renames { display: flex; flex-direction: column; gap: .2rem; }
.rename-row  { display: flex; align-items: baseline; gap: .4rem; flex-wrap: wrap; }
.rf     { color: #8080a0; word-break: break-all; }
.rarrow { color: #404060; flex-shrink: 0; }
.rt     { color: #b0b0d0; word-break: break-all; }
.rename-note { color: #505060; font-size: .7rem; }

/* files saved */
.run-files { margin-top: .35rem; }
.fname {
    display: inline-block; background: #1e1e2a; border-radius: 3px;
    padding: .05rem .35rem; color: #9090b8; margin: .1rem .2rem .1rem 0;
    word-break: break-all;
}

/* ---- reports section ---- */
.reports-section { margin-top: 2.5rem; border-top: 1px solid #2e2e40; padding-top: 1.5rem; }
.reports-hdr { margin-bottom: 1rem; }
.reports-title { font-size: 1rem; font-weight: 600; color: #a0a0c8; }

.rpt {
    background: #1e1e28; border: 1px solid #2e2e40;
    border-radius: 5px; margin-bottom: .45rem;
}
.rpt[open] { border-color: #44446a; }
.rpt-sum {
    display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;
    padding: .5rem .8rem; cursor: pointer; list-style: none; user-select: none;
}
.rpt-sum::-webkit-details-marker { display: none; }
.rpt-sum::before {
    content: '▶'; font-size: .65rem; color: #505070;
    transition: transform .15s; flex-shrink: 0;
}
.rpt[open] > .rpt-sum::before { transform: rotate(90deg); }
.rpt-title { font-size: .85rem; color: #c0c0e0; flex-shrink: 0; }
.rpt-badges { display: flex; gap: .3rem; flex-wrap: wrap; flex: 1; }
.rpt-badge {
    font-size: .66rem; padding: .1rem .4rem; border-radius: 3px;
    white-space: nowrap;
}
.rpt-badge.st-ok   { background: #1a3a1a; color: #60c060; border: 1px solid #2a5a2a; }
.rpt-badge.st-skip { background: #2a2a1e; color: #909060; border: 1px solid #3a3a28; }
.rpt-badge.st-fail { background: #3a1a1a; color: #c06060; border: 1px solid #5a2a2a; }
.rpt-badge.st-warn { background: #2e2210; color: #c09040; border: 1px solid #504020; }
.rpt-badge.st-other{ background: #1e2a2e; color: #6090a0; border: 1px solid #2a3e46; }
.rpt-fname { font-size: .68rem; color: #404060; flex-shrink: 0; margin-left: auto; }

.rpt-body { padding: .5rem .8rem 1rem; border-top: 1px solid #2e2e40; overflow-x: auto; }
.rpt-tbl {
    border-collapse: collapse; width: 100%; font-size: .75rem;
    table-layout: fixed;
}
.rpt-tbl th {
    text-align: left; color: #606080; font-weight: 600;
    padding: .3rem .6rem; border-bottom: 1px solid #2e2e40;
    white-space: nowrap; background: #1a1a22;
    overflow: hidden; text-overflow: ellipsis;
}
.rpt-tbl td {
    padding: .22rem .6rem; border-bottom: 1px solid #1e1e28;
    vertical-align: top; color: #9090b8;
    overflow: hidden;
}
.rpt-tbl tr:last-child td { border-bottom: none; }
.rpt-tbl tr:hover td { background: #22222e; }

/* Long-content cells: clip with ellipsis, click to expand */
.cell-clip {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: clamp(140px, 28vw, 420px);
    cursor: pointer;
    user-select: text;
}
.cell-clip::after {
    content: ' ⋯';
    font-size: .65em;
    color: #404060;
    vertical-align: middle;
}
.cell-clip.expanded {
    white-space: normal;
    overflow: visible;
    text-overflow: unset;
    max-width: none;
    word-break: break-all;
}
.cell-clip.expanded::after { content: ''; }
.rpt-path { color: #7090a8; font-size: .72rem; }
.rpt-url  { color: #6080a0; font-size: .72rem; }
.st { white-space: nowrap; font-size: .72rem; }
.st-ok   { color: #60c060; }
.st-skip { color: #909060; }
.st-fail { color: #c06060; }
.st-warn { color: #c09040; }
.st-other{ color: #6090a0; }
"""

_JS = """
let _sf = 'all';

function filterFolders(q) {
    q = q.toLowerCase().trim();
    document.querySelectorAll('.fi').forEach(el => {
        const nameOk   = !q || el.dataset.name.includes(q);
        const statusOk = _sf === 'all' || (' ' + el.dataset.status + ' ').includes(' ' + _sf + ' ');
        el.style.display = nameOk && statusOk ? '' : 'none';
    });
}

function setStatusFilter(btn) {
    document.querySelectorAll('.sf-btn').forEach(b => b.classList.remove('sf-active'));
    btn.classList.add('sf-active');
    _sf = btn.dataset.filter;
    filterFolders(document.querySelector('.folder-search').value);
}

function copyPath(btn) {
    const path = btn.closest('.fi').dataset.path;
    navigator.clipboard.writeText(path).then(() => {
        const orig = btn.textContent;
        btn.textContent = 'copied';
        setTimeout(() => btn.textContent = orig, 1500);
    }).catch(() => {});
}

document.addEventListener('click', function(e) {
    const el = e.target.closest('.cell-clip');
    if (el) el.classList.toggle('expanded');
});
"""


def _build_page(*, base, generated, cards, bars, folders, fs_csv_link='', reports='') -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Folder Audit Report</title>
<style>{_CSS}</style>
</head>
<body>

<header class="page-header">
  <div class="page-title">Folder Audit Report</div>
  <div class="page-meta">{base}<span class="sep">·</span>Generated {generated}<span class="sep">·</span><a class="jump-link" href="#reports">Jump to Reports</a></div>
</header>

<details class="tips-box">
  <summary class="tips-sum">Usage tips</summary>
  <div class="tips-body">
    <div class="tips-grid">
      <div class="tip"><span class="tip-key">Folders</span><span class="tip-val">Click any folder row to expand its run history.</span></div>
      <div class="tip"><span class="tip-key">Search</span><span class="tip-val">Type in the search box to filter folders by name.</span></div>
      <div class="tip"><span class="tip-key">Status filter</span><span class="tip-val">Use the All / Not started / Partial / Complete / Missing funscript buttons to filter the folder list. Works together with the search box.</span></div>
      <div class="tip"><span class="tip-key">Open folder</span><span class="tip-val">Click <b>open</b> on any folder row to open it in your file manager.</span></div>
      <div class="tip"><span class="tip-key">Copy path</span><span class="tip-val">Click <b>copy</b> on any folder row to copy the full path to your clipboard.</span></div>
      <div class="tip"><span class="tip-key">Reports</span><span class="tip-val">Click <b>Jump to Reports</b> in the header to skip straight to the CSV report tables at the bottom.</span></div>
      <div class="tip"><span class="tip-key">Long cells</span><span class="tip-val">Truncated cells in report tables show <b>⋯</b> — click a cell to expand it in place, click again to collapse.</span></div>
      <div class="tip"><span class="tip-key">Full path</span><span class="tip-val">Hover over any truncated path or URL cell to see the full value in a tooltip.</span></div>
    </div>
  </div>
</details>

<section class="summary-section">
  <div class="stat-cards">{cards}</div>
  <div class="prog-section-title">Script coverage{fs_csv_link}</div>
  {bars}
</section>

<section class="folders-section">
  <div class="folders-hdr">
    <span class="folders-title">Folders</span>
    <input class="folder-search" type="search" placeholder="Filter folders…" oninput="filterFolders(this.value)">
  </div>
  <div class="status-filters">
    <button class="sf-btn sf-active" data-filter="all"        onclick="setStatusFilter(this)">All</button>
    <button class="sf-btn"           data-filter="no-log"     onclick="setStatusFilter(this)">Not started</button>
    <button class="sf-btn"           data-filter="partial"    onclick="setStatusFilter(this)">Partial</button>
    <button class="sf-btn"           data-filter="complete"   onclick="setStatusFilter(this)">Complete</button>
    <button class="sf-btn"           data-filter="missing-fs" onclick="setStatusFilter(this)">Missing funscript</button>
  </div>
  {folders}
</section>

<section class="reports-section" id="reports">
  <div class="reports-hdr">
    <span class="reports-title">Reports</span>
  </div>
  {reports}
</section>

<script>{_JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if args:
        base = os.path.abspath(args[0])
    else:
        entered = input('Enter full path to scan: ').strip()
        base = os.path.abspath(entered) if entered else os.getcwd()

    if not os.path.isdir(base):
        print(f'Directory not found: {base}')
        sys.exit(1)

    page = generate(base)

    out_path = os.path.join(_reports_dir(base), 'audit_report.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(page)
    print(f'Report written to: {out_path}')


if __name__ == '__main__':
    main()

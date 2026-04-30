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

import html
import json
import os
import sys
import time

import folder_log as _flog


_SCRIPTS = ('prefixFix', 'downloadContent', 'fix_garbled_names', 'generate_html')
_LABELS  = {
    'prefixFix':        'Prefix Fix',
    'downloadContent':  'Download',
    'fix_garbled_names':'Name Fix',
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
    n_downloaded   = 0
    n_files_saved  = 0
    n_renames      = 0
    n_forced       = 0
    n_with_log     = 0

    for e in entries:
        log = e['log']
        if log:
            n_with_log += 1
        scripts_seen: set[str] = set()
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

    cards_html = ''.join([
        card(total,         'Total folders'),
        card(n_with_log,    'Folders logged'),
        card(n_unlogged,    'Not yet processed', 'warn' if n_unlogged else ''),
        card(script_done['downloadContent'], 'Downloads complete'),
        card(n_downloaded,  'Files downloaded'),
        card(n_files_saved, 'Files saved'),
        card(n_renames,     'Files renamed'),
        card(n_forced,      'Force-rerun entries', 'warn' if n_forced else ''),
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
        name = e['name']
        log  = e['log']
        scripts_done_set = {run.get('script') for run in log}

        badges = ''.join(
            f'<span class="fbadge {"fb-done" if s in scripts_done_set else "fb-pend"}" title="{_ea(s)}">'
            f'{_e(_LABELS[s])}</span>'
            for s in _SCRIPTS
        )

        runs_html = ''.join(_render_run(r) for r in log) if log else '<div class="no-log">No history recorded yet.</div>'

        folder_items.append(
            f'<details class="fi" data-name="{_ea(name.lower())}">'
            f'<summary class="fi-sum">'
            f'<span class="fi-name">{_e(name)}</span>'
            f'<span class="fi-badges">{badges}</span>'
            f'</summary>'
            f'<div class="fi-body">{runs_html}</div>'
            f'</details>'
        )

    folders_html = '\n'.join(folder_items)

    return _build_page(base=_e(base), generated=_e(now),
                       cards=cards_html, bars=bars_html, folders=folders_html)


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
    display: flex; align-items: center; gap: 1rem;
    margin-bottom: 1rem;
}
.folders-title { font-size: 1rem; font-weight: 600; color: #a0a0c8; }
.folder-search {
    flex: 1; max-width: 320px;
    background: #22222e; border: 1px solid #38384a; border-radius: 4px;
    padding: .35rem .65rem; color: #d0d0e8; font-size: .82rem;
    outline: none;
}
.folder-search:focus { border-color: #5050a0; }

/* ---- folder item ---- */
.fi {
    background: #1e1e28; border: 1px solid #2e2e40;
    border-radius: 5px; margin-bottom: .45rem;
}
.fi[open] { border-color: #44446a; }
.fi-sum {
    display: flex; align-items: center; gap: .75rem;
    padding: .55rem .8rem; cursor: pointer; list-style: none;
    user-select: none;
}
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
.s-generate_html    { background: #2e2a1a; color: #c0a040; border: 1px solid #4a4020; }

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
"""

_JS = """
function filterFolders(q) {
    q = q.toLowerCase().trim();
    document.querySelectorAll('.fi').forEach(el => {
        el.style.display = !q || el.dataset.name.includes(q) ? '' : 'none';
    });
}
"""


def _build_page(*, base, generated, cards, bars, folders) -> str:
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
  <div class="page-meta">{base}<span class="sep">·</span>Generated {generated}</div>
</header>

<section class="summary-section">
  <div class="stat-cards">{cards}</div>
  <div class="prog-section-title">Script coverage</div>
  {bars}
</section>

<section class="folders-section">
  <div class="folders-hdr">
    <span class="folders-title">Folders</span>
    <input class="folder-search" type="search" placeholder="Filter folders…" oninput="filterFolders(this.value)">
  </div>
  {folders}
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

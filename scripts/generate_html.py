#!/usr/bin/env python3
"""
Generate a description.html inside every Patreon post folder.

For each subfolder that contains a description.json the script writes a
standalone description.html next to it showing:
  - Post metadata (ID, date, title) parsed from the folder name
  - Rendered rich-text from the ProseMirror description.json
  - Local files listed by type (images shown inline using relative paths)
  - A "manual" badge if a .manual file is present

Usage
-----
  python generate_html.py [directory] [--dry-run]

  directory   root folder to search (defaults to current working directory)
  --dry-run   print what would be written without writing anything
"""

import html
import json
import os
import re
import sys
from pathlib import Path
import folder_log

# ---------------------------------------------------------------------------
# Folder name parsing
# ---------------------------------------------------------------------------

_FOLDER_RE = re.compile(r'^\[(\d+)\]\s+(\d{4}-\d{2}-\d{2})\s+(.+)$')


def _parse_folder_name(name: str) -> tuple[str, str, str]:
    """Return (post_id, date, title) or ('', '', name) if format not matched."""
    m = _FOLDER_RE.match(name)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return '', '', name


# ---------------------------------------------------------------------------
# ProseMirror → HTML renderer
# ---------------------------------------------------------------------------

_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif'}
_VIDEO_EXTS = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4v'}
_SCRIPT_EXTS = {'.funscript'}


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _escape_attr(text: str) -> str:
    return html.escape(text, quote=True)


def _apply_marks(text_html: str, marks: list) -> str:
    """Wrap *text_html* with the given ProseMirror mark list (innermost first)."""
    for mark in reversed(marks):
        t = mark.get('type', '')
        if t == 'bold':
            text_html = f'<strong>{text_html}</strong>'
        elif t == 'italic':
            text_html = f'<em>{text_html}</em>'
        elif t == 'underline':
            text_html = f'<u>{text_html}</u>'
        elif t == 'strike':
            text_html = f'<s>{text_html}</s>'
        elif t == 'code':
            text_html = f'<code>{text_html}</code>'
        elif t == 'link':
            href = _escape_attr(mark.get('attrs', {}).get('href', '#'))
            text_html = f'<a href="{href}" target="_blank" rel="noopener">{text_html}</a>'
    return text_html


def _children_html(node: dict) -> str:
    return ''.join(_node_to_html(c) for c in node.get('content', []))


def _node_to_html(node: dict) -> str:
    t = node.get('type', '')

    if t == 'doc':
        return _children_html(node)

    if t == 'paragraph':
        inner = _children_html(node)
        return f'<p>{inner if inner else "<br>"}</p>\n'

    if t == 'text':
        text_html = _escape(node.get('text', ''))
        marks = node.get('marks', [])
        return _apply_marks(text_html, marks) if marks else text_html

    if t == 'hardBreak':
        return '<br>'

    if t == 'heading':
        level = node.get('attrs', {}).get('level', 3)
        level = max(1, min(6, int(level)))
        inner = _children_html(node)
        return f'<h{level}>{inner}</h{level}>\n'

    if t == 'bulletList':
        return f'<ul>\n{_children_html(node)}</ul>\n'

    if t == 'orderedList':
        return f'<ol>\n{_children_html(node)}</ol>\n'

    if t == 'listItem':
        return f'<li>{_children_html(node)}</li>\n'

    if t == 'blockquote':
        return f'<blockquote>{_children_html(node)}</blockquote>\n'

    if t == 'image':
        attrs = node.get('attrs', {})
        src = _escape_attr(attrs.get('src') or '')
        alt = _escape_attr(attrs.get('alt') or '')
        caption = _escape(attrs.get('caption') or '')
        if src:
            fig = f'<figure class="remote-img"><img src="{src}" alt="{alt}" loading="lazy">'
            if caption:
                fig += f'<figcaption>{caption}</figcaption>'
            fig += '</figure>\n'
            return fig
        return ''

    # Graceful fallback — unknown node type: just render children
    return _children_html(node)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: system-ui, sans-serif;
    background: #1a1a1e;
    color: #e0e0e8;
    padding: 2rem;
    line-height: 1.6;
    max-width: 860px;
    margin: 0 auto;
}

/* ---- Post header ---- */
.post-header {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: .6rem;
    padding-bottom: .75rem;
    margin-bottom: 1.25rem;
    border-bottom: 1px solid #38384a;
}

.post-id {
    font-size: .75rem;
    color: #7070a0;
    font-family: monospace;
    white-space: nowrap;
}

.post-date {
    font-size: .85rem;
    color: #8080b0;
    white-space: nowrap;
}

.post-title {
    font-size: 1.15rem;
    font-weight: 700;
    color: #c8c8f0;
    flex: 1;
    min-width: 0;
}

.manual-badge {
    font-size: .72rem;
    background: #2a5a2a;
    color: #80d080;
    border: 1px solid #3a7a3a;
    border-radius: 4px;
    padding: .15rem .5rem;
    white-space: nowrap;
}

/* ---- Description ---- */
.description {
    font-size: .93rem;
    color: #d0d0e8;
}

.description p { margin: .45rem 0; }
.description p:first-child { margin-top: 0; }

.description h1, .description h2,
.description h3, .description h4,
.description h5, .description h6 {
    margin: 1rem 0 .35rem;
    color: #b0b0d8;
    line-height: 1.3;
}
.description h1 { font-size: 1.2rem; }
.description h2 { font-size: 1.1rem; }
.description h3 { font-size: 1rem; }
.description h4 { font-size: .95rem; }

.description ul, .description ol {
    margin: .5rem 0 .5rem 1.5rem;
}
.description li { margin: .2rem 0; }

.description blockquote {
    border-left: 3px solid #555570;
    padding-left: .85rem;
    color: #a0a0c0;
    margin: .5rem 0;
}

.description a { color: #7ab0ff; word-break: break-all; }
.description a:hover { color: #a0caff; }
.description strong { font-weight: 700; }
.description em { font-style: italic; }

.description code {
    font-family: monospace;
    font-size: .85em;
    background: #1e1e28;
    padding: .1em .35em;
    border-radius: 3px;
}

/* ---- Remote images (from description JSON) ---- */
.description figure.remote-img {
    margin: .8rem 0;
    text-align: center;
}
.description figure.remote-img img {
    max-width: 100%;
    max-height: 360px;
    border-radius: 5px;
    border: 1px solid #38384a;
}
.description figcaption {
    font-size: .75rem;
    color: #606080;
    margin-top: .3rem;
}

/* ---- Local files ---- */
.local-files {
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px solid #30303e;
}

.files-label {
    font-size: .72rem;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: #505070;
    margin-bottom: .6rem;
}

.local-images {
    display: flex;
    flex-wrap: wrap;
    gap: .5rem;
    margin-bottom: .75rem;
}

.local-images a img {
    height: 110px;
    width: auto;
    border-radius: 5px;
    border: 1px solid #38384a;
    object-fit: cover;
    cursor: pointer;
    transition: border-color .15s;
}
.local-images a img:hover { border-color: #7ab0ff; }

.file-list {
    font-size: .8rem;
    color: #8080a0;
}
.file-list .file-group { margin-bottom: .4rem; }
.file-list .file-group-label {
    color: #606080;
    margin-right: .35rem;
}
.file-list .file-name {
    display: inline-block;
    background: #1e1e28;
    border-radius: 3px;
    padding: .1em .4em;
    margin: .1rem .2rem .1rem 0;
    word-break: break-all;
}
"""

# ---------------------------------------------------------------------------
# Per-folder page generation
# ---------------------------------------------------------------------------

def _render_page(folder_path: str, folder_name: str) -> str:
    """Return a complete HTML document for the post in *folder_path*."""
    desc_path = os.path.join(folder_path, 'description.json')
    post_id, date, title = _parse_folder_name(folder_name)
    is_manual = os.path.exists(os.path.join(folder_path, '.manual'))

    # --- Render description ---
    try:
        with open(desc_path, 'r', encoding='utf-8') as f:
            doc = json.load(f)
        desc_html = _node_to_html(doc)
    except Exception as e:
        desc_html = f'<p><em>Could not parse description: {_escape(str(e))}</em></p>'

    # --- Collect local files (relative names — HTML lives in the same folder) ---
    images, scripts, videos, others = [], [], [], []
    try:
        for fname in sorted(os.listdir(folder_path)):
            if fname in ('description.json', 'description.html', '.manual') or fname.startswith('.'):
                continue
            ext = Path(fname).suffix.lower()
            fpath = os.path.join(folder_path, fname)
            if not os.path.isfile(fpath):
                continue
            if ext in _IMAGE_EXTS:
                images.append(fname)
            elif ext in _SCRIPT_EXTS:
                scripts.append(fname)
            elif ext in _VIDEO_EXTS:
                videos.append(fname)
            else:
                others.append(fname)
    except OSError:
        pass

    # --- Post header ---
    header_parts = ['<header class="post-header">']
    if post_id:
        header_parts.append(f'<span class="post-id">[{_escape(post_id)}]</span>')
    if date:
        header_parts.append(f'<span class="post-date">{_escape(date)}</span>')
    header_parts.append(f'<span class="post-title">{_escape(title)}</span>')
    if is_manual:
        header_parts.append('<span class="manual-badge">✔ manual</span>')
    header_parts.append('</header>')

    # --- Local files section ---
    files_html = ''
    if images or scripts or videos or others:
        parts = ['<section class="local-files">',
                 '<div class="files-label">Local files</div>']
        if images:
            parts.append('<div class="local-images">')
            for fname in images:
                enc = _escape_attr(fname)
                parts.append(
                    f'<a href="{enc}" target="_blank">'
                    f'<img src="{enc}" alt="{enc}" title="{enc}">'
                    f'</a>'
                )
            parts.append('</div>')
        parts.append('<div class="file-list">')
        for label, names in (('Scripts', scripts), ('Videos', videos), ('Other', others)):
            if names:
                parts.append(f'<div class="file-group"><span class="file-group-label">{label}:</span>')
                for n in names:
                    parts.append(f'<span class="file-name">{_escape(n)}</span>')
                parts.append('</div>')
        parts.append('</div>')  # file-list
        parts.append('</section>')
        files_html = '\n'.join(parts)

    page_title = f'{date} {title}' if date else title

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(page_title)}</title>
<style>{_CSS}</style>
</head>
<body>
{''.join(header_parts)}
<div class="description">
{desc_html}</div>
{files_html}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate(root_dir: str, dry_run: bool) -> int:
    root_dir = os.path.abspath(root_dir)
    written = 0

    entries = []
    try:
        for name in sorted(os.listdir(root_dir)):
            folder = os.path.join(root_dir, name)
            if os.path.isdir(folder) and os.path.isfile(os.path.join(folder, 'description.json')):
                entries.append((name, folder))
    except OSError as e:
        print(f'Error reading directory: {e}', file=sys.stderr)
        sys.exit(1)

    print(f'Found {len(entries)} post folder(s) in: {root_dir}')
    if dry_run:
        print('(dry run — no files will be written)\n')

    for name, folder in entries:
        if folder_log.has_run(folder, 'generate_html'):
            print(f'  skip (done)  {name}')
            continue
        out_path = os.path.join(folder, 'description.html')
        if dry_run:
            print(f'  WOULD WRITE  {out_path}')
        else:
            page_html = _render_page(folder, name)
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(page_html)
            print(f'  wrote  {out_path}')
            folder_log.append_run(folder, 'generate_html')
        written += 1

    return written


if __name__ == '__main__':
    args = sys.argv[1:]
    dry_run = '--dry-run' in args
    dirs = [a for a in args if not a.startswith('--')]
    if dirs:
        root = os.path.abspath(dirs[0])
    else:
        entered = input("Enter full path to scan (leave blank for current directory): ").strip()
        root = os.path.abspath(entered) if entered else os.getcwd()

    if not os.path.isdir(root):
        print(f'Directory not found: {root}', file=sys.stderr)
        sys.exit(1)

    count = generate(root, dry_run)
    label = 'would be written' if dry_run else 'written'
    print(f'\nDone. {count} file(s) {label}.')

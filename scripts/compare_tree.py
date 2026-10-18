"""Compare a library with the tree snapshot reset_library.py saved, and optionally restore old names.

    ./compare_tree.sh /path/to/library-root                      summary + HTML report (changes nothing)
    ./compare_tree.sh ROOT --creator Pize --only "[150487431]"   narrow it down (substring match on the path)
    ./compare_tree.sh ROOT --restore-names                       show which renames would put old names back
    ./compare_tree.sh ROOT --restore-names --apply               do them (journalled: the main menu's Undo reverses them)
    --version N picks an older snapshot (default: the newest).

The snapshot holds names and layout only. Matching is deliberately conservative:
  * a folder is "the same post" when it carries the same [post id] in its name under the same creator folder, and the
    id appears exactly once on each side; then the CURRENT folder can be renamed (or moved) back to its old path;
  * inside a matched folder, a file is "the same file" when exactly one missing and one new file share the same size.
Everything else is only listed, never renamed. An existing path is never overwritten.
"""
import argparse
import gzip
import json
import os
import re
import sys
import time

import action_log
import library_db
import reset_library as rs

ID_RE = re.compile(r'\[(\d{5,})\]')


def list_versions(root: str) -> list[str]:
    base = os.path.join(root, library_db.DATA_DIRNAME, rs.PREVIOUS)
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base) if d[:1] == 'v' and os.path.isfile(os.path.join(base, d, 'tree.jsonl.gz')))


def version_scope(root: str, version: str) -> str:
    """The folder (below the library root, '' = all) that snapshot covered."""
    try:
        with open(os.path.join(root, library_db.DATA_DIRNAME, rs.PREVIOUS, version, 'snapshot.json'), encoding='utf-8') as f:
            return json.load(f).get('scope', '')
    except (OSError, json.JSONDecodeError):
        return ''


def region_for(version_scope_: str, wanted: str):
    """The part of the library to compare for a snapshot of *version_scope_* when the user asked about *wanted*:
    the more specific of the two, or None if they do not overlap."""
    if not version_scope_ or wanted == version_scope_ or wanted.startswith(version_scope_ + os.sep):
        return wanted
    if not wanted or version_scope_.startswith(wanted + os.sep):
        return version_scope_
    return None


def load_snapshot(root: str, version: str) -> dict[str, tuple]:
    path = os.path.join(root, library_db.DATA_DIRNAME, rs.PREVIOUS, version, 'tree.jsonl.gz')
    out = {}
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        for line in f:
            r = json.loads(line)
            out[r['p']] = (r['t'], r['s'])
    return out


def _id_of(path: str):
    m = ID_RE.search(os.path.basename(path))
    return m.group(1) if m else None


def _creator(path: str) -> str:
    return path.split(os.sep)[0]


def compare(old: dict, new: dict, creator: str | None = None, only: str | None = None) -> dict:
    def keep(p: str) -> bool:
        return (not creator or _creator(p) == creator) and (not only or only in p)

    missing = sorted(p for p in old if p not in new and keep(p))
    added = sorted(p for p in new if p not in old and keep(p))
    missing_dirs = [p for p in missing if old[p][0] == 'd']
    added_dirs = [p for p in added if new[p][0] == 'd']

    by_id_old: dict = {}
    by_id_new: dict = {}
    for p in missing_dirs:
        i = _id_of(p)
        if i:
            by_id_old.setdefault((_creator(p), i), []).append(p)
    for p in added_dirs:
        i = _id_of(p)
        if i:
            by_id_new.setdefault((_creator(p), i), []).append(p)
    dir_renames = []
    for key, olds in by_id_old.items():
        news = by_id_new.get(key, [])
        if len(olds) == 1 and len(news) == 1:
            dir_renames.append({'from': news[0], 'to': olds[0]})
    # pairs of (old dir, current dir) whose direct files can be matched: renamed ones plus same-path ones
    pairs = [(r['to'], r['from']) for r in dir_renames]
    pairs += [(p, p) for p in old if old[p][0] == 'd' and p in new and keep(p)]
    file_renames = []
    for old_dir, new_dir in pairs:
        lost = [(n, old[os.path.join(old_dir, n)][1]) for n in _children(old, old_dir)
                if old[os.path.join(old_dir, n)][0] == 'f' and os.path.join(new_dir, n) not in new]
        fresh = [(n, new[os.path.join(new_dir, n)][1]) for n in _children(new, new_dir)
                 if new[os.path.join(new_dir, n)][0] == 'f' and os.path.join(old_dir, n) not in old]
        for n, s in lost:
            same_old = [x for x in lost if x[1] == s]
            same_new = [x for x in fresh if x[1] == s]
            if len(same_old) == 1 and len(same_new) == 1 and same_new[0][0] != n and s > 0:
                file_renames.append({'dir': old_dir, 'from': same_new[0][0], 'to': n})
    return {'missing': missing, 'added': added, 'dir_renames': sorted(dir_renames, key=lambda r: r['to']),
            'file_renames': file_renames}


def _children(tree: dict, d: str):
    prefix = d + os.sep
    for p in tree:
        if p.startswith(prefix) and os.sep not in p[len(prefix):]:
            yield p[len(prefix):]


def restore(root: str, result: dict, apply: bool, log=print) -> int:
    """Rename current folders/files back to their old names. Returns how many renames were (or would be) made."""
    n = 0
    if apply:
        action_log.start('compare_tree', root)
    try:
        for r in sorted(result['dir_renames'], key=lambda r: r['to'].count(os.sep)):
            src, dst = os.path.join(root, r['from']), os.path.join(root, r['to'])
            if not os.path.isdir(src) or os.path.exists(dst):
                log(f'  skip (source gone or target exists): {r["from"]} -> {r["to"]}')
                continue
            log(f'  {"rename" if apply else "would rename"}: {r["from"]}  ->  {r["to"]}')
            if apply:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.rename(src, dst)
                action_log.record('rename', old_path=src, new_path=dst)
            n += 1
        for r in result['file_renames']:
            folder = os.path.join(root, r['dir'])          # after the folder renames above, files sit under the old path
            src, dst = os.path.join(folder, r['from']), os.path.join(folder, r['to'])
            if apply and (not os.path.isfile(src) or os.path.exists(dst)):
                log(f'  skip (source gone or target exists): {r["dir"]}/{r["from"]}')
                continue
            log(f'  {"rename" if apply else "would rename"}: {r["dir"]}/{r["from"]}  ->  {r["to"]}')
            if apply:
                os.rename(src, dst)
                action_log.record('rename', old_path=src, new_path=dst)
            n += 1
    finally:
        if apply:
            action_log.finish()
    return n


def render_html(root: str, version: str, result: dict) -> str:
    e = rs._e

    def table(title, rows, head):
        body = ''.join('<tr>' + ''.join(f'<td>{e(c)}</td>' for c in r) + '</tr>' for r in rows[:500])
        more = f'<p class="muted">... and {len(rows) - 500} more.</p>' if len(rows) > 500 else ''
        return f'<h2>{e(title)} ({len(rows)})</h2><table><tr>{"".join(f"<th>{h}</th>" for h in head)}</tr>{body or "<tr><td class=muted>None.</td></tr>"}</table>{more}'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Library tree comparison</title><style>{rs._CSS}</style></head><body><main>
<h1>Library tree comparison</h1><p class="muted">{e(root)} vs snapshot {e(version)}. Nothing was changed.</p>
{table('Folders that can be renamed back (same post id)', [(r['from'], r['to']) for r in result['dir_renames']], ['Now', 'Was'])}
{table('Files that can be renamed back (same size)', [(r['dir'], r['from'], r['to']) for r in result['file_renames']], ['In folder', 'Now', 'Was'])}
{table('In the old tree but not now', [(p,) for p in result['missing']], ['Path'])}
{table('New since the snapshot', [(p,) for p in result['added']], ['Path'])}
</main></body></html>"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Compare a library with its saved pre-reset tree snapshot')
    parser.add_argument('root', help='the library root, or a folder inside it (compares just that folder)')
    parser.add_argument('--version', type=int, help='snapshot number (default: newest)')
    parser.add_argument('--creator', help='only this top-level creator folder')
    parser.add_argument('--only', help='only paths containing this text')
    parser.add_argument('--restore-names', action='store_true', help='list the renames that would restore old names')
    parser.add_argument('--apply', action='store_true', help='with --restore-names: actually rename')
    args = parser.parse_args(argv)
    root, wanted = rs.resolve_target(args.root)
    versions = list_versions(root)
    if not versions:
        print(f'No snapshot in {root}/.library-data/{rs.PREVIOUS}. Run reset.sh first.')
        return 2
    if args.version:
        version = next((v for v in versions if int(v[1:4]) == args.version), None)
        if not version:
            print(f'No snapshot v{args.version:03d}. Available: {", ".join(versions)}')
            return 2
        region = region_for(version_scope(root, version), wanted)
    else:
        version, region = next(((v, r) for v in reversed(versions)
                                if (r := region_for(version_scope(root, v), wanted)) is not None), (None, None))
    if version is None or region is None:
        print(f'No snapshot covers {wanted or "the library"}. Snapshots: ' +
              ', '.join(f'{v} ({version_scope(root, v) or "whole library"})' for v in versions))
        return 2
    prefix = region + os.sep if region else ''
    old = {p: v for p, v in load_snapshot(root, version).items() if not region or p == region or p.startswith(prefix)}
    new = {r: (k, s) for r, k, s, _m in rs.iter_tree(root, region)}
    print(f'Comparing {os.path.join(root, region) if region else root} with snapshot {version}.')
    result = compare(old, new, args.creator, args.only)
    print(f'Snapshot {version}: {len(result["missing"])} path(s) missing now, {len(result["added"])} new, '
          f'{len(result["dir_renames"])} folder(s) and {len(result["file_renames"])} file(s) renameable back.')
    if args.restore_names:
        count = restore(root, result, args.apply)
        print(f'{"Renamed" if args.apply else "Would rename"} {count} item(s).' +
              ('' if args.apply else ' Re-run with --apply to do it.'))
        return 0
    report = os.path.join(library_db.data_dir(root, 'reports'), f'compare_{time.strftime("%Y%m%d_%H%M%S")}.html')
    with open(report, 'w', encoding='utf-8') as f:
        f.write(render_html(root, version, result))
    print(f'Report: {report}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nCancelled.')
        sys.exit(130)

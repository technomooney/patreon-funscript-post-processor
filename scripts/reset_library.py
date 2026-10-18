"""Reset ("start fresh") a library, keeping a snapshot of how it looked, and leaving `.manual` folders alone.

For when the whole library has to be downloaded again (e.g. everything was compressed by an old compressor) and
you want the normal download process to rebuild it from scratch. Standalone, not in the menu:
    ./reset.sh /path/to/library-root                          1. plan + HTML report + confirmation token (read-only)
    ./reset.sh /path/to/library-root --confirm TOKEN          2. apply (resumable)
Compare afterwards with  ./compare_tree.sh /path/to/library-root   (see compare_tree.py).

What it does:
  * Every folder you marked `.manual` is kept exactly as it is, together with the folders above it. (`.manual` + `.consolidated` together is
    written by the tools themselves on folders they emptied, so those are NOT kept.)
  * Everything else (post folders, media, the Pize archives, ...) is REMOVED -- deliberately deleted, not trashed,
    because the point is to free the space and let the regular download recreate it. Not touched: .library-data,
    .trash, .av1-compressor, hidden files/folders and creator_config.json at the top two levels.
  * Before anything is removed, a version folder  <root>/.library-data/previous/vNNN_<timestamp>/  is written with
        tree.jsonl.gz   every path that existed (type, size, mtime) -- the "previous setup"
        library.db      a copy of the library database as it was
        legacy/         the legacy logs the migration filed away
        logs/           the per-folder logs / Pize manifests found in the removed area
        meta.json, README.txt
  * Afterwards the database forgets everything outside the `.manual` folders (run history, file identities,
    pixeldrain links, drift, markers, the imported Pize manifest data) so the downloader starts fresh. Discord
    message history is kept.

Safety: the plan is read-only and produces an HTML report plus a token that only works while the library still
matches the plan; the snapshot is written and checked BEFORE the first removal; each phase is recorded so Ctrl+C or a
crash resumes with the same command. There is no undo for the removal itself -- the snapshot records names and
layout only, file contents come back through the regular download.
"""
import argparse
import gzip
import json
import hashlib
import os
import shutil
import sqlite3
import sys
import time

import library_db
import rebuild_library as rb          # token + report styling are shared with the rebuild tool

TOOL = 'downloader'
KEEP_DIRNAMES = {library_db.DATA_DIRNAME, '.trash', '.av1-compressor'}
KEEP_CREATOR_FILES = {'creator_config.json'}
LOG_NAMES = {'.folder_log.json', '.manifest.json'}
PLAN_KEY = 'reset_plan:'
PREVIOUS = 'previous'
_UPDATED_BY = 'reset_library'


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def find_manual_folders(root: str) -> list[str]:
    """Relative paths of every folder the USER marked `.manual` ('' = the root itself). A `.manual` that sits next to
    `.consolidated` was written by consolidate_packs / extract_variant_archives, not by a person, so it is not kept."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in KEEP_DIRNAMES)
        if '.manual' in filenames and '.consolidated' not in filenames:
            rel = os.path.relpath(dirpath, root)
            found.append('' if rel == '.' else rel)
    return sorted(found)


def _ancestors(rel: str) -> set[str]:
    out, parts = set(), rel.split(os.sep) if rel else []
    for i in range(len(parts)):
        out.add(os.sep.join(parts[:i]))
    return out


def _under(rel: str, folders) -> bool:
    return any(rel == f or rel.startswith(f + os.sep) for f in folders)


def removal_entries(root: str, manual: list[str]) -> list[tuple[str, str]]:
    """[(relative path, 'd'|'f')] -- the minimal set of entries whose removal clears everything except the
    protected `.manual` folders, the folders above them and the hidden/config entries at the top two levels."""
    if '' in manual:
        return []                                      # the root itself is marked: nothing is removed
    keep_dirs = set(manual)
    above = set().union(*(_ancestors(m) for m in manual)) if manual else set()
    out: list[tuple[str, str]] = []

    def walk(rel: str, depth: int) -> None:
        with os.scandir(os.path.join(root, rel)) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            erel = os.path.join(rel, e.name) if rel else e.name
            is_dir = e.is_dir(follow_symlinks=False)
            if e.name in KEEP_DIRNAMES:
                continue
            if depth <= 1:                              # root and creator level
                if e.name.startswith('.') or (depth == 0 and not is_dir) or \
                        (depth == 1 and not is_dir and e.name in KEEP_CREATOR_FILES):
                    continue
            if not is_dir:
                out.append((erel, 'f'))
            elif erel in keep_dirs or _under(erel, keep_dirs):
                continue
            elif erel in above or depth == 0:           # a container of a protected folder / a creator folder
                walk(erel, depth + 1)
            else:
                out.append((erel, 'd'))

    walk('', 0)
    return out


def _tree_stats(root: str, rel: str, kind: str) -> tuple[int, int]:
    """(file count, bytes) of one entry."""
    full = os.path.join(root, rel)
    if kind == 'f':
        try:
            return 1, os.lstat(full).st_size
        except OSError:
            return 0, 0
    n = size = 0
    for dp, _dn, fn in os.walk(full):
        for f in fn:
            try:
                size += os.lstat(os.path.join(dp, f)).st_size
                n += 1
            except OSError:
                pass
    return n, size


def iter_tree(root: str):
    """Yield (rel, 'd'|'f', size, mtime_ns) for everything under root except the tools' own folders."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in KEEP_DIRNAMES)
        rel_dir = os.path.relpath(dirpath, root)
        for d in dirnames:
            full = os.path.join(dirpath, d)
            yield (d if rel_dir == '.' else os.path.join(rel_dir, d)), 'd', 0, _mtime(full)
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            yield (f if rel_dir == '.' else os.path.join(rel_dir, f)), 'f', st.st_size, st.st_mtime_ns


def _mtime(path: str) -> int:
    try:
        return os.lstat(path).st_mtime_ns
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# 1. Plan
# ---------------------------------------------------------------------------

def build_plan(root: str) -> dict:
    manual = find_manual_folders(root)
    entries = removal_entries(root, manual)
    items, total_files, total_bytes = [], 0, 0
    per_creator: dict[str, list[int]] = {}
    for rel, kind in entries:
        n, size = _tree_stats(root, rel, kind)
        items.append({'path': rel, 'kind': kind, 'files': n, 'bytes': size})
        total_files += n
        total_bytes += size
        c = per_creator.setdefault(rel.split(os.sep)[0], [0, 0])
        c[0] += n
        c[1] += size
    kept = []
    for m in manual:
        n, size = _tree_stats(root, m, 'd')
        kept.append({'path': m, 'files': n, 'bytes': size})
    material = json.dumps([library_db.library_id(root), [[i['path'], i['kind'], i['files'], i['bytes']] for i in items],
                           manual], sort_keys=True)
    return {'root': root, 'generated_at': _now(), 'fingerprint': hashlib.sha256(material.encode()).hexdigest(),
            'entries': items, 'manual': kept, 'total_files': total_files, 'total_bytes': total_bytes,
            'per_creator': {k: {'files': v[0], 'bytes': v[1]} for k, v in sorted(per_creator.items())}}


def record_plan(root: str, plan: dict) -> str:
    expires = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() + rb.TOKEN_VALID_DAYS * 86400))
    token = rb.make_token(plan['fingerprint'], expires)
    plan.update(token=token, expires_at=expires, status='planned', version=None, phase=None)
    library_db.tool_data_set(root, TOOL, PLAN_KEY + token, plan, _UPDATED_BY)
    return token


def _load(root: str, token: str) -> dict:
    plan = library_db.tool_data_get(root, TOOL, PLAN_KEY + token)
    if not plan:
        raise rb.TokenError('That token is not recognised. Generate a new plan (and report) first.')
    return plan


def _save(root: str, plan: dict) -> None:
    library_db.tool_data_set(root, TOOL, PLAN_KEY + plan['token'], plan, _UPDATED_BY)


def check_token(root: str, token: str) -> dict:
    plan = _load(root, token)
    if plan['status'] == 'done':
        raise rb.TokenError('This plan was already applied. Generate a new plan if you want to reset again.')
    if plan['expires_at'] < _now():
        raise rb.TokenError('That token has expired. Generate a new plan.')
    if plan['status'] == 'planned' and build_plan(root)['fingerprint'] != plan['fingerprint']:
        raise rb.TokenError('The library changed since the report was made. Generate a new plan and read its report.')
    return plan


def _gb(n: int) -> str:
    return f'{n / 1e9:.2f} GB'


def render_plan_html(plan: dict) -> str:
    e = rb._e
    cre = ''.join(f'<tr><td>{e(k)}</td><td>{v["files"]}</td><td>{_gb(v["bytes"])}</td></tr>' for k, v in plan['per_creator'].items())
    kept = ''.join(f'<tr><td>{e(m["path"] or "(library root)")}</td><td>{m["files"]}</td><td>{_gb(m["bytes"])}</td></tr>'
                   for m in plan['manual']) or '<tr><td colspan="3" class="muted">None.</td></tr>'
    top = ''.join(f'<tr><td>{e(i["kind"])}</td><td>{e(i["path"])}</td><td>{i["files"]}</td></tr>' for i in plan['entries'][:400])
    more = f'<p class="muted">... and {len(plan["entries"]) - 400} more entries.</p>' if len(plan['entries']) > 400 else ''
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Library reset plan</title><style>{rb._CSS}</style></head><body><main>
<h1>Library reset plan</h1>
<p class="muted">{e(plan['root'])} &middot; generated {e(plan['generated_at'])}. Nothing has been changed yet.</p>
<div class="cards"><div class="card"><b>{plan['total_files']}</b>files to remove</div><div class="card"><b>{_gb(plan['total_bytes'])}</b>to be freed</div>
<div class="card"><b>{len(plan['manual'])}</b>.manual folders kept</div></div>
<p><b class="bad">This DELETES everything listed below</b> (it is not moved to .trash). Before that, a snapshot of the whole tree
and a copy of the library database are saved in <code>.library-data/previous/</code>; only names and layout are kept, the files
themselves come back through your normal download. Folders holding a <code>.manual</code> marker, the folders above them,
<code>.library-data</code>, <code>.trash</code>, <code>.av1-compressor</code>, hidden entries and <code>creator_config.json</code> are never touched.
The library database then forgets run history, file identities, pixeldrain links and Pize manifest data outside the kept folders.</p>
<div class="token"><b>Confirmation token</b> (valid until {e(plan['expires_at'])}; only works while the library is unchanged):<br>
<code id="tok">{e(plan['token'])}</code> <button type="button" onclick="navigator.clipboard&&navigator.clipboard.writeText(document.getElementById('tok').textContent)">Copy</button><br>
<span class="muted">Apply with:</span> <code>./reset.sh {e(plan['root'])} --confirm {e(plan['token'])}</code></div>
<h2>Kept: .manual folders ({len(plan['manual'])})</h2><p class="muted">Untouched. These are the places you organised by hand.</p>
<table><tr><th>Folder</th><th>Files</th><th>Size</th></tr>{kept}</table>
<h2>Removed, per creator</h2><table><tr><th>Creator folder</th><th>Files</th><th>Size</th></tr>{cre}</table>
<h2>Removed entries (first 400)</h2><table><tr><th>Type</th><th>Path</th><th>Files</th></tr>{top}</table>{more}
</main></body></html>"""


# ---------------------------------------------------------------------------
# 2. Apply (phases: snapshot -> archived -> removed -> done; each recorded so a re-run resumes)
# ---------------------------------------------------------------------------

def _next_version_dir(root: str) -> str:
    base = library_db.data_dir(root, PREVIOUS)
    n = 1 + max([int(d[1:4]) for d in os.listdir(base) if d[:1] == 'v' and d[1:4].isdigit()] or [0])
    path = os.path.join(base, f'v{n:03d}_{time.strftime("%Y%m%d_%H%M%S")}')
    os.makedirs(path)
    return path


def _write_snapshot(root: str, vdir: str, manual: list[str]) -> int:
    tree = os.path.join(vdir, 'tree.jsonl.gz')
    count = 0
    with gzip.open(tree + '.tmp', 'wt', encoding='utf-8') as out:
        for rel, kind, size, mtime in iter_tree(root):
            out.write(json.dumps({'p': rel, 't': kind, 's': size, 'm': mtime}, ensure_ascii=False) + '\n')
            count += 1
            if kind == 'f' and os.path.basename(rel) in LOG_NAMES and not _under(os.path.dirname(rel), manual):
                dest = os.path.join(vdir, 'logs', rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                try:
                    shutil.copy2(os.path.join(root, rel), dest)
                except OSError:
                    pass
    with gzip.open(tree + '.tmp', 'rt', encoding='utf-8') as f:      # verify before it is relied on
        if sum(1 for _ in f) != count:
            raise RuntimeError('snapshot verification failed; nothing was removed')
    os.replace(tree + '.tmp', tree)
    return count


def _archive_db(root: str, vdir: str) -> None:
    src = sqlite3.connect(library_db.db_path(root))
    dst = sqlite3.connect(os.path.join(vdir, 'library.db'))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    legacy = os.path.join(root, library_db.DATA_DIRNAME, 'legacy')
    if os.path.isdir(legacy):
        os.replace(legacy, os.path.join(vdir, 'legacy'))


def _remove(root: str, entries: list[dict], progress=print) -> int:
    done = 0
    for i, it in enumerate(entries, 1):
        full = os.path.join(root, it['path'])
        if it['kind'] == 'd':
            if os.path.isdir(full):
                shutil.rmtree(full)
        elif os.path.lexists(full):
            os.remove(full)
        done += 1
        if i % 200 == 0:
            progress(f'  removed {i}/{len(entries)} entries...')
    return done


def _reset_db(root: str, manual: list[str]) -> dict:
    """Forget everything outside the protected folders. One transaction."""
    gone = {'runs': 0, 'files': 0, 'links': 0, 'drift': 0, 'markers': 0, 'tool_data': 0}
    with library_db.db(root, write=True) as con:
        for table, key in (('runs', 'folder'), ('files', 'folder'), ('drift', 'folder'), ('markers', 'path')):
            ids = [r['id'] for r in con.execute(f'SELECT id, {key} FROM {table}') if not _under(r[key] or '', manual)]
            con.executemany(f'DELETE FROM {table} WHERE id=?', [(i,) for i in ids])
            gone[table] = len(ids)
        con.execute("DELETE FROM pixeldrain_links WHERE link_id NOT IN "
                    "(SELECT pixeldrain_id FROM files WHERE COALESCE(pixeldrain_id,'')!='')")
        gone['links'] = con.execute('SELECT changes()').fetchone()[0]
        con.execute("DELETE FROM tool_data WHERE tool='pize_pixeldrain_archives'")
        gone['tool_data'] = con.execute('SELECT changes()').fetchone()[0]
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_reset', ?)", (_now(),))
    return gone


def apply(root: str, token: str, progress=print) -> dict:
    plan = check_token(root, token)
    manual = [m['path'] for m in plan['manual']]
    if plan['status'] == 'planned':
        plan['version'] = _next_version_dir(root)
        plan['status'] = 'running'
        plan['phase'] = 'start'
        _save(root, plan)
    vdir = plan['version']
    if plan['phase'] == 'start':
        progress('Writing the tree snapshot...')
        plan['snapshot_entries'] = _write_snapshot(root, vdir, manual)
        plan['phase'] = 'snapshot'
        _save(root, plan)
    if plan['phase'] == 'snapshot':
        progress('Archiving the library database and legacy logs...')
        _archive_db(root, vdir)
        plan['phase'] = 'archived'
        _save(root, plan)
    if plan['phase'] == 'archived':
        progress(f'Removing {len(plan["entries"])} entries ({_gb(plan["total_bytes"])})...')
        _remove(root, plan['entries'], progress)
        plan['phase'] = 'removed'
        _save(root, plan)
    result = {'version': vdir}
    if plan['phase'] == 'removed':
        result['forgot'] = _reset_db(root, manual)
        plan.update(phase='done', status='done', finished_at=_now())
        _save(root, plan)
        meta = {k: plan[k] for k in ('root', 'generated_at', 'finished_at', 'total_files', 'total_bytes',
                                     'snapshot_entries', 'manual', 'per_creator')}
        with open(os.path.join(vdir, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=1, ensure_ascii=False)
        with open(os.path.join(vdir, 'README.txt'), 'w', encoding='utf-8') as f:
            f.write('Snapshot of this library taken by reset_library.py just before its contents were removed.\n'
                    'tree.jsonl.gz: every path (p), type (t: d/f), size (s) and mtime (m). library.db: the database as it was.\n'
                    'legacy/ and logs/: old per-folder logs and manifests. Compare with the current library using\n'
                    'compare_tree.sh <library root> (names/layout only; file contents are not kept).\n')
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Reset a library for a fresh download, keeping a snapshot and .manual folders')
    parser.add_argument('root', help='library root folder')
    parser.add_argument('--confirm', metavar='TOKEN', help='apply the plan that produced this token')
    parser.add_argument('--yes', action='store_true', help='skip the typed confirmation (the token is still required)')
    args = parser.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isfile(library_db.db_path(root)):
        print(f'No library database in {root}. Run migrate.sh on it first.')
        return 2

    if not args.confirm:
        print('Planning (reading the tree; nothing is changed)...')
        plan = build_plan(root)
        token = record_plan(root, plan)
        report = os.path.join(library_db.data_dir(root, 'reports'), f'reset_plan_{time.strftime("%Y%m%d_%H%M%S")}.html')
        with open(report, 'w', encoding='utf-8') as f:
            f.write(render_plan_html(plan))
        print(f'  would remove {plan["total_files"]} file(s), {_gb(plan["total_bytes"])}; keeping {len(plan["manual"])} .manual folder(s)')
        print(f'\nReport written: {report}\nRead it, then apply with:\n  ./reset.sh "{root}" --confirm {token}')
        return 0

    try:
        plan = check_token(root, args.confirm)
        print(f'Plan from {plan["generated_at"]}: remove {plan["total_files"]} file(s), {_gb(plan["total_bytes"])}.')
        if plan['status'] == 'planned' and not args.yes and \
                input('This DELETES them (a snapshot is saved first). Type RESET to continue: ').strip() != 'RESET':
            print('Cancelled. Nothing changed.')
            return 1
        result = apply(root, args.confirm)
    except rb.TokenError as e:
        print(f'Refused: {e}')
        return 2
    print(f'Done. Snapshot: {result["version"]}\nDatabase forgot: {result.get("forgot")}\n'
          f'Now run the regular download; compare later with ./compare_tree.sh "{root}"')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nCancelled. Progress is saved; run the same command again to resume.')
        sys.exit(130)

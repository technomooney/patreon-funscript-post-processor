"""Pixeldrain sync engine: classify what a pixeldrain source holds against what you already own, download the rest.

Used by the "Pixeldrain repo sync" creator plugin and by plugins that find pixeldrain links elsewhere (e.g. in a
Discord channel). See creator_scripts/pixeldrain_repo_sync.py for the user-facing description.

  have      the id is recorded as done / already in the files table / the same original name+size is recorded /
            the file is on disk with the same size / the AV1 compressor's log says it processed that original
  ambiguous same name with a different size or extension, or a compressor record whose size does not match:
            it might be YOUR edited or transcoded copy, so it is reported and never overwritten or re-fetched
  new       everything else -- only these are downloaded (to a scratch folder, sha256-verified, then moved in)
  duplicate a new file that an earlier source in the same run already lists (a creator's early-access share and
            public share overlap while a release is being moved between them): fetched once, from the first source

A creator can have several sources (any mix of /l/, /u/ and /d/). A file moving between them is no event: files are
recognised by id / content hash / original name+size, never by which source listed them.
"""
import os
import time

import action_log
import folder_log
import library_db
import pixeldrain_util as pdu
import progress

SCRIPT_ID = 'pixeldrain_repo_sync'      # also the undo-journal / folder_log name
SOURCES_KEY = 'pixeldrain_sources'      # in the creator profile: [{'url': ..., 'dest': 'subfolder'}]
_VIDEO_EXTS = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4v', '.wmv', '.ts', '.flv', '.mpg', '.mpeg'}

HAVE, AMBIGUOUS, NEW, DUPLICATE = 'have', 'ambiguous', 'new', 'duplicate'


def _stem(name: str) -> str:
    return os.path.splitext(name)[0].casefold()


def classify(root: str, dest_folder: str, remote: dict) -> tuple[str, str, dict]:
    """(verdict, reason, extra) for one remote file. Read-only. extra['local'] names the local file that proves
    ownership (for the adoption record) when there is one."""
    rid, name, size, sha = remote['id'], safe_name(remote['name']), remote.get('size'), remote.get('sha256')
    link = library_db.get_link(root, rid)
    if link and link['status'] == 'done':
        return HAVE, 'recorded as downloaded', {}
    rows = library_db.find_files(root, pixeldrain_id=rid)
    if rows:
        return HAVE, 'its id is recorded in the files table', {'local': rows[0]['cur_name']}
    if sha:
        rows = library_db.find_files(root, sha256=sha)
        if rows:
            return HAVE, 'same content (sha256) is recorded', {'local': rows[0]['cur_name']}
    rows = [r for r in library_db.find_files(root, orig_name=name) if size is not None and r['orig_size'] == size]
    if rows:
        return HAVE, 'same original name and size is recorded', {'local': rows[0]['cur_name']}

    on_disk = os.path.join(dest_folder, name)
    if os.path.isfile(on_disk):
        if size is None or os.path.getsize(on_disk) == size:
            return HAVE, 'already on disk with the same size', {'local': name}
        return AMBIGUOUS, 'same name on disk but a different size (edited or transcoded in place?)', {'local': name}

    for rec in folder_log.read(dest_folder):
        if rec.get('script') != 'compress_library' or rec.get('status') != 'compressed':
            continue
        if rec.get('file') == name or (rec.get('file') and _stem(rec['file']) == _stem(name)):
            if size is not None and rec.get('original_size') == size:
                return HAVE, 'the AV1 compressor already processed this original', \
                    {'local': rec.get('final_file') or rec.get('file')}
            return AMBIGUOUS, 'the AV1 compressor processed a file of this name but its size does not match', \
                {'local': rec.get('final_file') or rec.get('file')}

    if os.path.isdir(dest_folder):
        for other in os.listdir(dest_folder):
            if os.path.splitext(other)[1].lower() in _VIDEO_EXTS and _stem(other) == _stem(name) and other != name:
                return AMBIGUOUS, f'same name with a different extension on disk ({other})', {'local': other}
    return NEW, 'not found locally', {}


def safe_name(name: str) -> str:
    return pdu.safe_name(name)


def plan(root: str, base_path: str, sources: list[dict]) -> list[dict]:
    """Classify every file of every source. Items: {source, dest_rel, remote, verdict, reason, extra}.
    A source that cannot be read yields one item with verdict 'unreadable'."""
    items = []
    claimed: set = set()        # content keys of NEW files already queued by an earlier source
    for src in sources:
        dest_folder = os.path.join(base_path, src.get('dest', ''))
        remotes = pdu.list_entries(src['url'], on_progress=lambda d, f: progress.live(
            f'  listing {src["url"].rsplit("/", 1)[-1]}... {f} file(s) in {d} folder(s) so far'))
        progress.clear()
        if remotes is None:
            items.append({'source': src, 'verdict': 'unreadable', 'reason': 'could not list this source'})
            continue
        for n, remote in enumerate(remotes, 1):
            progress.live(f'  checking what you already have... {n}/{len(remotes)}')
            folder = os.path.join(dest_folder, *[safe_name(p) for p in remote.get('subdir', '').split(os.sep) if p])
            verdict, reason, extra = classify(root, folder, remote)
            if verdict == NEW:
                key = remote.get('sha256') or (safe_name(remote['name']).casefold(), remote.get('size'))
                if key in claimed:
                    verdict, reason = DUPLICATE, 'also listed by an earlier source; fetched once from that one'
                claimed.add(key)
            items.append({'source': src, 'dest_folder': folder, 'remote': remote,
                          'verdict': verdict, 'reason': reason, 'extra': extra})
        progress.clear()
    return items


def summarize(items: list[dict]) -> dict:
    out = {HAVE: 0, AMBIGUOUS: 0, NEW: 0, DUPLICATE: 0, 'unreadable': 0}
    for it in items:
        out[it['verdict']] += 1
    return out


def _adopt(root: str, it: dict, now: str) -> None:
    """Remember a file we already own so later runs (and other tools) can find it by id without re-checking."""
    remote, dest_folder = it['remote'], it['dest_folder']
    rel = library_db.rel_folder(root, dest_folder)
    local = it['extra'].get('local')
    path = os.path.join(dest_folder, local) if local else None
    rows = library_db.find_files(root, pixeldrain_id=remote['id'])
    if rows:
        fid = rows[0]['id']
    else:
        fid = library_db.add_file(
            root, SCRIPT_ID, folder=rel, orig_name=safe_name(remote['name']), cur_name=local or safe_name(remote['name']),
            orig_size=remote.get('size'), cur_size=os.path.getsize(path) if path and os.path.isfile(path) else None,
            sha256=remote.get('sha256'), pixeldrain_id=remote['id'],
            state='compressed' if local and _stem(local) == _stem(remote['name']) and local != safe_name(remote['name'])
            else 'downloaded')
    library_db.upsert_link(root, remote['id'], SCRIPT_ID, source_url=it['source']['url'], status='done',
                           remote_sha256=remote.get('sha256'), remote_name=remote['name'],
                           remote_size=remote.get('size'), last_attempt=now, file_id=fid)


def execute(root: str, base_path: str, items: list[dict], download=None) -> dict:
    """Adopt what we already have and download what is new. Resumable: each file's outcome is recorded as it
    finishes, so an interrupted run simply picks up the remaining new files next time."""
    download = download or pdu.download_file
    result = {'adopted': 0, 'downloaded': 0, 'failed': 0}
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    todo = [it for it in items if it['verdict'] == NEW]
    todo_bytes = sum(it['remote'].get('size') or 0 for it in todo)
    if todo:
        progress.say(f'  downloading {len(todo)} new file(s), {progress.size(todo_bytes)} in total')
    n_done = 0
    action_log.start(SCRIPT_ID, base_path, journal=SCRIPT_ID)
    try:
        for it in items:
            if it['verdict'] == HAVE:
                _adopt(root, it, now)
                result['adopted'] += 1
            elif it['verdict'] == NEW:
                remote, rel = it['remote'], library_db.rel_folder(root, it['dest_folder'])
                library_db.upsert_link(root, remote['id'], SCRIPT_ID, source_url=it['source']['url'], status='pending',
                                       remote_sha256=remote.get('sha256'), remote_name=remote['name'],
                                       remote_size=remote.get('size'), first_attempt=now, last_attempt=now)
                n_done += 1
                progress.say(f'  [{n_done}/{len(todo)}] {remote["name"]} ({progress.size(remote.get("size"))})')
                path, why = download(remote, it['dest_folder'], base_path)
                if path is None:
                    library_db.upsert_link(root, remote['id'], SCRIPT_ID, status='failed')
                    progress.say(f'  FAILED ({why}): {remote["name"]}')
                    result['failed'] += 1
                    continue
                action_log.record('create_marker', path=path)
                fid = library_db.add_file(
                    root, SCRIPT_ID, folder=rel, orig_name=os.path.basename(path), cur_name=os.path.basename(path),
                    orig_size=remote.get('size'), cur_size=os.path.getsize(path), sha256=remote.get('sha256'),
                    pixeldrain_id=remote['id'], state='downloaded')
                library_db.upsert_link(root, remote['id'], SCRIPT_ID, status='done', file_id=fid)
                result['downloaded'] += 1
    finally:
        action_log.finish()
    return result


def resolve_root(base_path: str, interactive: bool) -> str:
    """The library root for base_path, declaring one (and importing old logs) when it has none yet."""
    return folder_log.ensure_library(base_path)


def sync_urls(base_path: str, urls: list[str], dest: str = '', dry_run: bool = False, interactive: bool = False) -> dict | None:
    """Public entry for other plugins (e.g. the Discord one): sync these pixeldrain URLs into base_path/dest."""
    root = resolve_root(base_path, interactive)
    sources = [{'url': u, 'dest': dest} for u in urls if pdu.kind(u) in pdu.SYNCABLE_KINDS]
    items = plan(root, base_path, sources)
    counts = summarize(items)
    print_plan(items, counts)
    if dry_run or not counts[NEW] and not counts[HAVE]:
        return {**counts, 'adopted': 0, 'downloaded': 0, 'failed': 0}
    return {**counts, **execute(root, base_path, items)}


def print_plan(items: list[dict], counts: dict) -> None:
    for it in items:
        if it['verdict'] == 'unreadable':
            print(f'  ? {it["source"]["url"]}: {it["reason"]}')
        elif it['verdict'] != HAVE:
            mark = {NEW: '+', DUPLICATE: '='}.get(it['verdict'], '!')
            print(f'  {mark} {it["remote"]["name"]}: {it["reason"]}')
    print(f'  new: {counts[NEW]}   already have: {counts[HAVE]}   ambiguous (left alone): {counts[AMBIGUOUS]}'
          + (f'   duplicates across sources: {counts[DUPLICATE]}' if counts[DUPLICATE] else '')
          + (f'   unreadable sources: {counts["unreadable"]}' if counts['unreadable'] else ''))

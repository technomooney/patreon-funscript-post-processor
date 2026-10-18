"""Rebuild ("nuke") a library's pixeldrain-sourced files from their sources -- carefully.

For when a library has become so messy that redoing it is easier than repairing it. Standalone (not in the menu):
    ./rebuild.sh /path/to/library-root                              1. plan + HTML report + confirmation token
    ./rebuild.sh /path/to/library-root --confirm TOKEN [--decisions file.json]     2. apply (resumable)

NOTHING IS DELETED until its replacement has been downloaded and verified. Old copies go to .trash (undoable).

Step 1 (read-only for your files; it writes only its own report and a plan record in the library database) probes
every pixeldrain link the database knows and sorts each into:
  live         link works, local copy differs or cannot be proven identical  -> will be re-downloaded
  identical    link works and the local copy already has the same sha256      -> left as is (nothing to fetch)
  transcoded   link works but the local file is an AV1-compressed version     -> kept (a re-download would undo it)
  manual       the file is in a `.manual` folder or changed by hand           -> protected, kept
  dead         pixeldrain says the file is gone                               -> KEPT, listed for you to decide
  unverified   could not be checked (network/auth)                            -> kept, retry later
  pize_archive / missing_local                                                -> not handled here, listed
Files with no known pixeldrain source are only counted per folder. Other hosts (mega, ...) are not re-downloaded by
this version. The HTML report lets you paste a replacement pixeldrain link for a dead/manual item and exports your
choices as a small JSON file; it also shows the CONFIRMATION TOKEN.

Step 2 refuses unless the token is valid, unexpired, and the library still matches the plan (anything changed since
the report -> run step 1 again). For each live item it re-checks the link, downloads to a staging folder, verifies the
sha256, then moves the old copy to .trash and the fresh one into the same folder under the source's name. State is
saved after every step, so Ctrl+C or a crash resumes where it stopped. A final HTML report lists what was replaced,
kept, failed or still unresolved.
"""
import argparse
import hashlib
import html
import json
import os
import secrets
import shutil
import sys
import time

import action_log
import library_db
import library_drift
import pixeldrain_util as pdu

SCRIPT_ID = 'rebuild_library'
TOOL = 'downloader'
TOKEN_VALID_DAYS = 7
STAGING = 'rebuild_staging'

_MIGRATION = [
    '''CREATE TABLE IF NOT EXISTS downloader_rebuild_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL, created_at TEXT, expires_at TEXT,
        fingerprint TEXT, status TEXT, report_path TEXT, summary_json TEXT)''',
    '''CREATE TABLE IF NOT EXISTS downloader_rebuild_items (
        run_id INTEGER NOT NULL, link_id TEXT NOT NULL, verdict TEXT, state TEXT, reason TEXT, detail_json TEXT,
        updated_at TEXT, PRIMARY KEY (run_id, link_id))''',
]


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _ensure_tables(root: str) -> None:
    library_db.register_migration(root, TOOL, 1, _MIGRATION)


# ---------------------------------------------------------------------------
# 1. Plan
# ---------------------------------------------------------------------------

def _sha256(path: str) -> str:
    return pdu.sha256_file(path)


def _local_state(root: str) -> list[dict]:
    """Every 'done' pixeldrain link with its local file rows -- the local half of the plan (no network)."""
    out = []
    with library_db.db(root) as con:
        links = [dict(r) for r in con.execute("SELECT * FROM pixeldrain_links WHERE status='done' ORDER BY link_id")]
        for l in links:
            files = [dict(r) for r in con.execute('SELECT * FROM files WHERE pixeldrain_id=? ORDER BY id', (l['link_id'],))]
            out.append({'link': l, 'files': files})
    return out


def _fingerprint(root: str, local: list[dict]) -> str:
    """Hash of everything the plan depends on locally (not remote probe results, which can flip). If any of it changes
    after the report was made, the token no longer applies."""
    material = []
    for item in local:
        l = item['link']
        material.append([l['link_id'], l['status'], l['remote_sha256'],
                         [[f['id'], f['folder'], f['cur_name'], f['cur_size'], f['state']] for f in item['files']]])
    markers = sorted((m['path'], m['kind']) for m in library_drift_markers(root))
    blob = json.dumps([library_db.library_id(root), material, markers], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def library_drift_markers(root: str) -> list[dict]:
    with library_db.db(root) as con:
        return [dict(r) for r in con.execute('SELECT path, kind FROM markers WHERE cleared_at IS NULL')]


def make_token(fingerprint: str, expires: str) -> str:
    return secrets.token_hex(4) + '-' + hashlib.sha256((fingerprint + expires + secrets.token_hex(8)).encode()).hexdigest()[:12]


def build_plan(root: str, list_entries=None, file_info=None, hash_file=None) -> dict:
    """Classify every done pixeldrain link. The three callables are injectable for tests."""
    list_entries = list_entries or pdu.list_entries
    file_info = file_info or pdu.file_info
    hash_file = hash_file or _sha256
    _ensure_tables(root)
    library_drift.scan(root)                      # refresh .manual markers + 'changed' findings first
    changed = {f['path'] for f in library_drift.open_findings(root, 'changed')}
    local = _local_state(root)
    fingerprint = _fingerprint(root, local)

    lists: dict[str, dict | None] = {}
    items = []
    for it in local:
        l, files = it['link'], it['files']
        extra = json.loads(l.get('extra_json') or '{}')
        base = {'link_id': l['link_id'], 'source_url': l['source_url'], 'remote_name': l['remote_name'],
                'files': [{'id': f['id'], 'folder': f['folder'], 'name': f['cur_name'], 'size': f['cur_size'],
                           'state': f['state']} for f in files]}
        if extra.get('packages'):
            items.append(dict(base, verdict='pize_archive', reason='a Pize archive; the Pize plugin owns its manifest'))
            continue
        if not files:
            items.append(dict(base, verdict='missing_local', reason='recorded as downloaded but no local file is recorded'))
            continue
        manual = [f for f in files if library_drift.has_manual_marker(root, os.path.join(root, f['folder'], f['cur_name']))
                  or os.path.join(f['folder'], f['cur_name']) in changed]
        if manual:
            items.append(dict(base, verdict='manual', reason='in a .manual folder or changed by hand'))
            continue
        # probe: one list request covers many files; ids missing from the list get their own check
        info = None
        src = l['source_url'] or ''
        if pdu.kind(src) == 'list':
            if src not in lists:
                got = list_entries(src)
                lists[src] = {e['id']: e for e in got} if got is not None else None
            info = (lists[src] or {}).get(l['link_id'])
        if info is None:
            info = file_info(l['link_id'])
        if info == 'gone':
            items.append(dict(base, verdict='dead', reason='pixeldrain says this file no longer exists'))
        elif info is None:
            items.append(dict(base, verdict='unverified', reason='could not check the link (network/auth); try again'))
        elif any(f['state'] == 'compressed' or f['codec'] == 'av1' for f in files):
            items.append(dict(base, verdict='transcoded', remote=_remote(info),
                              reason='local file is an AV1-compressed version; re-downloading would undo it'))
        else:
            same = False
            f0 = files[0]
            path = os.path.join(root, f0['folder'], f0['cur_name'])
            if info.get('sha256') and f0['cur_size'] == info.get('size') and os.path.isfile(path):
                try:
                    same = hash_file(path) == info['sha256']
                except OSError:
                    same = False
            items.append(dict(base, verdict='identical' if same else 'live', remote=_remote(info),
                              reason='local copy already matches the source' if same else 'will be re-downloaded and verified'))

    no_source: dict[str, int] = {}
    with library_db.db(root) as con:
        for r in con.execute("SELECT folder, COUNT(*) n FROM files WHERE COALESCE(pixeldrain_id,'')='' GROUP BY folder"):
            no_source[r['folder']] = r['n']
    return {'root': root, 'generated_at': _now(), 'fingerprint': fingerprint, 'items': items, 'no_source': no_source}


def _remote(info: dict) -> dict:
    return {'id': info['id'], 'name': info['name'], 'size': info['size'], 'sha256': info['sha256'], 'url': info['url']}


def counts(plan: dict) -> dict:
    out: dict[str, int] = {}
    for it in plan['items']:
        out[it['verdict']] = out.get(it['verdict'], 0) + 1
    return out


def record_plan(root: str, plan: dict, report_path: str) -> str:
    """Store the plan as a run and return its confirmation token."""
    expires = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() + TOKEN_VALID_DAYS * 86400))
    token = make_token(plan['fingerprint'], expires)
    with library_db.db(root, write=True) as con:
        cur = con.execute(
            'INSERT INTO downloader_rebuild_runs(token, created_at, expires_at, fingerprint, status, report_path, '
            'summary_json) VALUES(?,?,?,?,?,?,?)',
            (token, plan['generated_at'], expires, plan['fingerprint'], 'planned', report_path, json.dumps(counts(plan))))
        run_id = cur.lastrowid
        for it in plan['items']:
            con.execute(
                'INSERT INTO downloader_rebuild_items(run_id, link_id, verdict, state, reason, detail_json, updated_at) '
                'VALUES(?,?,?,?,?,?,?)',
                (run_id, it['link_id'], it['verdict'], 'planned', it['reason'], json.dumps(it), plan['generated_at']))
    plan['token'], plan['expires_at'], plan['run_id'] = token, expires, run_id
    return token


# ---------------------------------------------------------------------------
# HTML reports (self-contained: inline CSS/JS, nothing external)
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#fff;--fg:#1c1e21;--muted:#65676b;--card:#f4f5f7;--line:#d9dbe0;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--acc:#0b5fff}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e6e8eb;--muted:#9aa0a6;--card:#1d2026;--line:#333842;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#6ea8ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:1.5rem;margin:.2em 0}h2{font-size:1.15rem;margin:1.6em 0 .4em}
.cards{display:flex;flex-wrap:wrap;gap:10px}.card{background:var(--card);border-radius:8px;padding:10px 14px;min-width:120px}
.card b{display:block;font-size:1.4rem}.muted{color:var(--muted)}
.token{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0}
code{background:var(--card);padding:2px 6px;border-radius:4px;word-break:break-all}
table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top;word-break:break-word}
input[type=text]{width:100%;box-sizing:border-box;padding:5px;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg)}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 14px;cursor:pointer;font-size:.95rem}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
@media (max-width:640px){main{padding:12px}th:nth-child(n+4),td:nth-child(n+4){display:none}}
"""


def _e(x) -> str:
    return html.escape('' if x is None else str(x))


def _files_cell(it: dict) -> str:
    return '<br>'.join(_e(os.path.join(f['folder'], f['name'] or '')) for f in it['files']) or '<span class="muted">(none)</span>'


def render_plan_html(plan: dict) -> str:
    c = counts(plan)
    need_decision = [it for it in plan['items'] if it['verdict'] in ('dead', 'manual')]
    cards = ''.join(f'<div class="card"><b>{n}</b>{_e(k)}</div>' for k, n in sorted(c.items()))
    rows = ''.join(
        f'<tr><td>{_e(it["verdict"])}</td><td>{_files_cell(it)}</td><td><code>{_e(it["link_id"])}</code><br>'
        f'<span class="muted">{_e(it["source_url"])}</span></td><td>{_e(it["reason"])}</td>'
        f'<td><input type="text" data-link="{_e(it["link_id"])}" placeholder="optional: paste a replacement pixeldrain link"></td></tr>'
        for it in need_decision) or '<tr><td colspan="5" class="muted">None - every source link still works.</td></tr>'

    def simple(verdict, title, blurb):
        its = [it for it in plan['items'] if it['verdict'] == verdict]
        if not its:
            return ''
        body = ''.join(f'<tr><td>{_files_cell(it)}</td><td><code>{_e(it["link_id"])}</code></td><td>{_e(it["reason"])}</td></tr>'
                       for it in its[:500])
        more = f'<p class="muted">... and {len(its) - 500} more.</p>' if len(its) > 500 else ''
        return f'<h2>{_e(title)} ({len(its)})</h2><p class="muted">{_e(blurb)}</p><table><tr><th>Local file(s)</th><th>Link</th><th>Why</th></tr>{body}</table>{more}'

    live = [it for it in plan['items'] if it['verdict'] == 'live']
    fetch_bytes = sum((it.get('remote') or {}).get('size') or 0 for it in live)
    ns = ''.join(f'<tr><td>{_e(k or "(library root)")}</td><td>{n}</td></tr>' for k, n in sorted(plan['no_source'].items())[:300])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Library rebuild plan</title><style>{_CSS}</style></head><body><main>
<h1>Library rebuild plan</h1>
<p class="muted">{_e(plan['root'])} &middot; generated {_e(plan['generated_at'])}. Nothing has been changed. Read this before applying.</p>
<div class="cards">{cards}</div>
<p><b>{len(live)}</b> file(s) would be re-downloaded (about <b>{fetch_bytes / 1e9:.2f} GB</b>); each old copy goes to <code>.trash</code> only after its replacement is downloaded and its sha256 verified. Dead, manual, transcoded and unverified items are <b>never touched</b>.</p>
<div class="token"><b>Confirmation token</b> (valid until {_e(plan['expires_at'])}; it only works while the library is unchanged):<br>
<code id="tok">{_e(plan['token'])}</code> <button type="button" onclick="navigator.clipboard&&navigator.clipboard.writeText(document.getElementById('tok').textContent)">Copy</button><br>
<span class="muted">Apply with:</span> <code>./rebuild.sh {_e(plan['root'])} --confirm {_e(plan['token'])} --decisions rebuild_decisions.json</code></div>
<h2>Needs your decision ({len(need_decision)})</h2>
<p class="muted">Dead or protected items keep their old data by default. If you found where a file lives now, paste a pixeldrain link; it will be downloaded, verified, and replace the old copy. Then download your choices:</p>
<table><tr><th>Verdict</th><th>Local file(s)</th><th>Link</th><th>Why</th><th>Replacement link</th></tr>{rows}</table>
<p><button type="button" onclick="exportDecisions()">Download decisions (JSON)</button></p>
{simple('unverified', 'Could not be checked', 'Network or auth trouble. Kept; run the plan again later.')}
{simple('transcoded', 'Compressed copies (kept)', 'Re-downloading would undo your AV1 savings.')}
{simple('identical', 'Already identical to the source (left as is)', 'The local copy has the same sha256 as the source.')}
{simple('pize_archive', 'Pize archives', 'Handled by the Pize plugin, not here.')}
{simple('missing_local', 'Recorded but missing locally', 'Not re-fetched; use the drift report to review.')}
<h2>Files with no known pixeldrain source</h2><p class="muted">Not touched. Counted per folder (top 300).</p>
<table><tr><th>Folder</th><th>Files</th></tr>{ns or '<tr><td colspan="2" class="muted">None.</td></tr>'}</table>
</main><script>
function exportDecisions(){{var items={{}};document.querySelectorAll('input[data-link]').forEach(function(i){{var v=i.value.trim();
if(v)items[i.getAttribute('data-link')]={{action:'replace',url:v}};}});
var blob=new Blob([JSON.stringify({{token:{json.dumps(plan['token'])},items:items}},null,2)],{{type:'application/json'}});
var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='rebuild_decisions.json';a.click();}}
</script></body></html>"""


def render_final_html(root: str, run: dict, rows: list[dict]) -> str:
    tally: dict[str, int] = {}
    for r in rows:
        tally[r['state']] = tally.get(r['state'], 0) + 1
    cards = ''.join(f'<div class="card"><b>{n}</b>{_e(k)}</div>' for k, n in sorted(tally.items()))
    cls = {'swapped': 'ok', 'failed': 'bad', 'kept_old': 'warn'}
    body = ''.join(
        f'<tr><td class="{cls.get(r["state"], "")}">{_e(r["state"])}</td><td>{_e(r["verdict"])}</td>'
        f'<td><code>{_e(r["link_id"])}</code></td><td>{_e(r["reason"])}</td></tr>'
        for r in rows if r['verdict'] in ('live', 'dead', 'manual', 'unverified') or r['state'] != 'planned')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Library rebuild result</title><style>{_CSS}</style></head><body><main>
<h1>Library rebuild result</h1><p class="muted">{_e(root)} &middot; run {_e(run['id'])} &middot; {_e(_now())}</p>
<div class="cards">{cards}</div>
<p>Old copies of replaced files are in <code>.trash</code> (kept {_e(os.getenv('TRASH_RETENTION_DAYS', action_log.DEFAULT_TRASH_RETENTION_DAYS))} days by default) and can be restored with the creator-scripts undo / main-menu undo.
Items marked <b>kept_old</b> or <b>failed</b> still have their original data; re-run apply with the same token to retry failures.</p>
<table><tr><th>Result</th><th>Verdict</th><th>Link</th><th>Detail</th></tr>{body}</table></main></body></html>"""


# ---------------------------------------------------------------------------
# 2. Apply
# ---------------------------------------------------------------------------

class TokenError(Exception):
    pass


def check_token(root: str, token: str) -> dict:
    """The run for *token* if it is valid, unexpired and the library still matches its plan; else TokenError."""
    _ensure_tables(root)
    with library_db.db(root) as con:
        row = con.execute('SELECT * FROM downloader_rebuild_runs WHERE token=?', (token,)).fetchone()
    if not row:
        raise TokenError('That token is not recognised. Generate a new plan (and report) first.')
    run = dict(row)
    if run['expires_at'] < _now():
        raise TokenError('That token has expired. Generate a new plan.')
    # The whole-library check gates only the START of a run. Once applying has begun the run's own swaps change the
    # library, so a resume cannot be compared with the plan; instead every remaining item is re-verified on its turn
    # (link re-probed, file checked for hand edits / .manual in _swap).
    if run['status'] == 'planned' and _fingerprint(root, _local_state(root)) != run['fingerprint']:
        raise TokenError('The library changed since the report was made (files or links were added, removed or '
                         'edited). Generate a new plan and read its report.')
    return run


def _set_item(root: str, run_id: int, link_id: str, state: str, reason: str = '', **detail) -> None:
    with library_db.db(root, write=True) as con:
        row = con.execute('SELECT detail_json FROM downloader_rebuild_items WHERE run_id=? AND link_id=?',
                          (run_id, link_id)).fetchone()
        d = json.loads(row['detail_json'] or '{}') if row else {}
        d.update(detail)
        con.execute('UPDATE downloader_rebuild_items SET state=?, reason=?, detail_json=?, updated_at=? '
                    'WHERE run_id=? AND link_id=?', (state, reason, json.dumps(d), _now(), run_id, link_id))


def _swap(root: str, item: dict, staged: str, remote: dict, explicit: bool = False) -> tuple[bool, str, dict]:
    """Move the old copy to .trash, the verified new one into place. Restores the old copy if the move-in fails.

    Re-checks the disk right now: a file the user edited or a folder they marked .manual since the plan was made is
    left alone -- unless *explicit* (the user chose a replacement link for exactly this item in the report)."""
    old = item['files'][0]
    old_path = os.path.join(root, old['folder'], old['name'])
    target = os.path.join(root, old['folder'], pdu.safe_name(remote['name']))
    if not os.path.isfile(old_path):
        return False, 'the old file is no longer where it was recorded', {}
    if not explicit:
        why = library_drift.needs_attention(root, old['folder'], old['name'])
        if why:
            return False, f'old data kept: {why} (since the plan was made)', {}
    trash_dest = action_log.soft_delete(root, old_path)
    try:
        if os.path.exists(target):
            raise OSError(f'{target} already exists')
        shutil.move(staged, target)
    except OSError as e:
        os.rename(trash_dest, old_path)                 # put the original back exactly where it was
        return False, f'could not move the new file in ({e}); original restored', {}
    action_log.record('soft_delete', orig_path=old_path, trash_path=trash_dest)
    action_log.record('create_marker', path=target)
    return True, 'replaced', {'old_path': old_path, 'trash_path': trash_dest, 'new_path': target}


def apply(root: str, token: str, decisions: dict | None = None, list_entries=None, file_info=None, download=None,
          free_bytes=None) -> dict:
    """Run (or resume) the apply stage for a valid *token*. Returns the tally of final states."""
    list_entries = list_entries or pdu.list_entries
    file_info = file_info or pdu.file_info
    download = download or pdu.download_file
    run = check_token(root, token)
    decisions = (decisions or {}).get('items', {}) if decisions and decisions.get('token') in (None, token) else {}
    with library_db.db(root) as con:
        rows = [dict(r) for r in con.execute('SELECT * FROM downloader_rebuild_items WHERE run_id=?', (run['id'],))]
    todo = []
    for r in rows:
        item = json.loads(r['detail_json'])
        item['_state'] = r['state']
        dec = decisions.get(r['link_id'])
        if dec and dec.get('action') == 'replace' and r['verdict'] in ('dead', 'manual'):
            item['_replace_url'] = dec['url']
            todo.append(item)
        elif r['verdict'] == 'live' and r['state'] not in ('swapped', 'kept_old'):
            todo.append(item)
    biggest = max([((it.get('remote') or {}).get('size') or 0) for it in todo] + [0])
    free = (free_bytes or (lambda p: shutil.disk_usage(p).free))(root)
    if biggest * 1.1 > free:
        raise TokenError(f'Not enough free space to stage the largest file ({biggest / 1e9:.2f} GB needed, '
                         f'{free / 1e9:.2f} GB free). Nothing was changed.')

    with library_db.db(root, write=True) as con:
        con.execute("UPDATE downloader_rebuild_runs SET status='applying' WHERE id=?", (run['id'],))
    staging_root = library_db.data_dir(root, STAGING)
    action_log.start(SCRIPT_ID, root, journal=SCRIPT_ID)
    try:
        for item in todo:
            _apply_item(root, run['id'], item, staging_root, list_entries, file_info, download)
    finally:
        action_log.finish()
        shutil.rmtree(staging_root, ignore_errors=True)
    with library_db.db(root, write=True) as con:
        con.execute("UPDATE downloader_rebuild_runs SET status='done' WHERE id=?", (run['id'],))
        final = [dict(r) for r in con.execute('SELECT * FROM downloader_rebuild_items WHERE run_id=?', (run['id'],))]
    out = {}
    for r in final:
        out[r['state']] = out.get(r['state'], 0) + 1
    report = os.path.join(library_db.data_dir(root, 'reports'), f'rebuild_result_{time.strftime("%Y%m%d_%H%M%S")}.html')
    with open(report, 'w', encoding='utf-8') as f:
        f.write(render_final_html(root, run, final))
    out['_report'] = report
    return out


def _apply_item(root, run_id, item, staging_root, list_entries, file_info, download) -> None:
    link_id = item['link_id']
    replace_url = item.get('_replace_url')
    # 1. verify the source still works -- right now, not just at plan time
    if replace_url:
        found = list_entries(replace_url) if pdu.kind(replace_url) in ('list', 'file') else None
        if not found or len(found) != 1:
            return _set_item(root, run_id, link_id, 'kept_old',
                             'the replacement link is not a single working pixeldrain file; old data kept')
        remote = found[0]
    else:
        remote = file_info(link_id)
        if remote == 'gone':
            return _set_item(root, run_id, link_id, 'kept_old', 'the link no longer works; old data kept')
        if remote is None:
            return _set_item(root, run_id, link_id, 'failed', 'could not re-check the link; old data kept (apply again to retry)')
    # 2. download + verify into staging (sha256 is checked inside download_file)
    _set_item(root, run_id, link_id, 'downloading', '')
    stage_dir = os.path.join(staging_root, str(link_id))
    shutil.rmtree(stage_dir, ignore_errors=True)
    path, why = download(remote, stage_dir, root)
    if path is None:
        return _set_item(root, run_id, link_id, 'failed', f'{why}; old data kept')
    _set_item(root, run_id, link_id, 'verified', '', staged=path)
    # 3. only now touch the old copy
    ok, msg, moved = _swap(root, item, path, remote, explicit=bool(replace_url))
    if not ok:
        return _set_item(root, run_id, link_id, 'failed', msg)
    f0 = item['files'][0]
    library_db.update_file(root, f0['id'], SCRIPT_ID, cur_name=os.path.basename(moved['new_path']),
                           cur_size=os.path.getsize(moved['new_path']), sha256=remote.get('sha256'),
                           codec=None, state='rebuilt')
    library_db.upsert_link(root, link_id, SCRIPT_ID, status='done', last_attempt=_now(), remote_sha256=remote.get('sha256'),
                           remote_name=remote.get('name'), remote_size=remote.get('size'))
    _set_item(root, run_id, link_id, 'swapped', msg, **moved)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Rebuild a library from its pixeldrain sources (plan first, then apply)')
    parser.add_argument('root', help='library root folder')
    parser.add_argument('--confirm', metavar='TOKEN', help='apply the plan that produced this token')
    parser.add_argument('--decisions', help='JSON file exported from the report (replacement links)')
    parser.add_argument('--yes', action='store_true', help='skip the typed confirmation (the token is still required)')
    args = parser.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isfile(library_db.db_path(root)):
        print(f'No library database in {root}. Run migrate.sh on it first.')
        return 2

    if not args.confirm:
        print('Planning (probing every pixeldrain link; no file is changed)...')
        plan = build_plan(root)
        report = os.path.join(library_db.data_dir(root, 'reports'), f'rebuild_plan_{time.strftime("%Y%m%d_%H%M%S")}.html')
        record_plan(root, plan, report)
        with open(report, 'w', encoding='utf-8') as f:
            f.write(render_plan_html(plan))
        print(f'  {counts(plan)}')
        print(f'\nReport written: {report}\nRead it, then apply with:\n  ./rebuild.sh "{root}" --confirm {plan["token"]}'
              f'{" --decisions rebuild_decisions.json" if any(i["verdict"] in ("dead", "manual") for i in plan["items"]) else ""}')
        return 0

    try:
        decisions = None
        if args.decisions:
            with open(args.decisions, 'r', encoding='utf-8') as f:
                decisions = json.load(f)
        run = check_token(root, args.confirm)
        print(f'Plan {run["id"]} from {run["created_at"]}: {run["summary_json"]}')
        if not args.yes and input('Type REBUILD to download, verify and replace the "live" files: ').strip() != 'REBUILD':
            print('Cancelled. Nothing changed.')
            return 1
        tally = apply(root, args.confirm, decisions)
    except TokenError as e:
        print(f'Refused: {e}')
        return 2
    report = tally.pop('_report')
    print(f'Done: {tally}\nResult report: {report}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nCancelled. Progress is saved; run the same command again to resume.')
        sys.exit(130)

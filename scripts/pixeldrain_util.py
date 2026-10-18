"""Small pixeldrain API helpers shared by the repo-sync plugin and the Discord plugin.

Wraps what downloadContent already does (auth headers, retry on 401, the actual transfer) and adds what it throws
away: the per-file name/size/sha256 that pixeldrain's list and info endpoints return. downloadContent is imported
lazily so importing this module is cheap and test-friendly.
"""
import hashlib
import json
import os
import re
import shutil
from urllib.parse import urlparse

SCRATCH_DIRNAME = '_pixeldrain_sync'     # '_' prefix: the pipeline's own walkers skip it


def _dc():
    import downloadContent as dc
    return dc


def file_id(url: str) -> str:
    """The id of a /u/<id> file URL (or the last path segment of anything else)."""
    return urlparse(url).path.rstrip('/').split('/')[-1]


def kind(url: str) -> str:
    """'list' (/l/), 'file' (/u/), 'filesystem' (/d/) or '' for anything that is not a pixeldrain URL."""
    p = urlparse(url)
    if p.netloc.removeprefix('www.') != 'pixeldrain.com':
        return ''
    return {'l': 'list', 'u': 'file', 'd': 'filesystem'}.get(p.path.strip('/').split('/')[0], '')


def _get_json(api_url: str, label: str):
    """GET *api_url* with the project's pixeldrain auth (and its 401 retry). Raises on failure."""
    dc = _dc()
    headers = dc._pixeldrain_initial_headers(label)
    if headers is None:
        raise RuntimeError('no pixeldrain access configured')

    def fetch(h):
        req = dc.urllib.request.Request(api_url, headers=h)
        with dc.urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    try:
        return fetch(headers)
    except dc.urllib.error.HTTPError as e:
        if e.code != 401 or 'Authorization' not in headers:
            raise
        retry = dc._pixeldrain_retry_headers()
        if retry is None:
            raise
        return fetch(retry)


def _entry(f: dict) -> dict:
    return {'id': f.get('id'), 'name': f.get('name') or f.get('id'), 'size': f.get('size'),
            'sha256': f.get('hash_sha256') or None}


def list_entries(url: str):
    """Files behind a pixeldrain /l/ list or /u/ file URL: [{'id','name','size','sha256','url'}].

    Returns None when it could not be determined (network, auth, deleted list, unsupported /d/ share) --
    never an empty list for a failure, so "nothing there" and "could not look" stay different things."""
    k = kind(url)
    try:
        if k == 'list':
            data = _get_json(f'https://pixeldrain.com/api/list/{file_id(url)}', f'list {file_id(url)}')
            files = [_entry(f) for f in data.get('files', []) if f.get('id')]
        elif k == 'file':
            files = [_entry(_get_json(f'https://pixeldrain.com/api/file/{file_id(url)}/info', f'info {file_id(url)}'))]
            files[0]['id'] = files[0]['id'] or file_id(url)
        else:
            return None
    except Exception as e:      # noqa: BLE001 -- every failure mode means "could not look"
        print(f'  [pixeldrain] could not read {url}: {e}')
        return None
    for f in files:
        f['url'] = f'https://pixeldrain.com/u/{f["id"]}'
    return files


def safe_name(name: str) -> str:
    """A filename that cannot escape its folder."""
    name = re.sub(r'[\x00-\x1f]', '_', os.path.basename((name or '').replace('\\', '/'))).strip().strip('.')
    return name or 'file'


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def download_file(entry: dict, dest_dir: str, scratch_root: str) -> tuple[str | None, str]:
    """Download one listed file into *dest_dir* under its real name, verified against pixeldrain's sha256.

    Returns (final_path, reason): final_path is None on failure with a reason. The transfer goes to a scratch
    folder first so a half-finished download never appears among the real files, and it is cleaned up either way.
    """
    dc = _dc()
    scratch = os.path.join(scratch_root, SCRATCH_DIRNAME, '.scratch', str(entry['id']))
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)
    try:
        before = set(os.listdir(scratch))
        dc._last_fetch_original_name = None
        try:
            ok = dc.download_pixeldrain(None, entry['url'], scratch)
        finally:
            dc._clear_status()
        new = [f for f in os.listdir(scratch) if f not in before and not f.endswith('.part')]
        if not ok or not new:
            return None, 'download failed'
        got = os.path.join(scratch, new[0])
        if entry.get('sha256'):
            actual = sha256_file(got)
            if actual != entry['sha256']:
                return None, f'sha256 mismatch (expected {entry["sha256"][:12]}..., got {actual[:12]}...)'
        elif entry.get('size') and os.path.getsize(got) != entry['size']:
            return None, f'size mismatch (expected {entry["size"]}, got {os.path.getsize(got)})'
        os.makedirs(dest_dir, exist_ok=True)
        final = os.path.join(dest_dir, safe_name(entry['name']))
        if os.path.exists(final):       # never overwrite anything that is already there
            stem, ext = os.path.splitext(final)
            final = f'{stem} ({entry["id"]}){ext}'
        shutil.move(got, final)
        return final, 'ok'
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def file_info(fid: str):
    """pixeldrain's info for one file id: {'id','name','size','sha256','url'} when it is there, 'gone' when
    pixeldrain says 404 (deleted / never existed), or None when it could not be determined (network, auth)."""
    dc = _dc()
    try:
        data = _get_json(f'https://pixeldrain.com/api/file/{fid}/info', f'info {fid}')
    except dc.urllib.error.HTTPError as e:
        return 'gone' if e.code == 404 else None
    except Exception:       # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    entry = _entry(data)
    entry['id'] = entry['id'] or fid
    entry['url'] = f'https://pixeldrain.com/u/{entry["id"]}'
    return entry

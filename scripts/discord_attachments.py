"""Discord channel method "attachments": download the files people upload straight to a channel.

A message's uploads are links on cdn.discordapp.com / media.discordapp.net. Those links are SIGNED and expire after
a while, so they are downloaded right after the channel is read; if one has already expired (an earlier run failed
on it) the message is read again to get a fresh link. Each attachment is identified by its Discord attachment id, so
a file already fetched (or compressed since) is never fetched again, and a name that is already on disk is reported
and left alone, never overwritten.

Files land flat in <creator>/<dir>; only the extensions wanted are taken (a post's screenshots are skipped).
"""
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import unquote, urlparse

import action_log
import discord_history as dh
import discord_pixeldrain_sync as dps
import discord_store as store
import library_db
import pixeldrain_util as pdu
import progress

SCRIPT_ID = 'discord_attachments'
DEFAULT_EXTS = ('.funscript', '.zip', '.rar', '.7z', '.mp4', '.m4v', '.mkv', '.webm', '.mov', '.avi', '.wmv')
_HOSTS = ('cdn.discordapp.com', 'media.discordapp.net')
_ATTACHMENT = re.compile(r'^/attachments/(\d+)/(\d+)/(.+)$')


def attachments(message: dict, exts=DEFAULT_EXTS) -> list[dict]:
    """[{'id','name','url'}] for the wanted uploads in one message, in order."""
    out, seen = [], set()
    for href in message.get('hrefs') or []:
        p = urlparse(href)
        m = _ATTACHMENT.match(p.path) if p.netloc in _HOSTS else None
        if not m or m.group(2) in seen:
            continue
        name = unquote(m.group(3))
        if os.path.splitext(name)[1].lower() in exts:
            seen.add(m.group(2))
            out.append({'id': m.group(2), 'name': name, 'url': href})
    return out


class Expired(Exception):
    """Discord refused the (signed) link."""


def download(url: str, dest: str) -> int:
    """Fetch *url* to the file *dest* (via a .part file). Returns the size. Raises Expired on 401/403/404."""
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    tmp = dest + '.part'
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, 'wb') as f:
            total = int(resp.headers.get('Content-Length') or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                progress.live(f'    {os.path.basename(dest)[:50]}  {progress.size(done)}'
                              + (f' / {progress.size(total)}' if total else ''))
    except urllib.error.HTTPError as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        if e.code in (401, 403, 404):
            raise Expired(str(e))
        raise
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    progress.clear()
    os.replace(tmp, dest)
    return os.path.getsize(dest)


def _record(root: str, target: str, a: dict, size: int) -> None:
    action_log.record('create_marker', path=target)
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    fid = library_db.add_file(root, SCRIPT_ID, folder=library_db.rel_folder(root, os.path.dirname(target)),
                              orig_name=os.path.basename(target), cur_name=os.path.basename(target),
                              orig_size=size, cur_size=size, state='downloaded')
    library_db.upsert_link(root, f'dc:{a["id"]}', SCRIPT_ID, source_url=a['url'].split('?')[0], status='done',
                           remote_name=a['name'], remote_size=size, last_attempt=now, first_attempt=now, file_id=fid)


def sync(base_path: str, dest: str, channel: str | None = None, interactive: bool = False, dry_run: bool = False,
         exts=DEFAULT_EXTS, fetch=None, fetch_download=None) -> dict | None:
    """One incremental pass: read the channel, download its wanted attachments into <creator>/<dest>.
    *fetch*/*fetch_download* are injectable for tests."""
    got = dps.read_pending(base_path, channel, interactive, dry_run, fetch)
    if got is None:
        return None
    root, creator_key, key, pending = got
    exts = tuple(e if e.startswith('.') else '.' + e for e in (exts or DEFAULT_EXTS))
    folder = os.path.join(base_path, dest)
    for m in pending:
        m.setdefault('message_id', m.get('id'))
    todo = [(m, a) for m in pending for a in attachments(m, exts)]
    result = {'messages': len(pending), 'attachments': len(todo), 'downloaded': 0, 'failed': 0, 'ambiguous': 0}
    print(f'  [{SCRIPT_ID}] {len(pending)} unprocessed message(s), {len(todo)} wanted attachment(s) found.')
    if dry_run:
        for _m, a in todo:
            print(f'  + {a["name"]}')
        return result
    fetch_download = fetch_download or download
    failed_msgs: set[str] = set()
    stale: dict[str, tuple[dict, dict]] = {}
    action_log.start(SCRIPT_ID, base_path, journal=SCRIPT_ID)
    try:
        for n, (m, a) in enumerate(todo, 1):
            link = library_db.get_link(root, f'dc:{a["id"]}')
            if link and link['status'] == 'done':
                continue
            target = os.path.join(folder, pdu.safe_name(a['name']))
            if os.path.exists(target):          # never overwrite; it might be the user's own edited copy
                result['ambiguous'] += 1
                progress.say(f'  ! {a["name"]}: a file of that name is already there -- left alone')
                continue
            os.makedirs(folder, exist_ok=True)
            progress.say(f'  [{n}/{len(todo)}] {a["name"]}')
            try:
                size = fetch_download(a['url'], target)
            except Expired:
                stale[a['id']] = (m, a)
                continue
            except Exception as e:      # noqa: BLE001
                progress.say(f'  FAILED ({e}): {a["name"]}')
                result['failed'] += 1
                failed_msgs.add(m['message_id'])
                continue
            _record(root, target, a, size)
            result['downloaded'] += 1
        if stale:            # their links expired: read those messages again for fresh ones, then retry once
            progress.say(f'  {len(stale)} attachment link(s) expired -- reading those messages again for fresh links...')
            oldest = min(int(m['message_id']) for m, _a in stale.values())
            rows = (fetch or dh.fetch_channel_history)(creator_key, since_message_id=str(oldest - 1),
                                                      **({'channel': channel} if channel else {}))
            fresh = {}
            if rows:
                store.update_hrefs(root, key, rows)
                for r in rows:
                    for a2 in attachments(r, exts):
                        fresh[a2['id']] = a2
            for aid, (m, a_old) in stale.items():
                a = fresh.get(aid)
                target = os.path.join(folder, pdu.safe_name(a_old['name']))
                try:
                    if a is None:
                        raise Expired('not found when read again')
                    size = fetch_download(a['url'], target)
                except Exception as e:      # noqa: BLE001
                    progress.say(f'  FAILED ({e}): {a_old["name"]}')
                    result['failed'] += 1
                    failed_msgs.add(m['message_id'])
                    continue
                _record(root, target, a, size)
                result['downloaded'] += 1
    finally:
        action_log.finish()
    for m in pending:
        if m['message_id'] not in failed_msgs:
            store.mark_processed(root, key, m['message_id'])
    return result

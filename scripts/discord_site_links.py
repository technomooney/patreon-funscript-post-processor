"""Discord channel method "site_links": links to the sites the main downloader already supports (mega.nz, yandex,
gofile, yt-dlp sites, ...) posted in a channel are handed to the normal download engine.

Each message that carries such links becomes one task, saved in its own subfolder of <creator>/<dir> named
"[<message id>] <first words of the message>". Pixeldrain links belong to the "pixeldrain_links" method and Discord's
own links are ignored. A mega password written in the same message is passed along. Base64-hidden links count too.
"""
import os
import re

import discord_parser
import discord_pixeldrain_sync as dps
import discord_store as store
import pixeldrain_util as pdu

SCRIPT_ID = 'discord_site_links'
_IGNORED_HOSTS = ('discord.com', 'discordapp.com', 'discordapp.net', 'discord.gg', 'base64decode.org', 'pixeldrain.com')


def wanted_links(message: dict, is_supported) -> list[str]:
    """The message's links the downloader can handle, minus pixeldrain/Discord/decoder-helper links."""
    def ok(u):
        host = (re.match(r'https?://([^/\s]+)', u) or [None, ''])[1].lower().removeprefix('www.')
        return bool(host) and not any(host == h or host.endswith('.' + h) for h in _IGNORED_HOSTS) \
            and not pdu.kind(u) and is_supported(u)
    return discord_parser.links([message], accept=ok)


def _folder_name(message: dict) -> str:
    text = (message.get('text') or '').strip()
    first = re.sub(r'https?://\S+', '', text.splitlines()[0] if text else '')
    first = re.sub(r'[\\/:*?"<>|]+', ' ', first).strip()[:60].strip()
    return f'[{message["message_id"]}] {first}'.strip()


def sync(base_path: str, dest: str, channel: str | None = None, interactive: bool = False, dry_run: bool = False,
         fetch=None, download=None, is_supported=None) -> dict | None:
    """One incremental pass: read the channel and hand its site links to the download engine."""
    import downloadContent as dc
    got = dps.read_pending(base_path, channel, interactive, dry_run, fetch)
    if got is None:
        return None
    root, creator_key, key, pending = got
    is_supported = is_supported or dc._is_supported_domain
    tasks = []
    for m in pending:
        m.setdefault('message_id', m.get('id'))
        links = wanted_links(m, is_supported)
        if not links:
            continue
        for link in links:
            if dc._is_mega_domain(link):
                pw = dc._extract_mega_password(m.get('text') or '')
                if pw:
                    dc._mega_link_passwords[link] = pw
        name = _folder_name(m)
        tasks.append({'folder': os.path.join(base_path, dest, name), 'basename': name.split('] ', 1)[-1] or str(m['message_id']),
                      'links': links})
    print(f'  [{SCRIPT_ID}] {len(pending)} unprocessed message(s), {len(tasks)} with downloadable link(s).')
    result = {'messages': len(pending), 'tasks': len(tasks)}
    if dry_run:
        for t in tasks:
            print(f'  + {os.path.basename(t["folder"])}: {", ".join(t["links"])}')
        return result
    if tasks:
        for t in tasks:
            os.makedirs(t['folder'], exist_ok=True)
        (download or dc.find_and_download)(base_path, tasks=tasks, failures=[], script_name='downloadFromDiscord',
                                           report_suffix='_discord', resume=False,
                                           auto_confirm=None if interactive else True)
    for m in pending:
        store.mark_processed(root, key, m['message_id'])
    return result

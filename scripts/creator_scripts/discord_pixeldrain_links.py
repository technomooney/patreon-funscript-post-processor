"""Discord channel -> pixeldrain links -> download only what is new.

For creators who share their content as pixeldrain links in a Discord channel, often hidden as base64 text.
Reads the channel's WHOLE history the first time (then only newer messages), decodes base64-hidden links with the
shared decoder, collects every pixeldrain /l/ list and /u/ file link, and hands them to the same engine as the
"Pixeldrain repo sync" plugin: only files you do not already have are downloaded, transcoded files are
recognised and never re-fetched.

Browser-only Discord access (the saved login profile; no bot, no token). A message counts as processed only when
all its links synced without a failure, so a failed download is retried next run. The Discord channel is set per
creator (prompted on first use, stored in the creator profile). Files go to a subfolder of the creator folder
(default 'discord'). "Dry run" prints what was found and downloads nothing.
"""
import os

import creator_profiles
import discord_history as dh
import folder_log
import pixeldrain_sync as ps
import pixeldrain_util as pdu

SCRIPT_ID = 'discord_pixeldrain_links'
MENU_LABEL = 'Discord channel: download new pixeldrain links (incl. base64)'
MENU_DESCRIPTION = (
    'Reads the creator\'s Discord channel (whole history the first time), decodes base64-hidden pixeldrain links,\n'
    'and downloads only what you do not already have. Browser login is manual, once; no token is used.'
)
REPLACES_DOWNLOAD = False
DEST_KEY = 'discord_dest'
DEFAULT_DEST = 'discord'


def _is_pixeldrain(url: str) -> bool:
    return pdu.kind(url) in ('list', 'file')


def links_from_messages(messages: list[dict]) -> list[str]:
    """Distinct pixeldrain list/file URLs across stored messages, oldest message first: base64-hidden ones
    (decoded when the message was saved) plus ordinary hyperlinks."""
    urls: list[str] = []
    for m in messages:
        for u in list(m.get('decoded', [])) + list(m.get('hrefs', [])):
            if _is_pixeldrain(u) and u not in urls:
                urls.append(u)
    return urls


def sync_channel(base_path: str, dest: str = DEFAULT_DEST, dry_run: bool = False, interactive: bool = False,
                 fetch=None) -> dict | None:
    """One incremental pass. *fetch* is injectable for tests (defaults to the browser reader)."""
    creator_key = os.path.basename(os.path.normpath(base_path))
    root = ps.resolve_root(base_path, interactive)
    if root is None:
        return None
    fetch = fetch or dh.fetch_channel_history
    since = dh.newest_stored_id(root, creator_key)
    print(f'  [{SCRIPT_ID}] reading {"messages newer than " + since if since else "the whole channel history"}...')
    rows = fetch(creator_key, since_message_id=since)
    if rows is None:
        print(f'  [{SCRIPT_ID}] could not read the Discord channel; nothing changed.')
        return None
    if not dry_run:
        dh.save_messages(root, creator_key, rows)
        pending = dh.unprocessed(root, creator_key)
    else:   # dry run stores nothing: look at the fresh rows only
        pending = [dict(r, decoded=[f['href'] for f in dh.decode_links(r)]) for r in reversed(rows)]
    urls = links_from_messages(pending)
    print(f'  [{SCRIPT_ID}] {len(pending)} unprocessed message(s), {len(urls)} pixeldrain link(s) found.')
    if not urls:
        if not dry_run:
            for m in pending:
                dh.mark_processed(root, creator_key, m['message_id'])
        return {'messages': len(pending), 'links': 0}
    result = ps.sync_urls(base_path, urls, dest=dest, dry_run=dry_run, interactive=interactive)
    if result is None or dry_run:
        return result
    if result['failed'] == 0 and result['unreadable'] == 0:
        for m in pending:
            dh.mark_processed(root, creator_key, m['message_id'])
    else:
        print(f'  [{SCRIPT_ID}] some links failed or could not be read -- those messages stay unprocessed '
              f'and are retried next run.')
    folder_log.append_run(base_path, SCRIPT_ID, messages=len(pending), links=len(urls), **{
        k: result[k] for k in ('downloaded', 'adopted', 'failed', 'ambiguous') if k in result})
    return result


def run(base_path: str, options: dict | None = None) -> None:
    interactive = options is None
    creator_key = os.path.basename(os.path.normpath(base_path))
    dest = (options or {}).get('dest') or creator_profiles.get(creator_key).get(DEST_KEY) or DEFAULT_DEST
    dry = (options or {}).get('dry_run', False)
    if interactive:
        answer = input(f'  Save into which subfolder of this creator folder? (Enter = "{dest}"): ').strip()
        if answer and answer != dest:
            dest = answer
            profiles = creator_profiles.load()
            profiles.setdefault(creator_key, {})[DEST_KEY] = dest
            creator_profiles.save(profiles)
        dry = input('  Dry run first (print what would be downloaded, change nothing)? (Y/n): ').strip().lower() != 'n'
    try:
        sync_channel(base_path, dest=dest, dry_run=dry, interactive=interactive)
    finally:
        import discord_passwords
        discord_passwords.close()


def setup_unattended(current: dict) -> dict:
    dest = input(f'  subfolder to save into (Enter keeps "{current.get("dest", DEFAULT_DEST)}"): ').strip()
    return {**current, 'dest': dest or current.get('dest', DEFAULT_DEST)}

"""Core: Discord channel -> pixeldrain links -> download only what is new.

Not a menu option. Creator pipelines call sync_channel() when a creator's content links live in a Discord channel
(BekScript's hybrid audit does; any other creator opts in with "discord_pixeldrain_links": "<subfolder>" in
creator_config.json and a pipeline that calls sync_if_enabled()).

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
CONFIG_KEY = 'discord_pixeldrain_links'
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


def sync_if_enabled(base_path: str, interactive: bool = False, dry_run: bool = False) -> dict | None:
    """Run sync_channel when this creator's creator_config.json opts in via "discord_pixeldrain_links" (the value is
    the subfolder to save into; true means the default). Does nothing, and says nothing, for every other creator."""
    import creator_config
    flag = creator_config.get(base_path, CONFIG_KEY)
    if not flag:
        return None
    dest = flag if isinstance(flag, str) else DEFAULT_DEST
    try:
        return sync_channel(base_path, dest=dest, dry_run=dry_run, interactive=interactive)
    finally:
        import discord_passwords
        discord_passwords.close()

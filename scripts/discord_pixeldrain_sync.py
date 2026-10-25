"""Core: Discord channel -> pixeldrain links -> download only what is new.

Not a menu option. Creator pipelines call sync_channel() when a creator's content links live in a Discord channel.
Which channels a creator has and how each is used is configured in creator_config.json ("discord_channels", see
discord_channels.py); pipelines call discord_channels.run_jobs / sync_if_enabled.

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

import discord_history as dh
import discord_parser
import discord_store as store
import folder_log
import pixeldrain_sync as ps
import pixeldrain_util as pdu

SCRIPT_ID = 'discord_pixeldrain_links'
CONFIG_KEY = 'discord_pixeldrain_links'
DEFAULT_DEST = 'discord'


def _is_pixeldrain(url: str) -> bool:
    return pdu.kind(url) in pdu.SYNCABLE_KINDS


def links_from_messages(messages: list[dict]) -> list[str]:
    """Distinct pixeldrain list/file URLs across messages, oldest message first."""
    return discord_parser.links(messages, accept=_is_pixeldrain)


def read_pending(base_path: str, channel: str | None, interactive: bool, dry_run: bool, fetch=None):
    """Read the channel's new messages and return (library root, creator key, store key, unprocessed messages), or
    None when there is no library or the channel could not be read. A dry run stores nothing and returns just the
    freshly read messages. Shared by every channel method (links, attachments, site links)."""
    creator_key = os.path.basename(os.path.normpath(base_path))
    root = ps.resolve_root(base_path, interactive)
    key = store.scope(creator_key, channel)
    extra = {'channel': channel} if channel else {}
    if dry_run:     # a dry run stores nothing: look at the fresh rows only
        rows = (fetch or dh.fetch_channel_history)(creator_key, since_message_id=store.newest_stored_id(root, key), **extra)
        if rows is None:
            print(f'  [{SCRIPT_ID}] could not read the Discord channel; nothing changed.')
            return None
        pending = discord_parser.annotate(list(reversed(rows)))
    else:
        if dh.refresh(root, creator_key, fetch=fetch, channel=channel) is None:
            print(f'  [{SCRIPT_ID}] could not read the Discord channel; nothing changed.')
            return None
        pending = store.unprocessed(root, key)
    return root, creator_key, key, pending


def sync_channel(base_path: str, dest: str = DEFAULT_DEST, dry_run: bool = False, interactive: bool = False,
                 fetch=None, channel: str | None = None, latest_only: bool = False) -> dict | None:
    """One incremental pass over one channel (*channel* names one of the creator's several; None = the original).
    *latest_only*: for a channel where each post replaces the last (a re-posted archive link), sync only the newest
    post's links, falling back to the next older post only if the newer one can't be read or fails.
    *fetch* is injectable for tests (defaults to the browser reader)."""
    folder_log.ensure_library(base_path)
    got = read_pending(base_path, channel, interactive, dry_run, fetch)
    if got is None:
        return None
    root, creator_key, key, pending = got
    urls = links_from_messages(pending)
    print(f'  [{SCRIPT_ID}] {len(pending)} unprocessed message(s), {len(urls)} pixeldrain link(s) found.')
    if not urls:
        if not dry_run:
            for m in pending:
                store.mark_processed(root, key, m['message_id'])
        return {'messages': len(pending), 'links': 0}
    if latest_only:
        groups = [g for g in (links_from_messages([m]) for m in reversed(pending)) if g]     # newest post first
        if not dry_run:
            print(f'  [{SCRIPT_ID}] latest-only channel: using the newest post\'s link(s) '
                  f'({len(groups)} post(s) had links).')
    else:
        groups = [urls]
    result = None
    for group in groups:
        result = ps.sync_urls(base_path, group, dest=dest, dry_run=dry_run, interactive=interactive)
        if result is None or dry_run or (result['failed'] == 0 and result['unreadable'] == 0):
            break
        if latest_only:
            print(f'  [{SCRIPT_ID}] that post\'s link could not be fully read -- trying the next older post.')
    if result is None or dry_run:
        return result
    if result['failed'] == 0 and result['unreadable'] == 0:
        for m in pending:
            store.mark_processed(root, key, m['message_id'])
    else:
        print(f'  [{SCRIPT_ID}] some links failed or could not be read -- those messages stay unprocessed '
              f'and are retried next run.')
    folder_log.append_run(base_path, SCRIPT_ID, messages=len(pending), links=len(urls), **{
        k: result[k] for k in ('downloaded', 'adopted', 'failed', 'ambiguous') if k in result})
    return result


def sync_if_enabled(base_path: str, interactive: bool = False, dry_run: bool = False) -> list[dict]:
    """Run the creator's configured pixeldrain-link channel jobs (creator_config "discord_channels", or the older
    single "discord_pixeldrain_links" flag). Does nothing, and says nothing, for a creator with none."""
    import discord_channels
    return discord_channels.run_jobs(base_path, interactive=interactive, dry_run=dry_run, methods=('pixeldrain_links',))

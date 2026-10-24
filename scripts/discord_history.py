"""Channel history: drive the browser through a creator's whole Discord channel and keep track of where we got to.

This module owns the *reading strategy*, not the browser (discord_browser.py has every browser call) and not the
meaning of the messages (discord_parser.py). It decides how far back to read (up to the newest message already
stored, so repeat runs only read what is new) and hands the result on: to discord_store.py to keep, or straight
back to the caller to process.

  collect_history()      pure scroll-and-collect loop (injected callables) -- unit tested without a browser
  fetch_channel_history  open the channel, run the loop, return message dicts newest first (None on failure)
  refresh()              read what is new, annotate it (discord_parser), store it; returns the new rows
  password_history()     every archive password (reads the whole chat once, then only newer messages)
"""
import time

import discord_store as store
import progress
from discord_store import snowflake


def collect_history(read_rows, scroll_up, since_id=None, max_messages=None, stall_limit=6, wait=lambda: None,
                    progress=None) -> list[dict]:
    """Scroll up and gather messages until the start of the channel, a known message, or *max_messages*.

    read_rows()  -> list of message dicts currently rendered (each has an 'id' snowflake string)
    scroll_up()  -> asks the page to load older messages
    since_id     -> a message id already processed: stop once it (or anything older) has been seen, and return only
                    messages NEWER than it (incremental runs)
    stall_limit  -> consecutive scrolls that surface nothing new before we decide we are at the top
    progress     -> optional callable(messages_seen_so_far), called after every scroll
    Returns message dicts newest first (Discord ids are time-ordered snowflakes).
    """
    seen: dict[int, dict] = {}
    since = snowflake(since_id)
    stalls = 0
    while True:
        new = 0
        for row in read_rows():
            sid = snowflake(row.get('id'))
            if sid is not None and sid not in seen:
                seen[sid] = row
                new += 1
        if since is not None and seen and min(seen) <= since:
            break
        if max_messages and len(seen) >= max_messages:
            break
        stalls = 0 if new else stalls + 1
        if stalls >= stall_limit:
            break
        if progress:
            progress(len(seen))
        scroll_up()
        wait()
    ordered = sorted(seen, reverse=True)
    if since is not None:
        ordered = [i for i in ordered if i > since]
    if max_messages:
        ordered = ordered[:max_messages]
    return [seen[i] for i in ordered]


def fetch_channel_history(creator_key: str, since_message_id=None, max_messages=None, force: bool = False,
                          settle_seconds: float = 1.2, channel: str | None = None):
    """Open *creator_key*'s configured channel and return its history, newest first, or None on any failure.

    Each message: {'id', 'ts', 'author', 'text', 'raw', 'hrefs'}. See collect_history for since/max semantics."""
    import discord_browser as db

    try:
        opened = db.open_channel(creator_key, force=force, channel=channel)
        if opened is None:
            return None
        driver, discord_cfg = opened
        print(f'  [discord] reading channel history for "{creator_key}" '
              f'(this scrolls back through the chat, so it can take a while)...')
        return _read_open_channel(db, driver, discord_cfg, since_message_id, max_messages, settle_seconds)
    finally:
        db.release()        # give the downloader its tab back (a no-op when Discord ran on its own)


def _read_open_channel(db, driver, discord_cfg, since_message_id, max_messages, settle_seconds):
    try:
        rows = collect_history(lambda: db.read_rows(driver), lambda: db.scroll_up(driver), since_id=since_message_id, max_messages=max_messages,
                               wait=lambda: time.sleep(settle_seconds),
                               progress=lambda n: progress.live(f'  [discord] reading the channel... {n} message(s) so far'))
    except db.WebDriverException:
        progress.clear()
        print('  [discord] browser window was closed while reading the channel.')
        return None
    progress.clear()
    print(f'  [discord] read {len(rows)} message(s) from channel {discord_cfg["channel_id"]}.')
    return rows


def refresh(root: str, creator_key: str, fetch=None, force: bool = False, channel: str | None = None) -> list[dict] | None:
    """Read the messages newer than the last stored one (the whole channel the first time), annotate and store
    them. Returns the freshly read rows (newest first), or None when the channel could not be read.
    *channel* names one of the creator's several channels (each keeps its own position). *fetch* is injectable
    for tests (defaults to fetch_channel_history)."""
    import discord_parser
    fetch = fetch or fetch_channel_history
    key = store.scope(creator_key, channel)
    since = store.newest_stored_id(root, key)
    print(f'  [discord] reading {"messages newer than " + since if since else "the whole channel history"}'
          f'{" of the " + channel + " channel" if channel else ""}...')
    rows = fetch(creator_key, since_message_id=since, **({'channel': channel} if channel else {}))
    if rows is None:
        return None
    store.save_messages(root, key, discord_parser.annotate(rows))
    return rows


def password_history(creator_key: str, max_messages: int | None = None, force: bool = False,
                     rescan: bool = False, channel: str | None = None) -> list[str]:
    """Every archive password the creator has posted, newest first. Found passwords are recorded in creator_db, so
    they are kept for good; this only decides how much of the chat still has to be read.

    The first call reads the WHOLE chat (no message cap -- an older archive may need an older password). It then
    remembers the newest message it covered (creator_db.discord_scan), and later calls read only newer messages.
    *rescan* ignores that position and reads everything again (use it when no known password worked).
    *max_messages* is only for manual peeks: a capped read never moves the remembered position.
    *force*: re-ask channel setup even if this creator previously declined it.
    *channel*: one of the creator's named channels (None = the original); each keeps its own read position.

    Returns every password on file for the creator afterwards (not just the ones in the newly read messages)."""
    import creator_db
    import discord_parser
    scan_key = store.scope(creator_key, channel)
    since = None if rescan or max_messages else creator_db.get_discord_scan(scan_key)
    rows = fetch_channel_history(creator_key, since_message_id=since, max_messages=max_messages, force=force,
                                 **({'channel': channel} if channel else {}))
    if rows is None:
        return _known_passwords(creator_key)       # could not read: fall back to what is already on file
    found = discord_parser.record_passwords(creator_key, rows)
    if rows and not max_messages:
        creator_db.set_discord_scan(scan_key, str(max(snowflake(r['id']) for r in rows if snowflake(r['id']))))
    print(f'  [discord] read {len(rows)} message(s)' + (f' newer than {since}' if since else '') +
          f'; {len(found)} password(s) in them.')
    return _known_passwords(creator_key) if since else found


def _known_passwords(creator_key: str) -> list[str]:
    import creator_db
    return creator_db.get_password_history(creator_key)


def latest_password(creator_key: str, max_messages: int | None = None, force: bool = False) -> str | None:
    history = password_history(creator_key, max_messages, force=force)
    return history[0] if history else None


def _main() -> None:
    """Dry run: read a creator's channel history and print what would be found. Downloads nothing, writes nothing
    (unless --save, which stores the messages in the library database given with --library)."""
    import argparse
    import discord_browser
    import discord_parser
    import library_db

    parser = argparse.ArgumentParser(description='Read a creator\'s full Discord history (dry run)')
    parser.add_argument('creator')
    parser.add_argument('--max', type=int, default=None, help='stop after this many (newest) messages')
    parser.add_argument('--library', default=None, help='library root; with --save, store messages there')
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()

    rows = fetch_channel_history(args.creator, max_messages=args.max, force=True)
    if rows is None:
        return
    rows = discord_parser.annotate(rows)
    discord_parser.print_summary(rows)
    if args.save and args.library:
        root = library_db.use_root(args.library)
        print(f'  saved {store.save_messages(root, args.creator, rows)} new message(s) to {library_db.db_path(root)}')
    discord_browser.close()


if __name__ == '__main__':
    try:
        _main()
    except KeyboardInterrupt:
        print('\nCancelled.')

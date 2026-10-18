"""Read a creator's WHOLE Discord channel history (not just the latest messages), browser-only.

Same deliberate constraint as discord_passwords.py: a persistent logged-in browser profile, no bot and no user
token. The login is manual, once.

Why: the recent-messages reader only sees what Discord has rendered (a few dozen messages), so an older archive's
password -- or a base64-encoded pixeldrain link posted months ago -- was unreachable. This scrolls the channel
upwards, collecting every message it passes, until it reaches the start of the channel, a message it has
already processed, or a requested cap.

Pieces:
  collect_history()      pure scroll-and-collect loop (injected callables) -- unit tested without a browser
  fetch_channel_history  the browser glue: open channel, scroll, return message dicts newest first
  save_messages / newest_stored_id / mark_processed   incremental bookkeeping in the library database
  fetch_password_history_full   scan the whole chat for archive passwords (records them in creator_db)
  decode_links           base64-hidden and plain links in a message, via the shared link_decoder
Standard library only apart from the browser helpers imported lazily from discord_passwords.
"""
import json
import time

import library_db
import link_decoder

_UPDATED_BY = 'discord_history'

# Read every message currently rendered in the channel. Discord's list is virtualized (messages far off-screen
# are dropped from the DOM), so this is called repeatedly while scrolling and the results merged by id.
_READ_JS = r"""
const root = document.querySelector('[data-list-id="chat-messages"]');
if (!root) return [];
const rows = [];
let lastAuthor = '';
root.querySelectorAll('li[id^="chat-messages-"]').forEach(li => {
  const m = li.id.match(/^chat-messages-(?:\d+-)?(\d+)$/);
  if (!m) return;
  const content = li.querySelector('[id^="message-content-"]');
  const author = li.querySelector('[id^="message-username-"]');
  if (author) lastAuthor = author.innerText;      // grouped messages inherit the previous header's author
  const t = li.querySelector('time[datetime]');
  rows.push({
    id: m[1],
    ts: t ? t.getAttribute('datetime') : '',
    author: lastAuthor,
    text: content ? content.innerText : '',
    raw: content ? content.textContent : '',       // includes spoilered text innerText may hide
    hrefs: Array.from(li.querySelectorAll('a[href]')).map(a => a.href),
  });
});
return rows;
"""

# Scroll the message list's scroller (the nearest scrollable ancestor) to the top, which makes Discord load older
# messages. Returns false if there is nothing scrollable.
_SCROLL_UP_JS = r"""
let el = document.querySelector('[data-list-id="chat-messages"]');
while (el && el.scrollHeight <= el.clientHeight + 1) el = el.parentElement;
if (!el) return false;
el.scrollTop = 0;
return true;
"""


def _snowflake(value) -> int | None:
    s = str(value or '')
    return int(s) if s.isdigit() else None


def collect_history(read_rows, scroll_up, since_id=None, max_messages=None, stall_limit=6, wait=lambda: None) -> list[dict]:
    """Scroll up and gather messages until the start of the channel, a known message, or *max_messages*.

    read_rows()  -> list of message dicts currently rendered (each has an 'id' snowflake string)
    scroll_up()  -> asks the page to load older messages
    since_id     -> a message id already processed: stop once it (or anything older) has been seen, and return only
                    messages NEWER than it (incremental runs)
    stall_limit  -> consecutive scrolls that surface nothing new before we decide we are at the top
    Returns message dicts newest first (Discord ids are time-ordered snowflakes).
    """
    seen: dict[int, dict] = {}
    since = _snowflake(since_id)
    stalls = 0
    while True:
        new = 0
        for row in read_rows():
            sid = _snowflake(row.get('id'))
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
        scroll_up()
        wait()
    ordered = sorted(seen, reverse=True)
    if since is not None:
        ordered = [i for i in ordered if i > since]
    if max_messages:
        ordered = ordered[:max_messages]
    return [seen[i] for i in ordered]


def fetch_channel_history(creator_key: str, since_message_id=None, max_messages=None, force: bool = False,
                          settle_seconds: float = 1.2):
    """Open *creator_key*'s configured channel and return its history, newest first, or None on any failure.

    Each message: {'id', 'ts', 'author', 'text', 'raw', 'hrefs'}. See collect_history for since/max semantics."""
    import discord_passwords as dp

    opened = dp.open_channel(creator_key, force=force)
    if opened is None:
        return None
    driver, discord_cfg = opened
    print(f'  [discord] reading channel history for "{creator_key}" '
          f'(this scrolls back through the chat, so it can take a while)...')

    def read_rows():
        return driver.execute_script(_READ_JS) or []

    def scroll_up():
        driver.execute_script(_SCROLL_UP_JS)

    try:
        rows = collect_history(read_rows, scroll_up, since_id=since_message_id, max_messages=max_messages,
                               wait=lambda: time.sleep(settle_seconds))
    except dp.WebDriverException:
        print('  [discord] browser window was closed while reading the channel.')
        return None
    print(f'  [discord] read {len(rows)} message(s) from channel {discord_cfg["channel_id"]}.')
    return rows


# ---------------------------------------------------------------------------
# Incremental bookkeeping (library database, table discord_messages)
# ---------------------------------------------------------------------------

def decode_links(message: dict, accept=None) -> list[dict]:
    """Links in one message: base64-hidden ones via link_decoder, tagged. Visible hrefs are returned by the
    caller's normal link handling; only HIDDEN links need decoding here."""
    text = ' '.join(filter(None, [message.get('text'), message.get('raw')]))
    return link_decoder.find_encoded_urls(text, accept=accept)


def save_messages(root: str, creator: str, rows: list[dict]) -> int:
    """Store messages (new ones only; existing rows and their processed flag are kept). Returns how many were new."""
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    added = 0
    with library_db.db(root, write=True) as con:
        for m in rows:
            cur = con.execute(
                'INSERT OR IGNORE INTO discord_messages(creator, message_id, ts, author, text, hrefs_json, '
                'processed, updated_by, updated_at) VALUES(?,?,?,?,?,?,0,?,?)',
                (creator, str(m['id']), m.get('ts', ''), m.get('author', ''), m.get('text', ''),
                 json.dumps(m.get('hrefs', []), ensure_ascii=False), _UPDATED_BY, now))
            added += cur.rowcount
    return added


def newest_stored_id(root: str, creator: str):
    """Highest message id already stored for *creator* (resume point for incremental scans), or None."""
    with library_db.db(root) as con:
        ids = [r['message_id'] for r in con.execute(
            'SELECT message_id FROM discord_messages WHERE creator=?', (creator,))]
    nums = [i for i in map(_snowflake, ids) if i is not None]
    return str(max(nums)) if nums else None


def mark_processed(root: str, creator: str, message_id, decoded_links: list[str] | None = None) -> None:
    with library_db.db(root, write=True) as con:
        con.execute(
            'UPDATE discord_messages SET processed=1, decoded_links_json=?, updated_by=?, updated_at=? '
            'WHERE creator=? AND message_id=?',
            (json.dumps(decoded_links or [], ensure_ascii=False), _UPDATED_BY,
             time.strftime('%Y-%m-%dT%H:%M:%S'), creator, str(message_id)))


def unprocessed(root: str, creator: str) -> list[dict]:
    """Stored messages not yet marked processed, oldest first."""
    with library_db.db(root) as con:
        rows = con.execute('SELECT * FROM discord_messages WHERE creator=? AND processed=0', (creator,)).fetchall()
    out = [dict(r, hrefs=json.loads(r['hrefs_json'] or '[]')) for r in rows]
    out.sort(key=lambda m: _snowflake(m['message_id']) or 0)
    return out


# ---------------------------------------------------------------------------
# Passwords from the whole chat
# ---------------------------------------------------------------------------

def fetch_password_history_full(creator_key: str, force: bool = False, max_messages=None) -> list[str]:
    """Every archive password anywhere in the creator's channel, newest first (recorded in creator_db).

    For when the latest passwords failed against an older archive: scans the entire chat, not just the last 50."""
    import creator_db
    import discord_passwords as dp

    rows = fetch_channel_history(creator_key, max_messages=max_messages, force=force)
    if rows is None:
        return []
    passwords, labeled = dp.passwords_from_texts([r.get('text', '') for r in rows])
    if passwords:
        print(f'  [discord] found {len(passwords)} distinct password(s) for "{creator_key}" in the full history.')
        creator_db.record_passwords(creator_key, passwords)
        if labeled:
            creator_db.record_password_labels(creator_key, labeled)
    else:
        print(f'  [discord] no password-looking message anywhere in the {len(rows)} messages read.')
    return passwords


def _main() -> None:
    """Dry run: read a creator's channel history and print what would be found. Downloads nothing, writes nothing
    (unless --save, which stores the messages in the library database given with --library)."""
    import argparse
    import discord_passwords as dp
    import downloadContent as dc

    parser = argparse.ArgumentParser(description='Read a creator\'s full Discord history (dry run)')
    parser.add_argument('creator')
    parser.add_argument('--max', type=int, default=None, help='stop after this many (newest) messages')
    parser.add_argument('--library', default=None, help='library root; with --save, store messages there')
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()

    rows = fetch_channel_history(args.creator, max_messages=args.max, force=True)
    if rows is None:
        return
    supported = dc._is_supported_domain
    hidden_total = 0
    for m in rows:
        hidden = decode_links(m, accept=supported)
        visible = [h for h in m.get('hrefs', []) if supported(h)]
        hidden_total += len(hidden)
        if hidden or visible:
            print(f'  {m["id"]} {m["ts"][:10]} {m["author"]}:')
            for h in hidden:
                print(f'      base64 -> {h["href"]}')
            for h in visible:
                print(f'      link   -> {h}')
    passwords, _ = dp.passwords_from_texts([m.get('text', '') for m in rows])
    print(f'  {len(rows)} message(s), {hidden_total} base64-hidden link(s), {len(passwords)} password-looking value(s).')
    if args.save and args.library:
        root = library_db.use_root(args.library)
        print(f'  saved {save_messages(root, args.creator, rows)} new message(s) to {library_db.db_path(root)}')
    dp.close()


if __name__ == '__main__':
    try:
        _main()
    except KeyboardInterrupt:
        print('\nCancelled.')

"""Archive/mega passwords posted in a creator's Discord: PARSING and recording only.

Getting the messages is a separate concern (discord_browser.py opens the channel, discord_history.py reads and
stores the whole history). This module turns message text into passwords and records them in creator_db, either
from messages fetched on demand (fetch_password_history*) or from messages already stored in the library
database (record_from_stored) -- the latter needs no browser at all.

Importing this module does not need selenium; the browser is imported lazily by the fetch_* functions only.

Standalone usage:
    python scripts/discord_passwords.py fetch <creator>
    python scripts/discord_passwords.py list
    (set a channel with: python scripts/discord_browser.py <creator> <guild_id> <channel_id>)
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import creator_db
import creator_profiles

# Discord messages (confirmed real example from Pize, 2026-08-02) read like
# prose, not the "PW: xxxx" convention _MEGA_PASSWORD_RE anchors on for post
# text: "The password is currently set as pize2026080101." — so this needs
# to swallow the connector words ("is"/"was", "currently", "set as"/"set to")
# between the keyword and the actual value, then still stop cleanly at
# trailing sentence punctuation (the value here butts right up against a
# period with no space). \b keeps it off substrings like "passed"/"bypass".
_PASSWORD_RE = re.compile(
    r'\b(?:pw|pass(?:word)?)\b'
    r'\s*(?:is|was)?'
    r'\s*(?:currently\s+)?'
    r'\s*(?:set\s+(?:as|to)\s*)?'
    r'[:=]?\s*'
    r'([^\s,;!.?]+)',
    re.IGNORECASE,
)


def _looks_like_password(value: str) -> bool:
    """Reject short/junk regex captures — a leftover connector word or stray
    punctuation the pattern above couldn't fully rule out structurally."""
    value = value.strip()
    return len(value) >= 4 and any(c.isalnum() for c in value)


# Some creators label each password with the post date range/month and post
# type ("Collection"/"standalone post") it applies to, instead of (or as well
# as) the plain "PW: xxxx" convention _PASSWORD_RE anchors on, e.g.:
#   "2026.08.16-2026.08.31 standalone post PW😀  2<[58I@@ihW2Ga"
# Several labels can also share one trailing value, full/half-width-comma
# separated, with only the last one carrying it:
#   "2026.02-07 standalone post PW，2026.02-07 Collection per month PW，
#    2026.09.01 standalone post PW😀  y7$X#mQ2!vK9*pB5@wT4"
# Illustrative examples given by the user, not yet confirmed against a real
# post (unlike _PASSWORD_RE's prose form) — kept deliberately best-effort:
# see creator_db.record_password_labels/get_password_history for how a
# mis-parsed or unmatched label just fails to help, never blocks anything.
# "PW[^\w\s,，]*" swallows an emoji glued onto "PW" without also eating past
# a following comma/digit into the next clause or the value itself.
_LABELED_PASSWORD_RE = re.compile(
    r'(?P<date>\d{4}(?:[.\-]\d{1,2}){0,2}\s*[-~]\s*(?:\d{4}[.\-])?\d{1,2}(?:[.\-]\d{1,2})?'
    r'|\d{4}(?:[.\-]\d{1,2}){0,2})'
    r'\s*(?P<kind>collection(?:\s+per\s+month)?|standalone(?:\s+post)?)?'
    r'[^\S\n]*PW[^\w\s,，]*'
    r'[^\S\n]*(?P<value>[^\s,，;]+)?',
    re.IGNORECASE,
)


def parse_labeled_passwords(text: str) -> list[dict]:
    """Parse zero or more {'date_label', 'kind', 'password'} entries out of
    one Discord message (see _LABELED_PASSWORD_RE above for the shapes
    handled). A label with no value of its own inherits the next label's
    value — the shared-trailing-password shape — so every label in a chain
    like that ends up mapped to the same password."""
    entries: list[dict] = []
    pending: list[tuple[str, str]] = []
    for m in _LABELED_PASSWORD_RE.finditer(text):
        kind = (m.group('kind') or '').lower()
        kind = 'collection' if kind.startswith('collection') else ('standalone' if kind else '')
        pending.append((m.group('date').strip(), kind))
        value = m.group('value')
        if value and _looks_like_password(value):
            pw = value.strip()
            for date_label, k in pending:
                entries.append({'date_label': date_label, 'kind': k, 'password': pw})
            pending = []
    return entries

def passwords_from_texts(texts: list[str]) -> tuple[list[str], list[dict]]:
    """Every distinct password-looking value in *texts* (given newest first -> result newest first), plus the
    date-labelled hints (see parse_labeled_passwords). Pure parsing, no browser."""
    passwords: list[str] = []
    labeled: list[dict] = []
    for text in texts:
        labeled.extend(parse_labeled_passwords(text))
        match = _PASSWORD_RE.search(text)
        if match and _looks_like_password(match.group(1)):
            pw = match.group(1).strip()
            if pw not in passwords:
                passwords.append(pw)
    for entry in labeled:
        if entry['password'] not in passwords:
            passwords.append(entry['password'])
    return passwords, labeled


def fetch_password_history(creator_key: str, max_messages: int = 50, force: bool = False) -> list[str]:
    """Return every distinct password-looking value posted in *creator_key*'s
    channel within the last *max_messages* messages, most recent first.

    Unlike fetch_latest_password, this doesn't stop at the first match — some
    creators (confirmed: Pize) rotate the archive password over time, and an
    older archive isn't always re-encrypted under the newest one, so a caller
    that fails to extract with the latest password may still need to fall
    back through history. Results are persisted to creator_db so later runs
    (or extraction retries) have them without re-scanning Discord.

    *force*: see discord_browser._load_channel_messages -- pass True when the caller already
    knows a password is genuinely needed (e.g. every known password just
    failed against a real archive), so a previously-declined creator still
    gets asked in that specific situation.
    """
    import discord_browser
    loaded = discord_browser._load_channel_messages(creator_key, max_messages, force=force)
    if loaded is None:
        return []
    texts, discord_cfg = loaded

    passwords, labeled = passwords_from_texts(texts)

    if passwords:
        print(f'  [discord] found {len(passwords)} distinct password(s) for "{creator_key}" '
              f'in channel {discord_cfg["channel_id"]}')
        creator_db.record_passwords(creator_key, passwords)
        if labeled:
            creator_db.record_password_labels(creator_key, labeled)
    else:
        print(f'  [discord] no password-looking message found in the last {max_messages} '
              f'messages of channel {discord_cfg["channel_id"]}')
    return passwords


def fetch_latest_password(creator_key: str, max_messages: int = 50, force: bool = False) -> str | None:
    """Return the most recent password posted in *creator_key*'s configured Discord channel, or None."""
    history = fetch_password_history(creator_key, max_messages, force=force)
    return history[0] if history else None


def record_passwords(creator_key: str, texts: list[str]) -> list[str]:
    """Parse *texts* (newest first) and persist what was found to creator_db. Returns the passwords."""
    passwords, labeled = passwords_from_texts(texts)
    if passwords:
        creator_db.record_passwords(creator_key, passwords)
        if labeled:
            creator_db.record_password_labels(creator_key, labeled)
    return passwords


def record_from_stored(root: str, creator_key: str) -> list[str]:
    """Passwords from the messages already stored in the library database (no browser, no fetching). Newest first.

    The store is filled by discord_history / the Discord link sync; run those first to refresh it."""
    import discord_history as dh
    texts = [m['text'] or '' for m in reversed(dh.stored_messages(root, creator_key))]
    passwords = record_passwords(creator_key, texts)
    print(f'  [discord] {len(passwords)} password(s) for "{creator_key}" from {len(texts)} stored message(s).')
    return passwords


def fetch_password_history_full(creator_key: str, force: bool = False, max_messages=None) -> list[str]:
    """Every archive password anywhere in the creator's channel, newest first (recorded in creator_db).

    For when the latest passwords failed against an older archive: reads the entire chat, not just the last 50."""
    import discord_history as dh
    rows = dh.fetch_channel_history(creator_key, max_messages=max_messages, force=force)
    if rows is None:
        return []
    passwords = record_passwords(creator_key, [r.get('text', '') for r in rows])
    if passwords:
        print(f'  [discord] found {len(passwords)} distinct password(s) for "{creator_key}" in the full history.')
    else:
        print(f'  [discord] no password-looking message anywhere in the {len(rows)} messages read.')
    return passwords


def close() -> None:
    """Quit the shared Discord browser, if one was launched (kept so existing callers still work)."""
    import sys as _sys
    if 'discord_browser' in _sys.modules:
        _sys.modules['discord_browser'].close()


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description='Discord password fetching — standalone test/setup CLI')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_fetch = sub.add_parser('fetch', help='Fetch the latest password for a creator')
    p_fetch.add_argument('creator')

    sub.add_parser('list', help='List configured creators')

    args = parser.parse_args()
    if args.cmd == 'fetch':
        try:
            password = fetch_latest_password(args.creator)
            print(f'Password: {password}' if password else 'No password found.')
        finally:
            close()
    elif args.cmd == 'list':
        profiles = creator_profiles.load()
        if not profiles:
            print('No creators configured yet.')
        for key, profile in profiles.items():
            print(f'{key}: {profile.get("discord") or "(no discord config)"}')


if __name__ == '__main__':
    try:
        _main()
    except KeyboardInterrupt:
        print('\n\nCancelled.')
        close()

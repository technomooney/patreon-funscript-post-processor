"""Understand Discord messages: pull the useful data out of message text. No browser, no storage.

Takes message dicts ({'text','raw','hrefs', ...}, as read by discord_history or loaded from discord_store) and
extracts:
  * links -- ordinary hyperlinks plus links creators hide as base64 text (via the shared link_decoder);
  * archive passwords -- by handing the text to discord_passwords (regexes live there) and recording the result
    in creator_db.

Everything here is a pure function of its input except the record_* / *_from_stored helpers, which write to
creator_db or read the store.
"""
import link_decoder

import discord_passwords as passwords_mod


def decode_links(message: dict, accept=None) -> list[dict]:
    """Base64-hidden links in one message: [{'href','encoded','layers'}]. Uses the full text including spoilers
    (visible hyperlinks are in message['hrefs'] and need no decoding)."""
    text = ' '.join(filter(None, [message.get('text'), message.get('raw')]))
    return link_decoder.find_encoded_urls(text, accept=accept)


def annotate(rows: list[dict]) -> list[dict]:
    """Rows with 'decoded' (the hidden links' hrefs) added, ready for discord_store. Because only the visible text
    is stored, hidden links must be decoded BEFORE storing."""
    return [dict(r, decoded=[f['href'] for f in decode_links(r)]) for r in rows]


def links(messages: list[dict], accept=None) -> list[str]:
    """Distinct links across messages, in message order: hidden (decoded) first, then visible hyperlinks. Works on
    annotated rows and stored rows alike (both carry 'decoded'); un-annotated rows are decoded on the fly."""
    out: list[str] = []
    for m in messages:
        decoded = m['decoded'] if 'decoded' in m else [f['href'] for f in decode_links(m)]
        for u in list(decoded) + list(m.get('hrefs') or []):
            if u not in out and (accept is None or accept(u)):
                out.append(u)
    return out


def passwords(messages: list[dict]) -> tuple[list[str], list[dict]]:
    """(distinct passwords newest first if *messages* are, date-labelled hints) from message text."""
    return passwords_mod.passwords_from_texts([m.get('text') or '' for m in messages])


def record_passwords(creator_key: str, messages: list[dict]) -> list[str]:
    """Parse the passwords in *messages* (newest first) and persist them to creator_db. Returns them."""
    found, labeled = passwords(messages)
    passwords_mod.record(creator_key, found, labeled)
    return found


def record_passwords_from_stored(root: str, creator_key: str) -> list[str]:
    """Passwords from everything already in the store (no browser, no fetching), newest first."""
    import discord_store
    stored = list(reversed(discord_store.stored_messages(root, creator_key)))
    found = record_passwords(creator_key, stored)
    print(f'  [discord] {len(found)} password(s) for "{creator_key}" from {len(stored)} stored message(s).')
    return found


def print_summary(rows: list[dict]) -> None:
    """What a read found, for the dry-run CLI."""
    import downloadContent as dc
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
    found, _ = passwords(rows)
    print(f'  {len(rows)} message(s), {hidden_total} base64-hidden link(s), {len(found)} password-looking value(s).')

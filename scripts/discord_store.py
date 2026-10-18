"""Storage for Discord messages: the library database's discord_messages table. Nothing else.

No browser, no parsing. Rows arrive already read (discord_history) and annotated (discord_parser.annotate puts
the base64-hidden links it decoded under 'decoded', because only the visible text is stored and spoilered text
would otherwise be lost). Also the single source of "how far have we read" (newest_stored_id) and of "what has
a consumer already handled" (the processed flag).
"""
import json
import time

import library_db

UPDATED_BY = 'discord_store'


def snowflake(value) -> int | None:
    """Discord ids are time-ordered numeric snowflakes; None for anything else."""
    s = str(value or '')
    return int(s) if s.isdigit() else None


def save_messages(root: str, creator: str, rows: list[dict]) -> int:
    """Store messages (new ones only; existing rows and their processed flag are kept). Returns how many were new."""
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    added = 0
    with library_db.db(root, write=True) as con:
        for m in rows:
            hidden = list(m.get('decoded') or [])    # filled in by discord_parser.annotate before storing
            cur = con.execute(
                'INSERT OR IGNORE INTO discord_messages(creator, message_id, ts, author, text, hrefs_json, '
                'processed, decoded_links_json, updated_by, updated_at) VALUES(?,?,?,?,?,?,0,?,?,?)',
                (creator, str(m['id']), m.get('ts', ''), m.get('author', ''), m.get('text', ''),
                 json.dumps(m.get('hrefs', []), ensure_ascii=False), json.dumps(hidden, ensure_ascii=False),
                 UPDATED_BY, now))
            added += cur.rowcount
    return added


def stored_messages(root: str, creator: str) -> list[dict]:
    """Every stored message for *creator*, oldest first, regardless of processed state."""
    with library_db.db(root) as con:
        rows = con.execute('SELECT * FROM discord_messages WHERE creator=?', (creator,)).fetchall()
    out = [dict(r) for r in rows]
    out.sort(key=lambda m: snowflake(m['message_id']) or 0)
    return out


def newest_stored_id(root: str, creator: str):
    """Highest message id already stored for *creator* (resume point for incremental scans), or None."""
    with library_db.db(root) as con:
        ids = [r['message_id'] for r in con.execute(
            'SELECT message_id FROM discord_messages WHERE creator=?', (creator,))]
    nums = [i for i in map(snowflake, ids) if i is not None]
    return str(max(nums)) if nums else None


def mark_processed(root: str, creator: str, message_id) -> None:
    with library_db.db(root, write=True) as con:
        con.execute(
            'UPDATE discord_messages SET processed=1, updated_by=?, updated_at=? WHERE creator=? AND message_id=?',
            (UPDATED_BY, time.strftime('%Y-%m-%dT%H:%M:%S'), creator, str(message_id)))


def unprocessed(root: str, creator: str) -> list[dict]:
    """Stored messages not yet marked processed, oldest first."""
    with library_db.db(root) as con:
        rows = con.execute('SELECT * FROM discord_messages WHERE creator=? AND processed=0', (creator,)).fetchall()
    out = [dict(r, hrefs=json.loads(r['hrefs_json'] or '[]'), decoded=json.loads(r['decoded_links_json'] or '[]'))
           for r in rows]
    out.sort(key=lambda m: snowflake(m['message_id']) or 0)
    return out



"""Shared helpers for comparing .funscript files by their actual point data
rather than raw bytes or filename/size.

Used by both sync_new_folders.py (the existing/new-folder symmetry check)
and downloadContent.py (_dedup_existing) so a funscript that got re-touched
or re-saved with reformatted JSON -- different whitespace, key order, or a
different 'metadata' block -- is still recognized as the same script,
instead of being treated as a distinct file just because its bytes changed.
"""
import json

FUNSCRIPT_EXT = '.funscript'


def funscript_data(path: str):
    """The actual point data of a .funscript -- actions, inverted, range --
    the fields that affect playback -- as a hashable fingerprint. Ignores
    'metadata' (creator/title/tags/...) and 'version' entirely, and is
    immune to pure JSON formatting differences (whitespace, key order).

    None if the file isn't parseable in the expected shape (corrupt, or
    not actually a funscript despite the extension) -- callers should fall
    back to a raw byte comparison rather than silently treating that as
    "no match anywhere".
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        actions = data['actions']
        points = tuple((a['at'], a['pos']) for a in actions)
        return (points, data.get('inverted', False), data.get('range', 90))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return None


def metadata_richness(path: str) -> int:
    """Count of non-empty fields in a .funscript's 'metadata' block --
    video_url, script_url, title, description, tags, performers, ... --
    used to prefer whichever content-identical copy (same funscript_data)
    carries more descriptive info when picking which duplicate to keep,
    rather than defaulting to "whichever is older". 0 for anything that
    isn't a parseable funscript with a metadata dict.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    metadata = data.get('metadata')
    if not isinstance(metadata, dict):
        return 0
    return sum(1 for v in metadata.values() if v)

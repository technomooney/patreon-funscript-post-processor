#!/usr/bin/env python3
"""
Fix garbled filenames — two kinds of corruption are handled:

1. Percent-encoded names  (URL encoding)
   e.g.  %E3%81%B5%E3%81%9F%E3%81%AA%E3%82%8A...  →  ふたなり...
   Caused by download tools that write the raw URL path as the filename instead
   of decoding it first.  Fixed with a straight urllib.parse.unquote() call.

   Truncated result fallback: if the decoded name still ends with '....' (the
   filename was cut off at a URL length limit), the parent folder name is used
   instead — Patreon folder names are the post title and are never truncated.

2. Mojibake  (Latin-1 / cp1252 mis-decode of UTF-8)
   e.g.  "Ã©tÃ©" → "été"   (European text, fully reversible)
   Encode the garbled stem back to Latin-1 / cp1252, then re-decode as UTF-8.
   Accept the result only if it succeeds AND is shorter (multi-byte sequences
   collapsing into single codepoints is the hallmark of genuine mojibake).

   NOTE: CJK mojibake where C1 bytes were stripped (e.g. "ãé¸£æ½®ã...") cannot
   be reversed automatically.  Re-run downloadContent.py instead — it detects
   the garbled file as a duplicate and renames it via rename-on-dedup.

Usage
-----
  python fix_garbled_names.py [directory] [--dry-run]

  directory   defaults to current working directory
  --dry-run   show what would be renamed without making any changes
"""

import csv
import os
import sys
from urllib.parse import unquote

# Suffixes that indicate a filename was truncated at a URL / filesystem limit.
_TRUNCATION_SUFFIXES = ('....', '...', '\u2026')


def _try_percent_decode(filename: str) -> str | None:
    """
    If *filename* contains percent-encoded sequences, decode and return the
    result.  Returns None if there's nothing to decode or decoding produces
    replacement characters (meaning the byte sequence was not valid UTF-8).
    """
    if '%' not in filename:
        return None
    decoded = unquote(filename, encoding='utf-8', errors='replace')
    if decoded == filename:
        return None
    if '\ufffd' in decoded:
        # Undecodable bytes — don't trust the result.
        return None
    return decoded


def _is_truncated(name: str) -> bool:
    return any(name.endswith(s) for s in _TRUNCATION_SUFFIXES)


def _try_encoding_reversal(name: str) -> str | None:
    """Return the de-mojibaked name if it looks fixable, else None."""
    for encoding in ('cp1252', 'latin-1'):
        try:
            fixed = name.encode(encoding).decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        # Accept only if the string changed AND is shorter (multi-byte sequences
        # collapsed into single codepoints).  Same length = plain ASCII = no change.
        if fixed != name and len(fixed) < len(name):
            return fixed
    return None


def _resolve_new_name(filename: str, folder_name: str) -> tuple[str, str] | None:
    """
    Try all fix strategies for *filename*.
    Returns (new_filename, strategy_label) or None if no fix found.
    *folder_name* is the name of the immediate parent directory, used as a
    fallback when the decoded name is truncated.
    """
    stem, ext = os.path.splitext(filename)

    # --- Strategy 1: percent-decode ---
    decoded = _try_percent_decode(filename)
    if decoded is not None:
        dec_stem, dec_ext = os.path.splitext(decoded)
        use_ext = dec_ext or ext
        if _is_truncated(dec_stem) and folder_name:
            # Decoded name was cut off — use the folder name (post title) instead.
            return folder_name + use_ext, 'percent-decode + folder-name fallback'
        return decoded, 'percent-decode'

    # --- Strategy 2: mojibake reversal ---
    fixed_stem = _try_encoding_reversal(stem)
    if fixed_stem is not None:
        return fixed_stem + ext, 'mojibake reversal'

    return None


def process(root_dir: str, dry_run: bool) -> tuple[int, list[tuple[str, str]]]:
    """Return (renamed_count, failed_list).

    failed_list entries are (path, reason) for renames that were attempted but failed.
    """
    renamed = 0
    failed: list[tuple[str, str]] = []
    for dirpath, _, filenames in os.walk(root_dir):
        if '.manual' in filenames:
            print(f'  SKIP (manual)  {dirpath}')
            continue
        folder_name = os.path.basename(dirpath)
        for filename in filenames:
            result = _resolve_new_name(filename, folder_name)
            if result is None:
                continue

            new_name, strategy = result
            old_path = os.path.join(dirpath, filename)
            new_path = os.path.join(dirpath, new_name)

            if os.path.exists(new_path):
                print(f'  SKIP (target exists)  {filename}')
                print(f'                     -> {new_name}')
                failed.append((old_path, f'target already exists: {new_name}'))
                continue

            if dry_run:
                print(f'  WOULD RENAME [{strategy}]')
                print(f'    {filename}')
                print(f'    -> {new_name}')
            else:
                print(f'  RENAME [{strategy}]')
                print(f'    {old_path}')
                print(f'    -> {new_path}')
                try:
                    os.rename(old_path, new_path)
                except OSError as e:
                    print(f'  ERROR: {e}')
                    failed.append((old_path, str(e)))
                    continue
            renamed += 1

    return renamed, failed


if __name__ == '__main__':
    args = sys.argv[1:]
    dry_run = '--dry-run' in args
    dirs = [a for a in args if not a.startswith('--')]
    root = os.path.abspath(dirs[0] if dirs else os.getcwd())

    print(f'Processing: {root}')
    if dry_run:
        print('(dry run — no changes will be made)')
    print()

    count, failed = process(root, dry_run)
    label = 'would be renamed' if dry_run else 'renamed'
    print(f'\nDone. {count} file(s) {label}.')
    if count == 0:
        print()
        print('No files matched.  For CJK mojibake (e.g. Iwara files), re-run')
        print('downloadContent.py — it now renames garbled duplicates automatically.')
    if failed:
        csv_path = os.path.join(root, 'garbled_names_failed.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['path', 'reason'])
            writer.writeheader()
            writer.writerows({'path': p, 'reason': r} for p, r in failed)
        print(f'\n{len(failed)} file(s) could not be fixed — see {csv_path}')
        for path, reason in failed:
            print(f'  {path}')
            print(f'    reason: {reason}')

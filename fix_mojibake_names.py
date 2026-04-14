#!/usr/bin/env python3
"""
Fix mojibake filenames caused by UTF-8 text being misread as Latin-1/cp1252.

Background
----------
Servers send filenames like  「鸣潮」椿sex… (UTF-8).
Some download tools decode the raw UTF-8 bytes as Latin-1 (one byte → one char),
turning each 3-byte CJK codepoint into three Latin-1 characters.  The C1 control
characters in that mangled text (U+0080-U+009F) then get stripped, leaving an
irrecoverable garbled name like  ãé¸£æ½®ãæ¤¿sex…

When can this script help?
--------------------------
  YES — Latin-1/European names that were double-encoded:
        e.g.  "Ã©tÃ©" → "été"   (no C1 bytes involved, fully reversible)

  NO  — CJK / multi-byte names where C1 bytes were stripped:
        e.g.  "ãé¸£æ½®ã..." cannot be reversed automatically.

        For those, the best fix is to re-run downloadContent.py — it now
        detects the garbled file as a duplicate of the proper download and
        renames it automatically (rename-on-dedup).

Strategy
--------
  Encode the garbled stem as Latin-1 bytes and re-decode as UTF-8.  Accept the
  result only if it succeeds AND produces a shorter string (multi-byte sequences
  collapsed into single codepoints — the hallmark of genuine mojibake).

Usage
-----
  python fix_mojibake_names.py [directory] [--dry-run]

  directory   defaults to current working directory
  --dry-run   show what would be renamed without making any changes
"""

import os
import sys


def _try_encoding_reversal(name: str) -> str | None:
    """Return the de-mojibaked stem+ext if it looks fixable, else None."""
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


def process(root_dir: str, dry_run: bool) -> int:
    renamed = 0
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            stem, ext = os.path.splitext(filename)
            fixed_stem = _try_encoding_reversal(stem)
            if fixed_stem is None:
                continue

            new_name = fixed_stem + ext
            old_path = os.path.join(dirpath, filename)
            new_path = os.path.join(dirpath, new_name)

            if os.path.exists(new_path):
                print(f'  SKIP (target exists)  {filename}')
                print(f'                     -> {new_name}')
                continue

            if dry_run:
                print(f'  WOULD RENAME  {filename}')
                print(f'             -> {new_name}')
            else:
                print(f'  RENAME  {old_path}')
                print(f'       -> {new_path}')
                try:
                    os.rename(old_path, new_path)
                except OSError as e:
                    print(f'  ERROR: {e}')
                    continue
            renamed += 1

    return renamed


if __name__ == '__main__':
    args = sys.argv[1:]
    dry_run = '--dry-run' in args
    dirs = [a for a in args if not a.startswith('--')]
    root = os.path.abspath(dirs[0] if dirs else os.getcwd())

    print(f'Processing: {root}')
    if dry_run:
        print('(dry run — no changes will be made)')
    print()

    count = process(root, dry_run)
    label = 'would be renamed' if dry_run else 'renamed'
    print(f'\nDone. {count} file(s) {label}.')
    if count == 0:
        print()
        print('No files matched.  For CJK mojibake (e.g. Iwara files), re-run')
        print('downloadContent.py — it now renames garbled duplicates automatically.')

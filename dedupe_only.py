#!/usr/bin/env python3
"""
Run temp-file cleanup and duplicate removal without doing a full download.

Usage
-----
  python dedupe_only.py [directory]

  directory   defaults to prompting interactively
"""

import os
import sys

from downloadContent import _cleanup_temp_files_recursive, _dedup_existing

if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if args:
        base_path = os.path.abspath(args[0])
    else:
        base_path = input('Enter full directory path to dedupe: ').strip()
        base_path = os.path.abspath(base_path)

    if not os.path.isdir(base_path):
        print(f'Directory not found: {base_path}')
        sys.exit(1)

    dedup_existing = os.getenv('DEDUP_EXISTING', 'true').strip().lower() not in ('false', '0', 'no')
    if dedup_existing:
        _dedup_existing(base_path)
    else:
        print('Cleaning temp files...')
        _cleanup_temp_files_recursive(base_path)
        print('[dedup] skipped (DEDUP_EXISTING=false)')

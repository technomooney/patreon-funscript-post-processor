#!/usr/bin/env python3
"""
Run temp-file cleanup, duplicate removal, and pack consolidation without
doing a full download. This is the only place _dedup_existing ever runs --
downloads no longer auto-dedupe at the start of a run, so this is a
deliberate step, not a background one.

Usage
-----
  python dedupe_only.py [directory]

  directory   defaults to prompting interactively
"""

import os
import sys

from downloadContent import _dedup_existing

if __name__ == '__main__':
    try:
        args = [a for a in sys.argv[1:] if not a.startswith('--')]
        if args:
            base_path = os.path.abspath(args[0])
        else:
            base_path = input('Enter full directory path to dedupe: ').strip()
            base_path = os.path.abspath(base_path)

        if not os.path.isdir(base_path):
            print(f'Directory not found: {base_path}')
            sys.exit(1)

        _dedup_existing(base_path)
    except KeyboardInterrupt:
        print('\n\nCancelled.')

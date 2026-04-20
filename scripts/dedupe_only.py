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

from downloadContent import _dedup_existing

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

    _dedup_existing(base_path)

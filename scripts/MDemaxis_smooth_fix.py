#!/usr/bin/env python3
"""
Rename SMOOTH-prefixed and _maxinterval-suffixed files to variant naming.

  SMOOTH example.funscript        →  example (SMOOTH).funscript
  example_maxinterval.funscript   →  example (max interval).funscript

Works on any file extension — default is funscript.

Usage: python MDemaxis_smooth_fix.py [directory]
  directory defaults to current working directory (can also be entered interactively)
"""

import os
import sys


def _resolve_new_name(filename: str) -> tuple[str, str] | None:
    """
    Return (new_filename, rule_label) if a rename rule matches, else None.
    Rules are checked in order; only the first match is applied.
    """
    stem, ext = os.path.splitext(filename)

    if filename.startswith("SMOOTH "):
        base = stem[len("SMOOTH "):]
        return f"{base} (SMOOTH){ext}", "SMOOTH prefix"

    if stem.endswith("_maxinterval"):
        base = stem[: -len("_maxinterval")]
        return f"{base} (max interval){ext}", "max interval suffix"

    return None


def process(root_dir: str, extensions: list[str]) -> int:
    """
    Walk *root_dir* and rename matching files.
    *extensions* is a list of lowercase dot-prefixed extensions, e.g. ['.funscript'].
    Returns the number of files renamed.
    """
    renamed = 0
    for dirpath, _, filenames in os.walk(root_dir):
        if '.manual' in filenames:
            print(f'  SKIP (manual)  {dirpath}')
            continue
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if extensions and ext not in extensions:
                continue

            result = _resolve_new_name(filename)
            if result is None:
                continue

            new_name, rule = result
            old_path = os.path.join(dirpath, filename)
            new_path = os.path.join(dirpath, new_name)

            if os.path.exists(new_path):
                print(f"  SKIP (target exists) [{rule}]  {filename}")
                continue

            print(f"  RENAME [{rule}]")
            print(f"    {old_path}")
            print(f"    -> {new_path}")
            try:
                os.rename(old_path, new_path)
                renamed += 1
            except OSError as e:
                print(f"  ERROR: {e}")

    return renamed


if __name__ == "__main__":
    # Directory: from command-line arg or prompt
    if len(sys.argv) > 1:
        root = os.path.abspath(sys.argv[1])
    else:
        entered = input("Enter full path to process (leave blank for current directory): ").strip()
        root = os.path.abspath(entered) if entered else os.getcwd()

    if not os.path.isdir(root):
        print(f"Directory not found: {root}")
        sys.exit(1)

    # Extensions: prompt with funscript as default
    raw = input("File extensions to process, separated by semicolons (default: funscript): ").strip()
    if raw:
        extensions = ['.' + e.lstrip('.').lower() for e in raw.split(';') if e.strip()]
    else:
        extensions = ['.funscript']

    print(f"\nProcessing: {root}")
    print(f"Extensions: {', '.join(extensions)}\n")

    count = process(root, extensions)
    print(f"\nDone. {count} file(s) renamed.")

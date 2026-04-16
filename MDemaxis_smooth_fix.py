#!/usr/bin/env python3
"""
Rename SMOOTH-prefixed funscript files and delete _maxinterval files.

Usage: python MDemaxis_smooth_fix.py [directory]
  directory defaults to current working directory
"""

import os
import sys


def process(root_dir):
    for dirpath, _, filenames in os.walk(root_dir):
        if '.manual' in filenames:
            print(f'  SKIP (manual)  {dirpath}')
            continue
        for filename in filenames:
            stem, ext = os.path.splitext(filename)
            filepath = os.path.join(dirpath, filename)

            # Rename _maxinterval mp4s to their base name
            if ext == ".mp4" and stem.endswith("_maxinterval"):
                new_name = stem[: -len("_maxinterval")] + ext
                new_path = os.path.join(dirpath, new_name)
                print(f"  RENAME  {filepath}")
                print(f"       -> {new_path}")
                os.rename(filepath, new_path)
                continue

            # Rename SMOOTH-prefixed mp4s
            if ext == ".mp4" and filename.startswith("SMOOTH "):
                new_name = filename[len("SMOOTH "):]
                new_path = os.path.join(dirpath, new_name)
                print(f"  RENAME  {filepath}")
                print(f"       -> {new_path}")
                os.rename(filepath, new_path)
                continue

            if ext != ".funscript":
                continue

            # Delete funscripts whose stem ends with _maxinterval
            if stem.endswith("_maxinterval"):
                print(f"  DELETE  {filepath}")
                os.remove(filepath)
                continue

            # Rename funscripts whose name starts with "SMOOTH "
            if filename.startswith("SMOOTH "):
                new_name = filename[len("SMOOTH "):]
                new_path = os.path.join(dirpath, new_name)
                print(f"  RENAME  {filepath}")
                print(f"       -> {new_path}")
                os.rename(filepath, new_path)


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    root = os.path.abspath(root)
    print(f"Processing: {root}\n")
    process(root)
    print("\nDone.")

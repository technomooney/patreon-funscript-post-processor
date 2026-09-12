#!/usr/bin/env python3
"""
Download videos for funscripts that carry the source video's URL in their
own metadata.video_url field but don't have a matching video on disk yet.

Every OpenFunscripter-style .funscript already has a "metadata" object
(creator, title, performers, tags, video_url, ...) — most creators leave it
empty, but some are starting to fill video_url in on request. This scans for
that instead of relying on description.json, so it also picks up a bare
.funscript with no accompanying post link at all.

A folder that already has a video is left alone regardless of what its
funscripts' metadata says — there's nothing to fetch.

Reuses downloadContent.py's full download engine (same domain handlers,
retry/AV-similarity/quality-replace logic, undo journal, progress-resume) —
just tagged with its own script name and report filenames
(*_funscript_metadata.csv, folder_log script 'downloadFromFunscriptMetadata')
so this run's output never overwrites, or gets confused with, a regular
"Download content" run's.

Usage
-----
  python download_from_funscript_metadata.py [directory]

  directory   defaults to prompting interactively
"""
import os
import sys

from downloadContent import find_and_download_from_funscript_metadata

if __name__ == '__main__':
    try:
        args = [a for a in sys.argv[1:] if not a.startswith('--')]
        if args:
            base_path = os.path.abspath(args[0])
        else:
            base_path = input('Enter full directory path to scan: ').strip()
            base_path = os.path.abspath(base_path)

        if not os.path.isdir(base_path):
            print(f'Directory not found: {base_path}')
            sys.exit(1)

        find_and_download_from_funscript_metadata(base_path)
    except KeyboardInterrupt:
        print('\n\nCancelled.')

#!/usr/bin/env python3
"""
Find videos that don't have a matching funscript.

For each video file found, checks whether a .funscript with the same stem
exists in the same folder. When no exact match is found, fuzzy-matches
against all funscripts in the folder and reports the closest candidate.

Also reports funscripts that have no corresponding video (orphaned scripts).

Usage
-----
  python check_funscripts.py [directory] [--csv]

  directory   defaults to current working directory
  --csv       write a full report to funscript_check.csv in the scanned directory
"""

import csv
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Extension sets
# ---------------------------------------------------------------------------

_VIDEO_EXTS  = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4v'}
_SCRIPT_EXT  = '.funscript'
_AXIS_SUFFIXES = ('.surge', '.pitch', '.roll', '.twist', '.sway')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_stem(funscript_path: str) -> str:
    """
    Return the 'video stem' for a funscript — strips any axis suffix so
    'example.surge.funscript' maps to the same base as 'example.funscript'.
    """
    stem = Path(funscript_path).stem          # e.g. 'example.surge'
    for suffix in _AXIS_SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _tokenize(s: str) -> set[str]:
    """Split on non-alphanumeric chars; keep tokens longer than 2 characters."""
    return {t for t in re.split(r'[^a-z0-9]+', s.lower()) if len(t) > 2}


def _fuzzy_score(video_stem: str, funscript_base: str) -> float:
    """Token-overlap score between video stem and funscript base stem."""
    vt = _tokenize(video_stem)
    ft = _tokenize(funscript_base)
    if not ft:
        return 0.0
    return len(vt & ft) / max(len(vt), len(ft))


# ---------------------------------------------------------------------------
# Per-folder analysis
# ---------------------------------------------------------------------------

class FolderResult:
    def __init__(self, folder: str):
        self.folder = folder
        self.unmatched_videos: list[dict] = []   # {'video', 'suggestion', 'score'}
        self.orphan_scripts: list[str] = []       # funscript stems with no video

    @property
    def ok(self) -> bool:
        return not self.unmatched_videos and not self.orphan_scripts


def _check_folder(folder: str) -> FolderResult | None:
    """
    Analyse one folder.  Returns None if the folder has no videos and no
    funscripts (nothing to report).
    """
    try:
        entries = os.listdir(folder)
    except OSError:
        return None

    videos = [f for f in entries if Path(f).suffix.lower() in _VIDEO_EXTS
              and os.path.isfile(os.path.join(folder, f))]
    scripts = [f for f in entries if f.lower().endswith(_SCRIPT_EXT)
               and os.path.isfile(os.path.join(folder, f))]

    if not videos and not scripts:
        return None

    result = FolderResult(folder)

    # Build lookup: base_stem → list of funscript filenames
    script_bases: dict[str, list[str]] = {}
    for s in scripts:
        base = _base_stem(s)
        script_bases.setdefault(base, []).append(s)

    video_stems = {Path(v).stem: v for v in videos}

    # --- Videos without a funscript ---
    for vstem, vfile in sorted(video_stems.items()):
        if vstem in script_bases:
            continue  # exact match found

        # Find best fuzzy match across all funscript base stems
        best_script, best_score = '', 0.0
        for fbase, ffiles in script_bases.items():
            score = _fuzzy_score(vstem, fbase)
            if score > best_score:
                best_score = score
                best_script = ffiles[0]

        result.unmatched_videos.append({
            'video':      vfile,
            'suggestion': best_script,
            'score':      round(best_score, 3),
        })

    # --- Funscripts without a video ---
    for fbase, ffiles in sorted(script_bases.items()):
        if fbase in video_stems:
            continue
        # Check partial: any video whose stem fuzzy-matches well enough?
        best_score = max(
            (_fuzzy_score(vstem, fbase) for vstem in video_stems),
            default=0.0,
        )
        if best_score < 0.5:
            # Only report as orphan when no close video match exists
            result.orphan_scripts.extend(ffiles)

    return result


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan(root_dir: str, write_csv: bool) -> list[FolderResult]:
    root_dir = os.path.abspath(root_dir)
    results = []

    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames.sort()
        if '.manual' in filenames:
            continue
        result = _check_folder(dirpath)
        if result and not result.ok:
            results.append(result)

    return results


def _print_results(results: list[FolderResult]):
    if not results:
        print('  All videos have matching funscripts.')
        return

    total_unmatched = sum(len(r.unmatched_videos) for r in results)
    total_orphans   = sum(len(r.orphan_scripts)   for r in results)

    for r in results:
        folder_label = os.path.basename(r.folder)
        print(f'\n  [{folder_label}]')

        for item in r.unmatched_videos:
            print(f'    ✗ {item["video"]}')
            if item['suggestion']:
                pct = int(item['score'] * 100)
                print(f'        closest funscript: {item["suggestion"]}  ({pct}% match)')
            else:
                print(f'        (no funscripts in folder)')

        for s in r.orphan_scripts:
            print(f'    ? orphan script: {s}')

    print(f'\n  {total_unmatched} unmatched video(s), {total_orphans} orphan script(s) '
          f'across {len(results)} folder(s).')


def _write_csv(root_dir: str, results: list[FolderResult]):
    csv_path = os.path.join(root_dir, 'funscript_check.csv')
    rows = []
    for r in results:
        for item in r.unmatched_videos:
            rows.append({
                'folder':     r.folder,
                'issue':      'no funscript',
                'file':       item['video'],
                'suggestion': item['suggestion'],
                'score':      item['score'],
            })
        for s in r.orphan_scripts:
            rows.append({
                'folder':     r.folder,
                'issue':      'orphan script',
                'file':       s,
                'suggestion': '',
                'score':      '',
            })
    if not rows:
        return
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['folder', 'issue', 'file', 'suggestion', 'score'])
        writer.writeheader()
        writer.writerows(rows)
    print(f'  Report written to: {csv_path}')


if __name__ == '__main__':
    root = input('Enter full directory path to scan: ').strip()
    root = os.path.abspath(root)

    if not os.path.isdir(root):
        print(f'Directory not found: {root}')
        sys.exit(1)

    write_csv_input = input('Write CSV report? (y/N): ').strip().lower()
    write_csv = write_csv_input == 'y'

    print(f'\nScanning: {root}\n')
    results = scan(root, write_csv)
    _print_results(results)
    if write_csv:
        _write_csv(root, results)
    print()

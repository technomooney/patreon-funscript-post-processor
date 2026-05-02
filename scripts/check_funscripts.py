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

import folder_log

# ---------------------------------------------------------------------------
# Extension sets
# ---------------------------------------------------------------------------

_VIDEO_EXTS  = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4v'}
_SCRIPT_EXT  = '.funscript'
_AXIS_SUFFIXES = ('.surge', '.pitch', '.roll', '.twist', '.sway')

# Matches a trailing parenthetical variant, e.g. ' (SMOOTH)' or ' (max interval)'
_VARIANT_RE = re.compile(r'\s*\([^)]+\)\s*$')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_variants(stem: str) -> str:
    """Strip trailing parenthetical variant suffixes repeatedly.

    e.g. 'example (SMOOTH)' → 'example'
         'example (max interval) (SMOOTH)' → 'example'
    """
    while True:
        stripped = _VARIANT_RE.sub('', stem)
        if stripped == stem:
            return stem
        stem = stripped


def _base_stem(funscript_path: str) -> str:
    """Return the base video stem for a funscript.

    Strips axis suffixes (.surge, .pitch, …) and parenthetical variant
    suffixes ((SMOOTH), (max interval), …) so all variants of a funscript
    map to the same base as the plain video stem.

    Examples:
      'example.surge.funscript'         → 'example'
      'example (SMOOTH).funscript'      → 'example'
      'example (max interval).funscript'→ 'example'
    """
    stem = Path(funscript_path).stem          # e.g. 'example.surge' or 'example (SMOOTH)'
    for suffix in _AXIS_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return _strip_variants(stem)


def _video_base(video_filename: str) -> str:
    """Return the base stem for a video, stripping variant suffixes.

    e.g. 'example (SMOOTH).mp4' → 'example'
         'example.mp4'          → 'example'
    """
    return _strip_variants(Path(video_filename).stem)


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
        self.total_videos: int = 0
        self.unmatched_videos: list[dict] = []   # {'video', 'suggestion', 'score'}

    @property
    def ok(self) -> bool:
        return not self.unmatched_videos


def _check_folder(folder: str) -> FolderResult | None:
    """
    Analyse one folder.  Returns None if the folder has no videos.
    """
    try:
        entries = os.listdir(folder)
    except OSError:
        return None

    videos = [f for f in entries if Path(f).suffix.lower() in _VIDEO_EXTS
              and os.path.isfile(os.path.join(folder, f))]
    scripts = [f for f in entries if f.lower().endswith(_SCRIPT_EXT)
               and os.path.isfile(os.path.join(folder, f))]

    if not videos:
        return None

    result = FolderResult(folder)
    result.total_videos = len(videos)

    # Build lookup: base_stem → list of funscript filenames
    # base_stem strips axis suffixes AND parenthetical variant suffixes.
    script_bases: dict[str, list[str]] = {}
    for s in scripts:
        base = _base_stem(s)
        script_bases.setdefault(base, []).append(s)

    # Build lookup: video_base → list of video filenames
    # video_base strips parenthetical variant suffixes so 'example (SMOOTH).mp4'
    # and 'example.mp4' both map to the same base 'example'.
    video_base_map: dict[str, list[str]] = {}
    for v in videos:
        base = _video_base(v)
        video_base_map.setdefault(base, []).append(v)

    # --- Videos without a funscript ---
    for vbase, vfiles in sorted(video_base_map.items()):
        if vbase in script_bases:
            continue  # base match found (covers all variants)

        # Find best fuzzy match across all funscript base stems
        best_script, best_score = '', 0.0
        for fbase, ffiles in script_bases.items():
            score = _fuzzy_score(vbase, fbase)
            if score > best_score:
                best_score = score
                best_script = ffiles[0]

        # Report one entry per unmatched video file under this base
        for vfile in vfiles:
            result.unmatched_videos.append({
                'video':      vfile,
                'suggestion': best_script,
                'score':      round(best_score, 3),
            })

    return result


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan(root_dir: str) -> list[FolderResult]:
    root_dir = os.path.abspath(root_dir)
    results = []

    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames.sort()
        result = _check_folder(dirpath)
        if result is None:
            continue
        folder_log.append_run(
            dirpath, 'check_funscripts',
            total_videos=result.total_videos,
            missing=[item['video'] for item in result.unmatched_videos],
        )
        if not result.ok:
            results.append(result)

    return results


def _print_results(results: list[FolderResult]):
    if not results:
        print('  All videos have matching funscripts.')
        return

    total_unmatched = sum(len(r.unmatched_videos) for r in results)

    for r in results:
        folder_label = os.path.basename(r.folder)
        print(f'\n  [{folder_label}]')

        for item in r.unmatched_videos:
            print(f'    ✗ {item["video"]}')
            if item['suggestion']:
                pct = int(item['score'] * 100)
                print(f'        closest funscript: {item["suggestion"]}  ({pct}% match)')
            else:
                print('        (no funscripts in folder)')

    print(f'\n  {total_unmatched} video(s) missing funscripts across {len(results)} folder(s).')


def _reports_dir(root: str) -> str:
    path = os.path.join(root, '_reports')
    os.makedirs(path, exist_ok=True)
    return path


def _write_csv(root_dir: str, results: list[FolderResult]):
    csv_path = os.path.join(_reports_dir(root_dir), 'funscript_check.csv')
    rows = []
    for r in results:
        for item in r.unmatched_videos:
            rows.append({
                'folder':     r.folder,
                'file':       item['video'],
                'suggestion': item['suggestion'],
                'score':      item['score'],
            })
    if not rows:
        return
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['folder', 'file', 'suggestion', 'score'])
        writer.writeheader()
        writer.writerows(rows)
    print(f'  Report written to: {csv_path}')


if __name__ == '__main__':
    root = input('Enter full directory path to scan: ').strip()
    root = os.path.abspath(root)

    if not os.path.isdir(root):
        print(f'Directory not found: {root}')
        sys.exit(1)

    print(f'\nScanning: {root}\n')
    results = scan(root)
    _print_results(results)
    _write_csv(root, results)
    print()

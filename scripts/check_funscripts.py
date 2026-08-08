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
import json
import os
import re
import sys
from pathlib import Path

import action_log
import folder_log
from downloadContent import _video_duration

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


def _funscript_duration(path: str) -> float | None:
    """Return a funscript's duration in seconds — its last action's timestamp —
    or None if the file can't be read or has no actions.

    Checks both the plain 'actions' list (v1/single-axis) and the 'channels'
    dict (multi-axis v2), taking the max across all of them since not every
    axis necessarily runs to the very end.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    max_at = 0
    actions = data.get('actions')
    if isinstance(actions, list):
        for a in actions:
            if isinstance(a, dict) and isinstance(a.get('at'), (int, float)):
                max_at = max(max_at, a['at'])

    channels = data.get('channels')
    if isinstance(channels, dict):
        for ch in channels.values():
            if isinstance(ch, dict):
                ch_actions = ch.get('actions')
                if isinstance(ch_actions, list):
                    for a in ch_actions:
                        if isinstance(a, dict) and isinstance(a.get('at'), (int, float)):
                            max_at = max(max_at, a['at'])

    return (max_at / 1000) if max_at else None


def _pick_representative(funscript_files: list[str]) -> str:
    """Prefer a plain (non-axis-suffixed) funscript for duration comparison —
    axis variants should run the same length, but the primary file is the
    safest bet if they ever drift."""
    for f in funscript_files:
        stem = Path(f).stem
        if not any(stem.lower().endswith(sfx) for sfx in _AXIS_SUFFIXES):
            return f
    return funscript_files[0]


def _duration_matches(video_s: float, script_s: float) -> bool:
    """True if a funscript's duration is consistent with covering *video_s*.

    A funscript legitimately ending somewhat before the video (un-scripted
    intro/outro/credits) is normal; a funscript ending well before or after
    the video's actual length is not. Tolerance: 5 % of the video's length
    or 10 s, whichever is larger, plus a small buffer for the funscript
    running slightly past the last frame due to encoding rounding.
    """
    tolerance = max(10.0, 0.05 * video_s)
    return script_s <= video_s + 2.0 and (video_s - script_s) <= tolerance


# ---------------------------------------------------------------------------
# Per-folder analysis
# ---------------------------------------------------------------------------

class FolderResult:
    def __init__(self, folder: str):
        self.folder = folder
        self.total_videos: int = 0
        self.unmatched_videos: list[dict] = []   # {'video', 'suggestion', 'score', ...}
        self.renamed: list[dict] = []            # {'video', 'renamed_to', 'video_s', 'script_s'}

    @property
    def ok(self) -> bool:
        return not self.unmatched_videos


def _check_folder(folder: str, do_rename: bool = False) -> FolderResult | None:
    """
    Analyse one folder.  Returns None if the folder has no videos.

    do_rename: when a folder has exactly one video and its name doesn't match
    any funscript, but exactly one funscript's duration unambiguously matches
    the video's, rename the video to that funscript's name.
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

        for vfile in vfiles:
            video_path = os.path.join(folder, vfile)

            # Duration is a second, independent signal on top of the name
            # fuzzy-match — confirms (or casts doubt on) the suggestion.
            duration_match = None
            video_s = script_s = None
            if best_script:
                video_s = _video_duration(video_path)
                script_s = _funscript_duration(os.path.join(folder, best_script))
                if video_s and script_s:
                    duration_match = _duration_matches(video_s, script_s)

            # Only-video-in-folder case: a badly garbled video name can score
            # ~0 % on fuzzy text matching but still be provably the same file
            # by duration, so search every funscript base (not just the
            # fuzzy-picked one) for an unambiguous duration match.
            renamed_to = None
            if len(videos) == 1:
                if video_s is None:
                    video_s = _video_duration(video_path)
                if video_s:
                    dur_candidates = []
                    for fbase, ffiles in script_bases.items():
                        rep = _pick_representative(ffiles)
                        fs_dur = _funscript_duration(os.path.join(folder, rep))
                        if fs_dur and _duration_matches(video_s, fs_dur):
                            dur_candidates.append((fbase, fs_dur))

                    if len(dur_candidates) == 1:
                        match_base, match_dur = dur_candidates[0]
                        duration_match = True
                        script_s = match_dur
                        if do_rename:
                            new_name = match_base + Path(vfile).suffix
                            new_path = os.path.join(folder, new_name)
                            if not os.path.exists(new_path):
                                os.rename(video_path, new_path)
                                action_log.record('rename', old_path=video_path, new_path=new_path)
                                renamed_to = new_name

            if renamed_to:
                result.renamed.append({
                    'video':      vfile,
                    'renamed_to': renamed_to,
                    'video_s':    video_s,
                    'script_s':   script_s,
                })
            else:
                result.unmatched_videos.append({
                    'video':          vfile,
                    'suggestion':     best_script,
                    'score':          round(best_score, 3),
                    'duration_match': duration_match,
                    'video_s':        video_s,
                    'script_s':       script_s,
                })

    return result


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan(root_dir: str, do_rename: bool = False) -> list[FolderResult]:
    root_dir = os.path.abspath(root_dir)
    results = []

    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames.sort()
        result = _check_folder(dirpath, do_rename=do_rename)
        if result is None:
            continue
        folder_log.append_run(
            dirpath, 'check_funscripts',
            total_videos=result.total_videos,
            missing=[item['video'] for item in result.unmatched_videos],
            renamed=[item['renamed_to'] for item in result.renamed],
        )
        if not result.ok or result.renamed:
            results.append(result)

    return results


def _duration_note(item: dict) -> str:
    if item.get('duration_match') is None:
        return ''
    v_s, s_s = item.get('video_s'), item.get('script_s')
    if item['duration_match']:
        return f'  [durations match: video {v_s:.0f}s vs funscript {s_s:.0f}s]'
    return f'  [duration MISMATCH: video {v_s:.0f}s vs funscript {s_s:.0f}s — probably not the right one]'


def _print_results(results: list[FolderResult]):
    if not results:
        print('  All videos have matching funscripts.')
        return

    total_unmatched = sum(len(r.unmatched_videos) for r in results)
    total_renamed = sum(len(r.renamed) for r in results)

    for r in results:
        folder_label = os.path.basename(r.folder)
        print(f'\n  [{folder_label}]')

        for item in r.renamed:
            print(f'    ✓ {item["video"]}  ->  {item["renamed_to"]}'
                  f'  [duration match: {item["video_s"]:.0f}s vs {item["script_s"]:.0f}s]')

        for item in r.unmatched_videos:
            print(f'    ✗ {item["video"]}')
            if item['suggestion']:
                pct = int(item['score'] * 100)
                print(f'        closest funscript: {item["suggestion"]}  ({pct}% match)'
                      f'{_duration_note(item)}')
            else:
                print('        (no funscripts in folder)')

    if total_renamed:
        print(f'\n  {total_renamed} video(s) renamed to match by duration.')
    unmatched_folders = sum(1 for r in results if r.unmatched_videos)
    if total_unmatched:
        print(f'\n  {total_unmatched} video(s) missing funscripts across {unmatched_folders} folder(s).')


def _reports_dir(root: str) -> str:
    path = os.path.join(root, '_reports')
    os.makedirs(path, exist_ok=True)
    return path


def _write_csv(root_dir: str, results: list[FolderResult]):
    csv_path = os.path.join(_reports_dir(root_dir), 'funscript_check.csv')
    fieldnames = ['folder', 'file', 'suggestion', 'score', 'duration_match', 'renamed_to']
    rows = []
    for r in results:
        for item in r.renamed:
            rows.append({
                'folder': r.folder, 'file': item['video'], 'suggestion': '',
                'score': '', 'duration_match': True, 'renamed_to': item['renamed_to'],
            })
        for item in r.unmatched_videos:
            rows.append({
                'folder':         r.folder,
                'file':           item['video'],
                'suggestion':     item['suggestion'],
                'score':          item['score'],
                'duration_match': item['duration_match'],
                'renamed_to':     '',
            })
    if not rows:
        return
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f'  Report written to: {csv_path}')


if __name__ == '__main__':
    root = input('Enter full directory path to scan: ').strip()
    root = os.path.abspath(root)

    if not os.path.isdir(root):
        print(f'Directory not found: {root}')
        sys.exit(1)

    rename_answer = input(
        '\nWhen a folder has exactly one video that doesn\'t name-match any\n'
        'funscript there, but exactly one funscript\'s duration unambiguously\n'
        'matches the video\'s, rename the video to match it? (Y/n): '
    ).strip().lower()
    do_rename = rename_answer != 'n'

    print(f'\nScanning: {root}\n')
    if do_rename:
        action_log.start('check_funscripts', root)
    results = scan(root, do_rename=do_rename)
    _print_results(results)
    _write_csv(root, results)
    if do_rename:
        action_log.finish()
    print()

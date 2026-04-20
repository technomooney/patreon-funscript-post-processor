#!/usr/bin/env python3
"""
Fix garbled filenames — two kinds of corruption are handled:

1. Percent-encoded names  (URL encoding)
   e.g.  %E3%81%B5%E3%81%9F%E3%81%AA%E3%82%8A...  →  ふたなり...
   Caused by download tools that write the raw URL path as the filename instead
   of decoding it first.  Fixed with a straight urllib.parse.unquote() call.

   Truncated result fallback: if the decoded name still ends with '....' (the
   filename was cut off at a URL length limit), the parent folder name is used
   instead — Patreon folder names are the post title and are never truncated.

2. Mojibake  (Latin-1 / cp1252 mis-decode of UTF-8)
   e.g.  "Ã©tÃ©" → "été"   (European text, fully reversible)
   Encode the garbled stem back to Latin-1 / cp1252, then re-decode as UTF-8.
   Accept the result only if it succeeds AND is shorter (multi-byte sequences
   collapsing into single codepoints is the hallmark of genuine mojibake).

   NOTE: CJK mojibake where C1 bytes were stripped (e.g. "ãé¸£æ½®ã...") cannot
   be reversed automatically.  Re-run downloadContent.py instead — it detects
   the garbled file as a duplicate and renames it via rename-on-dedup.

Usage
-----
  python fix_garbled_names.py [directory] [--dry-run]

  directory   defaults to current working directory
  --dry-run   show what would be renamed without making any changes
"""

import csv
import difflib
import json
import os
import re
import sys
from urllib.parse import unquote

# Suffixes that indicate a filename was truncated at a URL / filesystem limit.
_TRUNCATION_SUFFIXES = ('....', '...', '\u2026')

# cp1252 maps 0x80–0x9F to specific Unicode points; the rest of that byte range
# passes through as Latin-1.  This inverse table lets us encode those special
# Unicode code points back to their cp1252 byte values.
_CP1252_TO_BYTE: dict[int, int] = {
    0x20AC: 0x80, 0x201A: 0x82, 0x0192: 0x83, 0x201E: 0x84, 0x2026: 0x85,
    0x2020: 0x86, 0x2021: 0x87, 0x02C6: 0x88, 0x2030: 0x89, 0x0160: 0x8A,
    0x2039: 0x8B, 0x0152: 0x8C, 0x017D: 0x8E, 0x2018: 0x91, 0x2019: 0x92,
    0x201C: 0x93, 0x201D: 0x94, 0x2022: 0x95, 0x2013: 0x96, 0x2014: 0x97,
    0x02DC: 0x98, 0x2122: 0x99, 0x0161: 0x9A, 0x203A: 0x9B, 0x0153: 0x9C,
    0x017E: 0x9E, 0x0178: 0x9F,
}


def _try_percent_decode(filename: str) -> str | None:
    """
    If *filename* contains percent-encoded sequences, decode and return the
    result.  Returns None if there's nothing to decode or decoding produces
    replacement characters (meaning the byte sequence was not valid UTF-8).
    """
    if '%' not in filename:
        return None
    decoded = unquote(filename, encoding='utf-8', errors='replace')
    if decoded == filename:
        return None
    if '\ufffd' in decoded:
        # Undecodable bytes — don't trust the result.
        return None
    return decoded


def _is_truncated(name: str) -> bool:
    return any(name.endswith(s) for s in _TRUNCATION_SUFFIXES)


def _try_encoding_reversal(name: str) -> str | None:
    """Return the de-mojibaked name if it looks fixable, else None."""
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


def _is_real_ext(ext: str) -> bool:
    """Return True if *ext* looks like a genuine file extension (short, ASCII, no spaces)."""
    return bool(ext) and len(ext) <= 12 and ext.isascii() and ' ' not in ext


def _wide_encode(s: str) -> bytearray | None:
    """
    Encode *s* to bytes using cp1252 semantics but also accepting Latin-1 values
    for cp1252's undefined slots (0x81, 0x8D, 0x8F, 0x90, 0x9D).
    Returns None if any character can't be encoded this way.
    """
    buf = bytearray()
    for ch in s:
        cp = ord(ch)
        if cp in _CP1252_TO_BYTE:
            buf.append(_CP1252_TO_BYTE[cp])
        elif cp < 0x100:
            buf.append(cp)
        else:
            return None
    return buf


def _try_wide_reversal(stem: str) -> str | None:
    """
    Mojibake reversal that handles cp1252's undefined 0x80–0x9F slots by
    treating them as their raw Latin-1 byte values.  Needed when a CJK UTF-8
    byte sequence spans a defined cp1252 slot (e.g. 0x80=€) AND an undefined
    one (e.g. 0x90=U+0090), which makes standard cp1252 encoding fail.
    Example: 'ã€\\x904k' → bytes E3 80 90 34 → '【4k'.
    """
    buf = _wide_encode(stem)
    if buf is None:
        return None
    try:
        fixed = buf.decode('utf-8')
    except UnicodeDecodeError:
        return None
    if fixed != stem and len(fixed) < len(stem):
        return fixed
    return None


def _try_wide_reversal_lossy(s: str) -> str | None:
    """
    Like _try_wide_reversal but ignores incomplete UTF-8 sequences at the end.
    Used for filenames truncated at a filesystem byte limit, where the last CJK
    character's UTF-8 bytes are cut off.  Requires at least 3 chars saved to
    confirm genuine mojibake (not just a dropped byte or two).
    """
    buf = _wide_encode(s)
    if buf is None:
        return None
    fixed = buf.decode('utf-8', errors='ignore')
    if fixed != s and len(s) - len(fixed) >= 3:
        return fixed
    return None


def _resolve_new_name(filename: str, folder_name: str) -> tuple[str, str] | None:
    """
    Try all fix strategies for *filename*.
    Returns (new_filename, strategy_label) or None if no fix found.
    *folder_name* is the name of the immediate parent directory, used as a
    fallback when the decoded name is truncated.
    """
    stem, ext = os.path.splitext(filename)

    # --- Strategy 1: percent-decode ---
    decoded = _try_percent_decode(filename)
    if decoded is not None:
        dec_stem, dec_ext = os.path.splitext(decoded)
        use_ext = dec_ext or ext
        if _is_truncated(dec_stem) and folder_name:
            # Decoded name was cut off — use the folder name (post title) instead.
            return folder_name + use_ext, 'percent-decode + folder-name fallback'
        return decoded, 'percent-decode'

    # --- Strategy 2: mojibake reversal ---
    fixed_stem = _try_encoding_reversal(stem)
    if fixed_stem is not None:
        return fixed_stem + ext, 'mojibake reversal'

    # --- Strategy 3: wide cp1252 reversal (handles undefined 0x80–0x9F slots) ---
    fixed_stem = _try_wide_reversal(stem)
    if fixed_stem is not None:
        return fixed_stem + ext, 'wide cp1252 reversal'

    # Strategies 4–5 apply to the FULL filename.  Needed when splitext() treats
    # a garbled or non-ASCII title segment (after an embedded '.') as the ext,
    # leaving the stem clean so stem-only strategies find nothing to fix.
    if not _is_real_ext(ext):
        fixed_full = _try_wide_reversal(filename)
        if fixed_full is not None:
            return fixed_full, 'wide cp1252 reversal (full filename)'

        fixed_full = _try_wide_reversal_lossy(filename)
        if fixed_full is not None:
            return fixed_full, 'wide cp1252 reversal (full filename, lossy)'

    return None


def process(root_dir: str, dry_run: bool) -> tuple[int, list[tuple[str, str]]]:
    """Return (renamed_count, failed_list).

    failed_list entries are (path, reason) for renames that were attempted but failed.
    """
    renamed = 0
    failed: list[tuple[str, str]] = []
    for dirpath, _, filenames in os.walk(root_dir):
        if '.manual' in filenames:
            print(f'  SKIP (manual)  {dirpath}')
            continue
        folder_name = os.path.basename(dirpath)
        for filename in filenames:
            result = _resolve_new_name(filename, folder_name)
            if result is None:
                continue

            new_name, strategy = result
            old_path = os.path.join(dirpath, filename)
            new_path = os.path.join(dirpath, new_name)

            if os.path.exists(new_path):
                print(f'  SKIP (target exists)  {filename}')
                print(f'                     -> {new_name}')
                failed.append((old_path, f'target already exists: {new_name}'))
                continue

            if dry_run:
                print(f'  WOULD RENAME [{strategy}]')
                print(f'    {filename}')
                print(f'    -> {new_name}')
            else:
                print(f'  RENAME [{strategy}]')
                print(f'    {old_path}')
                print(f'    -> {new_path}')
                try:
                    os.rename(old_path, new_path)
                except OSError as e:
                    print(f'  ERROR: {e}')
                    failed.append((old_path, str(e)))
                    continue
            renamed += 1

    return renamed, failed


def _reports_dir(root: str) -> str:
    path = os.path.join(root, '_reports')
    os.makedirs(path, exist_ok=True)
    return path


def _is_funscript_content(path: str) -> bool:
    """Return True if *path* contains a valid funscript JSON object."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    # v1 basic / v2 single-axis: top-level "actions" list with pos+at entries
    actions = data.get('actions')
    if isinstance(actions, list) and actions:
        first = actions[0]
        if isinstance(first, dict) and 'pos' in first and 'at' in first:
            return True
    # Multi-axis v2: "channels" dict where each channel has its own "actions" list
    channels = data.get('channels')
    if isinstance(channels, dict):
        for channel in channels.values():
            if isinstance(channel, dict):
                ch_actions = channel.get('actions')
                if isinstance(ch_actions, list) and ch_actions:
                    first = ch_actions[0]
                    if isinstance(first, dict) and 'pos' in first and 'at' in first:
                        return True
    return False


_VIDEO_EXTS = frozenset({'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.ts'})

_RES_PAT = re.compile(r'[_\s]*(2160p?|1080p?|720p?|480p?|4k|8k)[_\s]*$', re.IGNORECASE)

_FS_SUFFIX_LABEL = re.compile(
    r'[_\s]*[\[(]?\s*(SMOOTH|max[\s_]?interval|maxinterval)\s*[\])]?\s*$',
    re.IGNORECASE,
)
_FS_PREFIX_LABEL = re.compile(
    r'^(SMOOTH|MAX[\s_]?INTERVAL)\s*[-_]?\s*',
    re.IGNORECASE,
)

_LABEL_MAP = {
    'smooth': ' (SMOOTH)',
    'max interval': ' (max interval)',
    'maxinterval': ' (max interval)',
    'max_interval': ' (max interval)',
}


def _split_fs_label(stem: str) -> tuple[str, str]:
    """Return (base_stem, canonical_label_suffix) stripping funscript variant labels."""
    m = _FS_SUFFIX_LABEL.search(stem)
    if m:
        raw = m.group(1).lower().replace('_', ' ').strip()
        return stem[:m.start()].rstrip(), _LABEL_MAP.get(raw, f' ({m.group(1)})')
    m = _FS_PREFIX_LABEL.match(stem)
    if m:
        raw = m.group(1).lower().replace('_', ' ').strip()
        return stem[m.end():].lstrip(), _LABEL_MAP.get(raw, f' ({m.group(1)})')
    return stem, ''


def _normalize_for_match(stem: str) -> str:
    """Lowercase, strip resolution, remove bracket wrappers, unify separators."""
    s = _RES_PAT.sub('', stem)
    s = re.sub(r'[\[\(]([^\]\)]{1,40})[\]\)]', r'\1', s)
    s = re.sub(r'[-_]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip().lower()


def _prefix_match(norm_video: str, norm_fs_base: str) -> bool:
    """True if norm_video is a whole-word prefix of (or equal to) norm_fs_base."""
    return norm_fs_base == norm_video or norm_fs_base.startswith(norm_video + ' ')


def _extract_variant_label(orig_fs_base: str, norm_fs_base: str, norm_video: str) -> str:
    """
    Return the original-text suffix of orig_fs_base that follows the matched
    video-name portion.  Assumes _prefix_match(norm_video, norm_fs_base) is True.

    Uses space-split word counts to map from normalised back to original text,
    which works correctly as long as variant labels are space-separated from the
    base (e.g. "example hard", "example (insane)", "example _nutty_").
    """
    label_norm = norm_fs_base[len(norm_video):].lstrip()
    if not label_norm:
        return ''
    n_label_words = len(label_norm.split())
    orig_words = orig_fs_base.split()
    if n_label_words > 0 and len(orig_words) >= n_label_words:
        return ' ' + ' '.join(orig_words[-n_label_words:])
    return ''


def _find_best_match(
    fs_base: str,
    norm_base: str,
    norm_videos: dict[str, str],
) -> tuple[str | None, float, int, bool]:
    """
    Find the best matching video for a funscript base name.

    Tries three strategies in order:
      1. Direct prefix match  (norm_video is whole-word prefix of norm_base)
      2. Direct fuzzy match   (SequenceMatcher ratio)
      3. Prefix-stripped match — progressively remove up to _MAX_STRIP leading
         words from norm_base and repeat strategy 1.  This handles axis/variant
         labels that appear as prefixes (HARD, SUCK, SIDE, center, etc.).

    Returns (best_video_filename, score, n_words_stripped, is_prefix_match).
    n_words_stripped is the number of leading words removed from norm_base to
    achieve the match; 0 means the match was on the full base.
    """
    best_score = 0.0
    best_video: str | None = None
    best_n_strip = 0
    best_is_prefix = False
    best_prefix_len = 0

    words = norm_base.split()

    # Strategy 1 + 2: direct match on full base
    for vf, norm_vstem in norm_videos.items():
        if _prefix_match(norm_vstem, norm_base):
            if len(norm_vstem) > best_prefix_len:
                best_score = 1.0
                best_video = vf
                best_is_prefix = True
                best_prefix_len = len(norm_vstem)
        else:
            score = difflib.SequenceMatcher(None, norm_base, norm_vstem).ratio()
            if not best_is_prefix and score > best_score:
                best_score = score
                best_video = vf

    # Strategy 3: try stripping 1…_MAX_STRIP leading words for axis-label prefixes
    _MAX_STRIP = min(5, len(words) - 1)
    for n_strip in range(1, _MAX_STRIP + 1):
        if best_score >= 1.0:
            break
        stripped = ' '.join(words[n_strip:])
        if not stripped:
            continue
        for vf, norm_vstem in norm_videos.items():
            if _prefix_match(norm_vstem, stripped):
                if len(norm_vstem) > best_prefix_len or not best_is_prefix:
                    best_score = 1.0
                    best_video = vf
                    best_is_prefix = True
                    best_prefix_len = len(norm_vstem)
                    best_n_strip = n_strip
        if best_score >= 1.0:
            break

    return best_video, best_score, best_n_strip, best_is_prefix


def find_funscript_misnames(root_dir: str, dry_run: bool) -> list[dict]:
    """
    Scan for .json files and extension-less files that are actually funscripts.
    Returns report rows: old_path, new_path, status.
    """
    report = []
    for dirpath, _, filenames in os.walk(root_dir):
        if '.manual' in filenames:
            continue
        for filename in filenames:
            _, ext = os.path.splitext(filename)
            if ext.lower() in ('.funscript',):
                continue
            # Accept .json, .funsc (truncated .funscript), trailing dot, no-extension,
            # or a long/non-ASCII "extension" that is really a title segment.
            if _is_real_ext(ext) and ext.lower() not in ('.json', '.funsc') and ext != '.':
                continue

            old_path = os.path.join(dirpath, filename)
            if not _is_funscript_content(old_path):
                continue

            # Determine new name:
            #   real ext (.json, .funsc) → replace ext
            #   trailing dot or no real ext → strip trailing dots, append .funscript
            if _is_real_ext(ext) and ext != '.':
                new_name = os.path.splitext(filename)[0] + '.funscript'
            else:
                new_name = filename.rstrip('.') + '.funscript'
            new_path = os.path.join(dirpath, new_name)

            if os.path.exists(new_path):
                print(f'  SKIP (target exists)  {filename}')
                print(f'                     -> {new_name}')
                report.append({'old_path': old_path, 'new_path': new_path,
                                'status': 'skipped: target exists'})
                continue

            if dry_run:
                print(f'  WOULD RENAME [funscript fix]')
                print(f'    {filename}')
                print(f'    -> {new_name}')
                report.append({'old_path': old_path, 'new_path': new_path,
                                'status': 'would rename'})
            else:
                print(f'  RENAME [funscript fix]')
                print(f'    {old_path}')
                print(f'    -> {new_path}')
                try:
                    os.rename(old_path, new_path)
                    report.append({'old_path': old_path, 'new_path': new_path,
                                   'status': 'renamed'})
                except OSError as e:
                    print(f'  ERROR: {e}')
                    report.append({'old_path': old_path, 'new_path': new_path,
                                   'status': f'error: {e}'})
    return report


def find_funscript_video_mismatches(
    root_dir: str,
    dry_run: bool,
    threshold: float = 0.85,
    min_report: float = 0.40,
) -> list[dict]:
    """
    Per directory: fuzzy-match .funscript files to video files by normalised name.
    Renames funscripts scoring >= threshold against the best-matching video.
    Funscripts below threshold but above min_report are written to the report only.
    """
    report: list[dict] = []
    for dirpath, _, filenames in os.walk(root_dir):
        if '.manual' in filenames:
            continue

        video_stems = {
            f: os.path.splitext(f)[0]
            for f in filenames
            if os.path.splitext(f)[1].lower() in _VIDEO_EXTS
        }
        funscripts = [f for f in filenames if f.lower().endswith('.funscript')]

        if not video_stems or not funscripts:
            continue

        norm_videos = {vf: _normalize_for_match(vstem) for vf, vstem in video_stems.items()}

        for fs_name in funscripts:
            fs_stem = os.path.splitext(fs_name)[0]
            fs_base, fs_label = _split_fs_label(fs_stem)
            norm_base = _normalize_for_match(fs_base)

            best_video, best_score, n_strip, is_prefix = _find_best_match(
                fs_base, norm_base, norm_videos
            )

            if best_video is None or best_score < min_report:
                continue

            video_stem = video_stems[best_video]
            orig_words = fs_base.split()

            if is_prefix:
                # Reconstruct: [axis-prefix-label ] + video_stem + [suffix-variant ] + known_label
                prefix_label = (' '.join(orig_words[:n_strip]) + ' ') if n_strip else ''
                stripped_base = ' '.join(orig_words[n_strip:]) if n_strip else fs_base
                norm_stripped = _normalize_for_match(stripped_base)
                suffix_variant = _extract_variant_label(
                    stripped_base, norm_stripped, norm_videos[best_video]
                )
                new_name = prefix_label + video_stem + suffix_variant + fs_label + '.funscript'
            else:
                new_name = video_stem + fs_label + '.funscript'
            old_path = os.path.join(dirpath, fs_name)
            new_path = os.path.join(dirpath, new_name)

            if new_name == fs_name:
                continue  # already correctly named

            score_str = f'{best_score:.0%}'

            if best_score >= threshold:
                if os.path.exists(new_path):
                    print(f'  SKIP (target exists)  {fs_name}')
                    report.append({'funscript': old_path, 'suggested': new_path,
                                   'video': best_video, 'score': score_str,
                                   'status': 'skipped: target exists'})
                    continue
                if dry_run:
                    print(f'  WOULD RENAME [video match {score_str}]')
                    print(f'    {fs_name}')
                    print(f'    -> {new_name}')
                    report.append({'funscript': old_path, 'suggested': new_path,
                                   'video': best_video, 'score': score_str,
                                   'status': 'would rename'})
                else:
                    print(f'  RENAME [video match {score_str}]')
                    print(f'    {old_path}')
                    print(f'    -> {new_path}')
                    try:
                        os.rename(old_path, new_path)
                        report.append({'funscript': old_path, 'suggested': new_path,
                                       'video': best_video, 'score': score_str,
                                       'status': 'renamed'})
                    except OSError as e:
                        print(f'  ERROR: {e}')
                        report.append({'funscript': old_path, 'suggested': new_path,
                                       'video': best_video, 'score': score_str,
                                       'status': f'error: {e}'})
            else:
                report.append({'funscript': old_path, 'suggested': new_path,
                               'video': best_video, 'score': score_str,
                               'status': 'uncertain: review manually'})
    return report


if __name__ == '__main__':
    args = sys.argv[1:]
    dry_run = '--dry-run' in args
    dirs = [a for a in args if not a.startswith('--')]
    if dirs:
        root = os.path.abspath(dirs[0])
    else:
        entered = input("Enter full path to scan (leave blank for current directory): ").strip()
        root = os.path.abspath(entered) if entered else os.getcwd()

    print(f'Processing: {root}')
    if dry_run:
        print('(dry run — no changes will be made)')
    print()

    print('--- Garbled filename fix ---')
    count, failed = process(root, dry_run)
    label = 'would be renamed' if dry_run else 'renamed'
    print(f'\nDone. {count} file(s) {label}.')
    if count == 0:
        print()
        print('No files matched.  For CJK mojibake (e.g. Iwara files), re-run')
        print('downloadContent.py — it now renames garbled duplicates automatically.')
    if failed:
        csv_path = os.path.join(root, 'garbled_names_failed.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['path', 'reason'])
            writer.writeheader()
            writer.writerows({'path': p, 'reason': r} for p, r in failed)
        print(f'\n{len(failed)} file(s) could not be fixed — see {csv_path}')
        for path, reason in failed:
            print(f'  {path}')
            print(f'    reason: {reason}')

    print()
    print('--- Misnamed funscript fix ---')
    fs_report = find_funscript_misnames(root, dry_run)
    fs_label = 'would be renamed' if dry_run else 'renamed'
    fs_count = sum(1 for r in fs_report if r['status'] in ('renamed', 'would rename'))
    print(f'\nDone. {fs_count} funscript(s) {fs_label}.')
    if fs_report:
        report_path = os.path.join(_reports_dir(root), 'funscript_renames.csv')
        with open(report_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['old_path', 'new_path', 'status'])
            writer.writeheader()
            writer.writerows(fs_report)
        print(f'Report written to: {report_path}')

    print()
    print('--- Funscript-to-video name match ---')
    vm_report = find_funscript_video_mismatches(root, dry_run)
    vm_label = 'would be renamed' if dry_run else 'renamed'
    vm_renamed = sum(1 for r in vm_report if r['status'] in ('renamed', 'would rename'))
    vm_uncertain = sum(1 for r in vm_report if r['status'].startswith('uncertain'))
    print(f'\nDone. {vm_renamed} funscript(s) {vm_label}, {vm_uncertain} uncertain (see report).')
    if vm_report:
        vm_csv = os.path.join(_reports_dir(root), 'funscript_video_matches.csv')
        with open(vm_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['funscript', 'suggested', 'video', 'score', 'status'])
            writer.writeheader()
            writer.writerows(vm_report)
        print(f'Report written to: {vm_csv}')

"""Consolidate cross-folder video duplicates between an original post's own
folder and a later "pack"/collection folder that already carries the
matching funscript(s).

Some creators (confirmed: Pize) periodically repost already-released videos
+ scripts inside a later "collection" bundle post. The dedupe pass
(downloadContent._dedup_existing) already collapses identical *funscripts*
to one copy tree-wide (see project memory project_sync_symmetry_check) --
but videos are almost never byte-identical across a repost, since the pack
copy is typically a separately-encoded, lower-quality version. Dedupe's
exact-hash matching never catches that, so the result is: the original
post's folder keeps a video with no funscript sitting next to it --
invisible to dedupe, but very visible to any funscript player that matches
a video to its script by folder + filename -- while the pack folder holds a
redundant, usually-worse-quality copy of the same video.

This script finds those pairs -- matched by filename stem across every
folder under the scanned path as a cheap first pass, then confirmed as
genuinely the same content via downloadContent._videos_are_similar
(duration gate, audio fingerprint, video frame hash -- not just a name
match) before anything is touched -- and consolidates each pair to a single
copy living in the pack folder, at whichever quality actually fits
MAX_RESOLUTION better:

- the pack's own copy already fits at least as well -> the original
  folder's now-redundant copy is simply removed.
- the original folder's copy fits better (the common case in practice --
  packs tend to bundle a compressed re-encode) -> it replaces the pack's
  copy in place, under the pack's existing filename, so the pack's
  already-present funscripts keep matching it by name.

This project is about ending up with one best-quality library, not hoarding
every historical copy -- so either way the original post's folder ends up
with no video of its own. It's marked '.manual' (this project's existing
"skip automated touching" marker, already respected by every other script
here: collect_tasks, fix_garbled_names, prefixFix, generate_html's badge,
...) so nothing ever tries to re-download a video into it again, plus a
.folder_log.json entry recording what happened and where the surviving
copy lives, for the audit report and for a human glancing at the folder
later.

Not run automatically -- most creators never repost like this -- it's its
own menu option, pointed at whichever creator folder actually shows the
pattern.
"""
import concurrent.futures
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import action_log
import folder_log
from downloadContent import (
    _closer_to_target_resolution,
    _get_max_resolution,
    _is_video_filename,
    _video_quality,
    _videos_are_similar,
)

_FUNSCRIPT_EXT = '.funscript'
# keep in sync with check_funscripts.py / extract_variant_archives.py
_AXIS_SUFFIXES = ('.surge', '.pitch', '.roll', '.twist', '.sway')
_MANUAL_MARKER = '.manual'
_SCRIPT_NAME = 'consolidate_packs'


def _max_workers() -> int:
    """Same DEDUP_THREADS knob _dedup_existing already uses (set by the I/O
    benchmark in setup_config.py, or manually in .env) -- the confirmatory
    checks here are subprocess calls (ffprobe/fpcalc/ffmpeg) that release
    the GIL while they run, so a thread pool pays off the same way it does
    for dedupe's hashing, even though this isn't disk-throughput-bound the
    way that benchmark actually measures."""
    env = os.getenv('DEDUP_THREADS', '').strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return max(1, (os.cpu_count() or 4) - 2)


def _funscript_stems(files: list[str]) -> set[str]:
    """Funscript basenames in *files*, with any axis suffix stripped, so a
    video's .pitch/.twist/... scripts all count as "has a script here"
    under the same stem as the video itself."""
    stems = set()
    for f in files:
        if not f.lower().endswith(_FUNSCRIPT_EXT):
            continue
        stem = Path(f).stem
        for sfx in _AXIS_SUFFIXES:
            if stem.endswith(sfx):
                stem = stem[: -len(sfx)]
                break
        stems.add(stem)
    return stems


def _scan(base_path: str):
    """Walk base_path once and return (folder_videos, folder_fs_stems,
    stem_to_videos) -- .trash and any already-'.manual'-marked folder are
    skipped entirely (a .manual folder is either already consolidated by a
    prior run of this script, or flagged for a human to look at first)."""
    folder_videos: dict[str, list[tuple[str, str]]] = {}
    folder_fs_stems: dict[str, set[str]] = {}

    for root, dirs, files in os.walk(base_path):
        dirs.sort()
        if action_log.TRASH_DIRNAME in dirs:
            dirs.remove(action_log.TRASH_DIRNAME)
        if _MANUAL_MARKER in files:
            dirs[:] = []
            continue
        videos = [(Path(f).stem, os.path.join(root, f)) for f in files if _is_video_filename(f)]
        if videos:
            folder_videos[root] = videos
        folder_fs_stems[root] = _funscript_stems(files)

    stem_to_videos: dict[str, list[tuple[str, str]]] = {}
    for folder, vids in folder_videos.items():
        for stem, path in vids:
            stem_to_videos.setdefault(stem, []).append((folder, path))

    return folder_videos, folder_fs_stems, stem_to_videos


def _name_matched_pairs(base_path: str):
    """Cheap first pass: every (folder, video_path, [(pack_folder, pack_path), ...])
    where video_path has no local matching funscript, but a same-stem video
    exists in a different folder that does -- name-matched only, not yet
    confirmed as actually the same content."""
    folder_videos, folder_fs_stems, stem_to_videos = _scan(base_path)
    pending: list[tuple[str, str, list[tuple[str, str]]]] = []

    for folder, vids in folder_videos.items():
        local_stems = folder_fs_stems.get(folder, set())
        for stem, path in vids:
            if stem in local_stems:
                continue
            packs = [
                (pack_folder, pack_path)
                for pack_folder, pack_path in stem_to_videos.get(stem, [])
                if pack_folder != folder and stem in folder_fs_stems.get(pack_folder, set())
            ]
            if packs:
                pending.append((folder, path, packs))

    return pending


def find_candidates(base_path: str) -> list[dict]:
    """Return one entry per video that has no local matching funscript but
    has an AV-confirmed duplicate elsewhere in a folder that does -- the
    "pack". Filename-stem matching (_name_matched_pairs) is only the cheap
    first pass; every candidate pair is confirmed with _videos_are_similar
    before being reported, so a same-named-but-actually-different video
    never gets flagged. The confirmatory checks run in a thread pool (see
    _max_workers) since each one is a handful of subprocess calls, not
    real CPU work in this process.
    """
    pending = _name_matched_pairs(base_path)
    if not pending:
        return []

    # Flatten to individual (video, pack) pairs so every check -- including
    # a video's 2nd/3rd candidate pack, if it has more than one -- runs
    # concurrently, then re-group below by picking each video's first
    # confirmed match in its original candidate order (same result the old
    # sequential/short-circuiting loop would have found, just not
    # necessarily the cheapest path to it).
    checks = [(path, pack_path) for _folder, path, packs in pending for _pf, pack_path in packs]

    similar: dict[tuple[str, str], bool] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers()) as ex:
        futures = {ex.submit(_videos_are_similar, v, p): (v, p) for v, p in checks}
        for future in concurrent.futures.as_completed(futures):
            pair = futures[future]
            try:
                similar[pair] = future.result()
            except Exception:
                similar[pair] = False

    candidates = []
    for folder, path, packs in pending:
        for pack_folder, pack_path in packs:
            if similar.get((path, pack_path)):
                candidates.append({
                    'orig_folder': folder,
                    'orig_video': path,
                    'pack_folder': pack_folder,
                    'pack_video': pack_path,
                })
                break

    return candidates


def _resolve_action(candidate: dict) -> dict:
    """Attach the quality comparison and the replace/remove decision to
    *candidate*, per the same MAX_RESOLUTION policy downloadContent.py
    already uses for its own AV-similar quality-replace flow."""
    max_res = _get_max_resolution()
    orig_h = (_video_quality(candidate['orig_video']) or {}).get('height', 0)
    pack_h = (_video_quality(candidate['pack_video']) or {}).get('height', 0)
    candidate['orig_height'] = orig_h
    candidate['pack_height'] = pack_h
    candidate['replace'] = _closer_to_target_resolution(orig_h, pack_h, max_res)
    return candidate


def _resolve_actions(candidates: list[dict]) -> list[dict]:
    """_resolve_action for every candidate, in a thread pool -- each one is
    two more ffprobe subprocess calls, same rationale as _max_workers."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers()) as ex:
        return list(ex.map(_resolve_action, candidates))


def _mark_consolidated(base_path: str, orig_folder: str, candidate: dict, note: str) -> None:
    marker_path = os.path.join(orig_folder, _MANUAL_MARKER)
    if not os.path.exists(marker_path):
        with open(marker_path, 'w', encoding='utf-8'):
            pass
        action_log.record('create_marker', path=marker_path)

    folder_log.append_run(
        orig_folder, _SCRIPT_NAME,
        pack_folder=os.path.relpath(candidate['pack_folder'], base_path),
        note=note,
    )


def _apply(base_path: str, candidate: dict) -> str:
    orig_video = candidate['orig_video']
    pack_video = candidate['pack_video']

    if candidate['replace']:
        trash_dest = action_log.soft_delete(base_path, pack_video)
        action_log.record('soft_delete', orig_path=pack_video, trash_path=trash_dest)
        dest_path = pack_video  # same path -- the better copy takes the old name back
        shutil.move(orig_video, dest_path)
        action_log.record('copy', dst=dest_path)
        note = f'replaced pack copy ({candidate["pack_height"]}p -> {candidate["orig_height"]}p)'
    else:
        trash_dest = action_log.soft_delete(base_path, orig_video)
        action_log.record('soft_delete', orig_path=orig_video, trash_path=trash_dest)
        note = f'removed redundant copy ({candidate["orig_height"]}p -- pack already {candidate["pack_height"]}p)'

    _mark_consolidated(base_path, candidate['orig_folder'], candidate, note)
    return note


def consolidate(base_path: str) -> tuple[int, int]:
    """Non-interactive entry point: find every candidate under *base_path*
    and consolidate it immediately, no preview or confirmation prompt --
    for a caller (dedupe_only.py's dedupe step) that already treats this
    as routine, deliberate housekeeping the same way dedupe itself runs
    with no per-file confirmation, relying on the same soft-delete/trash/
    undo safety net rather than an interactive gate. Safe to call
    unconditionally: a creator with no repost/pack pattern costs almost
    nothing here (find_candidates' cheap first pass finds no name-matched
    pairs at all, so the AV-comparison thread pool never has anything to
    check). Returns (consolidated_count, error_count); does nothing and
    returns (0, 0) if nothing is found.
    """
    candidates = find_candidates(base_path)
    if not candidates:
        return 0, 0
    candidates = _resolve_actions(candidates)

    print(f'\n[consolidate] {len(candidates)} pack-redundant video(s) found — consolidating...')
    action_log.start(_SCRIPT_NAME, base_path)
    done = errors = 0
    for i, c in enumerate(candidates, 1):
        print(f'  [{i}/{len(candidates)}] {os.path.basename(c["orig_folder"])}')
        try:
            note = _apply(base_path, c)
            print(f'    {note}')
            done += 1
        except OSError as e:
            print(f'    ERROR: {e}')
            errors += 1
    action_log.finish()
    print(f'[consolidate] done — consolidated: {done}, errors: {errors}')
    return done, errors


def main() -> None:
    print()
    print("========================================")
    print("  Consolidate Video Packs")
    print("========================================")
    print()
    print("Some creators repost already-released videos+scripts inside a later")
    print("\"collection\"/pack post. Dedupe already collapses identical funscripts")
    print("to one copy, but a pack's video is almost never byte-identical to the")
    print("original, so it isn't caught the same way -- the original post's own")
    print("folder is left with a video and no funscript next to it (invisible to")
    print("dedupe, but very visible to any player that matches by folder).")
    print()
    print("This finds those pairs -- confirmed by actual audio/video comparison,")
    print("not just filename -- keeps whichever copy fits your MAX_RESOLUTION")
    print("setting better in the pack folder (replacing the pack's copy if the")
    print("original's is the better fit), and removes the now-redundant copy.")
    print("The original post's folder ends up with no video and is marked")
    print(".manual so nothing tries to re-download it.")
    print()
    print("Not every creator does this -- point it at whichever creator folder")
    print("actually shows the pattern.")
    print()

    base_path = input("Folder to scan (creator's working directory): ").strip().strip('"\'')
    if not os.path.isdir(base_path):
        print(f"Directory not found: {base_path}")
        return
    base_path = os.path.abspath(base_path)

    print()
    print("Scanning for candidates (this compares audio/video content, so it can take a while)...")
    candidates = find_candidates(base_path)
    if not candidates:
        print("\nNo consolidation candidates found.")
        return

    candidates = _resolve_actions(candidates)

    print(f"\nFound {len(candidates)} video(s) to consolidate:")
    preview_limit = 30
    for c in candidates[:preview_limit]:
        action = (f'replace pack copy ({c["pack_height"]}p) with this ({c["orig_height"]}p)' if c['replace']
                  else f'remove this copy ({c["orig_height"]}p) -- pack already {c["pack_height"]}p')
        print(f"  {os.path.relpath(c['orig_folder'], base_path)}")
        print(f"    -> pack: {os.path.relpath(c['pack_folder'], base_path)}  [{action}]")
    if len(candidates) > preview_limit:
        print(f"  ... and {len(candidates) - preview_limit} more")

    print()
    confirm = input(f"Consolidate {len(candidates)} video(s)? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Cancelled.")
        return

    action_log.start(_SCRIPT_NAME, base_path)
    print()
    done = errors = 0
    for i, c in enumerate(candidates, 1):
        print(f"  [{i}/{len(candidates)}] {os.path.basename(c['orig_folder'])}")
        try:
            note = _apply(base_path, c)
            print(f"    {note}")
            done += 1
        except OSError as e:
            print(f"    ERROR: {e}")
            errors += 1
    action_log.finish()

    print()
    print(f"Done — consolidated: {done}, errors: {errors}")


if __name__ == '__main__':
    main()

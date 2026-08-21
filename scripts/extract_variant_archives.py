"""Extract password-protected funscript-variant archives.

Some creators (confirmed: Pize, since their mid-August 2026 move off mega.nz
to pixeldrain — see project memory) now post scripts only as an archive
(.rar/.zip/.7z) instead of a bare .funscript, with the password posted only
in Discord (no "PW: xxxx" in the post text anymore). Several intensity
variants of the same script ("Soothing", "Moderate", "Stimulating", ...) each
get their own archive, e.g.:

    Iwara - some video [Source](Soothing).rar
    Iwara - some video [Source](Moderate).rar

but *inside* each archive the funscripts share one plain base name with no
variant marker at all — extracting more than one variant into the same
folder as-is would silently overwrite one with another. This script extracts
each archive into a temp dir, works out the variant tag from the difference
between the archive's own filename and its funscripts' actual base name, and
writes the funscripts back out with that tag folded in:

    Iwara - some video [Source](Soothing).funscript
    Iwara - some video [Source](Moderate).funscript

As of ~2026-08-19 (confirmed against real pixeldrain samples) some archives
also bundle the video file itself alongside the funscripts, e.g.:

    musouduki bride.zip
        musouduki bride.mp4
        musouduki bride.funscript
        musouduki bride.pitch.funscript
        musouduki bride.surge.funscript

A bundled video gets no variant tag — it's the same video shared across
every intensity variant, only the scripts differ — and is instead routed
through the same AV-similarity + resolution comparison downloadContent.py
already uses when a post links the same video twice (see
_extract_video()), which also guards against the collision risk of two
variant archives each bundling their own copy of the same video.

Every archive gets trashed once everything it held is safely out and
accounted for — funscripts always are once extraction succeeds, and a
bundled video is too (newly placed, or an already-existing AV-matched copy
confirmed and kept) — so a successful run never leaves an archive behind,
funscript-only or not. Soft-deleted via action_log, not removed outright:
recoverable from .trash for TRASH_RETENTION_DAYS, same as everything else
this project deletes. The one exception is a video left unresolved in
_extract_video (a same-name collision that couldn't be confirmed as a
duplicate) — its archive is kept, since it's the only remaining copy.

Passwords are resolved from creator_db.py's SQLite history first (fastest,
no browser), falling back to a live Discord fetch via discord_passwords.py
only when every known password fails — a creator can (and Pize has)
re-encrypt archives under a new password without warning, so this doesn't
assume "first thing that ever worked" stays valid forever, nor does it
re-launch a browser on every single archive.

Extracted-vs-failed state is tracked in creator_db so a repeat run doesn't
burn time re-extracting or re-testing what already ran — including an
archive that got trashed, since it no longer shows up in a directory scan
at all.

Usage:
    python scripts/extract_variant_archives.py [path] [creator_key]

*path* is prompted for if omitted. *creator_key* defaults to the scanned
path's own top-level folder name (e.g. ".../Patreon/Pize" -> "pize"), the
same convention downloadContent.py uses for the Discord password lookup.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import action_log
import creator_db
from downloadContent import (
    _closer_to_target_resolution,
    _get_max_resolution,
    _is_av_similar,
    _is_video_filename,
    _video_quality,
)

_ARCHIVE_EXTS = ('.rar', '.zip', '.7z')
_AXIS_SUFFIXES = ('.surge', '.pitch', '.roll', '.twist', '.sway')  # keep in sync with check_funscripts.py


def _normalize_variant_tag(raw: str) -> str:
    """Fold whatever wraps the base name in an archive's filename (parens, brackets,
    full-width or ASCII, one marker or several combined) into one ASCII '(...)' tag,
    matching the '(SMOOTH)'-style convention check_funscripts.py already expects.

    e.g. '(Soothing)' -> '(Soothing)'; '（Moderate）' -> '(Moderate)';
         '(Moderate) [alt2]' -> '(Moderate alt2)'; '[alt2]' -> '(alt2)'
    """
    text = raw.strip().replace('（', '(').replace('）', ')').replace('[', '(').replace(']', ')')
    parts = re.findall(r'\(([^)]*)\)', text)
    if not parts:
        leftover = text.strip(' -_')
        parts = [leftover] if leftover else []
    inner = ' '.join(p.strip() for p in parts if p.strip())
    return f'({inner})' if inner else ''


def _variant_tag(archive_stem: str, base_name: str) -> str:
    """The variant tag is literally whatever's left of the archive's filename once
    the funscripts' own (known-correct) base name is removed — robust to the tag
    sitting as a prefix or a suffix, and to whatever bracket style a creator uses."""
    idx = archive_stem.find(base_name)
    if idx == -1:
        return ''
    wrapper = archive_stem[:idx] + archive_stem[idx + len(base_name):]
    return _normalize_variant_tag(wrapper)


def _run_7z(cmd: list[str]) -> subprocess.CompletedProcess:
    # stdin=DEVNULL is load-bearing: a wrong/missing password on an encrypted
    # archive would otherwise make 7z sit waiting on an interactive retry prompt.
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)


def _extract(archive_path: str, dest: str, password: str | None) -> bool:
    cmd = ['7z', 'x', '-y', f'-o{dest}', f'-p{password}' if password else '-p', archive_path]
    try:
        result = _run_7z(cmd)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _funscript_base_name(extracted_dir: str) -> str | None:
    """Base name shared by the extracted .funscript files, with any axis suffix
    (.pitch, .roll, ...) stripped — None if the archive held no funscript at all."""
    for f in os.listdir(extracted_dir):
        if not f.lower().endswith('.funscript'):
            continue
        stem = Path(f).stem
        for sfx in _AXIS_SUFFIXES:
            if stem.endswith(sfx):
                return stem[: -len(sfx)]
        return stem
    return None


def _extract_video(tmp_video_path: str, folder: str, trash_root: str) -> bool:
    """Move a video extracted from a bundled archive into *folder*.

    No variant tag gets folded in — unlike funscripts, the video is the same
    file shared across every intensity variant. That means two variant
    archives for the same post can each bundle their own copy of it — and a
    post can *also* already have this same video sitting in *folder* from its
    plain, unencrypted "video link" (downloaded before the archive was even
    opened) — so this mirrors the AV-similarity + resolution check
    downloadContent.py already applies when a post links the same video
    twice (see project memory project_quality_replace_on_av_match): if an
    AV-similar video is already in *folder*, keep whichever resolution fits
    MAX_RESOLUTION better — replacing the existing file only on a genuine
    upgrade, keeping it as-is (and discarding the archive's copy) if the
    already-downloaded one is equal or better — naming the survivor after
    whichever copy is kept (preserves any existing funscript-name match).
    A video with no AV-similar match in *folder* is new and is moved in
    as-is.

    Returns True if the video ended up safely represented on disk somewhere
    (newly placed, or an existing copy confirmed and kept) — False if it was
    left behind, unsaved, in the tmp dir (an unresolved same-name collision),
    meaning the source archive still holds the only copy and must not be
    trashed.
    """
    filename = os.path.basename(tmp_video_path)
    similar = _is_av_similar(tmp_video_path, folder)
    if similar:
        max_res = _get_max_resolution()
        new_h = (_video_quality(tmp_video_path) or {}).get('height', 0)
        old_h = (_video_quality(similar) or {}).get('height', 0)
        if _closer_to_target_resolution(new_h, old_h, max_res):
            kept_name = os.path.basename(similar)
            print(f'  [extract] {filename} ({new_h}p) fits MAX_RESOLUTION better than '
                  f'existing {kept_name} ({old_h}p) — replacing')
            trash_dest = action_log.soft_delete(trash_root, similar)
            action_log.record('soft_delete', orig_path=similar, trash_path=trash_dest)
            dest_path = similar  # same path — new content takes the old name back
            shutil.move(tmp_video_path, dest_path)
            action_log.record('copy', dst=dest_path)
        else:
            print(f'  [extract] {filename} — AV-similar to existing '
                  f'{os.path.basename(similar)}, keeping existing (equal/better resolution)')
        return True

    dest_path = os.path.join(folder, filename)
    if os.path.exists(dest_path):
        print(f'  [extract] {filename} — a same-named file already exists and wasn\'t '
              'recognized as the same video (ffmpeg/fpcalc unavailable, or genuinely '
              'different) — leaving both; check by hand')
        return False
    shutil.move(tmp_video_path, dest_path)
    action_log.record('copy', dst=dest_path)
    print(f'  [extract] {filename}')
    return True


def extract_one(archive_path: str, creator_key: str, base_path: str) -> bool:
    """Extract *archive_path* in place, renaming its funscripts with the archive's
    variant tag folded in and routing any bundled video through _extract_video()
    (no tag — see its docstring). *base_path* is the trash root, used both for a
    video replaced during that routing and, once everything the archive held is
    safely extracted, for the archive itself — a successful run never leaves an
    archive behind. Returns True on success (including "nothing new to do,
    already extracted before")."""
    folder = os.path.dirname(archive_path)
    archive_stem = Path(archive_path).stem

    with tempfile.TemporaryDirectory() as tmp:
        password_used: str | None = None
        tried: set[str] = set()
        if not _extract(archive_path, tmp, None):
            for pw in creator_db.get_password_history(creator_key):
                tried.add(pw)
                if _extract(archive_path, tmp, pw):
                    password_used = pw
                    break
            if password_used is None:
                print(f'  [extract] no known password worked for "{os.path.basename(archive_path)}" '
                      f'— fetching current passwords from Discord for "{creator_key}"...')
                import discord_passwords
                for pw in discord_passwords.fetch_password_history(creator_key):
                    if pw in tried:
                        continue  # already tried above, from the local history this fetch just re-confirmed
                    if _extract(archive_path, tmp, pw):
                        password_used = pw
                        break
            if password_used is None:
                print(f'  [extract] could not extract "{os.path.basename(archive_path)}" — '
                      'no known or freshly-fetched Discord password worked.')
                creator_db.record_extraction(archive_path, 'failed', None)
                return False
            creator_db.mark_confirmed(creator_key, password_used)

        base_name = _funscript_base_name(tmp)
        videos = [f for f in os.listdir(tmp) if _is_video_filename(f)]
        if base_name is None and not videos:
            print(f'  [extract] "{os.path.basename(archive_path)}" extracted but contained no '
                  '.funscript or video — skipping')
            creator_db.record_extraction(archive_path, 'failed', password_used)
            return False

        if base_name is not None:
            tag = _variant_tag(archive_stem, base_name)
            for f in os.listdir(tmp):
                if not f.lower().endswith('.funscript'):
                    continue
                stem = Path(f).stem
                axis = ''
                for sfx in _AXIS_SUFFIXES:
                    if stem.endswith(sfx):
                        axis, stem = sfx, stem[: -len(sfx)]
                        break
                dest_path = os.path.join(folder, f'{stem}{tag}{axis}.funscript')
                if os.path.exists(dest_path):
                    continue  # already extracted (this run or a previous one)
                shutil.move(os.path.join(tmp, f), dest_path)
                action_log.record('copy', dst=dest_path)
                print(f'  [extract] {os.path.basename(dest_path)}')

        # List comprehension, not all(generator, ...) — every video must actually get
        # processed even if an earlier one comes back unresolved; short-circuiting
        # would skip later videos' side effects (moving/matching them) entirely.
        video_results = [_extract_video(os.path.join(tmp, f), folder, base_path) for f in videos]
        videos_saved = all(video_results)

    if videos_saved:
        # Everything the archive held is now fully represented on disk —
        # funscripts always are once we get here (extraction succeeded), and
        # any bundled video is too (newly placed, or an already-existing
        # AV-matched copy confirmed and kept) — so the archive itself never
        # needs to stick around; no archives should be left behind by a
        # successful run. videos_saved is vacuously True when the archive
        # held no video at all (all([]) == True), so a funscript-only
        # archive is trashed here same as a bundled one. The one case that
        # keeps the archive is a video left unresolved in _extract_video
        # (same-name collision, not confirmed as a duplicate) — it's the
        # only remaining copy of that video.
        trash_dest = action_log.soft_delete(base_path, archive_path)
        action_log.record('soft_delete', orig_path=archive_path, trash_path=trash_dest)
        reason = 'video extracted' if videos else 'funscripts extracted'
        print(f'  [extract] {os.path.basename(archive_path)} — {reason}, archive trashed')

    creator_db.record_extraction(archive_path, 'extracted', password_used)
    return True


def scan_and_extract(base_path: str, creator_key: str | None = None, ignore_manual: bool = False) -> None:
    """*ignore_manual*: process archives in '.manual'-marked folders too. Those
    folders are skipped by default, same as every other automated script in
    this project (fix_garbled_names, prefixFix, ...) -- .manual means "don't
    touch this folder without a human looking first". This is meant as a
    one-off override for a specific run, not a persisted setting."""
    base_path = os.path.normpath(base_path)
    creator_key = (creator_key or os.path.basename(base_path)).strip().lower()
    action_log.start('extract_variant_archives', base_path)

    found = extracted = skipped = failed = manual_skipped = 0
    for root, _dirs, files in os.walk(base_path):
        if '.manual' in files and not ignore_manual:
            has_archive = any(f.lower().endswith(_ARCHIVE_EXTS) for f in files)
            if has_archive:
                print(f'  SKIP (manual)  {root}')
                manual_skipped += 1
            continue
        for f in files:
            if not f.lower().endswith(_ARCHIVE_EXTS):
                continue
            archive_path = os.path.join(root, f)
            found += 1

            prior = creator_db.extraction_status(archive_path)
            if prior == 'extracted':
                skipped += 1
                continue

            print(f'[archive] {os.path.relpath(archive_path, base_path)}')
            if extract_one(archive_path, creator_key, base_path):
                extracted += 1
            else:
                failed += 1

    action_log.finish()
    manual_note = f', {manual_skipped} folder(s) skipped (.manual)' if manual_skipped else ''
    print(f'\n[done] {found} archive(s) found — {extracted} extracted, '
          f'{skipped} already done, {failed} failed{manual_note}.')


def _main() -> None:
    args = sys.argv[1:]
    path = args[0] if args else input('Enter full directory path to scan: ').strip()
    creator_key = args[1] if len(args) > 1 else None
    ignore_manual = input(
        'Also process folders marked .manual? (y/n, default n): '
    ).strip().lower() == 'y'
    try:
        scan_and_extract(path, creator_key, ignore_manual=ignore_manual)
    finally:
        try:
            import discord_passwords
            discord_passwords.close()
        except Exception:
            pass


if __name__ == '__main__':
    _main()

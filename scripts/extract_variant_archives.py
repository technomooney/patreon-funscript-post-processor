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

As of ~2026-08-13 (confirmed live, user-reported 2026-09-12) Pize's current
packaging nests things one level deeper still, wrapping everything in one
subfolder and putting each variant's funscript in its *own* inner archive
instead of a bare .funscript:

    [Yuluer] Citlali.zip
        [Yuluer] Citlali/
            [Yuluer] Citlali.mp4
            [Yuluer] Citlali(medium).rar   <- itself contains a .funscript
            [Yuluer] Citlali(soothing).rar <- ditto

_walk_files() finds content anywhere in the extracted tree (not just the top
level, which a flat directory listing would miss entirely), and
_extract_nested_archives() opens any inner .rar/.zip/.7z it finds the same
way the outer one was opened (same creator_db password history — almost
always the same password both layers) before anything is treated as "this
archive's real content."

Separately, some archives (also confirmed live: Pize's monthly "Free
multi-axis Collection"/"Laffey collection"/etc. posts, both the collection
posts' own attached archive *and*, oddly, the same bulk archive occasionally
turning up attached to an unrelated single-video post too) bundle dozens of
completely unrelated videos+funscripts in one zip — a bulk collection, not
a single post's variant set. extract_one() detects this (more than one
distinct funscript base name inside) and, per the user's explicit request
(2026-09-12), absorbs it via _handle_bulk_collection() rather than refusing
it: everything is extracted loose into the collection's own folder (no
attempt at per-video redistribution to each video's individual post folder
elsewhere in the tree — a harder problem this project still doesn't solve),
then every video that landed there is checked against the rest of the tree
for an AV-confirmed duplicate (a video posted on its own earlier, later
folded into this same collection) — whichever copy fits MAX_RESOLUTION
better survives in the collection folder (consolidate_packs.py's own
replace-in-place policy, just with the collection folder as the fixed
target), the other folder's own funscripts the collection doesn't already
have are migrated in, and that folder is marked '.manual'+'.consolidated'.
A video that still doesn't meet MAX_RESOLUTION with nothing better found
locally is queued via collection_redownload_queue.py for
downloadContent.find_and_download() to offer fetching a replacement for
next run — never fetched automatically here.

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
no browser, tried in most-likely-current order — see creator_db.
get_password_history, which also tries a password whose Discord label
matches this archive's own post date/type first when one is known), falling
back to a live Discord fetch via discord_passwords.py only when every known
password fails — a creator can (and Pize has) re-encrypt archives under a
new password without warning, so this doesn't assume "first thing that ever
worked" stays valid forever. That Discord fetch happens at most once per run
(per creator), not once per failing archive — repeatedly reloading the same
Discord channel for archives that all need the same not-yet-known password
wastes time and, well before that, just looks like automated abuse of the
account to Discord. A password the one fetch turns up is saved to creator_db
(with the date it was first seen) same as always, so every archive after the
first still benefits from it via the normal local-history lookup above.

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
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import action_log
import collection_redownload_queue
import consolidate_packs
import creator_db
import generate_html
from downloadContent import (
    _closer_to_target_resolution,
    _get_max_resolution,
    _is_av_similar,
    _is_video_filename,
    _video_quality,
    _videos_are_similar,
)

_ARCHIVE_EXTS = ('.rar', '.zip', '.7z')
_AXIS_SUFFIXES = ('.surge', '.pitch', '.roll', '.twist', '.sway')  # keep in sync with check_funscripts.py

# Discord-fetched password history, keyed by creator — populated at most once
# per creator per run (see extract_one), same rationale as downloadContent.
# _fetch_discord_password_cached: a browser reload of the same channel for
# every archive that fails the same not-yet-known password is both wasted
# time and enough repeated automated navigation to look like abuse.
_discord_history_cache: dict[str, list[str]] = {}


def _fetch_discord_history_cached(creator_key: str) -> list[str]:
    if creator_key not in _discord_history_cache:
        import discord_passwords
        _discord_history_cache[creator_key] = discord_passwords.fetch_password_history(creator_key)
    return _discord_history_cache[creator_key]


def _post_context(archive_path: str) -> tuple[datetime.date | None, bool | None]:
    """Best-effort (post_date, is_collection) for the post folder *archive_path*
    lives directly in, parsed from this project's '[id] YYYY-MM-DD title'
    folder-naming convention (generate_html._parse_folder_name) — used only to
    move a Discord-labeled password (see discord_passwords.parse_labeled_
    passwords) ahead of the rest in creator_db.get_password_history's try
    order. is_collection is a plain 'does the title say so' check, matching
    how creators actually name these (confirmed real examples: "Free
    multi-axis Collection", "Laffey collection", ... — see project memory
    project_archival_collection_links). Returns (None, None) if the folder
    name doesn't match — callers treat that as "no hint", same as any other
    unmatched/unparseable label.
    """
    folder_name = os.path.basename(os.path.dirname(archive_path))
    _post_id, date_str, title = generate_html._parse_folder_name(folder_name)
    if not date_str:
        return None, None
    try:
        post_date = datetime.date.fromisoformat(date_str)
    except ValueError:
        return None, None
    return post_date, 'collection' in title.lower()


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
    """Extract *archive_path* into *dest* with the given password (None = no
    password). On failure, removes anything 7z wrote to *dest* before hitting
    the error -- confirmed live this session: a wrong password against a zip
    can still leave a corrupted-but-present output file behind rather than
    writing nothing at all, which would otherwise contaminate a later retry
    with a different password, or a caller's "what does this archive
    actually contain" analysis once every password has been tried.
    """
    os.makedirs(dest, exist_ok=True)
    before = set(os.listdir(dest))
    cmd = ['7z', 'x', '-y', f'-o{dest}', f'-p{password}' if password else '-p', archive_path]
    try:
        result = _run_7z(cmd)
        ok = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        for name in set(os.listdir(dest)) - before:
            path = os.path.join(dest, name)
            try:
                shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            except OSError:
                pass
    return ok


def _walk_files(root_dir: str) -> list[str]:
    """Every file's full path under *root_dir*, recursively — some archives
    wrap everything in one subfolder instead of putting funscripts/videos
    loose at the zip root (confirmed live, user-reported 2026-09-12: a
    "no .funscript or video" false negative on an archive that genuinely had
    both, just one level down in a folder inside the zip). A flat
    os.listdir() of the extraction dir would only ever see that subfolder's
    name, not what's actually in it."""
    paths = []
    for dirpath, _dirs, files in os.walk(root_dir):
        paths.extend(os.path.join(dirpath, f) for f in files)
    return paths


def _funscript_base_names(extracted_dir: str) -> set[str]:
    """Every distinct funscript base name (axis suffix stripped) found
    anywhere in the extracted tree (see _walk_files). A normal single-post
    variant archive has exactly one — more than one means this is actually a
    bulk multi-video collection (see module docstring), not something
    extract_one() should extract automatically."""
    bases = set()
    for path in _walk_files(extracted_dir):
        f = os.path.basename(path)
        if not f.lower().endswith('.funscript'):
            continue
        stem = Path(f).stem
        for sfx in _AXIS_SUFFIXES:
            if stem.endswith(sfx):
                stem = stem[: -len(sfx)]
                break
        bases.add(stem)
    return bases


def _extract_nested_archives(tmp: str, creator_key: str, post_date, is_collection: bool,
                              tried: set[str]) -> tuple[bool, dict[str, str]]:
    """Recursively open any archive found *inside* the already-extracted
    *tmp* tree (see module docstring — a variant's funscript is sometimes its
    own inner .rar rather than a bare .funscript, with the funscript itself
    left bare/untagged the same way the outer archive's funscript is — the
    variant tag lives on whichever archive, inner or outer, actually wraps
    it). Tries no password, then the same creator_db history the outer
    archive used (nearly always the same password works at both layers) —
    deliberately does not trigger a fresh Discord fetch on its own; if
    that's genuinely not enough, this reports it and extract_one leaves the
    outer archive untouched rather than losing whatever was inside the
    still-locked inner one.

    Each inner archive is extracted into its own subdirectory (named after
    the inner archive's own stem) rather than flattened alongside it, so
    every file that came out of it can be traced back to it afterward —
    extract_one needs that to compute each funscript's variant tag from
    *its own* wrapping archive's name, not the outer archive's.

    Returns (all_opened, source_by_path): all_opened is True if every inner
    archive found was opened (or none existed) — callers use this to decide
    whether it's safe to trash the outer archive afterward. source_by_path
    maps each path that came out of an inner archive to that archive's stem.
    """
    all_opened = True
    source_by_path: dict[str, str] = {}
    while True:
        nested = [p for p in _walk_files(tmp) if p.lower().endswith(_ARCHIVE_EXTS)]
        if not nested:
            return all_opened, source_by_path
        progressed = False
        for archive in nested:
            archive_stem = Path(archive).stem
            dest = os.path.join(os.path.dirname(archive), archive_stem)
            os.makedirs(dest, exist_ok=True)
            opened = _extract(archive, dest, None)
            if not opened:
                for pw in creator_db.get_password_history(creator_key, post_date, is_collection):
                    if pw in tried:
                        continue
                    tried.add(pw)
                    if _extract(archive, dest, pw):
                        opened = True
                        break
            if opened:
                for path in _walk_files(dest):
                    source_by_path[path] = archive_stem
                os.remove(archive)
                progressed = True
                print(f'  [extract] opened nested archive: {os.path.basename(archive)}')
            else:
                print(f'  [extract] could not open nested archive: {os.path.basename(archive)} '
                      '— no known password worked')
                all_opened = False
        if not progressed:
            return all_opened, source_by_path  # nothing more openable this pass -- avoid spinning forever


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


def _find_stem_video_elsewhere(base_path: str, folder: str, stem: str) -> str | None:
    """A video file anywhere else under *base_path* (never inside *folder*
    itself or '.trash') whose stem matches *stem* — a cheap name-only first
    pass, same as consolidate_packs._name_matched_pairs; the caller still
    confirms it with _videos_are_similar before treating it as the same
    content. Returns the first match found, same "first candidate wins"
    simplification consolidate_packs already makes."""
    folder = os.path.normpath(folder)
    for root, dirs, files in os.walk(base_path):
        dirs.sort()
        if action_log.TRASH_DIRNAME in dirs:
            dirs.remove(action_log.TRASH_DIRNAME)
        if os.path.normpath(root) == folder:
            continue
        for f in files:
            if _is_video_filename(f) and Path(f).stem == stem:
                return os.path.join(root, f)
    return None


def _migrate_extra_funscripts(other_folder: str, folder: str, stem: str) -> None:
    """Move every axis of *stem*'s funscript from *other_folder* into
    *folder* that *folder* doesn't already have one of — the "anything extra
    in the non-collection folder should be moved to the collection" half of
    absorbing a bulk collection archive. A collection copy for that axis, if
    one already exists, always wins in place; this only fills in gaps."""
    for axis in ('',) + _AXIS_SUFFIXES:
        src = os.path.join(other_folder, f'{stem}{axis}.funscript')
        if not os.path.isfile(src):
            continue
        dst = os.path.join(folder, f'{stem}{axis}.funscript')
        if os.path.exists(dst):
            continue
        shutil.move(src, dst)
        action_log.record('copy', dst=dst)
        print(f'  [extract] migrated {os.path.basename(src)} into collection folder (axis not already present)')


def _resolve_video_pair(base_path: str, folder: str, collection_video: str,
                         other_video: str, other_folder: str) -> int:
    """Keep whichever of *collection_video* / *other_video* fits
    MAX_RESOLUTION better, ending up at *collection_video*'s own path either
    way — same replace-in-place policy as consolidate_packs._apply, just
    with the collection folder fixed as the survivor's location rather than
    a 'pack' folder found by name-matching (the roles here are reversed: the
    collection is always the side that keeps its filename). *other_folder*
    is marked '.manual'+'.consolidated' the same way consolidate_packs marks
    a folder it has emptied of its own video. Returns the final height (0 if
    ffprobe couldn't determine it)."""
    max_res = _get_max_resolution()
    coll_h = (_video_quality(collection_video) or {}).get('height', 0)
    other_h = (_video_quality(other_video) or {}).get('height', 0)

    if _closer_to_target_resolution(other_h, coll_h, max_res):
        trash_dest = action_log.soft_delete(base_path, collection_video)
        action_log.record('soft_delete', orig_path=collection_video, trash_path=trash_dest)
        shutil.move(other_video, collection_video)
        action_log.record('copy', dst=collection_video)
        print(f'  [extract] {os.path.basename(other_video)} ({other_h}p) fits MAX_RESOLUTION better than '
              f'collection copy ({coll_h}p) — replacing')
        final_h = other_h
    else:
        trash_dest = action_log.soft_delete(base_path, other_video)
        action_log.record('soft_delete', orig_path=other_video, trash_path=trash_dest)
        print(f'  [extract] {os.path.basename(other_video)} ({other_h}p) — redundant, collection copy '
              f'already {coll_h}p')
        final_h = coll_h

    consolidate_packs._mark_consolidated(
        base_path, other_folder, {'pack_folder': folder},
        'video absorbed into bulk collection archive extraction')
    return final_h


def _video_url_for_stem(folder: str, stem: str) -> str:
    """metadata.video_url from whichever of *stem*'s funscripts in *folder*
    has one (main script first, then each axis) — the only lead available
    for a possible redownload once nothing better already exists locally."""
    for axis in ('',) + _AXIS_SUFFIXES:
        path = os.path.join(folder, f'{stem}{axis}.funscript')
        if not os.path.isfile(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        url = (data.get('metadata') or {}).get('video_url', '')
        if isinstance(url, str) and url.strip():
            return url.strip()
    return ''


def _handle_bulk_collection(tmp: str, folder: str, archive_path: str, base_path: str,
                             creator_key: str, extracted_files: list[str],
                             videos: list[str], base_names: set[str],
                             password_used: str | None) -> bool:
    """Absorb a bulk multi-video collection archive (more than one distinct
    funscript base name inside — see module docstring) instead of refusing
    it. Everything is moved loose into *folder*; every video that ends up
    there is then checked against the rest of the tree for an AV-confirmed
    duplicate, consolidating to whichever copy fits MAX_RESOLUTION better
    (see _resolve_video_pair) and migrating in any of the other folder's
    funscripts the collection doesn't already have (_migrate_extra_
    funscripts). A video that still falls short of MAX_RESOLUTION afterward
    is queued via collection_redownload_queue rather than fetched here.

    Called from inside extract_one's TemporaryDirectory context, before
    *tmp* is cleaned up — same reason _extract_video is always called from
    there too. Returns True/False with the same meaning as extract_one
    itself (False only when a bundled video couldn't be safely resolved and
    the archive must be kept as its only remaining copy)."""
    moved_funscripts = 0
    for path in extracted_files:
        f = os.path.basename(path)
        if not f.lower().endswith('.funscript'):
            continue
        dest_path = os.path.join(folder, f)
        if os.path.exists(dest_path):
            continue  # already extracted (this run or a previous one)
        shutil.move(path, dest_path)
        action_log.record('copy', dst=dest_path)
        moved_funscripts += 1

    video_results = [_extract_video(path, folder, base_path) for path in videos]
    videos_saved = all(video_results)

    print(f'  [extract] "{os.path.basename(archive_path)}" is a bulk collection archive — '
          f'absorbed {moved_funscripts} funscript(s) and {len(videos)} video(s) into '
          f'{os.path.basename(folder)}')

    # Every video now actually sitting in *folder* -- freshly placed, or an
    # already-there copy _extract_video kept as-is -- keyed by its current
    # on-disk stem (not necessarily the archive's own name for it, since
    # _extract_video may have kept whichever side already existed). Also
    # walk every funscript base name the archive held, even one with no
    # bundled video at all -- a matching video might still exist elsewhere
    # in the tree already (a fully-formed single post, its own funscripts
    # included), and per the user's explicit request that copy is moved
    # into the collection folder too rather than left where it is.
    video_by_stem = {
        Path(f).stem: os.path.join(folder, f)
        for f in os.listdir(folder)
        if _is_video_filename(f) and os.path.isfile(os.path.join(folder, f))
    }
    max_res = _get_max_resolution()

    for stem in sorted(base_names | set(video_by_stem)):
        collection_video = video_by_stem.get(stem)
        other_video = _find_stem_video_elsewhere(base_path, folder, stem)
        confirmed = other_video is not None and (
            collection_video is None or _videos_are_similar(collection_video, other_video)
        )
        if confirmed:
            other_folder = os.path.dirname(other_video)
            _migrate_extra_funscripts(other_folder, folder, stem)
            if collection_video is None:
                # No local copy to compare against -- take the only one that
                # exists rather than running a resolution comparison.
                dest_path = os.path.join(folder, os.path.basename(other_video))
                shutil.move(other_video, dest_path)
                action_log.record('copy', dst=dest_path)
                print(f'  [extract] {os.path.basename(other_video)} — no local copy of "{stem}" '
                      f'in the collection, moved in from {os.path.basename(other_folder)}')
                consolidate_packs._mark_consolidated(
                    base_path, other_folder, {'pack_folder': folder},
                    'video absorbed into bulk collection archive extraction')
                final_height = (_video_quality(dest_path) or {}).get('height', 0)
            else:
                final_height = _resolve_video_pair(base_path, folder, collection_video, other_video, other_folder)
        elif collection_video is not None:
            final_height = (_video_quality(collection_video) or {}).get('height', 0)
        else:
            final_height = 0  # no video anywhere yet for this stem

        if final_height <= 0 or final_height < max_res:
            video_url = _video_url_for_stem(folder, stem)
            if video_url:
                collection_redownload_queue.add(
                    base_path, folder, stem, video_url, final_height, max_res, creator_key)

    if not videos_saved:
        # Same reasoning as extract_one's own tail: a bundled video left
        # unresolved (a same-name collision _extract_video couldn't confirm)
        # means the archive is still the only remaining copy -- keep it.
        print(f'  [extract] "{os.path.basename(archive_path)}" left in place — a bundled video '
              'could not be safely resolved (see above); will retry next run.')
        creator_db.record_extraction(archive_path, 'failed', password_used)
        return False

    trash_dest = action_log.soft_delete(base_path, archive_path)
    action_log.record('soft_delete', orig_path=archive_path, trash_path=trash_dest)
    print(f'  [extract] {os.path.basename(archive_path)} — bulk collection extracted, archive trashed')
    creator_db.record_extraction(archive_path, 'extracted', password_used)
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
    post_date, is_collection = _post_context(archive_path)

    with tempfile.TemporaryDirectory() as tmp:
        password_used: str | None = None
        tried: set[str] = set()
        if not _extract(archive_path, tmp, None):
            for pw in creator_db.get_password_history(creator_key, post_date, is_collection):
                tried.add(pw)
                if _extract(archive_path, tmp, pw):
                    password_used = pw
                    break
            if password_used is None:
                if creator_key not in _discord_history_cache:
                    print(f'  [extract] no known password worked for "{os.path.basename(archive_path)}" '
                          f'— fetching current passwords from Discord for "{creator_key}" (once per run)...')
                else:
                    print(f'  [extract] no known password worked for "{os.path.basename(archive_path)}" '
                          f'— already checked Discord for "{creator_key}" this run, not checking again.')
                _fetch_discord_history_cached(creator_key)  # persists any new finds (+ labels) to creator_db
                for pw in creator_db.get_password_history(creator_key, post_date, is_collection):
                    if pw in tried:
                        continue  # already tried above, from the local history this fetch just re-confirmed
                    tried.add(pw)
                    if _extract(archive_path, tmp, pw):
                        password_used = pw
                        break
            if password_used is None:
                print(f'  [extract] could not extract "{os.path.basename(archive_path)}" — '
                      'no known or freshly-fetched Discord password worked.')
                creator_db.record_extraction(archive_path, 'failed', None)
                return False
            creator_db.mark_confirmed(creator_key, password_used)

        # Some variants ship their funscript as its own inner archive rather
        # than a bare .funscript (see module docstring) -- open those before
        # deciding what this archive actually holds.
        nested_ok, nested_sources = _extract_nested_archives(tmp, creator_key, post_date, is_collection, tried)

        extracted_files = _walk_files(tmp)
        base_names = _funscript_base_names(tmp)
        videos = [p for p in extracted_files if _is_video_filename(p)]
        if not base_names and not videos:
            print(f'  [extract] "{os.path.basename(archive_path)}" extracted but contained no '
                  '.funscript or video — skipping')
            creator_db.record_extraction(archive_path, 'failed', password_used)
            return False

        if len(base_names) > 1:
            # A bulk multi-video collection, not one post's variant set (see
            # module docstring) -- absorbed in place rather than refused.
            print(f'  [extract] "{os.path.basename(archive_path)}" holds funscripts for '
                  f'{len(base_names)} different videos — looks like a bulk collection archive.')
            return _handle_bulk_collection(
                tmp, folder, archive_path, base_path, creator_key,
                extracted_files, videos, base_names, password_used)

        base_name = next(iter(base_names), None)
        if base_name is not None:
            for path in extracted_files:
                f = os.path.basename(path)
                if not f.lower().endswith('.funscript'):
                    continue
                # A funscript's variant tag comes from whichever archive
                # actually wraps it -- an inner archive's own name if it came
                # from one (nested_sources), otherwise the outer archive's,
                # same as before nested archives existed at all.
                tag = _variant_tag(nested_sources.get(path, archive_stem), base_name)
                stem = Path(f).stem
                axis = ''
                for sfx in _AXIS_SUFFIXES:
                    if stem.endswith(sfx):
                        axis, stem = sfx, stem[: -len(sfx)]
                        break
                dest_path = os.path.join(folder, f'{stem}{tag}{axis}.funscript')
                if os.path.exists(dest_path):
                    continue  # already extracted (this run or a previous one)
                shutil.move(path, dest_path)
                action_log.record('copy', dst=dest_path)
                print(f'  [extract] {os.path.basename(dest_path)}')

        # List comprehension, not all(generator, ...) — every video must actually get
        # processed even if an earlier one comes back unresolved; short-circuiting
        # would skip later videos' side effects (moving/matching them) entirely.
        video_results = [_extract_video(path, folder, base_path) for path in videos]
        videos_saved = all(video_results)

    if not nested_ok:
        # An inner archive couldn't be opened -- its content is about to be
        # lost once the TemporaryDirectory above is cleaned up. Leave the
        # outer archive in place (never trashed) and mark this 'failed' so a
        # later run retries from scratch, rather than reporting success over
        # content that's actually gone.
        print(f'  [extract] "{os.path.basename(archive_path)}" left in place — an inner '
              'archive inside it could not be opened (see above); will retry next run.')
        creator_db.record_extraction(archive_path, 'failed', password_used)
        return False

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
    this project (fix_garbled_names, prefixFix, ...) -- .manual means a human
    already looked at and handled this folder themselves, so automation
    leaves it alone. This is meant as a one-off override for a specific run,
    not a persisted setting."""
    base_path = os.path.normpath(base_path)
    creator_key = (creator_key or os.path.basename(base_path)).strip().lower()
    action_log.start('extract_variant_archives', base_path)

    found = extracted = skipped = failed = manual_skipped = 0
    for root, dirs, files in os.walk(base_path):
        if action_log.TRASH_DIRNAME in dirs:
            dirs.remove(action_log.TRASH_DIRNAME)
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
    try:
        _main()
    except KeyboardInterrupt:
        print('\n\nCancelled.')

"""A creator's pixeldrain archive of LOOSE FILES as the source of truth (the "pixeldrain_archive" mode of the
"Pixeldrain archives as source of truth" plugin).

For a creator like BekScript whose archive is a pixeldrain folder share of plain files (not password-protected
archives to unpack): the share is downloaded in full into one library folder (e.g. "!archive"), and the Patreon
downloader's post folders are then emptied of anything the library already has, so the library is the one place
the scripts and videos live.

Per run:
  1. fetch   the creator's Discord channel gives the archive link(s); the newest share is synced into the library
             folder (discord_pixeldrain_sync -- only what is new, compressed videos are recognised, never overwritten).
  2. index   every funscript in the library is fingerprinted by its POINT DATA (plus its axis: a '.roll' file is never
             confused with a main script), never by name.
  3. adopt   for each post folder (description.json) its funscripts, videos and archives are checked:
               a script whose data the library has                  -> soft-deleted (a duplicate; undoable)
               a video with the same CONTENT as an archive video     -> soft-deleted (a duplicate; undoable), unless
                 (same duration, then audio fingerprint / frame hash)   the post's copy fits MAX_RESOLUTION better
                                                                        than the archive's: then it is kept (adopted)
                                                                        and flagged for you; the archive is never
                                                                        altered or replaced
               a zip/rar/7z whose sha256 the library has recorded    -> soft-deleted (a duplicate; undoable)
               anything else                                         -> MOVED to <library>/_from_patreon/<post>/ so
                                                                        the library really holds everything
             Post folders keep description.json, images and the tool's own bookkeeping.
  4. report  _reports/pixeldrain_archive_adopted.csv lists what was adopted and trashed.

Opt-in per creator in creator_config.json (nothing here ever turns itself on):
    "pixeldrain_archive": {"dir": "!archive", "discord_channel": "archive", "latest_only": true}
Everything is journaled for undo (the plugin's own undo journal).
"""
import csv
import hashlib
import os
import shutil

import action_log
import creator_config
import folder_log
import funscript_utils as fu
import library_db
import progress

SETTINGS_KEY = 'pixeldrain_archive'
ADOPT_DIRNAME = '_from_patreon'
_ARCHIVE_EXTS = ('.zip', '.rar', '.7z')
_VIDEO_EXTS = ('.mp4', '.m4v', '.mkv', '.webm', '.avi', '.mov', '.wmv', '.flv', '.ts', '.m2ts', '.mts', '.mpg',
               '.mpeg', '.3gp')
_BOOKKEEPING = {'description.json', 'description.html', folder_log.FILENAME, action_log.JOURNAL_FILENAME,
                '.manual', '.consolidated', '.links'}


def settings(base_path: str) -> dict | None:
    """The creator's loose-archive settings, or None when the creator has not opted in."""
    cfg = creator_config.get(base_path, SETTINGS_KEY)
    if not cfg:
        return None
    if cfg is True:
        cfg = {}
    return {'dir': cfg.get('dir') or '!archive', 'discord_channel': cfg.get('discord_channel') or 'archive',
            'latest_only': bool(cfg.get('latest_only', True))}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _key(path: str):
    """(axis, point data) of a funscript, or None when it is not readable. Axis is part of the key so a '.roll' file
    that happens to match a main script is not treated as a duplicate of it."""
    data = fu.funscript_data(path)
    return None if data is None else (fu.axis_suffix(path) or '', data)


class Index:
    def __init__(self):
        self.scripts: set = set()
        self.sha: set[str] = set()
        self.videos: list[tuple[str, float | None]] = []      # (path, duration seconds) of every archive video


# The video checks live in downloadContent (ffprobe / fpcalc / ffmpeg); they are looked up lazily and replaceable so
# tests need none of those tools.
def _dc():
    import downloadContent as dc
    return dc


def _duration(path: str):
    return _dc()._video_duration(path)


def _similar(a: str, b: str) -> bool:
    return _dc()._videos_are_similar(a, b)


def _height(path: str) -> int:
    return (_dc()._video_quality(path) or {}).get('height', 0)


def _max_res() -> int:
    return _dc()._get_max_resolution()


def _closer(post_h: int, archive_h: int, max_res: int) -> bool:
    return _dc()._closer_to_target_resolution(post_h, archive_h, max_res)


_DURATION_TOOL = 'loose_archive'


def build_index(lib: str, root: str | None = None) -> Index:
    """Fingerprint the archive's funscripts and list its videos with their durations. Durations are cached in the
    library database (by path and size) so a big archive is probed once, not every run."""
    idx = Index()
    cache = library_db.tool_data_get(root, _DURATION_TOOL, 'durations', {}) if root else {}
    dirty = False
    n = v = 0
    for cur, dirs, files in os.walk(lib):
        dirs[:] = [d for d in dirs if d not in action_log.SKIP_DIRNAMES]
        for f in files:
            path = os.path.join(cur, f)
            low = f.lower()
            if low.endswith('.funscript'):
                n += 1
                progress.live(f'  indexing the archive... {n} funscript(s), {v} video(s)')
                key = _key(path)
                if key is not None:
                    idx.scripts.add(key)
            elif low.endswith(_VIDEO_EXTS):
                v += 1
                progress.live(f'  indexing the archive... {n} funscript(s), {v} video(s)')
                ck = f'{os.path.relpath(path, lib)}|{os.path.getsize(path)}'
                if ck not in cache:
                    cache[ck] = _duration(path)
                    dirty = True
                idx.videos.append((path, cache[ck]))
    if dirty and root:
        library_db.tool_data_set(root, _DURATION_TOOL, 'durations', cache, _DURATION_TOOL)
    progress.clear()
    return idx


def find_same_video(path: str, idx: Index, duration_gate: float = 1.0) -> str | None:
    """An archive video with the same content as *path*, or None. Candidates are narrowed by duration (the same
    gate downloadContent uses), same-named ones tried first; only those are compared by audio/frame content."""
    dur = _duration(path)
    stem = os.path.splitext(os.path.basename(path))[0].casefold()
    cands = [p for p, d in idx.videos if dur is None or d is None or abs(d - dur) <= duration_gate]
    cands.sort(key=lambda p: os.path.splitext(os.path.basename(p))[0].casefold() != stem)
    for cand in cands:
        if _similar(path, cand):
            return cand
    return None


def _soft_delete(base_path: str, path: str) -> None:
    trash = action_log.soft_delete(base_path, path)
    action_log.record('soft_delete', orig_path=path, trash_path=trash)


def _move(src: str, dst: str) -> str:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        stem, ext = os.path.splitext(dst)
        n = 2
        while os.path.exists(f'{stem} [{n}]{ext}'):
            n += 1
        dst = f'{stem} [{n}]{ext}'
    shutil.move(src, dst)
    action_log.record('rename', old_path=src, new_path=dst)
    return dst


def _library_has(root: str, lib: str, sha: str) -> bool:
    """True when a real file inside the library folder is recorded with this sha256. The record must be for a file
    in *lib* that still exists, so a post folder's own record (or a stale one) can never make its only copy look
    like a duplicate."""
    lib_rel = library_db.rel_folder(root, lib)
    for row in library_db.find_files(root, sha256=sha):
        folder = row.get('folder') or ''
        if (folder == lib_rel or folder.startswith(lib_rel + os.sep) or folder.startswith(lib_rel + '/')) \
                and os.path.isfile(os.path.join(root, folder, row.get('cur_name') or '')):
            return True
    return False


def adopt_post(base_path: str, root: str, lib: str, post_rel: str, idx: Index) -> dict:
    """Empty one post folder of what the library has; move the rest into the library. Returns a summary."""
    post = os.path.join(base_path, post_rel)
    out = {'trashed': [], 'adopted': [], 'left': []}
    if not os.path.isdir(post):
        return out
    adopt_dir = os.path.join(lib, ADOPT_DIRNAME, os.path.basename(post_rel))
    for f in sorted(os.listdir(post)):
        path = os.path.join(post, f)
        low = f.lower()
        if not os.path.isfile(path) or f in _BOOKKEEPING:
            continue
        if low.endswith('.funscript'):
            key = _key(path)
            if key is None:
                out['left'].append(f)            # unreadable: never guess, leave it where it is
            elif key in idx.scripts:
                _soft_delete(base_path, path)
                out['trashed'].append(f)
            else:
                _move(path, os.path.join(adopt_dir, f))
                idx.scripts.add(key)
                out['adopted'].append(f)
        elif low.endswith(_VIDEO_EXTS) or low.endswith(_ARCHIVE_EXTS):
            sha = _sha256(path)
            if sha in idx.sha or _library_has(root, lib, sha):
                _soft_delete(base_path, path)                      # byte-identical to something already archived
                out['trashed'].append(f)
                continue
            match = find_same_video(path, idx) if low.endswith(_VIDEO_EXTS) else None
            if match is not None:
                max_res = _max_res()
                if _closer(_height(path), _height(match), max_res):
                    # the post's copy fits MAX_RESOLUTION better: keep it, never touch the archive's copy
                    dst = _move(path, os.path.join(adopt_dir, f))
                    idx.videos.append((dst, _duration(dst)))
                    out['adopted'].append(f)
                    out.setdefault('better_than_archive', []).append(
                        f'{f} ({_height(dst)}p) vs {os.path.relpath(match, lib)} ({_height(match)}p)')
                else:
                    _soft_delete(base_path, path)                  # same content, archive copy is as good or better
                    out['trashed'].append(f)
                continue
            dst = _move(path, os.path.join(adopt_dir, f))
            idx.sha.add(sha)
            if low.endswith(_VIDEO_EXTS):
                idx.videos.append((dst, _duration(dst)))
            out['adopted'].append(f)
    return out


def post_folders(base_path: str, lib: str) -> list[str]:
    """Relative paths of every folder holding a description.json, skipping the library, other '!'/'_' folders and
    anything the creator's protected_dirs names."""
    protected = creator_config.protected_paths(base_path)
    found = []
    for cur, dirs, files in os.walk(base_path):
        dirs.sort()
        dirs[:] = [d for d in dirs if d not in action_log.SKIP_DIRNAMES and not d.startswith(('_', '!'))
                   and os.path.normpath(os.path.join(cur, d)) != os.path.normpath(lib)]
        creator_config.prune(protected, cur, dirs)
        if 'description.json' in files:
            found.append(os.path.relpath(cur, base_path))
    return found


def fetch(base_path: str, cfg: dict, interactive: bool) -> None:
    import creator_profiles
    import pixeldrain_sync as ps
    import discord_channels
    creator_key = os.path.basename(os.path.normpath(base_path))
    configured = [j for j in discord_channels.jobs(base_path) if j['method'] == 'pixeldrain_links']
    # No channel jobs configured: fall back to the single channel named in this mode's own settings.
    job_list = configured or [{'channel': cfg['discord_channel'], 'method': 'pixeldrain_links', 'dir': cfg['dir'],
                               'mode': 'latest' if cfg['latest_only'] else 'every'}]
    discord_channels.run_jobs(base_path, interactive=interactive, job_list=job_list,
                              step=lambda title: progress.say(f'  {title}'))
    sources = [s if isinstance(s, dict) else {'url': s, 'dest': ''}
               for s in creator_profiles.get(creator_key).get(ps.SOURCES_KEY) or []]
    if sources:
        progress.say(f'  {len(sources)} saved pixeldrain source(s) -> {cfg["dir"]}/')
        ps.sync_urls(base_path, [s['url'] for s in sources], dest=cfg['dir'], interactive=interactive)


def _write_csv(base_path: str, rows: list[list]) -> str:
    d = os.path.join(base_path, '_reports')
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, 'pixeldrain_archive_adopted.csv')
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['post', 'file', 'result'])
        w.writerows(rows)
    return path


def run(base_path: str, options: dict | None, script_id: str, cfg: dict | None = None) -> None:
    """The whole loose-archive run. *script_id* names the undo journal."""
    folder_log.ensure_library(base_path)
    import pixeldrain_sync as ps
    interactive = options is None
    options = options or {}
    base_path = os.path.abspath(base_path)
    cfg = cfg or settings(base_path)
    root = ps.resolve_root(base_path, interactive)
    lib = os.path.join(base_path, cfg['dir'])
    os.makedirs(lib, exist_ok=True)
    do_fetch = options.get('fetch', True) if not interactive else input(
        f'  First fetch new archive links from Discord into "{cfg["dir"]}" (opens the browser)? (Y/n): '
    ).strip().lower() != 'n'
    total = 3 if do_fetch else 2
    n = [0]

    def step(title):
        n[0] += 1
        progress.stage(n[0], total, title)

    action_log.start(script_id, base_path, journal=script_id)
    rows: list[list] = []
    try:
        if do_fetch:
            step(f'Fetching the archive ("{cfg["discord_channel"]}" channel) -> {cfg["dir"]}/')
            try:
                fetch(base_path, cfg, interactive)
            finally:
                if 'discord_browser' in __import__('sys').modules:
                    __import__('sys').modules['discord_browser'].close()
        step('Indexing the archive')
        idx = build_index(lib, root)
        progress.say(f'  {len(idx.scripts)} distinct funscript(s) in the archive.')
        step('Moving post folders\' scripts/videos into the archive (duplicates are trashed, undoable)')
        posts = post_folders(base_path, lib)
        totals = {'trashed': 0, 'adopted': 0, 'left': 0, 'better_than_archive': 0}
        for i, rel in enumerate(posts, 1):
            progress.live(f'  post {i}/{len(posts)}: {rel[:60]}')
            summary = adopt_post(base_path, root, lib, rel, idx)
            if any(summary.values()):
                folder_log.append_run(os.path.join(base_path, rel), script_id, **summary)
            for k, files in summary.items():
                totals[k] += len(files)
                rows += [[rel, f, k] for f in files]
        progress.clear()
        csv_path = _write_csv(base_path, rows)
        progress.say(f'  {totals["trashed"]} duplicate(s) trashed, {totals["adopted"]} file(s) moved into '
                     f'{cfg["dir"]}/{ADOPT_DIRNAME}/, {totals["left"]} unreadable script(s) left in place  ->  {csv_path}')
        if totals['better_than_archive']:
            progress.say(f'  {totals["better_than_archive"]} video(s) from posts fit MAX_RESOLUTION better than the '
                         f'archive\'s copy; kept (not trashed) and listed as "better_than_archive" in that report.')
    finally:
        action_log.finish()
    print('\nDone. Undo with the creator-scripts menu (u).')

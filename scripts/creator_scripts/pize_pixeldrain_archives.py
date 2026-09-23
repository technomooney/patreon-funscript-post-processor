"""Pize: treat the creator's pixeldrain archives as the single source of truth.

Why this exists (user decision, 2026-09-22): the general pipeline had grown
too many layers for Pize (inline extraction into post folders, bulk-
collection absorption, pack consolidation, tree-wide filename matching) and
scripts were ending up matched to the wrong videos. Pize packs each video
together with its own scripts in a password-protected pixeldrain archive,
then later re-wraps posts into monthly archives, and those into ~6-month
archives. So instead of guessing pairings, this script keeps whatever the
creator packed *together*, in one library folder per archive:

    <creator>/_pixeldrain/<archive name>/      video + its scripts, as packed

and post folders end up holding only what the Patreon downloader itself
created (description.json, images, ...) plus this project's own bookkeeping
files -- never videos, scripts or archives.

Per run:
  1. Collect every pixeldrain link from every post's description.json
     (lists expanded, duplicates merged, remembering which posts link it)
     plus each post's other ("original video") links.
  2. Download each new link with the existing pixeldrain handler into a
     per-run scratch dir (_pixeldrain/.scratch, wiped every run).
  3. Open it (no password -> creator_db history -> one Discord fetch of the
     whole password history per run) and recurse into inner archives:
     an inner archive holding only another copy of the parent's scripts is
     a *variant* (folded in with its variant tag, e.g. "(medium)"); anything
     else is its own *package*. An archive that holds only packages (a
     monthly / 6-month wrapper) gets no folder of its own.
  4. Merge each package into _pixeldrain/<name>/, never overwriting: a file
     whose name, funscript points, or video (AV-similarity) is already there
     is skipped -- a package re-wrapped into a later monthly/6-month archive
     merges into the folder it already has.
  4b. Fold duplicated videos toward the bigger package: a video also found
     in a bigger collection (a package with 2+ videos) moves there, single
     posts and smaller collections alike -- the bigger one wins: the copy
     closest to MAX_RESOLUTION survives under its filename, scripts it
     doesn't already have move in renamed to pair with it, and an emptied
     folder is removed (redirected in the manifest).
  5. Empty each fully-processed post folder: a funscript already in its
     package is trashed; a video AV-matching the package's keeps whichever
     copy fits MAX_RESOLUTION better (always in the package folder); a
     funscript-only package adopts its post's single video; the downloaded
     archive's leftover copy is trashed; anything else is moved (not
     deleted) to _pixeldrain/_unmatched/<post>/ and reported.
  6. Log everything: folder_log in each post (with claimed_links, so even a
     forced normal download never refetches them) and in each package.
  7. Queue under-resolution package videos' original links for a
     redownload (offered at the end, default no).

Archives no known password opens -- including every password in the
Discord channel history -- stay in _pixeldrain/.pending, are listed with
their pixeldrain link in _reports/pixeldrain_locked_archives.csv, and are
retried automatically on later runs only when an untried password exists.
Nothing is downloaded, and nothing is moved out of their posts, for them.

This is a download-replacement script (REPLACES_DOWNLOAD): once the user
sets download_script in Pize's creator_config.json, the normal download
runs this instead. Every change goes into this script's own undo journal.
"""
import csv
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import action_log
import collection_redownload_queue
import creator_config
import creator_db
import downloadContent as dc
import extract_variant_archives as eva
import folder_log
import funscript_utils

SCRIPT_ID = 'pize_pixeldrain_archives'
MENU_LABEL = 'Pize: pixeldrain archives as source of truth'
MENU_DESCRIPTION = (
    'Downloads every pixeldrain archive linked from Pize posts, extracts each into\n'
    '_pixeldrain/<archive name>/ (never overwriting), and moves all videos/scripts out\n'
    'of the post folders. Locked archives are reported, not guessed at.'
)
REPLACES_DOWNLOAD = True

LIB_DIRNAME = '_pixeldrain'
RECOMMENDED_CONFIG = {
    'download_script': SCRIPT_ID,
    'sync_exclude': ['video', 'funscript', 'archive'],
    'protected_dirs': [LIB_DIRNAME],
}

_SCRATCH = '.scratch'
_PENDING = '.pending'
_UNMATCHED = '_unmatched'
_MANIFEST = '.manifest.json'
_ARCHIVE_EXTS = eva._ARCHIVE_EXTS
_AXIS_SUFFIXES = eva._AXIS_SUFFIXES
_JUNK = {'thumbs.db', 'desktop.ini', '.ds_store'}
_BOOKKEEPING = {'description.json', 'description.html', folder_log.FILENAME,
                action_log.JOURNAL_FILENAME, '.manual', '.consolidated', '.links'}
# 7z per-attempt timeout: 6-month collection archives are many GB.
_7Z_TIMEOUT = 4 * 3600


class Locked(Exception):
    """An archive (or an archive nested inside it) no known password opens.
    *tried* is every password that failed on that archive."""
    def __init__(self, archive_name: str, tried: set[str]):
        super().__init__(archive_name)
        self.archive_name = archive_name
        self.tried = tried


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_media(name: str) -> bool:
    low = name.lower()
    return dc._is_video_filename(name) or low.endswith('.funscript') or low.endswith(_ARCHIVE_EXTS)


def _split_axis(stem: str) -> tuple[str, str]:
    for sfx in _AXIS_SUFFIXES:
        if stem.endswith(sfx):
            return stem[: -len(sfx)], sfx
    return stem, ''


def _funscript_base(name: str) -> str:
    return _split_axis(Path(name).stem)[0]


def _pw_hash(pw: str) -> str:
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()[:16]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _file_id(url: str) -> str:
    return url.rstrip('/').split('/')[-1]


def _clean_name(name: str) -> str:
    name = dc._ILLEGAL_FILENAME_CHARS.sub('_', name).strip().rstrip('.')
    return name or 'unnamed'


def _walk(root_dir: str) -> list[str]:
    """Every real file under *root_dir* (macOS resource forks / junk skipped)."""
    out = []
    for dirpath, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d != '__MACOSX']
        for f in files:
            if f.startswith('._') or f.lower() in _JUNK:
                continue
            out.append(os.path.join(dirpath, f))
    return out


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


# ---------------------------------------------------------------------------
# Manifest -- what's been downloaded/extracted/locked, and which posts and
# packages each link belongs to. Saved after every step so a Ctrl+C never
# leaves it inconsistent with what's on disk.
# ---------------------------------------------------------------------------

class Manifest:
    def __init__(self, lib: str):
        self.path = os.path.join(lib, _MANIFEST)
        self.data = {'links': {}, 'packages': {}, 'posts': {}}
        if os.path.isfile(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    for k in self.data:
                        self.data[k] = loaded.get(k) or {}
            except (OSError, json.JSONDecodeError):
                print(f'  [{SCRIPT_ID}] could not read {self.path} — starting a fresh manifest')

    @property
    def links(self) -> dict:
        return self.data['links']

    @property
    def packages(self) -> dict:
        return self.data['packages']

    @property
    def posts(self) -> dict:
        return self.data['posts']

    def save(self) -> None:
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# 1. Collect
# ---------------------------------------------------------------------------

def collect(base_path: str) -> tuple[dict[str, set[str]], dict[str, dict]]:
    """(links, posts): links maps each pixeldrain file URL to the set of
    post folders (relative to base_path) linking it; posts maps each post
    folder to {'pixeldrain': [...], 'originals': [...]}."""
    links: dict[str, set[str]] = {}
    posts: dict[str, dict] = {}
    protected = creator_config.protected_paths(base_path)
    lib_abs = os.path.normpath(os.path.join(base_path, LIB_DIRNAME))
    list_cache: dict[str, list[str]] = {}

    for root, dirs, files in os.walk(base_path):
        dirs.sort()
        if action_log.TRASH_DIRNAME in dirs:
            dirs.remove(action_log.TRASH_DIRNAME)
        dirs[:] = [d for d in dirs if os.path.normpath(os.path.join(root, d)) != lib_abs and not d.startswith('_')]
        creator_config.prune(protected, root, dirs)
        if 'description.json' not in files:
            continue
        rel = os.path.relpath(root, base_path)
        try:
            entries = dc.extract_link_entries_from_description(os.path.join(root, 'description.json'))
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(f'  [collect] could not read {rel}/description.json: {e}')
            continue
        pixeldrain: list[str] = []
        originals: list[str] = []
        for entry in entries:
            href = entry['href']
            domain = dc.get_domain(href)
            if domain == 'pixeldrain.com':
                if '/l/' in href:
                    if href not in list_cache:
                        list_cache[href] = dc._expand_pixeldrain_list(href)
                    expanded = list_cache[href]
                else:
                    expanded = [href]
                for u in expanded:
                    if u not in pixeldrain:
                        pixeldrain.append(u)
                    links.setdefault(u, set()).add(rel)
            elif not any(domain == s or domain.endswith('.' + s) for s in dc.SKIP_DOMAINS):
                if href not in originals:
                    originals.append(href)
        posts[rel] = {'pixeldrain': pixeldrain, 'originals': originals}
    return links, posts


# ---------------------------------------------------------------------------
# 2. Download
# ---------------------------------------------------------------------------

def download(url: str, dest_dir: str) -> str | None:
    """Fetch *url* into the empty *dest_dir* via the existing pixeldrain
    handler; returns the saved path under its real filename, or None."""
    os.makedirs(dest_dir, exist_ok=True)
    before = set(os.listdir(dest_dir))
    dc._last_fetch_original_name = None
    try:
        ok = dc.download_pixeldrain(None, url, dest_dir)
    finally:
        dc._clear_status()  # end the progress line before anything else prints
    if not ok:
        return None
    new = [f for f in os.listdir(dest_dir) if f not in before and not f.endswith('.part')]
    if not new:
        return None
    path = os.path.join(dest_dir, new[0])
    real = dc._last_fetch_original_name
    if real:
        real = _clean_name(dc._decode_filename(os.path.basename(real)))
        if not os.path.splitext(real)[1]:
            real += os.path.splitext(path)[1]
        target = os.path.join(dest_dir, real)
        if target != path:
            os.rename(path, target)
            path = target
    return path


# ---------------------------------------------------------------------------
# 3. Unpack into a package tree
# ---------------------------------------------------------------------------

class Package:
    def __init__(self, key: str, chain: list[str]):
        self.key = key                      # library folder name
        self.chain = chain                  # archive names, outermost first
        self.files: list[tuple[str, str]] = []   # (current path, final name)
        self.children: list['Package'] = []

    def media(self) -> list[tuple[str, str]]:
        return [(p, n) for p, n in self.files if _is_media(n)]

    def all_packages(self):
        """This package (unless it's a pure wrapper) and every descendant."""
        if self.media():
            yield self
        for c in self.children:
            yield from c.all_packages()


class Unpacker:
    def __init__(self, creator_key: str, context_folder: str | None,
                 known_failed: dict[str, set[str]] | None = None):
        """*known_failed*: archive name -> passwords already known not to
        open it (from an earlier locked attempt), so a retry only tries new
        ones. Every other archive gets the full password history -- in
        particular an inner archive is always tried with the password that
        opened its outer one (nearly always the same)."""
        self.creator_key = creator_key
        self.post_date, self.is_collection = eva._post_context('', context_folder) if context_folder else (None, None)
        self.known_failed = known_failed or {}

    def _open(self, archive: str, dest: str) -> None:
        name = os.path.basename(archive)
        tried = set(self.known_failed.get(name, set()))
        opened, _pw = eva.open_archive(archive, dest, self.creator_key, self.post_date,
                                       self.is_collection, tried, timeout=_7Z_TIMEOUT)
        if not opened:
            raise Locked(name, tried)

    def unpack(self, archive_path: str, work_dir: str) -> Package:
        name = os.path.basename(archive_path)
        if not name.lower().endswith(_ARCHIVE_EXTS):
            # pixeldrain can host a bare file too -- a one-file package
            pkg = Package(_clean_name(Path(name).stem), [name])
            pkg.files.append((archive_path, name))
            return self._rekey(pkg)
        out = os.path.join(work_dir, 'x')
        self._open(archive_path, out)
        return self._build(out, Path(name).stem, [name])

    def _build(self, dir_: str, stem: str, chain: list[str]) -> Package:
        pkg = Package(_clean_name(stem), chain)
        paths = _walk(dir_)
        inner = [p for p in paths if p.lower().endswith(_ARCHIVE_EXTS)]
        loose = [p for p in paths if p not in inner]
        pkg.files = [(p, os.path.basename(p)) for p in loose]
        parent_bases = {_funscript_base(os.path.basename(p)) for p in loose if p.lower().endswith('.funscript')}
        parent_bases |= {Path(p).stem for p in loose if dc._is_video_filename(p)}

        opened: list[tuple[str, str, list[str]]] = []   # (archive, out dir, files)
        for a in inner:
            out = os.path.join(os.path.dirname(a), Path(a).stem + '.__x')
            self._open(a, out)
            os.remove(a)
            opened.append((a, out, _walk(out)))

        # A variant = inner archive holding only funscripts of the parent's
        # own video (no video, no further archives). When the parent has no
        # loose media at all, funscript-only inners that all share one base
        # name are variants of each other.
        def _script_only(files):
            return (files and not any(f.lower().endswith(_ARCHIVE_EXTS) or dc._is_video_filename(f) for f in files)
                    and any(f.lower().endswith('.funscript') for f in files))
        script_only_bases = [{_funscript_base(os.path.basename(f)) for f in files if f.lower().endswith('.funscript')}
                             for _a, _o, files in opened if _script_only(files)]
        shared_single = (not parent_bases and script_only_bases
                         and all(len(b) == 1 for b in script_only_bases)
                         and len(set().union(*script_only_bases)) == 1)

        for a, out, files in opened:
            bases = {_funscript_base(os.path.basename(f)) for f in files if f.lower().endswith('.funscript')}
            is_variant = _script_only(files) and (bases <= parent_bases if parent_bases else shared_single)
            if is_variant:
                for f in files:
                    fname = os.path.basename(f)
                    if fname.lower().endswith('.funscript'):
                        base, axis = _split_axis(Path(fname).stem)
                        tag = eva._variant_tag(Path(a).stem, base)
                        pkg.files.append((f, f'{base}{tag}{axis}.funscript'))
                    else:
                        pkg.files.append((f, fname))
            else:
                pkg.children.append(self._build(out, Path(a).stem, chain + [os.path.basename(a)]))
        return self._rekey(pkg)

    @staticmethod
    def _rekey(pkg: Package) -> Package:
        """An archive named '<base>(<variant>)' holding one video's scripts
        (old per-variant archives, or per-variant archives that each bundle
        the same video) belongs in the '<base>' folder with the variant tag
        folded into its funscripts, so every variant lands together."""
        bases = {_funscript_base(n) for _p, n in pkg.files if n.lower().endswith('.funscript')}
        if len(bases) != 1:
            return pkg
        base = next(iter(bases))
        if base == pkg.key or base not in pkg.key:
            return pkg
        tag = eva._variant_tag(pkg.key, base)
        renamed = []
        for p, n in pkg.files:
            if n.lower().endswith('.funscript') and Path(n).stem.startswith(base):
                b, axis = _split_axis(Path(n).stem)
                if b == base:
                    n = f'{base}{tag}{axis}.funscript'
            renamed.append((p, n))
        pkg.files = renamed
        pkg.key = _clean_name(base)
        return pkg


# ---------------------------------------------------------------------------
# 4. Merge a package into the library, never overwriting
# ---------------------------------------------------------------------------

def _folder_media(folder: str) -> tuple[set, list[str], set[str]]:
    """(funscript point-data set, video paths, filenames) currently in *folder*."""
    fds, videos, names = set(), [], set()
    if not os.path.isdir(folder):
        return fds, videos, names
    for f in os.listdir(folder):
        full = os.path.join(folder, f)
        if f.startswith('.') or not os.path.isfile(full):
            continue  # bookkeeping (.folder_log.json, ...) isn't content
        names.add(f)
        if f.lower().endswith('.funscript'):
            fd = funscript_utils.funscript_data(full)
            if fd is not None:
                fds.add(fd)
        elif dc._is_video_filename(f):
            videos.append(full)
    return fds, videos, names


def _overlaps(pkg: Package, folder: str) -> bool:
    """Does *pkg* share any content with what's already in *folder*? No
    overlap at all means a *different* package that happens to share a
    name, not the same package seen again inside a later wrapper."""
    fds, videos, names = _folder_media(folder)
    if not names:
        return True
    for p, n in pkg.media():
        if n in names:
            return True
        if n.lower().endswith('.funscript') and funscript_utils.funscript_data(p) in fds:
            return True
        if dc._is_video_filename(n) and any(dc._videos_are_similar(p, v) for v in videos):
            return True
    return False


def merge(pkg: Package, lib: str, file_id: str) -> tuple[str, int, int]:
    """Move *pkg*'s new files into its library folder. Returns
    (folder key actually used, files added, files skipped as present)."""
    key = pkg.key
    folder = os.path.join(lib, key)
    if os.path.isdir(folder) and not _overlaps(pkg, folder):
        key = _clean_name(f'{pkg.key} [{file_id}]')
        folder = os.path.join(lib, key)
    os.makedirs(folder, exist_ok=True)
    fds, _videos, names = _folder_media(folder)
    added = skipped = 0
    for src, name in pkg.files:
        name = _clean_name(name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(folder, name)
        if name in names or os.path.exists(dst):
            skipped += 1
            continue
        if name.lower().endswith('.funscript'):
            fd = funscript_utils.funscript_data(src)
            if fd is not None and fd in fds:
                skipped += 1
                continue
        elif dc._is_video_filename(name):
            if dc._is_av_similar(src, folder):
                skipped += 1
                continue
        shutil.move(src, dst)
        action_log.record('copy', dst=dst)
        names.add(name)
        if name.lower().endswith('.funscript'):
            fd = funscript_utils.funscript_data(dst)
            if fd is not None:
                fds.add(fd)
        added += 1
    return key, added, skipped


# ---------------------------------------------------------------------------
# 4b. Fold single-video packages into the collections that contain them
# ---------------------------------------------------------------------------

# A package folder with at least this many videos is a collection (user's
# rule). Single-video (and script-only) packages whose content turns out to
# be inside a collection are folded into it -- the collection wins.
COLLECTION_MIN_VIDEOS = 2
# Same duration gate _videos_are_similar applies, used here as a cheap
# pre-filter so only plausible pairs get the expensive audio/frame check.
_DURATION_GATE_S = 1.0


def resolve_key(manifest: Manifest, key: str) -> str:
    """The package folder *key*'s content lives in now -- itself, or the
    collection it was fully folded into (following any chain)."""
    seen = set()
    while key in manifest.packages and manifest.packages[key].get('folded_into') and key not in seen:
        seen.add(key)
        key = manifest.packages[key]['folded_into']
    return key


def resolve_keys(manifest: Manifest, key: str) -> set[str]:
    """Every folder holding content that came from package *key*: the
    folder it now lives in plus every collection it was (partly) folded
    into -- a partly-folded collection's posts need to see both."""
    out: set[str] = set()
    todo = [key]
    while todo:
        k = todo.pop()
        if k in out:
            continue
        rec = manifest.packages.get(k, {})
        if rec.get('folded_into'):
            todo.append(rec['folded_into'])
        else:
            out.add(k)
        todo.extend(rec.get('folded_parts', []))
    return out


def _package_dirs(lib: str) -> list[str]:
    return sorted(d for d in os.listdir(lib)
                  if not d.startswith('.') and d != _UNMATCHED and os.path.isdir(os.path.join(lib, d)))


def _script_rest(script_name: str, video_stem: str) -> tuple[str, str]:
    """(variant tag, axis) of a funscript relative to the video it belongs
    to -- 'A(medium).roll.funscript' vs video 'A' -> ('(medium)', '.roll')."""
    base, axis = _split_axis(Path(script_name).stem)
    tag = base[len(video_stem):] if base.startswith(video_stem) else ''
    return tag, axis


def _free_script_name(folder: str, stem: str, tag: str, axis: str) -> str:
    name = f'{stem}{tag}{axis}.funscript'
    n = 1
    while os.path.exists(os.path.join(folder, name)):
        n += 1
        alt = f'alt{n}' if n > 2 else 'alt'
        name = f'{stem}{tag}({alt}){axis}.funscript'
    return name


def _fold_scripts(base_path: str, scripts: list[str], video_stem: str, target: str,
                  target_stem: str, moved: list[str]) -> None:
    """Move each script into *target* renamed to pair with *target_stem*
    (variant/axis kept), unless *target* already has the same points."""
    target_fds = _folder_media(target)[0]
    for sp in scripts:
        fd = funscript_utils.funscript_data(sp)
        if fd is not None and fd in target_fds:
            _soft_delete(base_path, sp)
            continue
        tag, axis = _script_rest(os.path.basename(sp), video_stem)
        dst = os.path.join(target, _free_script_name(target, target_stem, tag, axis))
        _move(sp, dst)
        moved.append(os.path.basename(dst))
        if fd is not None:
            target_fds.add(fd)


def _scripts_for_video(folder: str, video_stem: str, all_video_stems: list[str]) -> list[str]:
    """The funscripts in *folder* that belong to *video_stem*: in a
    one-video folder every script; otherwise those whose name starts with
    it -- and with no longer video stem that also matches ('ホシノ' vs
    'ホシノ＋')."""
    names = [f for f in os.listdir(folder) if f.lower().endswith('.funscript')]
    if len(all_video_stems) <= 1:
        return sorted(os.path.join(folder, f) for f in names)
    out = []
    for f in names:
        stem = Path(f).stem
        owners = [v for v in all_video_stems if stem.startswith(v)]
        if owners and max(owners, key=len) == video_stem:
            out.append(os.path.join(folder, f))
    return sorted(out)


def fold_into_collections(base_path: str, lib: str, manifest: Manifest) -> int:
    """Fold duplicated videos toward the bigger package, video by video.

    Package folders are ranked by video count (most first; ties broken by
    name, so the result is deterministic). Each video in a folder is
    compared against the videos of every higher-ranked *collection*
    (>= COLLECTION_MIN_VIDEOS videos) -- durations first as a cheap
    filter, then the AV check. On a match the bigger folder wins: the copy
    closest to MAX_RESOLUTION survives under the bigger folder's filename,
    the video's scripts the winner doesn't already have (by points) move
    in renamed to pair with it, duplicates go to trash. Videos only ever
    move toward a higher rank, so nothing can bounce back.

    A folder left with no videos and no scripts is removed and redirected
    ('folded_into'); one only partly folded keeps its remaining videos and
    records where the rest went ('folded_parts'), so its posts look there
    too. A script-only package folds only when every script is already in
    one collection. Returns the number of videos/packages folded."""
    max_res = dc._get_max_resolution()
    dirs = _package_dirs(lib)

    def videos(d):
        p = os.path.join(lib, d)
        return sorted(f for f in os.listdir(p) if dc._is_video_filename(f)) if os.path.isdir(p) else []

    initial = {d: len(videos(d)) for d in dirs}
    ranked = sorted(dirs, key=lambda d: (-initial[d], d))
    rank = {d: i for i, d in enumerate(ranked)}

    dur_cache: dict[str, float | None] = {}

    def dur(path):
        if path not in dur_cache:
            dur_cache[path] = dc._video_duration(path)
        return dur_cache[path]

    folded = 0
    for s_key in reversed(ranked):  # smallest first
        s_dir = os.path.join(lib, s_key)
        if not os.path.isdir(s_dir):
            continue
        winners = [t for t in ranked if rank[t] < rank[s_key] and initial[t] >= COLLECTION_MIN_VIDEOS]
        if not winners:
            continue
        received: list[str] = []
        s_videos = videos(s_key)
        s_stems = [Path(v).stem for v in s_videos]

        for vname in s_videos:
            v = os.path.join(s_dir, vname)
            vd = dur(v)
            match = None
            for t in winners:
                for tv in videos(t):
                    cv = os.path.join(lib, t, tv)
                    if vd is not None and dur(cv) is not None and abs(dur(cv) - vd) > _DURATION_GATE_S:
                        continue
                    if dc._videos_are_similar(v, cv):
                        match = (t, cv)
                        break
                if match:
                    break
            if match is None:
                continue
            target, cv = match
            target_dir = os.path.join(lib, target)
            target_stem = Path(cv).stem
            scripts = _scripts_for_video(s_dir, Path(vname).stem, s_stems)
            s_h = (dc._video_quality(v) or {}).get('height', 0)
            c_h = (dc._video_quality(cv) or {}).get('height', 0)
            print(f'  [fold] {s_key}/{vname} -> {target}/{Path(cv).name}')
            if dc._closer_to_target_resolution(s_h, c_h, max_res):
                _soft_delete(base_path, cv)
                _move(v, os.path.splitext(cv)[0] + os.path.splitext(v)[1])
                print(f'    kept the {s_key} copy ({s_h}p over {c_h}p)')
            else:
                _soft_delete(base_path, v)
            dur_cache.pop(v, None)
            moved: list[str] = []
            _fold_scripts(base_path, scripts, Path(vname).stem, target_dir, target_stem, moved)
            folder_log.append_run(target_dir, SCRIPT_ID, folded_in=f'{s_key}/{vname}',
                                  scripts_added=moved)
            collection_redownload_queue.remove(base_path, {(s_dir, Path(vname).stem)})
            if target not in received:
                received.append(target)
            folded += 1

        if not s_videos:
            # script-only package: fold only if every script is already in
            # one collection (by points) -- nothing to AV-check against
            scripts = sorted(os.path.join(s_dir, f) for f in os.listdir(s_dir) if f.lower().endswith('.funscript'))
            fds = [funscript_utils.funscript_data(sp) for sp in scripts]
            if scripts and all(fd is not None for fd in fds):
                target = next((t for t in winners
                               if all(fd in _folder_media(os.path.join(lib, t))[0] for fd in fds)), None)
                if target:
                    print(f'  [fold] {s_key} -> {target} (scripts already there)')
                    for sp in scripts:
                        _soft_delete(base_path, sp)
                    received.append(target)
                    folded += 1

        if not received:
            continue
        rec = manifest.packages.setdefault(s_key, {'posts': [], 'archives': [], 'links': []})
        emptied = not any(dc._is_video_filename(f) or f.lower().endswith('.funscript')
                          for f in os.listdir(s_dir))
        if emptied:
            # anything else left (readme, images) goes along; then the folder goes
            for f in sorted(os.listdir(s_dir)):
                full = os.path.join(s_dir, f)
                if not f.startswith('.') and os.path.isfile(full):
                    _move(full, os.path.join(lib, received[0], f))
            for f in os.listdir(s_dir):
                if f.startswith('.'):
                    try:
                        os.remove(os.path.join(s_dir, f))
                    except OSError:
                        pass
            try:
                os.rmdir(s_dir)
            except OSError:
                pass
            rec['folded_into'] = received[0]
        parts = rec.setdefault('folded_parts', [])
        for t in received:
            if t not in parts:
                parts.append(t)
            trec = manifest.packages.setdefault(t, {'posts': [], 'archives': [], 'links': []})
            for field in ('posts', 'originals'):
                for item in rec.get(field, []):
                    trec.setdefault(field, [])
                    if item not in trec[field]:
                        trec[field].append(item)
        manifest.save()
    return folded


# ---------------------------------------------------------------------------
# 5. Empty post folders into their packages
# ---------------------------------------------------------------------------

def _soft_delete(base_path: str, path: str) -> None:
    trash = action_log.soft_delete(base_path, path)
    action_log.record('soft_delete', orig_path=path, trash_path=trash)


def _move(src: str, dst: str) -> None:
    """Move a real library file (never a scratch copy) -- journaled as a
    rename so undo moves it back instead of deleting the only copy."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        stem, ext = os.path.splitext(dst)
        n = 2
        while os.path.exists(f'{stem} [{n}]{ext}'):
            n += 1
        dst = f'{stem} [{n}]{ext}'
    shutil.move(src, dst)
    action_log.record('rename', old_path=src, new_path=dst)


def _mark(folder: str, marker: str) -> None:
    path = os.path.join(folder, marker)
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8'):
            pass
        action_log.record('create_marker', path=path)


def _package_video_stem(folder: str) -> str | None:
    """The name a video in package *folder* should carry so every funscript
    variant there matches it: the folder's own name when its scripts are
    '<folder name>(<variant>)...' (see Unpacker._rekey), else the shortest
    funscript base name."""
    bases = {_funscript_base(n) for n in os.listdir(folder) if n.lower().endswith('.funscript')}
    key = os.path.basename(folder)
    if any(b.startswith(key) for b in bases):
        return key
    return min(bases, key=len) if bases else None


def empty_post(base_path: str, lib: str, post_rel: str, package_keys: list[str],
               archive_hashes: set[str], archive_names: set[str]) -> dict:
    """Move/trash every video, funscript and archive out of one post folder
    (see module docstring, step 5). Returns a summary for the logs."""
    post = os.path.join(base_path, post_rel)
    summary = {'trashed_duplicates': [], 'replaced_package_video': [], 'adopted_video': [],
               'unmatched': [], 'trashed_archives': []}
    if not os.path.isdir(post):
        return summary
    pkg_folders = [os.path.join(lib, k) for k in package_keys if os.path.isdir(os.path.join(lib, k))]
    max_res = dc._get_max_resolution()

    media = sorted(f for f in os.listdir(post)
                   if os.path.isfile(os.path.join(post, f)) and _is_media(f) and f not in _BOOKKEEPING)
    leftover_videos: list[str] = []
    leftover_other: list[str] = []

    for f in media:
        path = os.path.join(post, f)
        low = f.lower()
        if low.endswith(_ARCHIVE_EXTS):
            if f in archive_names or _sha256(path) in archive_hashes:
                _soft_delete(base_path, path)
                summary['trashed_archives'].append(f)
            else:
                leftover_other.append(path)
            continue
        if low.endswith('.funscript'):
            fd = funscript_utils.funscript_data(path)
            if fd is not None and any(fd in _folder_media(pf)[0] for pf in pkg_folders):
                _soft_delete(base_path, path)
                summary['trashed_duplicates'].append(f)
            else:
                leftover_other.append(path)
            continue
        # video: same-stem package videos first, then everything else
        candidates = [v for pf in pkg_folders for v in _folder_media(pf)[1]]
        candidates.sort(key=lambda v: Path(v).stem != Path(f).stem)
        match = next((v for v in candidates if dc._videos_are_similar(path, v)), None)
        if match is None:
            leftover_videos.append(path)
            continue
        post_h = (dc._video_quality(path) or {}).get('height', 0)
        pkg_h = (dc._video_quality(match) or {}).get('height', 0)
        if dc._closer_to_target_resolution(post_h, pkg_h, max_res):
            # the post's copy fits MAX_RESOLUTION better -- it takes the
            # package copy's place (and name), in the package folder
            _soft_delete(base_path, match)
            new_path = os.path.splitext(match)[0] + os.path.splitext(path)[1]
            _move(path, new_path)
            summary['replaced_package_video'].append(f'{f} ({post_h}p) -> {os.path.relpath(new_path, lib)} (was {pkg_h}p)')
        else:
            _soft_delete(base_path, path)
            summary['trashed_duplicates'].append(f)

    # A funscript-only package adopts its post's single remaining video --
    # the pairing is known from the post itself, not guessed from names.
    scriptonly = [pf for pf in pkg_folders if not _folder_media(pf)[1]]
    if len(scriptonly) == 1 and len(leftover_videos) == 1:
        pf = scriptonly[0]
        src = leftover_videos.pop()
        stem = _package_video_stem(pf) or Path(src).stem
        dst = os.path.join(pf, stem + os.path.splitext(src)[1])
        _move(src, dst)
        summary['adopted_video'].append(f'{os.path.basename(src)} -> {os.path.relpath(dst, lib)}')

    for path in leftover_videos + leftover_other:
        dst = os.path.join(lib, _UNMATCHED, os.path.basename(post), os.path.basename(path))
        _move(path, dst)
        summary['unmatched'].append(os.path.basename(path))
    return summary


# ---------------------------------------------------------------------------
# 7. Resolution check
# ---------------------------------------------------------------------------

def queue_under_res(base_path: str, lib: str, key: str, originals: list[str], creator_key: str,
                    attempted: set[str] = frozenset()) -> int:
    folder = os.path.join(lib, key)
    if not os.path.isdir(folder):
        return 0
    max_res = dc._get_max_resolution()
    videos = sorted(f for f in os.listdir(folder) if dc._is_video_filename(f))
    uniq_originals = list(dict.fromkeys(originals))
    queued = 0
    targets = [(Path(v).stem, (dc._video_quality(os.path.join(folder, v)) or {}).get('height', 0)) for v in videos]
    if not videos:
        stem = _package_video_stem(folder)
        if stem:
            targets = [(stem, 0)]
    for stem, height in targets:
        if (height and height >= max_res) or stem in attempted:
            continue  # fine as is, or a redownload was already tried once
        url = eva._video_url_for_stem(folder, stem)
        if not url and len(targets) == 1 and len(uniq_originals) == 1:
            url = uniq_originals[0]
        if not url:
            continue
        collection_redownload_queue.add(base_path, folder, stem, url, height, max_res, creator_key)
        queued += 1
    return queued


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _write_csv(base_path: str, name: str, header: list[str], rows: list[list]) -> str:
    d = os.path.join(base_path, '_reports')
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def write_locked_report(base_path: str, manifest: Manifest) -> int:
    rows = [[url, e.get('archive_name', ''), e.get('locked_part', ''), '; '.join(e.get('posts', [])),
             len(e.get('tried', [])), e.get('first_attempt', ''), e.get('last_attempt', '')]
            for url, e in sorted(manifest.links.items()) if e.get('status') == 'locked']
    _write_csv(base_path, 'pixeldrain_locked_archives.csv',
               ['pixeldrain_url', 'archive', 'locked_part', 'posts', 'passwords_tried',
                'first_attempt', 'last_attempt'], rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def _process_link(url: str, base_path: str, lib: str, manifest: Manifest, creator_key: str) -> None:
    """One link, start to finish. Its scratch folder (the download plus
    anything extracted but not moved into the library, e.g. duplicates) is
    deleted as soon as the link is done -- not left to pile up until the
    end of a many-hundred-link run."""
    work = os.path.join(lib, _SCRATCH, _file_id(url))
    try:
        _process_link_inner(url, base_path, lib, manifest, creator_key, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _process_link_inner(url: str, base_path: str, lib: str, manifest: Manifest, creator_key: str,
                        work: str) -> None:
    entry = manifest.links.setdefault(url, {})
    fid = _file_id(url)
    entry.setdefault('file_id', fid)
    entry.setdefault('first_attempt', _now())
    entry['last_attempt'] = _now()
    shutil.rmtree(work, ignore_errors=True)

    pending = entry.get('pending_path')
    if pending and os.path.isfile(os.path.join(lib, pending)):
        archive = os.path.join(lib, pending)
        print(f'  retrying locked archive: {os.path.basename(archive)}')
    else:
        archive = download(url, os.path.join(work, 'dl'))
        if archive is None:
            entry['status'] = 'failed'
            print(f'  download failed: {url}')
            return
        entry['archive_name'] = os.path.basename(archive)
        entry['sha256'] = _sha256(archive)

    posts = sorted(entry.get('posts', []))
    context = os.path.join(base_path, posts[0]) if posts else None
    known_failed = {}
    if entry.get('locked_part') and entry.get('tried'):
        tried_hashes = set(entry['tried'])
        known_failed[entry['locked_part']] = {pw for pw in creator_db.get_password_history(creator_key)
                                              if _pw_hash(pw) in tried_hashes}
    unpacker = Unpacker(creator_key, context, known_failed)
    try:
        root = unpacker.unpack(archive, work)
    except Locked as e:
        prior = set(entry.get('tried', [])) if entry.get('locked_part') == e.archive_name else set()
        entry['tried'] = sorted(prior | {_pw_hash(pw) for pw in e.tried})
        entry['status'] = 'locked'
        entry['locked_part'] = e.archive_name
        if not pending:
            # kept under its real name (in a per-link subfolder) -- the name
            # carries the variant tag and is what the locked report shows
            pdir = os.path.join(lib, _PENDING, fid)
            os.makedirs(pdir, exist_ok=True)
            dst = os.path.join(pdir, os.path.basename(archive))
            shutil.move(archive, dst)
            action_log.record('copy', dst=dst)
            entry['pending_path'] = os.path.relpath(dst, lib)
        print(f'  LOCKED: {entry.get("archive_name")} ({e.archive_name}) — no known or Discord password '
              'opens it; reported, will retry when a new password shows up')
        for post in posts:
            folder_log.append_run(os.path.join(base_path, post), 'pixeldrain_locked',
                                  pixeldrain_url=url, archive=entry.get('archive_name'),
                                  locked_part=e.archive_name)
        return
    finally:
        manifest.save()

    keys = []
    for pkg in root.all_packages():
        key, added, skipped = merge(pkg, lib, fid)
        keys.append(key)
        rec = manifest.packages.setdefault(key, {'posts': [], 'archives': [], 'links': []})
        # content (re)landed in this folder -- a previous fold no longer
        # applies until the fold step confirms it again
        rec.pop('folded_into', None)
        rec.pop('folded_parts', None)
        for p in posts:
            if p not in rec['posts']:
                rec['posts'].append(p)
        chain = ' > '.join(pkg.chain)
        if chain not in rec['archives']:
            rec['archives'].append(chain)
        if url not in rec['links']:
            rec['links'].append(url)
        print(f'  [{key}] +{added} file(s){f", {skipped} already present" if skipped else ""}  ({chain})')
        folder_log.append_run(os.path.join(lib, key), SCRIPT_ID, pixeldrain_url=url, archive_chain=chain,
                              source_posts=posts, added=added, already_present=skipped)
    if not keys:
        print(f'  {entry.get("archive_name")}: no videos or scripts inside')
    entry['packages'] = sorted(set(entry.get('packages', [])) | set(keys))
    entry['status'] = 'done'
    for k in ('pending_path', 'locked_part'):
        entry.pop(k, None)
    if pending:
        try:
            _soft_delete(base_path, archive)
            os.rmdir(os.path.dirname(archive))
        except OSError:
            pass
    manifest.save()


def run(base_path: str, options: dict | None = None) -> None:
    interactive = options is None
    options = options or {}
    base_path = os.path.abspath(base_path)
    creator_key = os.path.basename(os.path.normpath(base_path)).strip().lower()
    lib = os.path.join(base_path, LIB_DIRNAME)
    os.makedirs(lib, exist_ok=True)
    manifest = Manifest(lib)
    # Undo must also roll the manifest back, or the next run would think
    # the undone work is still done. Snapshot it now; journaled (as the
    # run's first entries, so undone last) only if the run changes anything.
    undo_snapshot = manifest.path + '.undo'
    had_manifest = os.path.isfile(manifest.path)
    if had_manifest:
        shutil.copy2(manifest.path, undo_snapshot)

    print(f'Collecting pixeldrain links under {base_path} ...')
    links, posts = collect(base_path)
    for url, post_set in links.items():
        e = manifest.links.setdefault(url, {})
        e['posts'] = sorted(set(e.get('posts', [])) | post_set)
    for rel, info in posts.items():
        manifest.posts.setdefault(rel, {}).update(info)
    manifest.save()

    todo = [u for u in links if manifest.links[u].get('status') != 'done']
    todo.sort(key=lambda u: manifest.links[u].get('status') != 'locked')  # retry locked first
    no_pd = sorted(rel for rel, info in posts.items() if not info['pixeldrain'])
    print(f'{len(links)} unique pixeldrain link(s) across {len(posts)} post(s); '
          f'{len(links) - len(todo)} already done, {len(todo)} to process.')
    if no_pd:
        print(f'{len(no_pd)} post(s) have no pixeldrain link — left untouched.')
    if interactive and todo:
        if input('Proceed? (y/n, default y): ').strip().lower() == 'n':
            print('Aborted.')
            if had_manifest:
                os.remove(undo_snapshot)
            return

    action_log.start(SCRIPT_ID, base_path, journal=SCRIPT_ID)
    try:
        for i, url in enumerate(todo, 1):
            print(f'\n[{i}/{len(todo)}] {url}')
            _process_link(url, base_path, lib, manifest, creator_key)

        # 4b. Single-video packages that are part of a collection fold into it.
        print('\nFolding duplicate videos into the bigger collections...')
        folded = fold_into_collections(base_path, lib, manifest)
        print(f'{folded} duplicate video(s)/package(s) folded into a bigger collection.')

        # 5-6. Empty every post whose pixeldrain links are all done.
        print('\nMoving videos/scripts out of post folders...')
        emptied = unmatched = 0
        for rel, info in sorted(posts.items()):
            if not info['pixeldrain']:
                continue
            entries = [manifest.links.get(u, {}) for u in info['pixeldrain']]
            if any(e.get('status') != 'done' for e in entries):
                continue  # locked / failed -- leave the post as it is
            post = os.path.join(base_path, rel)
            has_media = any(_is_media(f) and f not in _BOOKKEEPING for f in os.listdir(post)
                            if os.path.isfile(os.path.join(post, f)))
            if not has_media and manifest.posts.get(rel, {}).get('status') == 'emptied':
                continue
            keys = sorted({r for e in entries for k in e.get('packages', []) for r in resolve_keys(manifest, k)})
            summary = empty_post(base_path, lib, rel, keys,
                                 {e['sha256'] for e in entries if e.get('sha256')},
                                 {e['archive_name'] for e in entries if e.get('archive_name')})
            _mark(post, '.manual')
            _mark(post, '.consolidated')
            folder_log.append_run(post, SCRIPT_ID, pixeldrain_urls=info['pixeldrain'],
                                  original_links=info['originals'],
                                  extracted_to=[f'{LIB_DIRNAME}/{k}' for k in keys],
                                  claimed_links=info['pixeldrain'] + info['originals'], **summary)
            manifest.posts[rel]['status'] = 'emptied'
            manifest.posts[rel]['packages'] = keys
            for k in keys:
                rec = manifest.packages.setdefault(k, {'posts': [], 'archives': [], 'links': []})
                rec.setdefault('originals', [])
                for o in info['originals']:
                    if o not in rec['originals']:
                        rec['originals'].append(o)
            emptied += 1
            unmatched += len(summary['unmatched'])
            manifest.save()
        print(f'{emptied} post folder(s) emptied into {LIB_DIRNAME}/'
              f'{f", {unmatched} unmatched file(s) moved to {LIB_DIRNAME}/{_UNMATCHED}/" if unmatched else ""}.')
        if unmatched:
            rows = []
            udir = os.path.join(lib, _UNMATCHED)
            for d in sorted(os.listdir(udir)):
                for f in sorted(os.listdir(os.path.join(udir, d))):
                    rows.append([d, f])
            path = _write_csv(base_path, 'pixeldrain_unmatched_post_files.csv', ['post', 'file'], rows)
            print(f'  review: {path}')

        # 7. Resolution check for every package.
        queued = 0
        for key, rec in sorted(manifest.packages.items()):
            if rec.get('folded_into'):
                continue
            queued += queue_under_res(base_path, lib, key, rec.get('originals', []), creator_key,
                                      set(rec.get('redownload_attempted', [])))
        locked = write_locked_report(base_path, manifest)
        failed = sum(1 for e in manifest.links.values() if e.get('status') == 'failed')
        print(f'\nLocked archives: {locked} (see _reports/pixeldrain_locked_archives.csv)'
              f'{f"; failed downloads: {failed} (retried next run)" if failed else ""}')

        # Redownload offer -- only entries for this library's own folders.
        lib_prefix = os.path.normpath(lib) + os.sep
        flagged = [e for e in collection_redownload_queue.load(base_path)
                   if os.path.normpath(e.get('folder', '')).startswith(lib_prefix)]
        if flagged:
            if interactive:
                go = input(f'\n{len(flagged)} video(s) below MAX_RESOLUTION have an original link — '
                           'try to download better copies now? (y/n, default n): ').strip().lower() == 'y'
            else:
                go = bool(options.get('redownload_flagged', False))
            if go:
                dc._run_flagged_collection_redownloads(base_path, flagged, resume=True,
                                                       auto_confirm=None if interactive else True)
                # Offered once: a source that only has this resolution
                # shouldn't be re-offered every single run.
                for e in flagged:
                    key = os.path.relpath(os.path.normpath(e['folder']), lib)
                    rec = manifest.packages.setdefault(key, {'posts': [], 'archives': [], 'links': []})
                    rec.setdefault('redownload_attempted', [])
                    if e['stem'] not in rec['redownload_attempted']:
                        rec['redownload_attempted'].append(e['stem'])
            else:
                print(f'{len(flagged)} under-resolution video(s) stay queued for a later run.')
        elif queued == 0:
            print('No under-resolution videos with a known original link.')
    finally:
        manifest.save()
        if action_log.has_entries():
            # Undo runs entries in reverse: this 'copy' (last) is undone
            # first, deleting this run's manifest; the prepended
            # 'soft_delete' (first) is undone last, moving the snapshot back.
            action_log.record('copy', dst=manifest.path)
            if had_manifest:
                action_log.prepend('soft_delete', orig_path=manifest.path, trash_path=undo_snapshot)
        elif had_manifest:
            os.remove(undo_snapshot)
        action_log.finish()
        shutil.rmtree(os.path.join(lib, _SCRATCH), ignore_errors=True)
        if 'discord_passwords' in __import__('sys').modules:
            try:
                __import__('sys').modules['discord_passwords'].close()
            except Exception:
                pass
    print(f'\nDone. Undo with the creator-scripts menu (u) -> {MENU_LABEL}.')


def setup_unattended(current: dict) -> dict:
    cur = bool(current.get('redownload_flagged', False))
    raw = input(f'  Download better copies of under-resolution videos automatically? '
                f'(y/n) [{"y" if cur else "n"}]: ').strip().lower()
    return {'redownload_flagged': (raw == 'y') if raw in ('y', 'n') else cur}

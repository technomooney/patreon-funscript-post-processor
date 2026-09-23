"""Per-creator behavior flags, stored as a plain hand-editable JSON file in
the root of the creator folder itself: <creator folder>/creator_config.json
(e.g. .../Patreon/Pize/creator_config.json).

Everything here is explicit opt-in. A missing file, or a missing key, means
"behave exactly as before this file existed" -- no script ever writes this
file on its own; it's only set by hand, by setup_unattended.py ('sa'), or by
the creator-scripts submenu's "Configure creator flags" entry, all at the
user's explicit request. In particular, having a creator script for a
creator does NOT imply any of these (MDemaxis's naming-fix plugin, for one,
has nothing to do with downloading).

Keys:
  download_script        id of a creator script (one declaring
                         REPLACES_DOWNLOAD = True) that runs *instead of*
                         the normal download step for this creator. The
                         normal download/extract then only runs when forced.
  force_normal_download  true = unattended runs use the normal download
                         anyway, despite download_script.
  sync_exclude           kinds ("video", "funscript", "archive") and/or
                         extensions (".mp4", ...) sync_new_folders must never
                         copy into this creator's folders.
  protected_dirs         subfolders (relative to the creator folder) every
                         core tree-walking script must skip entirely, e.g.
                         ["_pixeldrain"] -- owned by a creator script, never
                         renamed/deduped/consolidated by core tools.
"""
import json
import os

FILENAME = 'creator_config.json'

DEFAULTS: dict = {
    'download_script': None,
    'force_normal_download': False,
    'sync_exclude': [],
    'protected_dirs': [],
}

# Same set as downloadContent._VIDEO_EXTENSIONS (not imported: this module
# must stay cheap to import from every tree walker).
_VIDEO_EXTS = ('.mp4', '.m4v', '.mkv', '.webm', '.avi', '.mov', '.wmv',
               '.flv', '.ts', '.m2ts', '.mts', '.mpg', '.mpeg', '.3gp')
_KIND_EXTS = {
    'video': _VIDEO_EXTS,
    'funscript': ('.funscript',),
    'archive': ('.rar', '.zip', '.7z'),
}

# How far up from a scanned path to look for a creator_config.json -- scripts
# are normally pointed at the creator folder itself, but some get a single
# post folder inside it.
_MAX_PARENT_LEVELS = 3


def find_root(path: str) -> str | None:
    """The folder holding the creator_config.json that applies to *path*
    (*path* itself or up to a few parents), or None if there isn't one."""
    cur = os.path.abspath(path)
    for _ in range(_MAX_PARENT_LEVELS + 1):
        if os.path.isfile(os.path.join(cur, FILENAME)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _read(root: str) -> dict:
    try:
        with open(os.path.join(root, FILENAME), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        print(f'  [creator_config] could not read {os.path.join(root, FILENAME)} — using defaults')
        return {}


def load(path: str) -> dict:
    """The effective flags for *path* -- DEFAULTS overlaid with whatever
    creator_config.json applies (see find_root). Always a full dict."""
    cfg = dict(DEFAULTS)
    root = find_root(path)
    if root is not None:
        cfg.update(_read(root))
    return cfg


def load_raw(creator_folder: str) -> dict:
    """Exactly what's in *creator_folder*/creator_config.json ({} if absent)
    -- for editing, so saving doesn't write every default back out."""
    if not os.path.isfile(os.path.join(creator_folder, FILENAME)):
        return {}
    return _read(creator_folder)


def save(creator_folder: str, cfg: dict) -> None:
    path = os.path.join(creator_folder, FILENAME)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, path)


def get(path: str, key: str, default=None):
    value = load(path).get(key)
    return default if value is None else value


def download_script(path: str) -> str | None:
    return load(path).get('download_script') or None


# ---------------------------------------------------------------------------
# protected_dirs
# ---------------------------------------------------------------------------

def protected_paths(path: str) -> set[str]:
    """Absolute, normalized paths of every protected_dirs entry that applies
    to *path*."""
    root = find_root(path)
    if root is None:
        return set()
    dirs = _read(root).get('protected_dirs') or []
    return {os.path.normpath(os.path.join(root, d)) for d in dirs if isinstance(d, str) and d.strip()}


def prune(protected: set[str], root: str, dirs: list[str]) -> None:
    """For use inside an os.walk loop, same spot the '.trash' pruning goes:
    drop any subdirectory of *root* that's a protected path, in place."""
    if not protected:
        return
    dirs[:] = [d for d in dirs if os.path.normpath(os.path.abspath(os.path.join(root, d))) not in protected]


def is_protected(protected: set[str], path: str) -> bool:
    """True if *path* is, or is inside, a protected directory."""
    p = os.path.normpath(os.path.abspath(path))
    return any(p == d or p.startswith(d + os.sep) for d in protected)


# ---------------------------------------------------------------------------
# sync_exclude
# ---------------------------------------------------------------------------

def sync_excluded_exts(path: str) -> tuple[str, ...]:
    """Lowercase extensions sync_new_folders must not copy for *path*'s
    creator. () when nothing is excluded (the default)."""
    exts: list[str] = []
    for item in load(path).get('sync_exclude') or []:
        if not isinstance(item, str):
            continue
        item = item.strip().lower()
        if item in _KIND_EXTS:
            exts.extend(_KIND_EXTS[item])
        elif item.startswith('.'):
            exts.append(item)
    return tuple(exts)


def is_sync_excluded(filename: str, excluded_exts: tuple[str, ...]) -> bool:
    return bool(excluded_exts) and filename.lower().endswith(excluded_exts)

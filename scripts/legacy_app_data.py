"""One-time, non-destructive carry-over of app data that used to live in this repo's directory.

Creator profiles, password history, the undo pointer and the logged-in browser profiles used to sit next to the
code (gitignored). They now live in the user's data directory (app_paths). These helpers COPY the old data the
first time the new location is empty; the old files are never moved or deleted (the user can remove them once
they are happy). Standard library only.
"""
import os
import shutil

import app_paths

# The repository root: the folder that holds scripts/.
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def legacy_path(name: str) -> str:
    return os.path.join(REPO_DIR, name)


def copy_file_once(legacy_name: str, dest: str) -> bool:
    """Copy <repo>/<legacy_name> to *dest* if the destination does not exist yet. True if it copied."""
    src = legacy_path(legacy_name)
    if os.path.exists(dest) or not os.path.isfile(src):
        return False
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        return True
    except OSError as e:
        print(f'  [app data] could not copy {src} -> {dest}: {e}')
        return False


def profile_dir(name: str, legacy_dirname: str) -> str:
    """Persistent browser-profile directory <app home>/profiles/<name>, created on demand.

    The first time it is needed, a profile that exists at <repo>/<legacy_dirname> is copied in so the user stays
    logged in. Chromium's Singleton* lock files are skipped (they point at a long-dead process)."""
    dest = os.path.join(app_paths.app_home(), 'profiles', name)
    src = legacy_path(legacy_dirname)
    if not os.path.exists(dest) and os.path.isdir(src):
        try:
            shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns('Singleton*'))
            print(f'  [app data] copied browser profile {legacy_dirname} -> {dest} (the old folder is untouched)')
        except (OSError, shutil.Error) as e:
            print(f'  [app data] could not copy browser profile {src}: {e} -- starting a fresh one')
    os.makedirs(dest, exist_ok=True)
    return dest

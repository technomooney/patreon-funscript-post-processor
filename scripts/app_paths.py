"""Machine-local storage for per-user app data (creator profiles, password history, browser profiles, ...).

Why this exists: that data used to sit in the tool's own repo directory, so it was tied to one checkout and
had to be gitignored. It belongs in the user's own data directory instead, shared by every tool built on
library_db (the downloader, the AV1 compressor, future tools).

    Linux/macOS: $XDG_DATA_HOME/library-tools   (default ~/.local/share/library-tools)
    Windows:     %LOCALAPPDATA%\\library-tools
    Override:    LIBRARY_TOOLS_HOME=<dir>        (tests, portable installs)

Standard library only; this file is kept byte-identical in the sibling AV1 compressor repo.
"""
import os
import sys

APP_DIRNAME = 'library-tools'
APP_DB_NAME = 'app.db'


def app_home() -> str:
    """Absolute path of the per-user data directory, created on demand."""
    override = os.getenv('LIBRARY_TOOLS_HOME', '').strip()
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    elif sys.platform == 'win32':
        base = os.getenv('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
        path = os.path.join(base, APP_DIRNAME)
    else:
        base = os.getenv('XDG_DATA_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'share')
        path = os.path.join(base, APP_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def app_db_path() -> str:
    return os.path.join(app_home(), APP_DB_NAME)


def profiles_dir(name: str = '') -> str:
    """Directory for persistent browser profiles (<app home>/profiles[/name]), created on demand."""
    path = os.path.join(app_home(), 'profiles', name) if name else os.path.join(app_home(), 'profiles')
    os.makedirs(path, exist_ok=True)
    return path

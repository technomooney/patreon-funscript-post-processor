#!/usr/bin/env python3
"""
Interactive setup for runtime settings and credentials.
Run this once (or again to change a value).

Settings are written to .env.
Secrets are stored in the OS keyring (Windows Credential Manager, macOS
Keychain, or Linux Secret Service); if the keyring is unavailable they
fall back to .env.

Press Enter at any prompt to keep the value shown in brackets.
"""
import getpass
import os
import sys

SERVICE = 'patreon-funscript-video-downloader'
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def _read_env(key: str) -> str:
    """Return the current value of *key* from .env, or '' if absent."""
    if not os.path.exists(_ENV_PATH):
        return ''
    with open(_ENV_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith(f'{key}='):
                return line[len(key) + 1:].rstrip('\n')
    return ''


def _write_env(key: str, value: str, comment: str = '') -> None:
    """Write or update *key*=*value* in .env, preserving all other content."""
    lines: list[str] = []
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    for i, line in enumerate(lines):
        if line.startswith(f'{key}='):
            lines[i] = f'{key}={value}\n'
            break
    else:
        if lines and not lines[-1].endswith('\n'):
            lines.append('\n')
        if comment:
            lines.append(f'# {comment}\n')
        lines.append(f'{key}={value}\n')

    with open(_ENV_PATH, 'w', encoding='utf-8') as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Keyring helpers
# ---------------------------------------------------------------------------

try:
    import keyring as _keyring
    _KEYRING_OK = True
except ImportError:
    _KEYRING_OK = False


def _keyring_get(key: str) -> str:
    if _KEYRING_OK:
        try:
            return _keyring.get_password(SERVICE, key) or ''
        except Exception:
            pass
    return _read_env(key)


def _keyring_set(key: str, value: str) -> None:
    if _KEYRING_OK:
        try:
            _keyring.set_password(SERVICE, key, value)
            return
        except Exception as e:
            print(f'  WARNING: keyring unavailable ({e}) — saving to .env instead.')
    _write_env(key, value)


# ---------------------------------------------------------------------------
# Prompt helpers  (press Enter to keep the value shown in brackets)
# ---------------------------------------------------------------------------

def _ask(label: str, current: str) -> str:
    """Prompt with the current value in brackets; Enter keeps it."""
    hint = f'[{current}]' if current else '[not set]'
    value = input(f'  {label} {hint}: ').strip()
    return value if value else current


def _ask_secret(label: str, current: str) -> str:
    """Like _ask but hides input; shows [set] or [not set] instead of the value."""
    hint = '[set — Enter to keep]' if current else '[not set]'
    value = getpass.getpass(f'  {label} {hint}: ')
    return value if value else current


def _ask_bool(label: str, current: bool) -> bool:
    """Prompt for true/false; Enter keeps the current value."""
    hint = 'true' if current else 'false'
    raw = input(f'  {label} [{hint}]: ').strip().lower()
    if raw in ('true', 'yes', '1'):
        return True
    if raw in ('false', 'no', '0'):
        return False
    return current


# ---------------------------------------------------------------------------
# Main setup
# ---------------------------------------------------------------------------

def main():
    print()
    print('==============================================')
    print('  Setup  (press Enter to keep current values)')
    print('==============================================')

    # -------------------------------------------------------------------------
    # Settings  (written to .env)
    # -------------------------------------------------------------------------
    print()
    print('--- Settings ---')

    headless = _ask_bool(
        'Run browser in headless mode? (true/false)',
        current=_read_env('BROWSER_HEADLESS').lower() not in ('', 'false', '0', 'no'),
    )
    _write_env('BROWSER_HEADLESS', 'true' if headless else 'false',
               comment='Run the browser in headless mode. Set to false if sites block automation.')

    while True:
        raw = _ask('Max download resolution (e.g. 1080, 720, 2160)',
                   current=_read_env('MAX_RESOLUTION') or '1080')
        try:
            if int(raw) > 0:
                _write_env('MAX_RESOLUTION', raw,
                           comment='Maximum resolution to download. Downloads highest quality up to this value.')
                break
        except ValueError:
            pass
        print('  Please enter a positive integer (e.g. 1080).')

    dedup = _ask_bool(
        'Scan for duplicate videos on startup and remove extras? (true/false)',
        current=_read_env('DEDUP_EXISTING').lower() not in ('false', '0', 'no'),
    )
    _write_env('DEDUP_EXISTING', 'true' if dedup else 'false',
               comment='Remove duplicate video files on startup. Set to false to skip (faster on large libraries).')

    # -------------------------------------------------------------------------
    # Credentials  (keyring, with .env fallback)
    # -------------------------------------------------------------------------
    print()
    print('--- Credentials ---')

    print()
    print('Pixeldrain  (API key at https://pixeldrain.com/user/api)')
    print('  Leave blank to download as anonymous (public files only).')
    api_key = _ask('API key', current=_keyring_get('PIXELDRAIN_API_KEY'))
    if api_key:
        _keyring_set('PIXELDRAIN_API_KEY', api_key)

    print()
    print('iwara.tv  (required for 18+ content; leave blank to skip)')
    email = _ask('Email', current=_keyring_get('IWARA_EMAIL'))
    if email:
        _keyring_set('IWARA_EMAIL', email)
        password = _ask_secret('Password', current=_keyring_get('IWARA_PASSWORD'))
        if password:
            _keyring_set('IWARA_PASSWORD', password)
    else:
        print('  Skipping iwara.tv.')

    print()
    print('mega.nz  (only needed for private/account links; leave blank to skip)')
    mega_email = _ask('Email', current=_keyring_get('MEGA_EMAIL'))
    if mega_email:
        _keyring_set('MEGA_EMAIL', mega_email)
        mega_password = _ask_secret('Password', current=_keyring_get('MEGA_PASSWORD'))
        if mega_password:
            _keyring_set('MEGA_PASSWORD', mega_password)
    else:
        print('  Skipping mega.nz.')

    print()
    print('spankbang.com  (required for all downloads; leave blank to skip)')
    sb_user = _ask('Username', current=_keyring_get('SPANKBANG_USERNAME'))
    if sb_user:
        _keyring_set('SPANKBANG_USERNAME', sb_user)
        sb_pass = _ask_secret('Password', current=_keyring_get('SPANKBANG_PASSWORD'))
        if sb_pass:
            _keyring_set('SPANKBANG_PASSWORD', sb_pass)
    else:
        print('  Skipping spankbang.com.')

    print()
    print('Done.')
    print(f'  .env: {_ENV_PATH}')
    print()


if __name__ == '__main__':
    main()

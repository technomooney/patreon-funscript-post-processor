#!/usr/bin/env python3
"""
Interactive setup for credentials and runtime settings.

Secrets (passwords, API keys) are stored in the OS keyring (Windows Credential
Manager, macOS Keychain, or Linux Secret Service) so they are never written to
disk as plain text.  If the keyring is unavailable the value is written to .env
as a fallback.

Non-secret settings (headless mode, resolution cap, dedup) are written directly
to the .env file alongside the project.
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
    """Return the current value of *key* from .env, or '' if not set."""
    if not os.path.exists(_ENV_PATH):
        return ''
    with open(_ENV_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if line.startswith(f'{key}='):
                return line[len(key) + 1:]
    return ''


def _update_env(key: str, value: str, comment: str = '') -> None:
    """Write or update *key*=*value* in .env, preserving all other content.

    If the key is not already present it is appended (with an optional comment
    on the preceding line).  The file is created if it does not exist.
    """
    lines: list[str] = []
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    found = False
    for i, line in enumerate(lines):
        if line.startswith(f'{key}='):
            lines[i] = f'{key}={value}\n'
            found = True
            break

    if not found:
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
    import keyring
    _KEYRING_OK = True
except ImportError:
    _KEYRING_OK = False
    print('WARNING: keyring package not installed — credentials will be stored in .env.')


def _keyring_get(key: str) -> str:
    if not _KEYRING_OK:
        return _read_env(key)
    try:
        return keyring.get_password(SERVICE, key) or ''
    except Exception:
        return _read_env(key)


def _keyring_set(key: str, value: str) -> None:
    """Save *value* to the OS keyring; fall back to .env if unavailable."""
    if not _KEYRING_OK:
        _update_env(key, value)
        print(f'  Saved {key} to .env (keyring not available).')
        return
    try:
        keyring.set_password(SERVICE, key, value)
    except Exception as e:
        print(f'  WARNING: keyring unavailable ({e}) — saving to .env as fallback.')
        _update_env(key, value)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _prompt_secret(label: str, key: str) -> str:
    """Prompt for a secret; press Enter to keep the existing stored value."""
    current = _keyring_get(key)
    hint = '[currently set — press Enter to keep]' if current else '[not set]'
    value = getpass.getpass(f'  {label} {hint}: ')
    return value if value else current


def _prompt_plain(label: str, key: str, default: str = '') -> str:
    """Prompt for a plain-text value; press Enter to keep / use default."""
    current = _keyring_get(key) or _read_env(key) or default
    hint = f'[{current}]' if current else '[not set]'
    value = input(f'  {label} {hint}: ').strip()
    return value if value else current


def _prompt_env(label: str, key: str, default: str = '', comment: str = '') -> str:
    """Prompt for a .env setting; press Enter to keep / use default."""
    current = _read_env(key) or default
    hint = f'[{current}]' if current else '[not set]'
    value = input(f'  {label} {hint}: ').strip()
    chosen = value if value else current
    _update_env(key, chosen, comment=comment)
    return chosen


def _prompt_bool_env(label: str, key: str, default: bool = True, comment: str = '') -> bool:
    """Prompt for a true/false .env setting."""
    current_str = _read_env(key)
    if current_str:
        current = current_str.lower() not in ('false', '0', 'no')
    else:
        current = default
    hint = 'true' if current else 'false'
    value = input(f'  {label} [{hint}] (true/false): ').strip().lower()
    if value in ('true', 'yes', '1'):
        chosen = True
    elif value in ('false', 'no', '0'):
        chosen = False
    else:
        chosen = current   # keep existing on Enter
    _update_env(key, 'true' if chosen else 'false', comment=comment)
    return chosen


# ---------------------------------------------------------------------------
# Setup sections
# ---------------------------------------------------------------------------

def setup_credentials():
    # --- Pixeldrain ----------------------------------------------------------
    print('Pixeldrain')
    print('  API key found at https://pixeldrain.com/user/api')
    print('  Leave blank to download as anonymous (public files only).')
    api_key = _prompt_plain('API key:', 'PIXELDRAIN_API_KEY')
    if api_key:
        _keyring_set('PIXELDRAIN_API_KEY', api_key)

    # --- iwara.tv ------------------------------------------------------------
    print()
    print('iwara.tv  (required for 18+ content; leave blank to skip)')
    email = _prompt_plain('Email:', 'IWARA_EMAIL')
    if email:
        _keyring_set('IWARA_EMAIL', email)
        password = _prompt_secret('Password:', 'IWARA_PASSWORD')
        if password:
            _keyring_set('IWARA_PASSWORD', password)
    else:
        print('  Skipping iwara.tv credentials.')

    # --- mega.nz -------------------------------------------------------------
    print()
    print('mega.nz  (only needed for private/account links; leave blank to skip)')
    mega_email = _prompt_plain('Email:', 'MEGA_EMAIL')
    if mega_email:
        _keyring_set('MEGA_EMAIL', mega_email)
        mega_password = _prompt_secret('Password:', 'MEGA_PASSWORD')
        if mega_password:
            _keyring_set('MEGA_PASSWORD', mega_password)
    else:
        print('  Skipping mega.nz credentials.')

    # --- spankbang.com -------------------------------------------------------
    print()
    print('spankbang.com  (required for all downloads; leave blank to skip)')
    sb_username = _prompt_plain('Username:', 'SPANKBANG_USERNAME')
    if sb_username:
        _keyring_set('SPANKBANG_USERNAME', sb_username)
        sb_password = _prompt_secret('Password:', 'SPANKBANG_PASSWORD')
        if sb_password:
            _keyring_set('SPANKBANG_PASSWORD', sb_password)
    else:
        print('  Skipping spankbang.com credentials.')


def setup_env_settings():
    print()
    print('Runtime settings  (written to .env)')
    print()

    _prompt_bool_env(
        'Run browser in headless mode?',
        'BROWSER_HEADLESS',
        default=False,
        comment='Run the browser in headless mode (no visible window). Set to false if sites block automation.',
    )

    # MAX_RESOLUTION: validate it is a positive integer.
    while True:
        current = _read_env('MAX_RESOLUTION') or '1080'
        value = input(f'  Maximum download resolution (e.g. 720, 1080, 2160) [{current}]: ').strip()
        chosen = value if value else current
        try:
            if int(chosen) > 0:
                _update_env(
                    'MAX_RESOLUTION', chosen,
                    comment='Maximum resolution to download (e.g. 1080, 720, 2160). Downloads the highest quality available up to this value.',
                )
                break
        except ValueError:
            pass
        print('  Please enter a positive integer (e.g. 1080).')

    _prompt_bool_env(
        'Scan for duplicate videos on startup and remove extras?',
        'DEDUP_EXISTING',
        default=True,
        comment='Scan existing videos for duplicates on startup and keep only one copy. Set to false to skip (faster startup on large libraries).',
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print()
    print('========================================')
    print('  Setup')
    print('  Secrets → OS keyring (or .env fallback)')
    print('  Settings → .env')
    print('========================================')
    print()

    setup_credentials()
    setup_env_settings()

    print()
    print('Setup complete.')
    print(f'  .env location: {_ENV_PATH}')
    print()


if __name__ == '__main__':
    main()

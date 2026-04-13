#!/usr/bin/env python3
"""
Interactive credential setup.
Stores secrets in the OS keyring (Windows Credential Manager, macOS Keychain,
or Linux Secret Service) so they are never written to disk as plain text.
"""
import getpass
import sys

SERVICE = 'patreon-downloader'

try:
    import keyring
except ImportError:
    print('ERROR: keyring package not installed. Run setup first.')
    sys.exit(1)


def _current(key: str) -> str:
    try:
        return keyring.get_password(SERVICE, key) or ''
    except Exception:
        return ''


def _prompt(label: str, key: str, secret: bool = False) -> str:
    """Prompt for a value; press Enter to keep the existing stored value."""
    current = _current(key)
    if secret:
        hint = '[currently set — press Enter to keep]' if current else '[not set]'
        value = getpass.getpass(f'  {label} {hint}: ')
    else:
        hint = f'[{current}]' if current else '[not set]'
        value = input(f'  {label} {hint}: ').strip()
    return value if value else current


def main():
    print()
    print('========================================')
    print('  Credential Setup')
    print('  Secrets are stored in your OS keyring,')
    print('  not written to any file on disk.')
    print('========================================')
    print()

    # --- Pixeldrain ------------------------------------------------------
    print('Pixeldrain')
    print('  API key found at https://pixeldrain.com/user/api')
    print('  Leave blank to download as anonymous (public files only).')
    api_key = _prompt('API key:', 'PIXELDRAIN_API_KEY')
    try:
        keyring.set_password(SERVICE, 'PIXELDRAIN_API_KEY', api_key)
    except Exception as e:
        print(f'  WARNING: could not save to keyring: {e}')
        print('  Set PIXELDRAIN_API_KEY in .env as a fallback.')

    # --- iwara.tv --------------------------------------------------------
    print()
    print('iwara.tv  (required for 18+ content; leave blank to skip)')
    email = _prompt('Email:', 'IWARA_EMAIL')
    if email:
        try:
            keyring.set_password(SERVICE, 'IWARA_EMAIL', email)
        except Exception as e:
            print(f'  WARNING: could not save to keyring: {e}')
            print('  Set IWARA_EMAIL in .env as a fallback.')

        password = _prompt('Password:', 'IWARA_PASSWORD', secret=True)
        if password:
            try:
                keyring.set_password(SERVICE, 'IWARA_PASSWORD', password)
            except Exception as e:
                print(f'  WARNING: could not save to keyring: {e}')
                print('  Set IWARA_PASSWORD in .env as a fallback.')
    else:
        print('  Skipping iwara.tv credentials.')

    print()
    print('Credentials saved.')
    print()


if __name__ == '__main__':
    main()

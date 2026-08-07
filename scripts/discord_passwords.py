"""Fetch mega.nz passwords posted in a creator's Discord — via plain browser
automation of the normal Discord web app. No bot account, no self-bot/user
token: the user explicitly does not want their Discord account automated in
either of those forms (see project_discord_password_plan memory). Instead
this drives the same kind of Selenium session used elsewhere in this project
to a page the user is (or logs into, once, by hand) already logged into, and
reads the password off the rendered message the way a person would.

Per-creator Discord locations (guild/channel IDs) live in .creators.json,
managed by creator_profiles.py — not here.

Standalone usage:
    python scripts/discord_passwords.py set-channel <creator> <guild_id> <channel_id>
    python scripts/discord_passwords.py fetch <creator>
    python scripts/discord_passwords.py list
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import creator_profiles
import downloadContent as dc

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Separate from the main automation profile (used for Mega/Iwara/SpankBang)
# because it needs to *keep* its login between runs — the whole point is the
# user logs into Discord once by hand and every later run reuses that
# session, instead of us ever touching Discord credentials ourselves.
_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.discord_browser_profile'
)

# Same keyword-anchored shape as _extract_mega_password in downloadContent.py
# (pw/pass/password, optional colon or equals) — passwords posted in Discord
# tend to follow the same "PW: xxxx" convention creators use in post text.
_PASSWORD_RE = re.compile(r'(?:pw|pass(?:word)?)\s*[:=]?\s*([^\s,;]+)', re.IGNORECASE)

_driver = None  # module-level singleton so repeated lookups in one run share one login session


def _discord_driver():
    """Return a persistent-profile browser dedicated to Discord.

    Always windowed, regardless of BROWSER_HEADLESS — the first login (and
    any occasional re-verification Discord asks for) needs a real window to
    interact with.
    """
    global _driver
    if _driver is not None:
        return _driver

    os.makedirs(_PROFILE_DIR, exist_ok=True)
    browser = dc._find_browser()
    if browser is None:
        raise RuntimeError(
            'Brave Browser not found. Install it from https://brave.com — or install Chromium as a fallback.'
        )
    version = dc._get_browser_major_version(browser)
    launch_browser = dc._stripped_test_type_browser(browser)
    cached_driver = dc._cached_chromedriver(version) if version else None

    options = uc.ChromeOptions()
    kwargs = dict(
        options=options,
        browser_executable_path=launch_browser,
        version_main=version,
        user_data_dir=_PROFILE_DIR,
    )
    if cached_driver:
        kwargs['driver_executable_path'] = cached_driver

    _driver = uc.Chrome(**kwargs)
    return _driver


def _is_logged_in(driver) -> bool:
    url = driver.current_url
    return '/login' not in url and '/register' not in url


def ensure_login(driver, timeout_minutes: int = 5) -> bool:
    """Navigate to Discord; if not already logged in, wait for the user to log in by hand.

    Returns True once logged in (immediately, or after the user finishes),
    False if the wait timed out.
    """
    driver.get('https://discord.com/channels/@me')
    if _is_logged_in(driver):
        return True

    print('  [discord] not logged in — log in in the browser window that just opened.')
    print(f'  [discord] waiting up to {timeout_minutes} minutes for login...')
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        if _is_logged_in(driver):
            print('  [discord] logged in.')
            return True
        time.sleep(2)
    print('  [discord] timed out waiting for login.')
    return False


def fetch_latest_password(creator_key: str, max_messages: int = 50) -> str | None:
    """Return the most recent password posted in *creator_key*'s configured Discord channel, or None."""
    profile = creator_profiles.get(creator_key)
    discord_cfg = profile.get('discord')
    if not discord_cfg or not discord_cfg.get('guild_id') or not discord_cfg.get('channel_id'):
        print(f'  [discord] no Discord channel configured for "{creator_key}" '
              f'— run: python scripts/discord_passwords.py set-channel {creator_key} <guild_id> <channel_id>')
        return None

    driver = _discord_driver()
    if not ensure_login(driver):
        return None

    url = f"https://discord.com/channels/{discord_cfg['guild_id']}/{discord_cfg['channel_id']}"
    driver.get(url)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, '//*[@data-list-id="chat-messages"]'))
        )
    except Exception:
        print(f'  [discord] channel did not load: {url}')
        return None

    # Messages render newest-at-bottom; scan from the end for the first match.
    messages = driver.find_elements(
        By.XPATH, '//*[@data-list-id="chat-messages"]//*[@id and contains(@id, "message-content-")]'
    )
    for el in reversed(messages[-max_messages:]):
        text = el.text or ''
        match = _PASSWORD_RE.search(text)
        if match:
            password = match.group(1).strip()
            print(f'  [discord] found password for "{creator_key}" in channel {discord_cfg["channel_id"]}')
            return password

    print(f'  [discord] no password-looking message found in the last {max_messages} '
          f'messages of channel {discord_cfg["channel_id"]}')
    return None


def close() -> None:
    """Quit the shared Discord driver, if one was launched."""
    global _driver
    if _driver is not None:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description='Discord password fetching — standalone test/setup CLI')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_set = sub.add_parser('set-channel', help="Record a creator's Discord password channel")
    p_set.add_argument('creator')
    p_set.add_argument('guild_id')
    p_set.add_argument('channel_id')
    p_set.add_argument('--note', default='')

    p_fetch = sub.add_parser('fetch', help='Fetch the latest password for a creator')
    p_fetch.add_argument('creator')

    sub.add_parser('list', help='List configured creators')

    args = parser.parse_args()
    if args.cmd == 'set-channel':
        creator_profiles.set_discord_channel(args.creator, args.guild_id, args.channel_id, args.note)
        print(f'Saved Discord channel for "{args.creator}".')
    elif args.cmd == 'fetch':
        try:
            password = fetch_latest_password(args.creator)
            print(f'Password: {password}' if password else 'No password found.')
        finally:
            close()
    elif args.cmd == 'list':
        profiles = creator_profiles.load()
        if not profiles:
            print('No creators configured yet.')
        for key, profile in profiles.items():
            print(f'{key}: {profile.get("discord") or "(no discord config)"}')


if __name__ == '__main__':
    _main()

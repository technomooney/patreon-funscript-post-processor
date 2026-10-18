"""Browser access to Discord: persistent logged-in profile, one-time manual login, open a creator's channel.

No bot account, no self-bot/user token -- the user does not want their Discord account automated in either of
those forms. This drives the same kind of Selenium session used elsewhere in this project to a page the user is
(or logs into, once, by hand) already logged into, and reads messages off the rendered page.

This module only gets you INTO a channel. What is done with the messages is elsewhere: discord_history.py reads
and stores the whole history, discord_pixeldrain_sync.py turns its links into downloads, discord_passwords.py
parses archive passwords out of message text. Per-creator guild/channel IDs live in creator_profiles.py.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import creator_profiles
import downloadContent as dc
import legacy_app_data

import undetected_chromedriver as uc
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Separate from the main automation profile (used for Mega/Iwara/SpankBang) because it needs to *keep* its login
# between runs. Lives in the machine-local app data directory (app_paths); a profile that still sits at the old
# repo-root .discord_browser_profile is copied there once so the existing login carries over.
def _profile_dir() -> str:
    return legacy_app_data.profile_dir('discord', '.discord_browser_profile')


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

    profile_dir = _profile_dir()
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
        user_data_dir=profile_dir,
    )
    if cached_driver:
        kwargs['driver_executable_path'] = cached_driver

    _driver = uc.Chrome(**kwargs)
    return _driver


# Multiple fallback selectors for the logged-in app shell — Discord's DOM
# markup shifts between client versions, so leaning on one attribute is
# fragile. Any one of these present means a real session is active.
_LOGGED_IN_XPATHS = (
    '//*[@data-list-id="guildsnav"]',
    '//nav[@aria-label="Servers sidebar"]',
    '//*[@aria-label="Servers sidebar"]',
)


def _is_logged_in(driver) -> bool:
    """Check for the actual logged-in app UI, not just the URL.

    Right after driver.get(), the URL is still whatever we navigated to —
    Discord's client-side router needs a moment to check auth state and
    redirect to /login if there's no session. Checking current_url alone
    immediately after navigation is a race: it reads as "logged in" on a
    completely fresh, never-logged-in profile before the redirect happens.
    The guild sidebar only ever renders once the client has a real session.
    """
    if '/login' in driver.current_url or '/register' in driver.current_url:
        return False
    return any(driver.find_elements(By.XPATH, xpath) for xpath in _LOGGED_IN_XPATHS)


def ensure_login(driver, timeout_minutes: int = 20) -> bool:
    """Navigate to Discord; if not already logged in, wait for the user to log in by hand.

    Returns True once logged in (immediately, or after the user finishes),
    False if the wait timed out or the browser window was closed. 20 minutes
    by default and deliberately generous — this only ever has to happen
    *once*: the persistent profile (_profile_dir()) keeps the session for
    every run after this one, so there's no reason to rush it or automate
    it. No credentials or 2FA codes should ever be typed in by this script;
    login is the one part of this flow that stays entirely manual.
    """
    driver.get('https://discord.com/channels/@me')
    # Give the SPA a moment to hydrate and settle on either the login form
    # or the app UI before checking — see _is_logged_in for why this matters.
    try:
        WebDriverWait(driver, 15).until(
            lambda d: '/login' in d.current_url or any(
                d.find_elements(By.XPATH, xpath) for xpath in _LOGGED_IN_XPATHS
            )
        )
    except Exception:
        pass  # fall through to _is_logged_in / the wait loop below either way
    if _is_logged_in(driver):
        return True

    print('  [discord] not logged in — log in in the browser window that just opened.')
    print('  [discord] this is one-time only — future runs reuse this saved session.')
    print(f'  [discord] take your time, waiting up to {timeout_minutes} minutes...')
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        try:
            if _is_logged_in(driver):
                print('  [discord] logged in.')
                return True
        except WebDriverException:
            print('  [discord] browser window was closed — run fetch again when ready to log in.')
            return False
        time.sleep(2)
    print('  [discord] timed out waiting for login.')
    return False


def _looks_like_snowflake(value: str) -> bool:
    """Discord IDs (guild/channel) are numeric snowflakes, ~17-20 digits."""
    return value.isdigit() and 15 <= len(value) <= 21


def _prompt_setup(creator_key: str) -> dict | None:
    """Ask, right here, for *creator_key*'s Discord password channel and save
    it -- there's no separate global setup step for this, since it's a
    per-creator thing that's only ever needed the first time a password
    lookup for that creator actually falls through to Discord. Returns the
    saved discord config, or None if declined, not answerable (no TTY), or
    left incomplete -- every decline path also records set_discord_declined
    so _load_channel_messages stops asking on future speculative lookups
    (see its own docstring for the `force` escape hatch).
    """
    if not sys.stdin.isatty():
        return None
    print(f'  [discord] no Discord channel configured yet for "{creator_key}".')
    if input('  [discord] set one up now? (y/n): ').strip().lower() != 'y':
        creator_profiles.set_discord_declined(creator_key)
        print('  [discord] won\'t ask again for this creator unless a password is actually needed.')
        return None

    print('  [discord] needs Discord\'s Developer Mode on (User Settings > Advanced),')
    print('  [discord] then right-click the server icon / the channel > Copy Server ID / Copy Channel ID.')

    def _ask_id(label: str) -> str:
        while True:
            value = input(f'  [discord] {label}: ').strip()
            if not value:
                return ''
            if _looks_like_snowflake(value):
                return value
            print('  [discord] that doesn\'t look like a Discord ID (should be a plain number) — try again, '
                  'or leave blank to cancel.')

    guild_id = _ask_id('server (guild) ID')
    if not guild_id:
        creator_profiles.set_discord_declined(creator_key)
        print('  [discord] skipped — won\'t ask again unless a password is actually needed.')
        return None
    channel_id = _ask_id('channel ID')
    if not channel_id:
        creator_profiles.set_discord_declined(creator_key)
        print('  [discord] skipped — won\'t ask again unless a password is actually needed.')
        return None
    note = input('  [discord] note (optional, e.g. "mega password channel"): ').strip()

    creator_profiles.set_discord_channel(creator_key, guild_id, channel_id, note)
    print(f'  [discord] saved — "{creator_key}" now points at that channel.')
    return creator_profiles.get(creator_key).get('discord')


def open_channel(creator_key: str, force: bool = False):
    """Log in (once, by hand) and open *creator_key*'s configured Discord channel.

    Returns (driver, discord_cfg) with the channel's first batch of messages rendered, or None on any failure
    (no channel configured and not set up when asked, login declined/timed out, channel didn't load).
    Shared by the recent-messages reader here and the full-history reader (discord_history.py).

    *force*: re-prompt even if this creator previously declined setup
    (set_discord_declined). Speculative callers (e.g. "no inline password
    found, check Discord just in case" for a mega link that might not even
    need one) should leave this False so a declined creator is never asked
    again; a caller that already knows a password is genuinely needed (every
    known password failed against an actual archive) should pass True.
    """
    profile = creator_profiles.get(creator_key)
    discord_cfg = profile.get('discord')
    if not discord_cfg or not discord_cfg.get('guild_id') or not discord_cfg.get('channel_id'):
        if discord_cfg and discord_cfg.get('declined') and not force:
            return None
        discord_cfg = _prompt_setup(creator_key)
        if not discord_cfg:
            print(f'  [discord] no Discord channel configured for "{creator_key}" '
                  f'— run: python scripts/discord_browser.py set-channel {creator_key} <guild_id> <channel_id>')
            return None

    driver = _discord_driver()
    if not ensure_login(driver):
        return None

    message_xpath = '//*[@data-list-id="chat-messages"]//*[@id and contains(@id, "message-content-")]'
    url = f"https://discord.com/channels/{discord_cfg['guild_id']}/{discord_cfg['channel_id']}"
    try:
        driver.get(url)
        # Waiting for the chat-messages *container* alone isn't enough — it
        # mounts before Discord's virtualized list has actually rendered any
        # message children into it, so a find_elements() run right after can
        # legitimately see zero (or a partial batch of) messages depending on
        # timing (caught live: identical code returned 8 messages one run and
        # 0 the next). Wait for at least one message element specifically,
        # then let the count settle before trusting it's the full batch.
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, message_xpath))
        )
        stable_checks = 0
        last_count = -1
        for _ in range(20):  # ~6s max
            count = len(driver.find_elements(By.XPATH, message_xpath))
            if count == last_count:
                stable_checks += 1
                if stable_checks >= 2:
                    break
            else:
                stable_checks = 0
                last_count = count
            time.sleep(0.3)
    except WebDriverException:
        print('  [discord] browser window was closed before the channel could load.')
        return None
    except Exception:
        print(f'  [discord] channel did not load: {url}')
        return None

    return driver, discord_cfg


def _load_channel_messages(creator_key: str, max_messages: int, force: bool = False) -> tuple[list[str], dict] | None:
    """Return *creator_key*'s configured Discord channel's recent message texts (newest first) alongside the
    channel config, or None on any failure (see open_channel).

    *force*: see open_channel's callers -- re-prompt even if this creator previously declined setup.
    """
    opened = open_channel(creator_key, force)
    if opened is None:
        return None
    driver, discord_cfg = opened
    message_xpath = '//*[@data-list-id="chat-messages"]//*[@id and contains(@id, "message-content-")]'
    # Messages render newest-at-bottom; callers want newest-first.
    messages = driver.find_elements(By.XPATH, message_xpath)
    texts = [(el.text or '') for el in reversed(messages[-max_messages:])]
    return texts, discord_cfg


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
    parser = argparse.ArgumentParser(description='Discord channel setup')
    parser.add_argument('creator')
    parser.add_argument('guild_id')
    parser.add_argument('channel_id')
    parser.add_argument('--note', default='')
    args = parser.parse_args()
    creator_profiles.set_discord_channel(args.creator, args.guild_id, args.channel_id, args.note)
    print(f'Saved Discord channel for "{args.creator}".')


if __name__ == '__main__':
    try:
        _main()
    except KeyboardInterrupt:
        print('\n\nCancelled.')

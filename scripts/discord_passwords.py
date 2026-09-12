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
import creator_db
import creator_profiles
import downloadContent as dc

import undetected_chromedriver as uc
from selenium.common.exceptions import WebDriverException
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

# Discord messages (confirmed real example from Pize, 2026-08-02) read like
# prose, not the "PW: xxxx" convention _MEGA_PASSWORD_RE anchors on for post
# text: "The password is currently set as pize2026080101." — so this needs
# to swallow the connector words ("is"/"was", "currently", "set as"/"set to")
# between the keyword and the actual value, then still stop cleanly at
# trailing sentence punctuation (the value here butts right up against a
# period with no space). \b keeps it off substrings like "passed"/"bypass".
_PASSWORD_RE = re.compile(
    r'\b(?:pw|pass(?:word)?)\b'
    r'\s*(?:is|was)?'
    r'\s*(?:currently\s+)?'
    r'\s*(?:set\s+(?:as|to)\s*)?'
    r'[:=]?\s*'
    r'([^\s,;!.?]+)',
    re.IGNORECASE,
)


def _looks_like_password(value: str) -> bool:
    """Reject short/junk regex captures — a leftover connector word or stray
    punctuation the pattern above couldn't fully rule out structurally."""
    value = value.strip()
    return len(value) >= 4 and any(c.isalnum() for c in value)


# Some creators label each password with the post date range/month and post
# type ("Collection"/"standalone post") it applies to, instead of (or as well
# as) the plain "PW: xxxx" convention _PASSWORD_RE anchors on, e.g.:
#   "2026.08.16-2026.08.31 standalone post PW😀  2<[58I@@ihW2Ga"
# Several labels can also share one trailing value, full/half-width-comma
# separated, with only the last one carrying it:
#   "2026.02-07 standalone post PW，2026.02-07 Collection per month PW，
#    2026.09.01 standalone post PW😀  y7$X#mQ2!vK9*pB5@wT4"
# Illustrative examples given by the user, not yet confirmed against a real
# post (unlike _PASSWORD_RE's prose form) — kept deliberately best-effort:
# see creator_db.record_password_labels/get_password_history for how a
# mis-parsed or unmatched label just fails to help, never blocks anything.
# "PW[^\w\s,，]*" swallows an emoji glued onto "PW" without also eating past
# a following comma/digit into the next clause or the value itself.
_LABELED_PASSWORD_RE = re.compile(
    r'(?P<date>\d{4}(?:[.\-]\d{1,2}){0,2}\s*[-~]\s*(?:\d{4}[.\-])?\d{1,2}(?:[.\-]\d{1,2})?'
    r'|\d{4}(?:[.\-]\d{1,2}){0,2})'
    r'\s*(?P<kind>collection(?:\s+per\s+month)?|standalone(?:\s+post)?)?'
    r'[^\S\n]*PW[^\w\s,，]*'
    r'[^\S\n]*(?P<value>[^\s,，;]+)?',
    re.IGNORECASE,
)


def parse_labeled_passwords(text: str) -> list[dict]:
    """Parse zero or more {'date_label', 'kind', 'password'} entries out of
    one Discord message (see _LABELED_PASSWORD_RE above for the shapes
    handled). A label with no value of its own inherits the next label's
    value — the shared-trailing-password shape — so every label in a chain
    like that ends up mapped to the same password."""
    entries: list[dict] = []
    pending: list[tuple[str, str]] = []
    for m in _LABELED_PASSWORD_RE.finditer(text):
        kind = (m.group('kind') or '').lower()
        kind = 'collection' if kind.startswith('collection') else ('standalone' if kind else '')
        pending.append((m.group('date').strip(), kind))
        value = m.group('value')
        if value and _looks_like_password(value):
            pw = value.strip()
            for date_label, k in pending:
                entries.append({'date_label': date_label, 'kind': k, 'password': pw})
            pending = []
    return entries

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
    *once*: the persistent profile (_PROFILE_DIR) keeps the session for
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
    left incomplete.
    """
    if not sys.stdin.isatty():
        return None
    print(f'  [discord] no Discord channel configured yet for "{creator_key}".')
    if input('  [discord] set one up now? (y/n): ').strip().lower() != 'y':
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
        print('  [discord] skipped.')
        return None
    channel_id = _ask_id('channel ID')
    if not channel_id:
        print('  [discord] skipped.')
        return None
    note = input('  [discord] note (optional, e.g. "mega password channel"): ').strip()

    creator_profiles.set_discord_channel(creator_key, guild_id, channel_id, note)
    print(f'  [discord] saved — "{creator_key}" now points at that channel.')
    return creator_profiles.get(creator_key).get('discord')


def _load_channel_messages(creator_key: str, max_messages: int) -> tuple[list[str], dict] | None:
    """Navigate to *creator_key*'s configured Discord channel and return its recent
    message texts (newest first) alongside the channel config, or None on any
    failure (no channel configured and not set up when asked, login
    declined/timed out, channel didn't load)."""
    profile = creator_profiles.get(creator_key)
    discord_cfg = profile.get('discord')
    if not discord_cfg or not discord_cfg.get('guild_id') or not discord_cfg.get('channel_id'):
        discord_cfg = _prompt_setup(creator_key)
        if not discord_cfg:
            print(f'  [discord] no Discord channel configured for "{creator_key}" '
                  f'— run: python scripts/discord_passwords.py set-channel {creator_key} <guild_id> <channel_id>')
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

    # Messages render newest-at-bottom; callers want newest-first.
    messages = driver.find_elements(By.XPATH, message_xpath)
    texts = [(el.text or '') for el in reversed(messages[-max_messages:])]
    return texts, discord_cfg


def fetch_password_history(creator_key: str, max_messages: int = 50) -> list[str]:
    """Return every distinct password-looking value posted in *creator_key*'s
    channel within the last *max_messages* messages, most recent first.

    Unlike fetch_latest_password, this doesn't stop at the first match — some
    creators (confirmed: Pize) rotate the archive password over time, and an
    older archive isn't always re-encrypted under the newest one, so a caller
    that fails to extract with the latest password may still need to fall
    back through history. Results are persisted to creator_db so later runs
    (or extraction retries) have them without re-scanning Discord.
    """
    loaded = _load_channel_messages(creator_key, max_messages)
    if loaded is None:
        return []
    texts, discord_cfg = loaded

    passwords: list[str] = []
    labeled: list[dict] = []
    for text in texts:
        labeled.extend(parse_labeled_passwords(text))
        match = _PASSWORD_RE.search(text)
        if match and _looks_like_password(match.group(1)):
            pw = match.group(1).strip()
            if pw not in passwords:
                passwords.append(pw)
    for entry in labeled:
        if entry['password'] not in passwords:
            passwords.append(entry['password'])

    if passwords:
        print(f'  [discord] found {len(passwords)} distinct password(s) for "{creator_key}" '
              f'in channel {discord_cfg["channel_id"]}')
        creator_db.record_passwords(creator_key, passwords)
        if labeled:
            creator_db.record_password_labels(creator_key, labeled)
    else:
        print(f'  [discord] no password-looking message found in the last {max_messages} '
              f'messages of channel {discord_cfg["channel_id"]}')
    return passwords


def fetch_latest_password(creator_key: str, max_messages: int = 50) -> str | None:
    """Return the most recent password posted in *creator_key*'s configured Discord channel, or None."""
    history = fetch_password_history(creator_key, max_messages)
    return history[0] if history else None


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
    try:
        _main()
    except KeyboardInterrupt:
        print('\n\nCancelled.')
        close()

"""Browser access to Discord: persistent logged-in profile, one-time manual login, open a creator's channel.

No bot account, no self-bot/user token -- the user does not want their Discord account automated in either of
those forms. This drives the same kind of Selenium session used elsewhere in this project to a page the user is
(or logs into, once, by hand) already logged into, and reads messages off the rendered page.

Everything that touches the browser lives here (login, opening a channel, reading/scrolling it). What is done
with the messages is elsewhere: discord_history.py drives the reading and tracks how far it got, discord_store.py
stores, discord_parser.py extracts links/passwords, discord_passwords.py parses archive passwords. Per-creator guild/channel IDs live in creator_profiles.py.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_session
import creator_profiles
import downloadContent as dc
import legacy_app_data

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

# Separate from the main automation profile (used for Mega/Iwara/SpankBang) because it needs to *keep* its login
# between runs. Lives in the machine-local app data directory (app_paths); a profile that still sits at the old
# repo-root .discord_browser_profile is copied there once so the existing login carries over.
def _profile_dir() -> str:
    return legacy_app_data.profile_dir('discord', '.discord_browser_profile')


def _discord_driver():
    """The app's one browser (browser_session), with its own Discord tab in front.

    Discord shares the saved profile with the downloader and JOIGang -- a profile can only be open in one browser
    at a time -- so it borrows the browser the downloader already has open instead of launching another. Callers
    that read Discord inside a download run give the downloader's tab back with release() when done; close()
    drops the Discord tab. The first Discord login is still manual: it needs the (default) windowed browser.
    """
    return browser_session.use_tab('discord')


def release() -> None:
    """Hand the browser's front tab back to the downloader. Safe if Discord was never opened."""
    browser_session.back_to_main()


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


def _ensure_login(driver, timeout_minutes: int = 20) -> bool:
    """Navigate to Discord; if not already logged in, wait for the user to log in by hand.

    Returns True once logged in (immediately, or after the user finishes),
    False if the wait timed out or the browser window was closed. 20 minutes
    by default and deliberately generous — this only ever has to happen
    *once*: the persistent profile (_profile_dir()) keeps the session for
    every run after this one, so there's no reason to rush it or automate
    it. No credentials or 2FA codes should ever be typed in by this script;
    login is the one part of this flow that stays entirely manual.
    """
    driver.get(_HOME_URL)
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


def debug_dir() -> str:
    import app_paths
    path = os.path.join(app_paths.app_home(), 'discord_debug')
    os.makedirs(path, exist_ok=True)
    return path


def capture_debug(driver, label: str) -> str | None:
    """Save what the browser is showing (screenshot, URL, title, page-text snippet, console log) to
    <app home>/discord_debug/<time>_<label>.*, so a blank/gray Discord can be diagnosed. Never raises."""
    stem = os.path.join(debug_dir(), f'{time.strftime("%Y%m%d_%H%M%S")}_{label}')
    lines: list[str] = []

    def step(name, fn):
        try:
            lines.append(f'{name}: {fn()}')
        except Exception as e:      # a crashed tab raises on everything -- record that instead of giving up
            lines.append(f'{name}: FAILED ({type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ""})')

    step('url', lambda: driver.current_url)
    step('title', lambda: driver.title)
    step('html length', lambda: driver.execute_script('return document.documentElement.outerHTML.length'))
    step('page text', lambda: repr(driver.execute_script(
        'return document.body ? document.body.innerText.slice(0, 1500) : "(no body)"')))
    step('window handles', lambda: len(driver.window_handles))
    try:
        logs = driver.get_log('browser')
        lines += ['', f'console ({len(logs)} entries):'] + [f'{e.get("level")}: {e.get("message")}'[:400] for e in logs[-60:]]
    except Exception as e:
        lines.append(f'console log: FAILED ({type(e).__name__})')
    try:
        with open(stem + '.txt', 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        shot = ''
        try:
            driver.save_screenshot(stem + '.png')
            shot = ' / .png'
        except Exception:
            pass
        print(f'  [discord] saved what the browser shows to {stem}.txt{shot}')
        return stem
    except Exception:
        return None


SETTLE_SECONDS = float(os.getenv('DISCORD_SETTLE_SECONDS', '4'))
_HOME_URL = 'https://discord.com/channels/@me'


def settle(driver, seconds: float | None = None) -> None:
    """Give the Discord app time to finish loading after a navigation: the page itself, then a fixed pause.

    Discord's single-page app shows its sidebar well before it is ready to switch channels; jumping to a channel
    straight away is what makes the channel come up empty. Override the pause with DISCORD_SETTLE_SECONDS."""
    try:
        WebDriverWait(driver, 15).until(lambda d: d.execute_script('return document.readyState') == 'complete')
    except Exception:
        pass
    time.sleep(SETTLE_SECONDS if seconds is None else seconds)


def ensure_login(driver, timeout_minutes: int = 20) -> bool:
    """Start at Discord's home (@me), log in by hand if needed (see _ensure_login), then let the app settle there
    before anything navigates to a channel."""
    if not _ensure_login(driver, timeout_minutes):
        return False
    settle(driver)
    try:
        if len((driver.execute_script('return document.body ? document.body.innerText : ""') or '').strip()) < 20:
            print('  [discord] the Discord home page looks blank (gray screen).')
            capture_debug(driver, 'home_blank')
    except Exception:
        pass
    return True


def _looks_like_snowflake(value: str) -> bool:
    """Discord IDs (guild/channel) are numeric snowflakes, ~17-20 digits."""
    return value.isdigit() and 15 <= len(value) <= 21


def _prompt_setup(creator_key: str, channel: str | None = None) -> dict | None:
    """Ask, right here, for *creator_key*'s Discord password channel and save
    it -- there's no separate global setup step for this, since it's a
    per-creator thing that's only ever needed the first time a password
    lookup for that creator actually falls through to Discord. Returns the
    saved discord config, or None if declined, not answerable (no TTY), or
    left incomplete -- every decline path also records set_discord_declined
    so open_channel stops asking on future speculative lookups
    (see its own docstring for the `force` escape hatch).
    """
    if not sys.stdin.isatty():
        return None
    label = f'Discord "{channel}" channel' if channel else 'Discord channel'
    print(f'  [discord] no {label} configured yet for "{creator_key}".')
    if input('  [discord] set one up now? (y/n): ').strip().lower() != 'y':
        creator_profiles.set_discord_declined(creator_key, channel)
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
        creator_profiles.set_discord_declined(creator_key, channel)
        print('  [discord] skipped — won\'t ask again unless a password is actually needed.')
        return None
    channel_id = _ask_id('channel ID')
    if not channel_id:
        creator_profiles.set_discord_declined(creator_key, channel)
        print('  [discord] skipped — won\'t ask again unless a password is actually needed.')
        return None
    note = input('  [discord] note (optional, e.g. "mega password channel"): ').strip()

    creator_profiles.set_discord_channel(creator_key, guild_id, channel_id, note, channel=channel)
    print(f'  [discord] saved — "{creator_key}" now points at that channel.')
    return creator_profiles.get_discord(creator_key, channel)


_AGE_MARKERS = ('age-restricted', 'age restricted', 'nsfw', 'over 18', '18+')
_CONTINUE_XPATH = ("//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                   "'abcdefghijklmnopqrstuvwxyz'), 'continue')]")


def dismiss_age_gate(driver) -> bool:
    """If the page is Discord's age-restricted-channel prompt, press its Continue button. True if it clicked.

    Only acts when the page text says the channel is age-restricted, so an unrelated "Continue" button elsewhere
    is never pressed. Discord remembers the choice on the account, so this normally happens once per channel."""
    try:
        text = (driver.execute_script('return document.body ? document.body.innerText : ""') or '').lower()
        if not any(marker in text for marker in _AGE_MARKERS):
            return False
        for button in driver.find_elements(By.XPATH, _CONTINUE_XPATH):
            if button.is_displayed():
                driver.execute_script('arguments[0].click();', button)     # a JS click ignores overlays
                print('  [discord] age-restricted channel: pressed Continue.')
                return True
    except WebDriverException:
        raise
    except Exception:
        pass        # a stale element mid-navigation: the caller simply tries again
    return False


def open_channel(creator_key: str, force: bool = False, channel: str | None = None):
    """Log in (once, by hand) and open *creator_key*'s configured Discord channel.

    Returns (driver, discord_cfg) with the channel's first batch of messages rendered, or None on any failure
    (no channel configured and not set up when asked, login declined/timed out, channel didn't load).
    Shared by the recent-messages reader here and the full-history reader (discord_history.py).

    *channel*: None = the creator's original (password) channel; otherwise a named channel ('archive',
    'secrets', ...) from the creator profile -- a creator can have several.

    *force*: re-prompt even if this creator previously declined setup
    (set_discord_declined). Speculative callers (e.g. "no inline password
    found, check Discord just in case" for a mega link that might not even
    need one) should leave this False so a declined creator is never asked
    again; a caller that already knows a password is genuinely needed (every
    known password failed against an actual archive) should pass True.
    """
    discord_cfg = creator_profiles.get_discord(creator_key, channel)
    if not discord_cfg or not discord_cfg.get('guild_id') or not discord_cfg.get('channel_id'):
        if discord_cfg and discord_cfg.get('declined') and not force:
            return None
        discord_cfg = _prompt_setup(creator_key, channel)
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
        for attempt in (1, 2):
            driver.get(url)
            # Waiting for the chat-messages *container* alone isn't enough — it
            # mounts before Discord's virtualized list has actually rendered any
            # message children into it, so a find_elements() run right after can
            # legitimately see zero (or a partial batch of) messages depending on
            # timing (caught live: identical code returned 8 messages one run and
            # 0 the next). Wait for at least one message element specifically,
            # then let the count settle before trusting it's the full batch.
            # An age-restricted channel shows a "Continue" prompt instead of messages; dismiss it while waiting.
            deadline = time.time() + 20
            while not driver.find_elements(By.XPATH, message_xpath):
                if time.time() > deadline:
                    break
                dismiss_age_gate(driver)
                time.sleep(1)
            if driver.find_elements(By.XPATH, message_xpath):
                break
            capture_debug(driver, f'channel_attempt{attempt}')
            if attempt == 1:
                print('  [discord] channel did not come up -- going back to the home page and trying once more...')
                driver.get(_HOME_URL)
                settle(driver, SETTLE_SECONDS + 3)
        else:
            raise TimeoutError('no messages rendered')
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
        capture_debug(driver, 'channel_failed')
        print(f'  [discord] channel did not load: {url}')
        return None

    return driver, discord_cfg


# ---------------------------------------------------------------------------
# Reading the open channel (primitives; the loop that uses them is discord_history.collect_history)
# ---------------------------------------------------------------------------

# Read every message currently rendered in the channel. Discord's list is virtualized (messages far off-screen
# are dropped from the DOM), so this is called repeatedly while scrolling and the results merged by id.
READ_JS = r"""
const root = document.querySelector('[data-list-id="chat-messages"]');
if (!root) return [];
const rows = [];
let lastAuthor = '';
root.querySelectorAll('li[id^="chat-messages-"]').forEach(li => {
  const m = li.id.match(/^chat-messages-(?:\d+-)?(\d+)$/);
  if (!m) return;
  const content = li.querySelector('[id^="message-content-"]');
  const author = li.querySelector('[id^="message-username-"]');
  if (author) lastAuthor = author.innerText;      // grouped messages inherit the previous header's author
  const t = li.querySelector('time[datetime]');
  rows.push({
    id: m[1],
    ts: t ? t.getAttribute('datetime') : '',
    author: lastAuthor,
    text: content ? content.innerText : '',
    raw: content ? content.textContent : '',       // includes spoilered text innerText may hide
    hrefs: Array.from(li.querySelectorAll('a[href]')).map(a => a.href),
  });
});
return rows;
"""

# Scroll the message list's scroller (the nearest scrollable ancestor) to the top, which makes Discord load older
# messages. Returns false if there is nothing scrollable.
SCROLL_UP_JS = r"""
let el = document.querySelector('[data-list-id="chat-messages"]');
while (el && el.scrollHeight <= el.clientHeight + 1) el = el.parentElement;
if (!el) return false;
el.scrollTop = 0;
return true;
"""



def read_rows(driver) -> list[dict]:
    """Every message currently rendered in the open channel: {'id','ts','author','text','raw','hrefs'}."""
    return driver.execute_script(READ_JS) or []


def scroll_up(driver) -> None:
    """Ask the open channel to load older messages."""
    driver.execute_script(SCROLL_UP_JS)


def close() -> None:
    """Close the Discord tab (the browser itself is the app's, and is quit by whoever started it)."""
    browser_session.close_tab('discord')


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

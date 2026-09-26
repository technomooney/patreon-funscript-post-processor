"""JOIGang (joigang.com) bulk download via its funscript library page.

JOIGang isn't Patreon-post-driven like every other handler in this project --
its own /funscript-files/ page is a flat, site-maintained catalog of every
funscript that exists (193+ entries at last count), each one linking to its
own download and to the exact video it belongs to. That page is the source
of truth here, not Patreon post descriptions, so this plugin owns the whole
download process for this creator instead of feeding into downloadContent.py
(see REPLACES_DOWNLOAD below, same shape as the Pize archive-as-source-of-
truth redesign).

Login is Patreon OAuth (site account also exists, but the user's own account
is Patreon-linked) -- rather than script anything on patreon.com itself
(a large platform, far more likely to challenge automation with CAPTCHA than
a small site), this uses a persistent browser profile the user logs into by
hand, once, exactly like discord_passwords.py's Discord session. Every later
run reuses that saved session.

Per funscript-library row, the flow (confirmed against real, logged-in DOM,
2026-09-26):
  1. The library row's OWN video link can point to the wrong (Public, not
     Exclusive) variant -- WordPress data quirk, not something to trust.
  2. Its funscript-page link (the "Download" button) is always correct for
     that row, and that funscript page's own "<- Back to the video" link is
     the actually-correct matching video -- use that instead.
  3. The funscript page's download link is a plain, real <a href> (a signed
     admin-ajax.php URL, not a JS-only button like joi.moe) -- click it and
     let the browser download it.
  4. The video page embeds the video as a Bunny.net (mediadelivery.net)
     iframe -- yt-dlp handles that URL shape natively, given the right
     Referer header (Bunny CDN embeds commonly restrict by referrer).

Videos and funscripts land in one folder per title under the base_path the
user gives when running this from the menu -- there's no Patreon post
structure to mirror here, unlike every other creator this project handles.
"""
import os
import re
import time
from pathlib import Path

import action_log
import downloadContent as dc

import undetected_chromedriver as uc
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

SCRIPT_ID = 'joigang_bulk_download'
MENU_LABEL = 'JOIGang bulk download (funscript library)'
MENU_DESCRIPTION = (
    'joigang.com only: scrapes /funscript-files/ (the site\'s own catalog) and\n'
    'downloads every video + funscript pair into its own folder. Not Patreon-\n'
    'post-driven -- this replaces the normal download step for this creator.'
)
REPLACES_DOWNLOAD = True

_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    '.joigang_browser_profile',
)

_driver = None  # module-level singleton, same convention as discord_passwords._driver


def _joigang_driver():
    """Return a persistent-profile browser dedicated to joigang.com (kept
    separate from the main automation profile so its Patreon-linked login
    survives between runs, same reasoning as discord_passwords._discord_driver)."""
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
    """joigang.com's WordPress theme adds a "logged-in" class to <body> only
    for an authenticated session -- simpler and more reliable than checking
    the URL, and needs no JS execution."""
    return bool(driver.find_elements(By.XPATH, '//body[contains(@class,"logged-in")]'))


def _ensure_login(driver, timeout_minutes: int = 20) -> bool:
    """Navigate to joigang.com; if not already logged in, wait for the user
    to log in by hand (Account -> Login with Patreon -> Patreon login ->
    Allow). Returns True once logged in, False on timeout or a closed window.
    No credentials are ever typed in by this script -- see module docstring.
    """
    driver.get('https://joigang.com/membership-account/')
    try:
        WebDriverWait(driver, 15).until(lambda d: _is_logged_in(d) or True)
    except WebDriverException:
        pass
    if _is_logged_in(driver):
        return True

    print('  [joigang] not logged in — in the browser window that just opened, click')
    print('  [joigang] "Account", then "Login with Patreon", and Allow on the Patreon prompt.')
    print('  [joigang] this is one-time only — future runs reuse this saved session.')
    print(f'  [joigang] waiting up to {timeout_minutes} minutes...')
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        try:
            if _is_logged_in(driver):
                print('  [joigang] logged in.')
                return True
        except WebDriverException:
            print('  [joigang] browser window was closed — run again when ready to log in.')
            return False
        time.sleep(2)
    print('  [joigang] timed out waiting for login.')
    return False


def _dismiss_age_gate(driver) -> None:
    """Click "Yes" on the WordPress age-gate popup if it's showing (only
    appears once per browser profile, cookie-remembered after that)."""
    try:
        btn = driver.find_element(By.XPATH, '//button[contains(@class,"age-gate-submit-yes")]')
        if btn.is_displayed():
            driver.execute_script('arguments[0].click()', btn)
            time.sleep(1)
    except WebDriverException:
        pass


def _scrape_library(driver) -> list[dict]:
    """Return every row on /funscript-files/ as {'title', 'file_name', 'funscript_url'}.

    Deliberately ignores each row's own video link (jg-fslib-title href) --
    confirmed live it can point to the wrong Public/Exclusive variant; the
    funscript page itself resolves that correctly (see _resolve_entry).
    """
    driver.get('https://joigang.com/funscript-files/')
    _dismiss_age_gate(driver)
    time.sleep(1.5)

    entries = []
    for row in driver.find_elements(By.XPATH, '//div[contains(@class,"jg-fslib-row")]'):
        try:
            title = row.find_element(By.XPATH, './/a[contains(@class,"jg-fslib-title")]').text.strip()
            file_name = row.find_element(By.XPATH, './/span[contains(@class,"jg-fslib-file")]').text.strip()
            funscript_url = row.find_element(By.XPATH, './/a[contains(@class,"jg-btn-sm")]').get_attribute('href')
        except WebDriverException:
            continue
        if title and funscript_url:
            entries.append({'title': title, 'file_name': file_name, 'funscript_url': funscript_url})
    return entries


def _resolve_entry(driver, funscript_url: str) -> tuple[str, str] | None:
    """Visit a funscript page and return (download_url, video_url), or None
    if either link is missing (e.g. this account's tier doesn't cover it)."""
    driver.get(funscript_url)
    _dismiss_age_gate(driver)
    time.sleep(1)
    try:
        download_url = driver.find_element(By.XPATH, '//a[contains(@class,"jgvp-fsgate-dl")]').get_attribute('href')
        video_url = driver.find_element(By.XPATH, '//a[contains(@class,"jgvp-fsgate-back")]').get_attribute('href')
    except WebDriverException:
        return None
    if not download_url or not video_url:
        return None
    return download_url, video_url


def _video_iframe_src(driver, video_url: str) -> str | None:
    """Visit a video page and return its Bunny.net (mediadelivery.net) iframe src."""
    driver.get(video_url)
    _dismiss_age_gate(driver)
    time.sleep(1.5)
    try:
        iframe = driver.find_element(By.XPATH, '//iframe[contains(@src,"mediadelivery.net")]')
    except WebDriverException:
        return None
    return iframe.get_attribute('src')


def _download_funscript(driver, folder: str, download_url: str) -> str | None:
    """Click the funscript page's download link and wait for the file to
    land in *folder*. Returns the saved path, or None on failure."""
    dc.set_download_dir(driver, folder)
    before_files = set(os.listdir(folder))
    try:
        link = driver.find_element(By.XPATH, '//a[contains(@class,"jgvp-fsgate-dl")]')
        driver.execute_script('arguments[0].click()', link)
    except WebDriverException as e:
        print(f'  [joigang] could not click the download link: {e}')
        return None
    if not dc._wait_for_download_to_start(folder, before_files):
        print('  [joigang] clicked but no download appears to have started')
        return None
    return dc.wait_for_download(folder, before_files, timeout=30)


def _download_video(iframe_src: str, folder: str) -> bool:
    """Fetch *iframe_src* (a Bunny.net embed URL) via yt-dlp into *folder*.

    Uses yt-dlp directly (not downloadContent.download_ytdlp) so a
    joigang.com-specific --referer can be added -- Bunny CDN embeds commonly
    restrict playback by referrer, and download_ytdlp is shared by every
    other generic-fallback site, so it isn't the place for that.
    """
    ytdlp_prefix = dc._ytdlp_cmd()
    if ytdlp_prefix is None:
        print('  [joigang] yt-dlp not found — install with: pip install yt-dlp')
        return False

    max_res = dc._get_max_resolution()
    output_tmpl = os.path.join(folder, '_joigang_ytdlp_temp.%(ext)s')
    cmd = [
        *ytdlp_prefix,
        '--no-playlist',
        '--no-write-subs',
        '--no-write-auto-subs',
        '--no-keep-fragments',
        '--referer', 'https://joigang.com/',
        '--merge-output-format', 'mp4',
        '-f', (
            f'bestvideo[height<={max_res}][ext=mp4]+bestaudio[ext=m4a]'
            f'/bestvideo[height<={max_res}]+bestaudio'
            f'/best[height<={max_res}]/best'
        ),
        '-o', output_tmpl,
        iframe_src,
    ]
    import subprocess
    print('  [joigang] fetching video via yt-dlp...')
    try:
        result = subprocess.run(cmd, timeout=3600)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print('  [joigang] yt-dlp timed out after 1 hour')
    except Exception as e:
        print(f'  [joigang] yt-dlp error: {e}')
    return False


def _entry_folder(base_path: str, title: str) -> str:
    safe = dc._sanitize_filename_stem(title) or 'untitled'
    return os.path.join(base_path, safe)


def _entry_already_done(folder: str) -> bool:
    """True if *folder* already has both a video and a funscript -- skip re-fetching it."""
    if not os.path.isdir(folder):
        return False
    files = os.listdir(folder)
    has_video = any(dc._is_video_filename(f) for f in files)
    has_script = any(f.lower().endswith('.funscript') or f.lower().endswith('.zip') for f in files)
    return has_video and has_script


def run(base_path: str) -> None:
    driver = _joigang_driver()
    if not _ensure_login(driver):
        print('  [joigang] cannot continue without a working login')
        return

    print('  [joigang] loading the funscript library (this can take a moment)...')
    entries = _scrape_library(driver)
    if not entries:
        print('  [joigang] no entries found — the page layout may have changed')
        return
    print(f'  [joigang] found {len(entries)} funscript entries')

    action_log.start(SCRIPT_ID, base_path, journal=SCRIPT_ID)
    try:
        done = skipped = failed = 0
        for i, entry in enumerate(entries, start=1):
            title = entry['title']
            folder = _entry_folder(base_path, title)
            print(f'\n  [{i}/{len(entries)}] {title}')

            if _entry_already_done(folder):
                print('    [skip] already have a video and a funscript here')
                skipped += 1
                continue

            resolved = _resolve_entry(driver, entry['funscript_url'])
            if resolved is None:
                print('    [fail] could not find the download/video links on this funscript page'
                      ' (tier may not cover it)')
                failed += 1
                continue
            download_url, video_url = resolved

            os.makedirs(folder, exist_ok=True)

            script_path = _download_funscript(driver, folder, download_url)
            if script_path:
                action_log.record('copy', dst=script_path)
                print(f'    [funscript] saved: {os.path.basename(script_path)}')
            else:
                print('    [funscript] failed to download')

            iframe_src = _video_iframe_src(driver, video_url)
            if not iframe_src:
                print('    [video] could not find the video player on the linked page')
                failed += 1
                continue

            before = set(os.listdir(folder))
            if _download_video(iframe_src, folder):
                new_files = set(os.listdir(folder)) - before
                for f in new_files:
                    action_log.record('copy', dst=os.path.join(folder, f))
                print('    [video] saved')
                done += 1
            else:
                print('    [video] failed to download')
                failed += 1
    finally:
        action_log.finish()

    print(f'\n  [joigang] done — {done} new, {skipped} already had both, {failed} failed')

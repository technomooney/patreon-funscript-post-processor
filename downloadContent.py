import base64
import csv
import re
import json
import mimetypes
import os
import shutil
import time
import glob
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

load_dotenv()

_KEYRING_SERVICE = 'patreon-funscript-video-downloader'


def _get_secret(key: str, default: str = '') -> str:
    """Read a secret from the OS keyring, falling back to .env / environment variables."""
    try:
        import keyring
        value = keyring.get_password(_KEYRING_SERVICE, key)
        if value:
            return value
    except Exception:
        pass
    return os.getenv(key, default)


# Add new domains here along with a handler function in DOMAIN_HANDLERS below.
# If a URL's domain is not listed, the script will raise an error and skip it.
KNOWN_DOMAINS = [
    'hanime1.me',
    'hanime.tv',
    'gofile.io',
    'iwara.tv',
    'pixeldrain.com',
    'rule34video.com',
    'rule34.xxx',
    'fap-nation.org',
]

# Links to these domains are creator pages / social profiles — no file to download.
# They are silently skipped without an error.
SKIP_DOMAINS = {
    'patreon.com',
    'subscribestar.adult',
    'fanbox.cc',
    'discuss.eroscripts.com',
    'carrd.co',
}


class UnknownDomainError(Exception):
    pass


class CloudflareBlockedError(Exception):
    """Raised when a handler detects a Cloudflare challenge page."""
    pass


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def extract_links_from_description(desc_path: str) -> list:
    """Recursively extract all href values from link marks in a ProseMirror JSON file."""
    with open(desc_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    links = []

    def traverse(node):
        if isinstance(node, dict):
            for mark in node.get('marks', []):
                if mark.get('type') == 'link':
                    href = mark.get('attrs', {}).get('href')
                    if href:
                        links.append(href)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    traverse(value)
        elif isinstance(node, list):
            for item in node:
                traverse(item)

    traverse(data)
    return list(dict.fromkeys(links))  # deduplicate, preserve order


def get_funscript_basename(folder: str):
    """
    Return the stem (no extension) of the primary .funscript in *folder*.
    Axis-variant scripts (.surge, .pitch, .roll, .twist, .sway) are deprioritised
    so the main script name is used as the download basename.
    Returns None if no .funscript files exist.
    """
    funscripts = glob.glob(os.path.join(glob.escape(folder), '*.funscript'))
    if not funscripts:
        return None

    axis_suffixes = ('.surge', '.pitch', '.roll', '.twist', '.sway')
    main_scripts = [
        fs for fs in funscripts
        if not any(Path(fs).stem.endswith(s) for s in axis_suffixes)
    ]

    chosen = main_scripts[0] if main_scripts else funscripts[0]
    return Path(chosen).stem


def get_domain(url: str) -> str:
    """Extract the registrable domain (strips leading www.)."""
    return urlparse(url).netloc.removeprefix('www.')


def check_domain(url: str) -> str:
    """
    Return the matched KNOWN_DOMAINS entry for *url*, or raise UnknownDomainError.
    Raises UnknownDomainError with an actionable message so the caller knows
    what to add to KNOWN_DOMAINS and DOMAIN_HANDLERS.
    """
    domain = get_domain(url)
    for known in KNOWN_DOMAINS:
        if domain == known or domain.endswith('.' + known):
            return known
    raise UnknownDomainError(
        f"Domain '{domain}' is not supported. "
        f"To add support: (1) append '{domain}' to KNOWN_DOMAINS, "
        f"(2) write a handler function download_{domain.replace('.', '_')}(driver, url), "
        f"(3) add it to DOMAIN_HANDLERS."
    )


# ---------------------------------------------------------------------------
# Selenium / Chrome helpers
# ---------------------------------------------------------------------------

def _find_browser() -> str | None:
    """Return the path to a Chromium-compatible browser, or None if not found."""
    for name in ('brave', 'brave-browser', 'brave-bin', 'google-chrome', 'chromium', 'chromium-browser'):
        path = shutil.which(name)
        if path:
            return path
    return None


def setup_driver(initial_download_dir: str):
    """Create an undetected Chrome WebDriver that saves files to *initial_download_dir*."""
    options = uc.ChromeOptions()
    prefs = {
        'download.default_directory': os.path.abspath(initial_download_dir),
        'download.prompt_for_download': False,
        'download.directory_upgrade': True,
        'safebrowsing.enabled': True,
        'credentials_enable_service': False,
        'profile.password_manager_enabled': False,
    }
    options.add_experimental_option('prefs', prefs)

    headless = os.getenv('BROWSER_HEADLESS', 'false').strip().lower() == 'true'
    if headless:
        options.add_argument('--headless=new')
        # Make headless Chrome look as close to a real browser as possible.
        # Cloudflare and similar bot-detection systems fingerprint window size,
        # the automation flag, and WebGL renderer strings.
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--no-sandbox')

    browser = _find_browser()
    if browser is None:
        raise RuntimeError('No Chromium-compatible browser found. Install Brave, Chrome, or Chromium.')
    print(f'Using browser: {browser} ({"headless" if headless else "windowed"})')

    driver = uc.Chrome(options=options, browser_executable_path=browser)
    return driver


def _is_cloudflare_blocked(driver) -> bool:
    """Return True if the current page is a Cloudflare challenge/block page."""
    title = driver.title or ''
    return any(phrase in title for phrase in (
        'Attention Required! | Cloudflare',
        'Just a moment',
        'Access denied',
        'Checking your browser',
    ))


def set_download_dir(driver, download_dir: str):
    """Change the Chrome download directory on-the-fly via CDP (no restart needed)."""
    driver.execute_cdp_cmd('Browser.setDownloadBehavior', {
        'behavior': 'allow',
        'downloadPath': os.path.abspath(download_dir),
    })


def _switch_to_new_tab(driver, original_handles: set, timeout: int = 8) -> bool:
    """
    Poll until a new tab appears, then switch to it. Returns True if switched.
    A single snapshot check misses tabs that open slightly after the click;
    polling up to *timeout* seconds handles slow servers reliably.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        new_handles = set(driver.window_handles) - original_handles
        if new_handles:
            driver.switch_to.window(next(iter(new_handles)))
            time.sleep(1)
            return True
        time.sleep(0.5)
    return False


def wait_for_download(download_dir: str, before_files: set[str], timeout: int | None = None):
    """
    Poll *download_dir* until a new fully written file appears.
    Temporary browser download files (.part, .crdownload, .tmp) are ignored.
    Pass timeout (seconds) to give up after that duration; None waits indefinitely.
    Returns the full path of the downloaded file, or None if timed out.
    """
    deadline = time.time() + timeout if timeout is not None else None
    while True:
        current: set[str] = set(os.listdir(download_dir))
        new_files = current - before_files
        complete = [
            f for f in new_files
            if not f.endswith(('.part', '.crdownload', '.tmp'))
        ]
        if complete:
            return os.path.join(download_dir, complete[0])
        if deadline is not None and time.time() >= deadline:
            return None
        time.sleep(1)


# ---------------------------------------------------------------------------
# Shared download utilities
# ---------------------------------------------------------------------------

def _direct_fetch(video_url: str, download_dir: str, temp_prefix: str, headers: dict[str, str]) -> bool:
    """Download *video_url* straight to *download_dir* using urllib, no browser needed.

    Writes to a .part file while in progress so that wait_for_download ignores
    it until the download is complete, then renames to the final temp name.
    This ensures an interrupted download is never mistaken for a finished one.
    """
    ext = os.path.splitext(urlparse(video_url).path)[1] or '.mp4'
    final_temp = os.path.join(download_dir, f'{temp_prefix}{ext}')
    writing_path = final_temp + '.part'
    req = urllib.request.Request(video_url, headers=headers)
    with urllib.request.urlopen(req) as response:
        size_mb = int(response.headers.get('Content-Length', 0)) / 1024 / 1024
        if size_mb:
            print(f'  file size: {size_mb:.1f} MB')
        with open(writing_path, 'wb') as f:
            while chunk := response.read(65536):
                f.write(chunk)
    os.rename(writing_path, final_temp)
    return True


def _get_max_resolution() -> int:
    """Read MAX_RESOLUTION from the environment (default 1080)."""
    try:
        return int(os.getenv('MAX_RESOLUTION', '1080'))
    except ValueError:
        return 1080


def _parse_resolution(text: str) -> int:
    """Return the first resolution value (e.g., 1080) found in *text*, or 0."""
    for res in [2160, 1080, 720, 480, 360, 240]:
        if str(res) in text:
            return res
    return 0


def _pick_best(candidates: list, resolution_fn) -> tuple:
    """
    Return (best_candidate, resolution) honoring MAX_RESOLUTION.
    Picks the highest resolution <= MAX_RESOLUTION.
    Falls back to the lowest available if every option exceeds the cap.
    """
    max_res = _get_max_resolution()
    scored = [(c, resolution_fn(c)) for c in candidates]
    eligible = [(c, r) for c, r in scored if 0 < r <= max_res]
    if eligible:
        return max(eligible, key=lambda x: x[1])
    # Nothing at or below the cap — take the lowest available to avoid an oversized download.
    return min(scored, key=lambda x: x[1])


# ---------------------------------------------------------------------------
# Per-domain download handlers
# Each handler receives (driver, url, download_dir) and is responsible for
# placing a completed file in download_dir.  Returns True on success.
# ---------------------------------------------------------------------------

def download_gofile(driver, url: str, _download_dir: str) -> bool:
    """Navigate to a gofile.io share and click the download button."""
    driver.get(url)

    try:
        time.sleep(1)  # let the JS-heavy page render

        # gofile.io embeds its file-manager state in window.__NUXT__ (Vue/Nuxt app).
        # Check for an error status before attempting any clicks.
        try:
            status = driver.execute_script("""
                try {
                    var nuxt = window.__NUXT__;
                    if (nuxt) {
                        var s = JSON.stringify(nuxt);
                        var m = s.match(/"status"\\s*:\\s*"(error-[^"]+)"/);
                        return m ? m[1] : null;
                    }
                } catch(e) {}
                return null;
            """)
            if status:
                print(f'  [gofile.io] link is invalid — server returned status: {status}')
                return False
        except Exception as e:
            print(f'  [gofile.io] could not read page state: {e}')  # fall through to DOM checks

        # gofile.io renders file rows with a download icon/button per file.
        # Try the most common selectors; adjust if gofile changes their markup.
        candidates = driver.find_elements(By.XPATH, (
            '//*['
            'contains(@class,"downloadButton") or '
            'contains(@class,"download-btn") or '
            '(self::button and contains('
            '  translate(normalize-space(.),"DOWNLOAD","download"),'
            '  "download"'
            '))'
            ']'
        ))
        if candidates:
            candidates[0].click()
            return True

        # Fallback: any anchor whose href contains "gofile.io" and "download"
        links = driver.find_elements(By.XPATH, '//a[contains(@href,"gofile.io")]')
        for link in links:
            href = link.get_attribute('href') or ''
            text = (link.text or '').lower().strip()
            if 'download' in href.lower() or text == 'download':
                link.click()
                return True

    except Exception as e:
        print(f"  [gofile.io] handler error: {e}")

    return False


def download_hanime(driver, url: str, download_dir: str) -> bool:
    """Navigate to a hanime1.me watch page and download the highest available resolution."""
    driver.get(url)

    try:
        wait = WebDriverWait(driver, 5)

        # Wait for the download anchor to be present — headless mode renders
        # slower so a fixed sleep is not reliable here.
        try:
            download_btn = wait.until(EC.presence_of_element_located((By.ID, 'downloadBtn')))
        except TimeoutException:
            if _is_cloudflare_blocked(driver):
                raise CloudflareBlockedError('hanime1.me blocked by Cloudflare in headless mode')
            print(f'  [hanime1.me] timed out waiting for #downloadBtn')
            print(f'  [hanime1.me] current URL : {driver.current_url}')
            print(f'  [hanime1.me] page title  : {driver.title!r}')
            return False

        download_page_url = download_btn.get_attribute('href')
        if not download_page_url:
            print('  [hanime1.me] downloadBtn has no href')
            return False

        # Navigate directly to the download page in the same tab.
        driver.get(download_page_url)

        # Wait for at least one quality link to appear in the resolution table.
        try:
            wait.until(EC.presence_of_element_located((By.XPATH, '//a[@data-url]')))
        except TimeoutException:
            print(f'  [hanime1.me] timed out waiting for quality links on download page')
            print(f'  [hanime1.me] current URL : {driver.current_url}')
            print(f'  [hanime1.me] page title  : {driver.title!r}')
            return False
        links = driver.find_elements(By.XPATH, '//a[@data-url]')
        if not links:
            print('  [hanime1.me] no data-url links found on download page')
            return False

        best, resolution = _pick_best(
            links,
            lambda el: _parse_resolution(el.get_attribute('data-url') or ''),
        )
        video_url = best.get_attribute('data-url')
        if not video_url:
            print('  [hanime1.me] best link has no data-url value')
            return False

        # The data-url contains a self-contained token, so no browser session is needed.
        # Downloading via urllib avoids the browser opening the mp4 inline.
        print(f'  [hanime] fetching {resolution}p...')
        return _direct_fetch(video_url, download_dir, '_hanime_temp', {'Referer': 'https://hanime1.me/'})

    except CloudflareBlockedError:
        raise  # let find_and_download handle the retry prompt
    except Exception as e:
        print(f'  [hanime1.me] handler error: {e}')

    return False


def download_fapnation(driver, url: str, download_dir: str) -> bool:
    """Navigate to a fap-nation.org post and download the best quality within MAX_RESOLUTION.

    Quality buttons are rendered as .wp-block-button__link anchors whose visible
    text contains the resolution label (e.g. "1080P", "720P").
    """
    driver.get(url)

    try:
        time.sleep(2)

        links = driver.find_elements(By.XPATH,
            '//a[contains(@class,"wp-block-button__link") and @href]'
        )
        if not links:
            print('  [fap-nation.org] no quality buttons found on page')
            return False

        best, resolution = _pick_best(
            links,
            lambda el: _parse_resolution(el.text or ''),
        )
        video_url = best.get_attribute('href') or ''
        if not video_url:
            print('  [fap-nation.org] best quality button has no href')
            return False

        print(f'  [fap-nation.org] fetching {resolution}p...')
        return _direct_fetch(video_url, download_dir, '_fapnation_temp',
                             {'Referer': 'https://fap-nation.org/'})

    except Exception as e:
        print(f'  [fap-nation.org] handler error: {e}')

    return False


def download_rule34xxx(driver, url: str, download_dir: str) -> bool:
    """Navigate to a rule34.xxx post page and download the original file."""
    driver.get(url)

    try:
        time.sleep(1)

        # The original file link is an <a> whose visible text is "Original image".
        link = driver.find_element(By.XPATH,
            '//a[contains(normalize-space(.),"Original image")]'
        )
        video_url = link.get_attribute('href') or ''
        if not video_url:
            print('  [rule34.xxx] "Original image" link has no href')
            return False

        # Strip query string for a clean extension, but keep full URL for the request.
        print(f'  [rule34.xxx] fetching original...')
        return _direct_fetch(video_url, download_dir, '_r34xxx_temp',
                             {'Referer': 'https://rule34.xxx/'})

    except Exception as e:
        print(f'  [rule34.xxx] handler error: {e}')

    return False


def download_rule34video(driver, url: str, download_dir: str) -> bool:
    """Navigate to a rule34video.com video page and download the highest quality."""
    driver.get(url)

    try:
        time.sleep(1)

        # rule34video.com hides quality links behind a download button.
        # Use JS to click it so the DOM reveals the links without the browser
        # treating it as a real click and starting its own download.
        download_btns = driver.find_elements(By.XPATH, (
            '//a[contains(@class,"download")] | '
            '//button[contains(@class,"download") or '
            'contains(translate(normalize-space(.),"DOWNLOAD","download"),"download")]'
        ))
        if download_btns:
            driver.execute_script('arguments[0].click()', download_btns[0])
            time.sleep(1)

        mp4_links = driver.find_elements(By.XPATH, '//a[contains(@href,".mp4")]')
        if not mp4_links:
            print('  [rule34video.com] no mp4 links found')
            return False

        best, resolution = _pick_best(
            mp4_links,
            lambda el: _parse_resolution((el.text or '') + (el.get_attribute('href') or '')),
        )
        video_url = best.get_attribute('href')
        if not video_url:
            print('  [rule34video.com] best link has no href')
            return False

        print(f'  [rule34video.com] fetching {resolution}p...')
        return _direct_fetch(video_url, download_dir, '_r34v_temp', {'Referer': 'https://rule34video.com/'})

    except Exception as e:
        print(f'  [rule34video.com] handler error: {e}')

    return False


def download_pixeldrain(_driver, url: str, download_dir: str) -> bool:
    """Download a pixeldrain.com file directly via its public API (no browser needed)."""
    try:
        # Page URL: /u/<id> → API URL: /api/file/<id>
        file_id = urlparse(url).path.rstrip('/').split('/')[-1]
        video_url = f'https://pixeldrain.com/api/file/{file_id}'
        print(f'  [pixeldrain.com] fetching {file_id}...')

        headers: dict[str, str] = {'Referer': 'https://pixeldrain.com/'}
        api_key = _get_secret('PIXELDRAIN_API_KEY').strip()
        if api_key:
            # Pixeldrain uses HTTP Basic Auth: empty username, API key as password.
            token = base64.b64encode(f':{api_key}'.encode()).decode()
            headers['Authorization'] = f'Basic {token}'

        return _direct_fetch(video_url, download_dir, '_pixeldrain_temp', headers)

    except Exception as e:
        print(f'  [pixeldrain.com] handler error: {e}')

    return False


def download_hanimetv(driver, url: str, download_dir: str) -> bool:
    """Navigate to a hanime.tv watch page and download via its pixeldrain-backed quality links."""
    driver.get(url)

    try:
        time.sleep(1)

        # Step 1: click the top-level DOWNLOAD button to open the quality selection page.
        download_btn = driver.find_element(
            By.XPATH,
            '//span[contains(@class,"hvpabb-text") and '
            'contains(normalize-space(.),"DOWNLOAD")]'
        )
        original_handles = set(driver.window_handles)
        download_btn.click()
        time.sleep(1)

        # Switch to the new tab if one was opened.
        _switch_to_new_tab(driver, original_handles)

        # Step 2: click "Get Download Links" to reveal the quality buttons.
        get_links_btn = driver.find_element(
            By.XPATH,
            '//div[contains(@class,"btn__content") and '
            'contains(normalize-space(.),"Get Download Links")]'
        )
        driver.execute_script('arguments[0].click()', get_links_btn)
        time.sleep(1)

        # Step 3: collect quality anchor elements inside content__dls__btn containers.
        # Each <a> wraps a button whose text is the resolution label (e.g. "720p").
        links = driver.find_elements(
            By.XPATH,
            '//div[contains(@class,"content__dls__btn")]//a[@href]'
        )
        if not links:
            print('  [hanime.tv] no quality download links found')
            return False

        best, resolution = _pick_best(
            links,
            lambda el: _parse_resolution(el.text or ''),
        )
        pixeldrain_url = best.get_attribute('href')
        if not pixeldrain_url:
            print('  [hanime.tv] best quality link has no href')
            return False

        # Close the quality-selection tab before the (potentially long) fetch.
        extra_handles = set(driver.window_handles) - original_handles
        if extra_handles:
            driver.close()
            driver.switch_to.window(driver.window_handles[0])

        print(f'  [hanime.tv] fetching {resolution}p via pixeldrain...')
        # The quality links point to pixeldrain, so reuse the pixeldrain handler.
        return download_pixeldrain(driver, pixeldrain_url, download_dir)

    except Exception as e:
        print(f'  [hanime.tv] handler error: {e}')

    return False


# ---------------------------------------------------------------------------
# iwara.tv handler
# ---------------------------------------------------------------------------

# Cached Bearer token — obtained once per session on first iwara.tv download.
_iwara_token: str | None = None

# Whether the browser session has already completed the iwara.tv login flow.
_iwara_browser_logged_in: bool = False


def _iwara_browser_login(driver) -> bool:
    """Log into iwara.tv via the browser UI. Returns True if successful."""
    global _iwara_browser_logged_in
    if _iwara_browser_logged_in:
        return True

    email    = _get_secret('IWARA_EMAIL').strip()
    password = _get_secret('IWARA_PASSWORD').strip()
    if not email or not password:
        print('  [iwara.tv] no credentials — set IWARA_EMAIL and IWARA_PASSWORD via setup_credentials.py')
        return False

    for attempt in range(3):
        driver.get('https://www.iwara.tv/login')
        time.sleep(2)

        page_src = driver.page_source.lower()
        if 'too many requests' in page_src or '429' in driver.title:
            wait = [1, 5, 10][attempt]
            print(f'  [iwara.tv] rate-limited on login page — waiting {wait}s before retry {attempt + 1}/3...')
            time.sleep(wait)
            continue
        break
    else:
        print('  [iwara.tv] login page rate-limited after 3 attempts')
        return False

    # Dismiss age gate if present.
    try:
        age_btn = driver.find_element(By.XPATH,
            '//button[contains(normalize-space(.),"18") or '
            'contains(normalize-space(.),"Yes") or '
            'contains(normalize-space(.),"Enter") or '
            'contains(normalize-space(.),"I am")]'
        )
        age_btn.click()
        time.sleep(1)
    except Exception:
        pass

    try:
        wait = WebDriverWait(driver, 10)

        email_field = wait.until(EC.presence_of_element_located(
            (By.XPATH, '//input[@type="email" or @name="email" or @autocomplete="email"]')
        ))
        email_field.click()
        time.sleep(0.3)
        email_field.clear()
        for char in email:
            email_field.send_keys(char)
            time.sleep(0.05)

        pw_field = driver.find_element(By.XPATH, '//input[@type="password"]')
        pw_field.click()
        time.sleep(0.3)
        for char in password:
            pw_field.send_keys(char)
            time.sleep(0.05)

        time.sleep(0.5)
        # Find the submit button scoped to the login form so we don't
        # accidentally click a navbar/search form's submit button instead.
        try:
            login_form = pw_field.find_element(By.XPATH, './ancestor::form')
            submit = login_form.find_element(By.XPATH, './/button[@type="submit"]')
        except Exception:
            # Fallback: pick the last submit button (search is usually first)
            submits = driver.find_elements(By.XPATH, '//button[@type="submit"]')
            submit = submits[-1] if submits else driver.find_element(By.XPATH, '//button[@type="submit"]')
        for submit_attempt in range(3):
            driver.execute_script('arguments[0].click()', submit)

            # Wait up to 10 s for the URL to change away from the login page.
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: 'login' not in d.current_url
                )
            except Exception:
                pass

            # If still on the login page, check for a rate-limit response.
            if 'login' in driver.current_url:
                page_src = driver.page_source.lower()
                if 'too many requests' in page_src or '429' in page_src:
                    wait = [1, 5, 10][submit_attempt]
                    print(f'  [iwara.tv] rate-limited after submit — waiting {wait}s before retry {submit_attempt + 1}/3...')
                    time.sleep(wait)
                    continue
                # Not a rate-limit — just failed.
                print(f'  [iwara.tv] login did not redirect — still on: {driver.current_url}')
                print(f'  [iwara.tv] page title: {driver.title!r}')
                return False
            break
        else:
            print('  [iwara.tv] login submit rate-limited after 3 attempts')
            return False

        print('  [iwara.tv] browser login successful')

        _iwara_browser_logged_in = True
        return True
    except Exception as e:
        print(f'  [iwara.tv] browser login failed: {e}')
        return False


def _download_iwara_browser(driver, url: str, download_dir: str) -> bool:
    """Use the browser to scrape download links when the API quality list is incomplete."""
    if not _iwara_browser_login(driver):
        return False

    time.sleep(2)  # Let session establish before navigating.
    driver.get(url)
    time.sleep(5)
    print(f'  [iwara.tv] navigated to video — current URL: {driver.current_url}')

    # If the SPA redirected us away from the video path (e.g. to home/search),
    # navigate again — the session cookie is usually fully set by now.
    video_path = urlparse(url).path
    if urlparse(driver.current_url).path != video_path:
        print(f'  [iwara.tv] unexpected redirect, retrying navigation...')
        driver.get(url)
        time.sleep(5)
        print(f'  [iwara.tv] current URL after retry: {driver.current_url}')

    # If the React app shows an error page on first load, a refresh usually fixes it.
    if 'error' in driver.title.lower():
        print(f'  [iwara.tv] got error page ({driver.title!r}), refreshing...')
        driver.refresh()
        time.sleep(5)

    # Dismiss age gate if it appears on the video page.
    try:
        age_btn = driver.find_element(By.XPATH,
            '//button[contains(normalize-space(.),"18") or '
            'contains(normalize-space(.),"Yes") or '
            'contains(normalize-space(.),"Enter") or '
            'contains(normalize-space(.),"I am")]'
        )
        age_btn.click()
        time.sleep(2)
    except Exception:
        pass

    print(f'  [iwara.tv] page title after load: {driver.title!r}')

    # Collect all CDN download/view links rendered by the React app.
    links = driver.find_elements(By.XPATH,
        '//a[contains(@href,".iwara.tv/download") or contains(@href,".iwara.tv/view")]'
    )

    if not links:
        # Print all hrefs to help diagnose the correct selector.
        all_hrefs = [el.get_attribute('href') for el in driver.find_elements(By.XPATH, '//a[@href]')]
        print('  [iwara.tv] no CDN links found — all hrefs on page:')
        for href in all_hrefs:
            print(f'    {href}')
        return False

    max_res = int(os.getenv('MAX_RESOLUTION', '1080'))

    def _res_from_href(href: str) -> int:
        href_lower = href.lower()
        if 'preview' in href_lower:
            return 0
        for label in ('source',):
            if label in href_lower:
                return 9999
        # filename pattern: ..._1080.mp4, ..._540.mp4, ..._360.mp4
        m = re.search(r'_(\d+)\.mp4', href_lower)
        if m:
            return int(m.group(1))
        return 1

    scored = [(el, _res_from_href(el.get_attribute('href') or '')) for el in links]
    scored.sort(key=lambda x: x[1], reverse=True)

    # If source is available and every transcode is below max_res, prefer source —
    # a lower-resolution transcode is not better than the original.
    best_transcode = max((r for _, r in scored if r not in (0, 9999)), default=0)
    source_items = [(el, r) for el, r in scored if r == 9999]
    if source_items and best_transcode < max_res:
        chosen_el, chosen_res = source_items[0]
    else:
        chosen_el, chosen_res = scored[0]
        for el, res in scored:
            if res <= max_res:
                chosen_el, chosen_res = el, res
                break

    download_url = chosen_el.get_attribute('href') or ''
    if download_url.startswith('//'):
        download_url = 'https:' + download_url

    label = f'{chosen_res}p' if chosen_res not in (0, 9999) else ('source' if chosen_res == 9999 else 'preview')
    print(f'  [iwara.tv] browser found {label}: {download_url}')

    cdn_headers: dict[str, str] = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://www.iwara.tv/',
    }
    return _direct_fetch(download_url, download_dir, '_iwara_temp', cdn_headers)


def _iwara_login() -> str | None:
    """POST credentials to the iwara API and return a Bearer token, or None."""
    email = _get_secret('IWARA_EMAIL').strip()
    password = _get_secret('IWARA_PASSWORD').strip()
    if not email or not password:
        return None
    data = json.dumps({'email': email, 'password': password}).encode()
    req = urllib.request.Request(
        'https://api.iwara.tv/user/login',
        data=data,
        headers={
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Origin': 'https://www.iwara.tv',
            'Referer': 'https://www.iwara.tv/',
        },
        method='POST',
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                return result.get('token')
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            if e.code == 429:
                wait = [1, 5, 10][attempt]
                print(f'  [iwara.tv] rate-limited (429) — waiting {wait}s before retry {attempt + 1}/3...')
                time.sleep(wait)
                continue
            print(f'  [iwara.tv] login failed: HTTP {e.code} — {body}')
            return None
        except Exception as e:
            print(f'  [iwara.tv] login failed: {e}')
            return None
    print('  [iwara.tv] login failed after 3 attempts (rate limited)')
    return None


def download_iwara(_driver, url: str, download_dir: str) -> bool:
    """Download from iwara.tv via the REST API (requires IWARA_EMAIL / IWARA_PASSWORD in .env)."""
    global _iwara_token

    try:
        # URL format: https://www.iwara.tv/video/{id}/{optional-slug}
        path_parts = urlparse(url).path.strip('/').split('/')
        if len(path_parts) < 2 or path_parts[0] != 'video':
            print('  [iwara.tv] unrecognised URL — expected /video/{id}/...')
            return False
        video_id = path_parts[1]

        # Authenticate once per session.
        if _iwara_token is None:
            _iwara_token = _iwara_login()
            if _iwara_token is None:
                print('  [iwara.tv] login failed — set IWARA_EMAIL and IWARA_PASSWORD in .env')
                return False

        api_headers: dict[str, str] = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Origin': 'https://www.iwara.tv',
            'Referer': 'https://www.iwara.tv/',
            'Authorization': f'Bearer {_iwara_token}',
        }

        # Fetch video metadata.
        try:
            meta_req = urllib.request.Request(
                f'https://api.iwara.tv/video/{video_id}',
                headers=api_headers,
            )
            with urllib.request.urlopen(meta_req) as resp:
                video_meta = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            if e.code == 401:
                print('  [iwara.tv] 401 Unauthorized — credentials may be wrong.')
                print('  Re-run setup_credentials.py to update them.')
            elif e.code == 403:
                print('  [iwara.tv] 403 Forbidden — token rejected.')
                print('  Re-run setup_credentials.py to update your credentials.')
            elif e.code == 404:
                print('  [iwara.tv] 404 — video not found or account lacks access.')
            else:
                print(f'  [iwara.tv] HTTP {e.code} fetching video metadata: {body}')
            return False

        # fileUrl is an API endpoint that returns a JSON array of quality options.
        # file    is the raw upload metadata (height, size, etc.).
        file_list_url = video_meta.get('fileUrl') or ''
        if not file_list_url:
            print(f'  [iwara.tv] no fileUrl in metadata — keys: {list(video_meta.keys())}')
            return False

        cdn_headers: dict[str, str] = {
            'User-Agent': api_headers['User-Agent'],
            'Referer': 'https://www.iwara.tv/',
        }

        # Fetch the quality list — requires Authorization to get all qualities.
        try:
            fl_req = urllib.request.Request(file_list_url, headers=api_headers)
            with urllib.request.urlopen(fl_req) as resp:
                content_type = resp.headers.get('Content-Type', '')
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            print(f'  [iwara.tv] HTTP {e.code} fetching quality list: {body}')
            return False

        try:
            files: list[dict] = json.loads(raw)
        except Exception:
            print(f'  [iwara.tv] quality list response is not JSON ({content_type}): {raw[:200]!r}')
            return False

        if not isinstance(files, list) or not files:
            print(f'  [iwara.tv] unexpected quality list: {files!r}')
            return False

        available = [f.get('name') for f in files]
        print(f'  [iwara.tv] available qualities: {available}')

        # If the API only returns low-quality transcodes (known site bug),
        # fall back to the browser which renders the full quality list.
        if all((f.get('name') or '').lower() in ('preview', '360') for f in files):
            print('  [iwara.tv] API quality list is incomplete — switching to browser')
            return _download_iwara_browser(_driver, url, download_dir)

        # Pick the best quality within MAX_RESOLUTION.
        # Names are e.g. "preview", "360", "540", "720", "1080", "Source".
        max_res = int(os.getenv('MAX_RESOLUTION', '1080'))

        def _iwara_res(entry: dict) -> int:
            name = (entry.get('name') or '').lower()
            if name == 'source':
                return 9999
            if name == 'preview':
                return 0
            digits = ''.join(c for c in name if c.isdigit())
            return int(digits) if digits else 0

        files_sorted = sorted(files, key=_iwara_res, reverse=True)

        # If source is available and every transcode is below max_res, prefer source —
        # a lower-resolution transcode is not better than the original.
        best_transcode = max((_iwara_res(f) for f in files_sorted if _iwara_res(f) not in (0, 9999)), default=0)
        source_entries = [f for f in files_sorted if _iwara_res(f) == 9999]
        if source_entries and best_transcode < max_res:
            chosen = source_entries[0]
        else:
            chosen = files_sorted[0]
            for candidate in files_sorted:
                if _iwara_res(candidate) <= max_res:
                    chosen = candidate
                    break

        src = chosen.get('src') or {}
        # Prefer view URL — some CDNs serve the file on view and metadata on download.
        download_url = src.get('view') or src.get('download') or ''
        if not download_url:
            print(f'  [iwara.tv] no URL in chosen quality: {chosen}')
            return False

        # URLs are protocol-relative (//host/path) — prepend https:.
        if download_url.startswith('//'):
            download_url = 'https:' + download_url

        res_label = chosen.get('name', '?')
        print(f'  [iwara.tv] downloading {res_label}...')

        return _direct_fetch(download_url, download_dir, '_iwara_temp', cdn_headers)

    except Exception as e:
        print(f'  [iwara.tv] handler error: {e}')

    return False


# Map each KNOWN_DOMAINS entry to its handler.
# When adding a new domain, add it to KNOWN_DOMAINS above AND here.
DOMAIN_HANDLERS = {
    'hanime1.me':      download_hanime,
    'hanime.tv':       download_hanimetv,
    'gofile.io':       download_gofile,
    'iwara.tv':        download_iwara,
    'pixeldrain.com':  download_pixeldrain,
    'rule34video.com': download_rule34video,
    'rule34.xxx':      download_rule34xxx,
    'fap-nation.org':  download_fapnation,
}


# ---------------------------------------------------------------------------
# Main scanning + download logic
# ---------------------------------------------------------------------------

def _cleanup_temp_files(folder: str):
    """Remove any leftover temp files created by the download handlers."""
    for f in os.listdir(folder):
        stem = Path(f).stem  # strips last extension, e.g. _iwara_temp.mp4.part → _iwara_temp.mp4
        outer_stem = Path(stem).stem  # strips one more, e.g. _iwara_temp.mp4 → _iwara_temp
        is_temp = outer_stem.endswith('_temp') or stem.endswith('_temp')
        is_part = f.endswith('.part')
        if is_temp or is_part:
            path = os.path.join(folder, f)
            try:
                os.remove(path)
                print(f'  [cleanup] removed temp file: {f}')
            except OSError as e:
                print(f'  [cleanup] could not remove {f}: {e}')


def _video_exists(folder: str, basename: str) -> str | None:
    """Return the path of an existing video for *basename* in *folder*, or None.
    Uses MIME type detection so any video format is recognized, not just a fixed list.
    """
    for f in os.listdir(folder):
        stem, _ = os.path.splitext(f)
        if stem != basename:
            continue
        mime, _ = mimetypes.guess_type(f)
        if mime and mime.startswith('video/'):
            return os.path.join(folder, f)
    return None


def _update_env_file(key: str, value: str):
    """Update a key=value pair in the .env file, preserving all other lines."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.exists(env_path):
        return
    lines = []
    found = False
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith(f'{key}='):
                lines.append(f'{key}={value}\n')
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f'{key}={value}\n')
    with open(env_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)


def _match_links_to_funscripts(links: list[str], funscript_paths: list[str]) -> dict[str, str]:
    """
    Fuzzy-match each link URL to the best-fitting funscript stem using token
    overlap.  Returns {link: funscript_stem}.

    Scoring: tokenise both the funscript name and the URL path (split on
    non-alphanumeric chars, ignore tokens ≤ 2 chars), then compute
    (matching tokens) / (total funscript tokens).  The funscript with the
    highest score wins; ties go to the first funscript.  If the best score
    is below 0.25 the link is left unmatched (falls back to first funscript).
    """
    def tokenize(s: str) -> list[str]:
        return [t for t in re.split(r'[^a-z0-9]+', s.lower()) if len(t) > 2]

    stems = [Path(p).stem for p in funscript_paths]
    stem_tokens = {stem: set(tokenize(stem)) for stem in stems}
    fallback = stems[0]

    result: dict[str, str] = {}
    for link in links:
        url_tokens = set(tokenize(link))
        best_stem, best_score = fallback, 0.0
        for stem, fs_tokens in stem_tokens.items():
            if not fs_tokens:
                continue
            score = len(fs_tokens & url_tokens) / len(fs_tokens)
            if score > best_score:
                best_score, best_stem = score, stem
        result[link] = best_stem if best_score >= 0.25 else fallback
    return result


def collect_tasks(base_path: str) -> tuple[list, list]:
    """
    Walk *base_path* looking for folders that contain both a description.json
    and at least one .funscript.  Returns (tasks, failures).
    Unsupported domains are added to failures instead of aborting the run.
    """
    tasks = []
    failures = []

    axis_suffixes = ('.surge', '.pitch', '.roll', '.twist', '.sway')

    for root, dirs, files in os.walk(base_path):
        if 'description.json' not in files:
            continue

        all_funscripts = glob.glob(os.path.join(glob.escape(root), '*.funscript'))
        if not all_funscripts:
            print(f"[SKIP] No .funscript in: {root}")
            continue
        main_scripts = [fs for fs in all_funscripts
                        if not any(Path(fs).stem.endswith(s) for s in axis_suffixes)]
        if not main_scripts:
            main_scripts = all_funscripts
        funscript_basename = Path(main_scripts[0]).stem

        desc_path = os.path.join(root, 'description.json')
        links = extract_links_from_description(desc_path)

        if not links:
            print(f"[SKIP] No links in: {desc_path}")
            continue

        validated_links = []
        for link in links:
            domain = get_domain(link)
            if any(domain == s or domain.endswith('.' + s) for s in SKIP_DOMAINS):
                continue  # reference/social link — nothing to download
            try:
                check_domain(link)
                validated_links.append(link)
            except UnknownDomainError as e:
                print(f"[ERROR] {e}")
                print(f"        Skipping link: {link}  (in {root})")
                failures.append({
                    'link': link,
                    'funscript_name': funscript_basename,
                    'save_directory': root,
                    'domain': domain,
                })

        if not validated_links:
            continue

        # When there are multiple funscripts AND multiple links, fuzzy-match
        # each link to the funscript whose name shares the most URL tokens.
        if len(main_scripts) > 1 and len(validated_links) > 1:
            link_to_stem = _match_links_to_funscripts(validated_links, main_scripts)
            stem_to_links: dict[str, list[str]] = {}
            for link, stem in link_to_stem.items():
                stem_to_links.setdefault(stem, []).append(link)
            for stem, stem_links in stem_to_links.items():
                print(f"  [fuzzy] '{stem}' matched {len(stem_links)} link(s)")
                tasks.append({'folder': root, 'basename': stem, 'links': stem_links})
        else:
            tasks.append({
                'folder': root,
                'basename': funscript_basename,
                'links': validated_links,
            })

    return tasks, failures


def _write_playlist(base_path: str, newly_downloaded: list[str] | None = None):
    """
    Scan *base_path* recursively for video files and write playlist.m3u8.
    Files are sorted newest-first by modification time so the most recently
    downloaded videos appear at the top when opened in a media player.
    Temp files and the playlist itself are excluded.

    If *newly_downloaded* is provided, also write playlist_new.m3u8 containing
    only those files (in the same newest-first order).
    """
    def _write_m3u8(path: str, video_paths: list[str]):
        with open(path, 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
            for video_path in video_paths:
                title = Path(video_path).stem
                rel_path = os.path.relpath(video_path, base_path).replace('\\', '/')
                f.write(f'#EXTINF:-1,{title}\n')
                f.write(f'{rel_path}\n')

    video_files = []
    for root, dirs, files in os.walk(base_path):
        for f in files:
            # Skip temp files left by handlers
            if Path(f).stem.endswith('_temp'):
                continue
            full_path = os.path.join(root, f)
            mime, _ = mimetypes.guess_type(full_path)
            if mime and mime.startswith('video/'):
                video_files.append(full_path)

    if not video_files:
        return

    # Newest downloads first
    video_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    playlist_path = os.path.join(base_path, 'playlist.m3u8')
    _write_m3u8(playlist_path, video_files)
    print(f'\nPlaylist updated ({len(video_files)} videos): {playlist_path}')

    if newly_downloaded:
        new_sorted = sorted(
            (p for p in newly_downloaded if os.path.exists(p)),
            key=lambda p: os.path.getmtime(p),
            reverse=True,
        )
        if new_sorted:
            new_path = os.path.join(base_path, 'playlist_new.m3u8')
            _write_m3u8(new_path, new_sorted)
            print(f'New-downloads playlist ({len(new_sorted)} videos): {new_path}')


def _write_failures_csv(base_path: str, failures: list):
    """Write failed download entries to failed_downloads.csv in *base_path*."""
    if not failures:
        return
    csv_path = os.path.join(base_path, 'failed_downloads.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['link', 'funscript_name', 'save_directory', 'domain'])
        writer.writeheader()
        writer.writerows(failures)
    print(f"\nFailed downloads ({len(failures)}) written to: {csv_path}")


def find_and_download(base_path: str):
    tasks, failures = collect_tasks(base_path)

    if not tasks:
        print("No valid download tasks found.")
        _write_failures_csv(base_path, failures)
        return

    print(f"\nFound {len(tasks)} folder(s) to process:")
    for t in tasks:
        print(f"  {t['basename']}")
        for link in t['links']:
            print(f"    -> {link}")

    confirm = input("\nProceed with downloads? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Aborted.")
        return

    driver = setup_driver(tasks[0]['folder'])

    # State tracked so KeyboardInterrupt can finish/clean the active download.
    current_folder: str = tasks[0]['folder']
    current_before_files: set[str] = set()
    current_basename: str = ''
    current_link_idx: int = 0

    newly_downloaded: list[str] = []
    total = len(tasks)
    try:
        for task_idx, task in enumerate(tasks, start=1):
            folder   = task['folder']
            basename = task['basename']
            links    = task['links']

            current_folder = folder
            current_basename = basename

            print(f"\n[{task_idx}/{total}] {basename}")
            _cleanup_temp_files(folder)
            set_download_dir(driver, folder)

            existing = _video_exists(folder, basename)
            if existing:
                print(f"  [SKIP] video already exists: {os.path.basename(existing)}")
                continue

            for link_idx, link in enumerate(links):
                domain  = check_domain(link)
                handler = DOMAIN_HANDLERS[domain]

                before_files: set[str] = set(os.listdir(str(folder)))
                current_before_files = before_files
                current_link_idx = link_idx
                print(f"  [{domain}] {link}")
                print("  Downloading...")

                try:
                    triggered = handler(driver, link, folder)
                except CloudflareBlockedError as cf_err:
                    print(f'  [cloudflare] {cf_err}')
                    answer = input('  Switch to windowed mode and retry? (y/n): ').strip().lower()
                    if answer != 'y':
                        failures.append({
                            'link': link,
                            'funscript_name': basename,
                            'save_directory': folder,
                            'domain': domain,
                        })
                        continue
                    # Restart the driver in windowed mode for this run only.
                    driver.quit()
                    os.environ['BROWSER_HEADLESS'] = 'false'
                    print('  Restarting browser in windowed mode...')
                    driver = setup_driver(folder)
                    set_download_dir(driver, folder)
                    before_files = set(os.listdir(str(folder)))
                    try:
                        triggered = handler(driver, link, folder)
                    except CloudflareBlockedError:
                        print('  Still blocked after switching to windowed mode.')
                        failures.append({
                            'link': link,
                            'funscript_name': basename,
                            'save_directory': folder,
                            'domain': domain,
                        })
                        continue

                if not triggered:
                    print("  Could not trigger download — check the handler for this domain.")
                    failures.append({
                        'link': link,
                        'funscript_name': basename,
                        'save_directory': folder,
                        'domain': domain,
                    })
                    continue

                downloaded = wait_for_download(folder, before_files)

                if downloaded is None:
                    print("  Download did not complete.")
                    failures.append({
                        'link': link,
                        'funscript_name': basename,
                        'save_directory': folder,
                        'domain': domain,
                    })
                    continue

                ext = os.path.splitext(downloaded)[1]
                if link_idx == 0:
                    dest_name = basename + ext
                else:
                    dest_name = f"{basename} ({link_idx + 1}){ext}"

                dest_path = os.path.join(folder, dest_name)

                if os.path.exists(dest_path):
                    print(f"  Already exists, skipping rename: {dest_name}")
                else:
                    os.rename(downloaded, dest_path)
                    print(f"  Saved as: {dest_name}")
                    newly_downloaded.append(dest_path)

            _cleanup_temp_files(folder)

    except KeyboardInterrupt:
        print('\n\nInterrupted — waiting up to 120 s for the active download to finish...')
        downloaded = wait_for_download(current_folder, current_before_files, timeout=120)
        if downloaded:
            ext = os.path.splitext(downloaded)[1]
            if current_link_idx == 0:
                dest_name = current_basename + ext
            else:
                dest_name = f"{current_basename} ({current_link_idx + 1}){ext}"
            dest_path = os.path.join(current_folder, dest_name)
            if not os.path.exists(dest_path):
                os.rename(downloaded, dest_path)
                newly_downloaded.append(dest_path)
                print(f'  Saved as: {dest_name}')
        else:
            print('  Download did not complete in time — removing temp files.')
            _cleanup_temp_files(current_folder)

    finally:
        driver.quit()
        _write_failures_csv(base_path, failures)
        _write_playlist(base_path, newly_downloaded)


def main():
    base_path = input("Enter full file path to scan for downloads: ").strip()
    if not os.path.isdir(base_path):
        print(f"Directory not found: {base_path}")
        return
    find_and_download(base_path)


if __name__ == "__main__":
    main()
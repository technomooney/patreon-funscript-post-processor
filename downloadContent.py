import base64
import csv
import json
import mimetypes
import os
import shutil
import time
import glob
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

load_dotenv()


# Add new domains here along with a handler function in DOMAIN_HANDLERS below.
# If a URL's domain is not listed, the script will raise an error and skip it.
KNOWN_DOMAINS = [
    'hanime1.me',
    'hanime.tv',
    'gofile.io',
    'pixeldrain.com',
    'rule34video.com',
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
    }
    options.add_experimental_option('prefs', prefs)

    headless = os.getenv('BROWSER_HEADLESS', 'false').strip().lower() == 'true'
    if headless:
        # --headless=new is Chrome's modern headless mode; much harder to detect
        # than the legacy --headless flag.  Set BROWSER_HEADLESS=false in .env
        # if a site starts blocking the automation.
        options.add_argument('--headless=new')

    browser = _find_browser()
    if browser is None:
        raise RuntimeError('No Chromium-compatible browser found. Install Brave, Chrome, or Chromium.')
    print(f'Using browser: {browser} ({"headless" if headless else "windowed"})')

    driver = uc.Chrome(options=options, browser_executable_path=browser)
    return driver


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
            time.sleep(2)
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
    """Download *video_url* straight to *download_dir* using urllib, no browser needed."""
    ext = os.path.splitext(urlparse(video_url).path)[1] or '.mp4'
    temp_path = os.path.join(download_dir, f'{temp_prefix}{ext}')
    req = urllib.request.Request(video_url, headers=headers)
    with urllib.request.urlopen(req) as response:
        size_mb = int(response.headers.get('Content-Length', 0)) / 1024 / 1024
        if size_mb:
            print(f'  file size: {size_mb:.1f} MB')
        with open(temp_path, 'wb') as f:
            while chunk := response.read(65536):
                f.write(chunk)
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
        time.sleep(2)  # let the JS-heavy page render

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
        time.sleep(3)  # let the video player initialize

        # Read the href from the download anchor and navigate directly in the same
        # tab — avoids relying on target="_blank" opening a new tab reliably.
        download_btn = driver.find_element(By.ID, 'downloadBtn')
        download_page_url = download_btn.get_attribute('href')
        if not download_page_url:
            print('  [hanime1.me] downloadBtn has no href')
            return False

        driver.get(download_page_url)
        time.sleep(3)  # let the download table render

        # The resolution table has <a data-url="...1080p.mp4?token=..."> entries.
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

    except Exception as e:
        print(f'  [hanime1.me] handler error: {e}')

    return False


def download_rule34video(driver, url: str, download_dir: str) -> bool:
    """Navigate to a rule34video.com video page and download the highest quality."""
    driver.get(url)

    try:
        time.sleep(3)

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
        api_key = os.getenv('PIXELDRAIN_API_KEY', '').strip()
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
        time.sleep(3)

        # Step 1: click the top-level DOWNLOAD button to open the quality selection page.
        download_btn = driver.find_element(
            By.XPATH,
            '//span[contains(@class,"hvpabb-text") and '
            'contains(normalize-space(.),"DOWNLOAD")]'
        )
        original_handles = set(driver.window_handles)
        download_btn.click()
        time.sleep(2)

        # Switch to the new tab if one was opened.
        _switch_to_new_tab(driver, original_handles)

        # Step 2: click "Get Download Links" to reveal the quality buttons.
        get_links_btn = driver.find_element(
            By.XPATH,
            '//div[contains(@class,"btn__content") and '
            'contains(normalize-space(.),"Get Download Links")]'
        )
        driver.execute_script('arguments[0].click()', get_links_btn)
        time.sleep(2)

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


# Map each KNOWN_DOMAINS entry to its handler.
# When adding a new domain, add it to KNOWN_DOMAINS above AND here.
DOMAIN_HANDLERS = {
    'hanime1.me':     download_hanime,
    'hanime.tv':      download_hanimetv,
    'gofile.io':      download_gofile,
    'pixeldrain.com': download_pixeldrain,
    'rule34video.com': download_rule34video,
}


# ---------------------------------------------------------------------------
# Main scanning + download logic
# ---------------------------------------------------------------------------

def _cleanup_temp_files(folder: str):
    """Remove any leftover temp files created by the download handlers."""
    for f in os.listdir(folder):
        stem, _ = os.path.splitext(f)
        if stem.endswith('_temp'):
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


def collect_tasks(base_path: str) -> tuple[list, list]:
    """
    Walk *base_path* looking for folders that contain both a description.json
    and at least one .funscript.  Returns (tasks, failures).
    Unsupported domains are added to failures instead of aborting the run.
    """
    tasks = []
    failures = []

    for root, dirs, files in os.walk(base_path):
        if 'description.json' not in files:
            continue

        funscript_basename = get_funscript_basename(root)
        if funscript_basename is None:
            print(f"[SKIP] No .funscript in: {root}")
            continue

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
                    'description_json_path': desc_path,
                    'domain': domain,
                })

        if not validated_links:
            continue

        tasks.append({
            'folder': root,
            'basename': funscript_basename,
            'links': validated_links,
        })

    return tasks, failures


def _write_failures_csv(base_path: str, failures: list):
    """Write failed download entries to failed_downloads.csv in *base_path*."""
    if not failures:
        return
    csv_path = os.path.join(base_path, 'failed_downloads.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['link', 'funscript_name', 'description_json_path', 'domain'])
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

    try:
        for task in tasks:
            folder   = task['folder']
            basename = task['basename']
            links    = task['links']
            desc_path = os.path.join(folder, 'description.json')

            print(f"\n--- {basename} ---")
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
                print(f"  [{domain}] {link}")

                triggered = handler(driver, link, folder)
                if not triggered:
                    print("  Could not trigger download — check the handler for this domain.")
                    failures.append({
                        'link': link,
                        'funscript_name': basename,
                        'description_json_path': desc_path,
                        'domain': domain,
                    })
                    continue

                print("  Waiting for download to complete...")
                downloaded = wait_for_download(folder, before_files)

                if downloaded is None:
                    print("  Download did not complete.")
                    failures.append({
                        'link': link,
                        'funscript_name': basename,
                        'description_json_path': desc_path,
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

            _cleanup_temp_files(folder)

    finally:
        driver.quit()
        _write_failures_csv(base_path, failures)


def main():
    base_path = input("Enter full file path to scan for downloads: ").strip()
    if not os.path.isdir(base_path):
        print(f"Directory not found: {base_path}")
        return
    find_and_download(base_path)


if __name__ == "__main__":
    main()
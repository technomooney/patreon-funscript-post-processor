import base64
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


def wait_for_download(download_dir: str, before_files: set[str], timeout: int | None = None):
    """
    Poll *download_dir* until a new fully-written file appears.
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

def _direct_fetch(video_url: str, download_dir: str, temp_prefix: str, referer: str) -> bool:
    """Download *video_url* straight to *download_dir* using urllib, no browser needed."""
    ext = os.path.splitext(urlparse(video_url).path)[1] or '.mp4'
    temp_path = os.path.join(download_dir, f'{temp_prefix}{ext}')
    req = urllib.request.Request(video_url, headers={'Referer': referer})
    with urllib.request.urlopen(req) as response:
        size_mb = int(response.headers.get('Content-Length', 0)) / 1024 / 1024
        if size_mb:
            print(f'  file size: {size_mb:.1f} MB')
        with open(temp_path, 'wb') as f:
            shutil.copyfileobj(response, f)
    return True


def _get_max_resolution() -> int:
    """Read MAX_RESOLUTION from the environment (default 1080)."""
    try:
        return int(os.getenv('MAX_RESOLUTION', '1080'))
    except ValueError:
        return 1080


def _parse_resolution(text: str) -> int:
    """Return the first resolution value (e.g. 1080) found in *text*, or 0."""
    for res in [2160, 1080, 720, 480, 360, 240]:
        if str(res) in text:
            return res
    return 0


def _pick_best(candidates: list, resolution_fn) -> tuple:
    """
    Return (best_candidate, resolution) honouring MAX_RESOLUTION.
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
# Per-domain download handlers — added in subsequent commits
# ---------------------------------------------------------------------------

# Map each KNOWN_DOMAINS entry to its handler.
# When adding a new domain, add it to KNOWN_DOMAINS above AND here.
DOMAIN_HANDLERS: dict = {}


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
    Uses MIME type detection so any video format is recognised, not just a fixed list.
    """
    for f in os.listdir(folder):
        stem, _ = os.path.splitext(f)
        if stem != basename:
            continue
        mime, _ = mimetypes.guess_type(f)
        if mime and mime.startswith('video/'):
            return os.path.join(folder, f)
    return None


def collect_tasks(base_path: str) -> list:
    """
    Walk *base_path* looking for folders that contain both a description.json
    and at least one .funscript.  Returns a list of task dicts.
    Folders whose description.json contains an unsupported domain are skipped
    with an error message (but do not abort the whole run).
    """
    tasks = []

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

        if not validated_links:
            continue

        tasks.append({
            'folder': root,
            'basename': funscript_basename,
            'links': validated_links,
        })

    return tasks


def find_and_download(base_path: str):
    tasks = collect_tasks(base_path)

    if not tasks:
        print("No valid download tasks found.")
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
                    continue

                print("  Waiting for download to complete...")
                downloaded = wait_for_download(folder, before_files)

                if downloaded is None:
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


def main():
    base_path = input("Enter full file path to scan for downloads: ").strip()
    if not os.path.isdir(base_path):
        print(f"Directory not found: {base_path}")
        return
    find_and_download(base_path)


if __name__ == "__main__":
    main()
import base64
import concurrent.futures
import csv
import hashlib
import re
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import glob
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, quote, unquote
from dotenv import load_dotenv
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
import urllib3.exceptions

load_dotenv()

# Prepend the venv bin directory to PATH so that ffmpeg/ffprobe installed there
# by setup_config.py are found by shutil.which even when the venv isn't activated.
_VENV_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '.venv',
                         'Scripts' if sys.platform == 'win32' else 'bin')
if os.path.isdir(_VENV_BIN) and _VENV_BIN not in os.environ.get('PATH', ''):
    os.environ['PATH'] = _VENV_BIN + os.pathsep + os.environ.get('PATH', '')

# Ensure stdout is UTF-8 on all platforms so Unicode filenames print cleanly.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Matches any ANSI/VT escape sequence (e.g. ESC[8m makes text invisible).
_ANSI_ESCAPE_RE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def _safe(s: str) -> str:
    """Strip ANSI escape sequences and C0/C1 control characters from *s*.

    Strips C0 (U+0000-U+001F), DEL (U+007F), and C1 (U+0080-U+009F).
    C1 characters are particularly dangerous: U+0090 (DCS) and U+009D (ST)
    appear in mojibake filenames and put the terminal into a hidden-input state,
    making all subsequent output invisible.
    Printable Unicode (CJK, emoji, etc.) is preserved.
    """
    s = _ANSI_ESCAPE_RE.sub('', s)
    return ''.join(c for c in s if not (ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f) or c in '\n\t')

# ---------------------------------------------------------------------------
# In-place status line — updates a single terminal line without scrolling.
# A monkey-patched print() clears the status before every real log line.
# Disabled automatically when stdout is not a TTY (e.g. log files, CI).
# ---------------------------------------------------------------------------

_STATUS_TTY: bool = sys.stdout.isatty()
_status_active: str = ''        # current status text, '' means nothing shown
_builtin_print = print          # save reference before we shadow it


def _set_status(msg: str) -> None:
    """Write *msg* as an overwriting status line (no newline, TTY only)."""
    global _status_active
    if not _STATUS_TTY:
        return
    cols = shutil.get_terminal_size(fallback=(120, 24)).columns
    line = msg[:cols - 1].ljust(cols - 1)
    sys.stdout.write(f'\r{line}')
    sys.stdout.flush()
    _status_active = msg


def _clear_status() -> None:
    """Erase the status line so a regular print can take its place."""
    global _status_active
    if not _STATUS_TTY or not _status_active:
        return
    cols = shutil.get_terminal_size(fallback=(120, 24)).columns
    sys.stdout.write('\r' + ' ' * (cols - 1) + '\r')
    sys.stdout.flush()
    _status_active = ''


def print(*args, **kwargs):  # noqa: A001
    _clear_status()
    _builtin_print(*args, **kwargs)


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


# ------------------------------------------------------------------------
# File hashing — used for duplicate detection within a session and on disk.
# ---------------------------------------------------------------------------

def _file_hash(path: str, block_size: int = 1 << 20) -> str:
    """Return the SHA-256 hex digest of *path*, reading in 1 MB chunks."""
    h = hashlib.sha256()
    name = os.path.basename(path)
    done = 0
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(block_size), b''):
            h.update(chunk)
            done += len(chunk)
            _set_status(f'  hashing {_safe(name)}... {done // (1 << 20)} MB')
    return h.hexdigest()


# Maps hash → final saved path for every file downloaded in this session.
# Lets us detect when two different links resolve to the exact same content.
_session_hashes: dict[str, str] = {}

# Original filename captured by _direct_fetch from Content-Disposition or URL.
# Reset before each download attempt; read by find_and_download after the handler returns.
_last_fetch_original_name: str | None = None

# Set True by _direct_fetch when a pre-download check determines the file already exists.
# find_and_download reads this to distinguish "skipped duplicate" from "download failed".
_last_download_skipped: bool = False

# Set by _precheck_url when the remote file is the same content but a better candidate
# (smaller size at equal/better quality).  _direct_fetch reads this after a successful
# download and deletes the old file so the new one takes its place.
_precheck_replace_target: str | None = None

# Extensions that mimetypes may not register on all systems (e.g. .mkv on Linux)
# but are unambiguously video containers, including common AV1 delivery formats.
_VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    '.mp4', '.m4v', '.mkv', '.webm', '.avi', '.mov', '.wmv',
    '.flv', '.ts', '.m2ts', '.mts', '.mpg', '.mpeg', '.3gp',
})


def _is_video_filename(filename: str) -> bool:
    """Return True if *filename* appears to be a video file.

    Uses mimetypes first, then falls back to a known-extension set so that
    containers like .mkv (often unregistered on Linux) and AV1-in-WebM are
    not missed.
    """
    mime, _ = mimetypes.guess_type(filename)
    if mime and mime.startswith('video/'):
        return True
    _, ext = os.path.splitext(filename)
    return ext.lower() in _VIDEO_EXTENSIONS


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
    'eporner.com',
    'disk.yandex.com',
    'disk.yandex.ru',
    'mega.nz',
    'mega.co.nz',
    'rule34video.party',
    'spankbang.com',
    'faptap.net',
    'e621.net',
]

# Links to these domains are creator pages / social profiles — no file to download.
# They are silently skipped without an error.
SKIP_DOMAINS = {
    'patreon.com',
    'subscribestar.adult',
    'fanbox.cc',
    'discuss.eroscripts.com',
    'carrd.co',
    'discord.gg',    # invite/server links — not downloadable (CDN links are fine)
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


def _ensure_driver_alive(driver, folder: str):
    """Probe the driver; if it is not responding, quit and return a fresh instance.

    Uses driver.current_url as a lightweight liveness check — it exercises the
    WebDriver wire protocol without causing any navigation.  Any exception
    (WebDriverException, connection refused, process dead, etc.) triggers a
    restart.

    The new driver has its download directory set to *folder* via CDP so the
    caller does not need to call set_download_dir() again for that task.
    """
    try:
        _ = driver.current_url   # lightweight probe — no navigation
        return driver
    except (WebDriverException, urllib3.exceptions.MaxRetryError, urllib3.exceptions.ReadTimeoutError):
        print('  [browser] driver not responding — restarting...')
        try:
            driver.quit()
        except (WebDriverException, urllib3.exceptions.MaxRetryError, urllib3.exceptions.ReadTimeoutError):
            pass
        new_driver = setup_driver(folder)
        set_download_dir(new_driver, folder)
        return new_driver


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
    t0 = time.time()
    while True:
        current: set[str] = set(os.listdir(download_dir))
        new_files = current - before_files
        # Show size of the largest in-progress partial file so we know it's moving.
        partials = [f for f in new_files if f.endswith(('.part', '.crdownload', '.tmp'))]
        if partials:
            try:
                partial_mb = max(
                    os.path.getsize(os.path.join(download_dir, p)) for p in partials
                ) / (1 << 20)
                _set_status(f'  waiting for browser download... {partial_mb:.1f} MB so far'
                            f' ({int(time.time() - t0)}s)')
            except OSError:
                _set_status(f'  waiting for browser download... ({int(time.time() - t0)}s)')
        else:
            _set_status(f'  waiting for browser download... ({int(time.time() - t0)}s)')
        complete = [
            f for f in new_files
            if not f.endswith(('.part', '.crdownload', '.tmp'))
            and os.path.isfile(os.path.join(download_dir, f))
        ]
        if complete:
            return os.path.join(download_dir, complete[0])
        if deadline is not None and time.time() >= deadline:
            return None
        time.sleep(1)


# ---------------------------------------------------------------------------
# Shared download utilities
# ---------------------------------------------------------------------------

def _ext_from_response(response, url: str) -> str:
    """Determine the file extension from response headers or URL, defaulting to .mp4.

    Priority:
    1. Content-Disposition filename (has the original name the server chose)
    2. Extension present in the URL path
    3. mimetypes guess from Content-Type
    4. Fall back to .mp4
    """
    cd = response.headers.get('Content-Disposition', '')
    if cd:
        # RFC 5987 encoded form: filename*=UTF-8''name.ext
        m = re.search(r"filename\*=[^']*''([^\s;]+)", cd, re.IGNORECASE)
        if not m:
            m = re.search(r'filename=["\']?([^"\';\r\n]+)["\']?', cd, re.IGNORECASE)
        if m:
            ext = os.path.splitext(m.group(1).strip())[1]
            if ext:
                return ext

    ext = os.path.splitext(urlparse(url).path)[1]
    if ext:
        return ext

    ct = response.headers.get('Content-Type', '').split(';')[0].strip()
    if ct:
        guessed = mimetypes.guess_extension(ct)
        if guessed:
            return guessed

    return '.mp4'


def _decode_filename(name: str) -> str:
    """Decode all known encoding layers a filename might have picked up.

    Applies in order:
      1. Percent-decoding  (%E3%80%8C → 「)
      2. Mojibake reversal — if the string looks like UTF-8 bytes that were
         misread as cp1252/Latin-1 (e.g. ãé¸£æ½® → 「鸣潮), re-encode as
         cp1252 and decode as UTF-8.  Accepted only when the result is shorter
         (multi-byte sequences collapsed into single codepoints) so plain ASCII
         or already-correct Unicode passes through unchanged.
    """
    # Step 1: percent-decode (idempotent for strings without %)
    decoded = unquote(name)
    # Step 2: mojibake reversal
    for encoding in ('cp1252', 'latin-1'):
        try:
            fixed = decoded.encode(encoding).decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != decoded and len(fixed) < len(decoded):
            decoded = fixed
            break
    return decoded


def _name_from_response(response, url: str) -> str | None:
    """Extract the original filename from Content-Disposition, falling back to the URL path.

    Returns None if no meaningful name can be found.
    Both percent-encoding and cp1252/Latin-1 mojibake are corrected.
    """
    cd = response.headers.get('Content-Disposition', '')
    if cd:
        m = re.search(r"filename\*=[^']*''([^\s;]+)", cd, re.IGNORECASE)
        if not m:
            m = re.search(r'filename=["\']?([^"\';\r\n]+)["\']?', cd, re.IGNORECASE)
        if m:
            return _decode_filename(m.group(1).strip())
    # Fall back to the last path component of the URL when it has an extension.
    url_basename = urlparse(url).path.rstrip('/').split('/')[-1]
    if url_basename and '.' in url_basename:
        return _decode_filename(url_basename)
    return None


def _truncate_filename(name: str, max_bytes: int = 255) -> str:
    """Shorten *name* so the UTF-8-encoded form fits within *max_bytes*.

    The file extension is preserved; only the stem is cut.
    Avoids splitting mid-codepoint by decoding with errors='ignore'.
    """
    if len(name.encode('utf-8')) <= max_bytes:
        return name
    stem, ext = os.path.splitext(name)
    ext_bytes = ext.encode('utf-8')
    allowed = max_bytes - len(ext_bytes)
    truncated_stem = stem.encode('utf-8')[:allowed].decode('utf-8', errors='ignore')
    return truncated_stem + ext


_AUDIO_ONLY_EXTENSIONS: frozenset[str] = frozenset({'.m4a', '.aac', '.mp3', '.ogg', '.opus', '.flac', '.wav'})


def _fix_av_extension(path: str) -> str:
    """If *path* has an audio-only extension but actually contains a video stream, rename to .mp4.

    Returns the (possibly new) path.  No-ops when ffprobe is unavailable or the extension is fine.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in _AUDIO_ONLY_EXTENSIONS:
        return path
    ffprobe = shutil.which('ffprobe')
    if ffprobe is None:
        return path
    cmd = [ffprobe, '-v', 'error', '-select_streams', 'v:0',
           '-show_entries', 'stream=codec_type', '-of', 'json', path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        if data.get('streams'):
            new_path = path[:-len(ext)] + '.mp4'
            os.rename(path, new_path)
            print(f'  [ext-fix] renamed {os.path.basename(path)} → {os.path.basename(new_path)} (has video stream)')
            return new_path
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return path


def _video_quality(path_or_url: str, headers: dict[str, str] | None = None) -> dict | None:
    """Return video quality info for a local file or remote URL via ffprobe.

    Returns a dict with keys: width, height, bitrate (bps), codec.
    Any unavailable field is absent from the dict.  Returns None if ffprobe
    is missing or the probe completely fails.
    """
    ffprobe = shutil.which('ffprobe')
    if ffprobe is None:
        return None
    cmd = [ffprobe, '-v', 'error']
    if headers:
        cmd += ['-headers', ''.join(f'{k}: {v}\r\n' for k, v in headers.items())]
    cmd += ['-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,bit_rate,codec_name:format=bit_rate',
            '-of', 'json', path_or_url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        out: dict = {}
        streams = data.get('streams', [])
        if streams:
            s = streams[0]
            if s.get('width'):
                out['width'] = int(s['width'])
            if s.get('height'):
                out['height'] = int(s['height'])
            if s.get('codec_name'):
                out['codec'] = s['codec_name']
            br = s.get('bit_rate') or data.get('format', {}).get('bit_rate')
            if br and str(br).isdigit():
                out['bitrate'] = int(br)
        elif data.get('format', {}).get('bit_rate'):
            br = data['format']['bit_rate']
            if str(br).isdigit():
                out['bitrate'] = int(br)
        return out if out else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _quality_is_replacement_candidate(remote_q: dict, local_q: dict,
                                       remote_size: int, local_size: int) -> bool:
    """Return True if the remote file is worth downloading to replace the local one.

    Replacement is only warranted when the remote file is the same resolution
    (within 2 %) AND strictly smaller in file size — i.e. better-compressed
    same content.  Resolution upgrades are intentionally ignored: the user's
    MAX_RESOLUTION setting controls what gets downloaded, and a higher-resolution
    remote would bypass that preference.

    remote_size / local_size of 0 means unknown — treated conservatively.
    """
    r_w, r_h = remote_q.get('width', 0), remote_q.get('height', 0)
    l_w, l_h = local_q.get('width', 0), local_q.get('height', 0)

    # If we know both resolutions, require them to match within 2 %
    if r_w and r_h and l_w and l_h:
        if abs(r_w * r_h - l_w * l_h) > l_w * l_h * 0.02:
            return False  # different resolution — skip regardless of size

    # Same (or unknown) resolution: replace only if remote is smaller
    if 0 < remote_size < local_size:
        return True

    return False


def _remote_range_bytes(url: str, headers: dict[str, str], start: int, length: int) -> bytes | None:
    """Fetch a byte range from *url* using an HTTP Range request.

    Returns the raw bytes on success, or None if Range requests are not
    supported or the request fails.
    """
    try:
        req_headers = dict(headers)
        req_headers['Range'] = f'bytes={start}-{start + length - 1}'
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (206, 200):
                return None
            return resp.read(length)
    except (urllib.error.URLError, OSError):
        return None


def _remote_partial_match(url: str, headers: dict[str, str], content_length: int,
                           download_dir: str, ext: str) -> str | None:
    """Return an existing filename whose content matches the remote file via Range probing.

    Fetches the first 64 KB of the remote file and compares it against all
    non-temp files with the same extension in *download_dir*.  For candidates
    that pass the first-chunk check, also verifies the last 64 KB.  Returns
    the matching filename, or None if no match is found.

    Only meaningful when *content_length* > 0 so that we can check the tail.
    """
    chunk = 64 * 1024  # 64 KB
    print('  [pre-check] fetching remote start chunk...', end='\r', flush=True)
    remote_start = _remote_range_bytes(url, headers, 0, chunk)
    if not remote_start:
        return None

    for entry in os.listdir(download_dir):
        if _is_temp_file(entry):
            continue
        _, entry_ext = os.path.splitext(entry)
        if entry_ext.lower() != ext.lower():
            continue
        full = os.path.join(download_dir, entry)
        if not os.path.isfile(full):
            continue
        local_size = os.path.getsize(full)
        if local_size < len(remote_start):
            continue  # local file is smaller than the probe chunk — can't be the same

        with open(full, 'rb') as f:
            local_start = f.read(chunk)
        if local_start != remote_start[:len(local_start)]:
            continue

        # First chunk matches — verify the tail to reduce false positives
        if content_length > chunk:
            tail_offset = content_length - chunk
            print('  [pre-check] fetching remote end chunk...', end='\r', flush=True)
            remote_end = _remote_range_bytes(url, headers, tail_offset, chunk)
            if remote_end:
                with open(full, 'rb') as f:
                    f.seek(max(0, local_size - chunk))
                    local_end = f.read(chunk)
                if remote_end != local_end:
                    continue

        return entry
    return None


def _remote_full_hash(url: str, headers: dict[str, str]) -> str | None:
    """Download the full content of *url* and return its SHA-256 hex digest.

    Only intended for small files (≤ 50 MB).  Returns None on any error.
    """
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            h = hashlib.sha256()
            while True:
                block = resp.read(1 << 20)  # 1 MB
                if not block:
                    break
                h.update(block)
            return h.hexdigest()
    except (urllib.error.URLError, OSError):
        return None


def _remote_audio_fingerprint(url: str, headers: dict[str, str]) -> list[int] | None:
    """Return a chromaprint fingerprint for the audio track of a remote URL.

    Pipes audio from ffmpeg (with optional auth headers) into fpcalc reading
    raw PCM via stdin — avoids saving a temp file and only streams the first
    120 seconds.  Returns None if either tool is unavailable or the probe fails.
    """
    ffmpeg = shutil.which('ffmpeg')
    fpcalc = shutil.which('fpcalc')
    if ffmpeg is None or fpcalc is None:
        return None
    try:
        ffmpeg_cmd = [ffmpeg, '-v', 'error']
        if headers:
            ffmpeg_cmd += ['-headers', ''.join(f'{k}: {v}\r\n' for k, v in headers.items())]
        ffmpeg_cmd += ['-i', url, '-t', '120', '-vn', '-ac', '1', '-ar', '11025',
                       '-f', 's16le', 'pipe:1']
        fpcalc_cmd = [fpcalc, '-raw', '-json', '-rate', '11025', '-channels', '1',
                      '-length', '120', '-']
        ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
        fpcalc_proc = subprocess.Popen(fpcalc_cmd, stdin=ffmpeg_proc.stdout,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if ffmpeg_proc.stdout:
            ffmpeg_proc.stdout.close()
        fpcalc_out, _ = fpcalc_proc.communicate(timeout=180)
        ffmpeg_proc.wait(timeout=30)
        data = json.loads(fpcalc_out.decode())
        fp = data.get('fingerprint')
        return fp if isinstance(fp, list) else None
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None


def _remote_video_duration(url: str, headers: dict[str, str]) -> float | None:
    """Probe a remote URL with ffprobe to get its duration without downloading the file.

    ffprobe streams only the first few KB needed to identify the container and
    read the duration field from the header — far cheaper than a full download.
    Returns None if ffprobe is unavailable or the probe fails.
    """
    ffprobe = shutil.which('ffprobe')
    if ffprobe is None:
        return None
    cmd = [ffprobe, '-v', 'error']
    if headers:
        # ffprobe -headers expects "Key: Value\r\n" pairs in one string.
        cmd += ['-headers', ''.join(f'{k}: {v}\r\n' for k, v in headers.items())]
    cmd += ['-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        text = result.stdout.strip()
        return float(text) if text else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _stream_frame(url: str, headers: dict[str, str], timestamp: str) -> bytes | None:
    """Seek to *timestamp* in a remote URL and extract one 32×32 grayscale frame.

    ffmpeg uses HTTP Range requests internally to seek efficiently — only the
    data around the requested keyframe is actually downloaded.
    Returns None if ffmpeg is unavailable or the seek fails.
    """
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        return None
    cmd = [ffmpeg, '-v', 'error']
    if headers:
        cmd += ['-headers', ''.join(f'{k}: {v}\r\n' for k, v in headers.items())]
    # Input-side seek (-ss before -i) lets ffmpeg use Range requests rather
    # than reading from the start of the stream.
    cmd += ['-ss', timestamp, '-i', url,
            '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray',
            '-vf', 'scale=32:32', 'pipe:1']
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode == 0 and len(result.stdout) == 32 * 32:
            return result.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _remote_visually_similar(url: str, headers: dict[str, str],
                              url_dur: float, local_path: str) -> bool:
    """Return True if the remote video at *url* is visually similar to *local_path*.

    Samples one frame every 15 seconds (capped at 8 samples), starting from
    ~5 % of the video duration so opening-title frames are avoided.  Each
    sample downloads only the data around that seek point via HTTP Range
    requests.  At least 75 % of sampled frames must match for a True result.
    """
    timestamps: list[str] = []
    t = max(url_dur * 0.05, 2.0)
    while t < url_dur * 0.92 and len(timestamps) < 8:
        timestamps.append(_format_ts(t))
        t += 15.0
    if not timestamps:
        return False

    n = len(timestamps)
    match_count = 0
    total_count = 0
    for i, ts in enumerate(timestamps, start=1):
        print(f'  [pre-check] frame sample {i}/{n} at {ts}...', end='\r', flush=True)
        remote_frame = _stream_frame(url, headers, ts)
        local_frame  = _video_frame_hash(local_path, ts)
        if remote_frame and local_frame:
            total_count += 1
            sim = _frame_similarity(remote_frame, local_frame)
            match = sim >= 0.85
            if match:
                match_count += 1
            print(f'  [pre-check] frame {i}/{n} at {ts}: {"match" if match else "no match"} ({sim:.0%})',
                  flush=True)
        else:
            print(f'  [pre-check] frame {i}/{n} at {ts}: could not extract', flush=True)

    return total_count > 0 and (match_count / total_count) >= 0.75


_VISUAL_PRECHECK_MIN_BYTES = 100 * 1024 * 1024  # 100 MB


def _decide_skip_or_replace(match_path: str, url: str, headers: dict[str, str],
                              content_length: int, skip_reason: str) -> str | None:
    """Given a confirmed content match, decide whether to skip the download or replace.

    Probes both the remote URL and the local matched file with ffprobe to
    compare resolution and size.  If the remote is the same resolution but
    strictly smaller, sets _precheck_replace_target and returns None so the
    download proceeds and the old file is replaced.  Otherwise returns
    *skip_reason* unchanged.

    For non-video files or when quality cannot be determined, falls back to a
    pure size comparison.
    """
    global _precheck_replace_target
    local_size = os.path.getsize(match_path)

    # Probe quality only when meaningful (video files or known different size)
    local_q = _video_quality(match_path) or {}
    remote_q: dict = {}
    if local_q or (content_length > 0 and content_length != local_size):
        print('  [pre-check] probing remote quality...', end='\r', flush=True)
        remote_q = _video_quality(url, headers) or {}

    if _quality_is_replacement_candidate(remote_q, local_q, content_length, local_size):
        if content_length > 0:
            note = (f'smaller file {content_length / 1024 / 1024:.1f} MB '
                    f'vs {local_size / 1024 / 1024:.1f} MB')
        else:
            note = 'smaller file'
        cname = _safe(os.path.basename(match_path))
        print(f'  [pre-check] same content, {note} — will replace {cname}', flush=True)
        _precheck_replace_target = match_path
        return None  # proceed with download; _direct_fetch will delete match_path
    return skip_reason


def _precheck_url(url: str, headers: dict[str, str], download_dir: str) -> str | None:
    """Check whether *url* appears to point to content already in *download_dir*.

    Returns a human-readable reason string if the download should be skipped,
    or None if it should proceed.

    Checks in order of increasing cost:
      1. HEAD request → filename match, exact byte-size match (free).
         For size-matched files: MIME/extension check selects the path —
         non-video files use multi-chunk sampling (1×64 KB per 5 MB for
         50–500 MB, 1×64 KB per 10 MB for 500 MB–1 GB, max 100 chunks);
         video files fall through to the AV pipeline.
      1b. Partial content probe via HTTP Range requests for large (> 50 MB)
          non-video files — fetches first + last 64 KB only.
      2. ffprobe on the remote URL → duration gate for video files.
      3. Audio fingerprint — pipes 120 s of remote audio through fpcalc
         (~2.6 MB downloaded); eliminates or confirms candidates.
      4. Visual frame sampling (only for files ≥ 100 MB) → ffmpeg seeks to
         one frame every 15 s; each seek downloads only the data around that
         keyframe.
    """
    # --- 1. HEAD request: filename and size ---
    print('  [pre-check] HEAD request...', end='\r', flush=True)
    content_length = 0
    head_name: str | None = None
    try:
        req = urllib.request.Request(url, headers=headers, method='HEAD')
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_length = int(resp.headers.get('Content-Length', 0))
            head_name = _name_from_response(resp, url)
    except (urllib.error.URLError, OSError, ValueError):
        pass  # HEAD not supported by all servers — continue to other checks

    size_str = f'{content_length / 1024 / 1024:.1f} MB' if content_length else 'unknown size'
    print(f'  [pre-check] HEAD: {size_str}{f", name: {_safe(head_name)}" if head_name else ""}', flush=True)

    if head_name:
        name_path = os.path.join(download_dir, head_name)
        if os.path.isfile(name_path) and not _is_temp_file(head_name):
            reason = f'already exists: {_safe(head_name)}'
            return _decide_skip_or_replace(name_path, url, headers, content_length, reason)

    _HASH_MAX_BYTES = 50 * 1024 * 1024  # full hash for files ≤ 50 MB; chunks for larger

    if content_length > 0:
        # Collect every existing file whose size matches the remote size.
        size_matched: list[tuple[str, str]] = []
        for entry in os.listdir(download_dir):
            if _is_temp_file(entry):
                continue
            full = os.path.join(download_dir, entry)
            if os.path.isfile(full) and os.path.getsize(full) == content_length:
                size_matched.append((entry, full))

        if size_matched:
            if content_length <= _HASH_MAX_BYTES:
                # Small file: download the full remote content and compare SHA-256.
                print('  [pre-check] downloading for hash comparison...', end='\r', flush=True)
                remote_hash = _remote_full_hash(url, headers)
                if remote_hash:
                    for entry, full in size_matched:
                        if _file_hash(full) == remote_hash:
                            reason = f'hash match: {_safe(entry)}'
                            return _decide_skip_or_replace(full, url, headers, content_length, reason)
                    print('  [pre-check] same size but different content — proceeding with download', flush=True)
                # If remote_hash is None (network error) fall through and proceed.
            else:
                # Large file: use MIME/extension to pick the verification path.
                # Video files skip straight to the AV pipeline below; non-video
                # files are sampled with evenly-spaced 64 KB chunks.
                is_video = _is_video_filename(head_name or url)
                if not is_video:
                    chunk = 64 * 1024
                    _MB = 1024 * 1024
                    # Determine target chunk count from file size, then derive
                    # spacing from the full file length so chunks are always
                    # evenly distributed end-to-end regardless of file size.
                    if content_length <= 500 * _MB:
                        target = content_length // (5 * _MB)   # ~1 per 5 MB
                    else:
                        target = content_length // (10 * _MB)  # ~1 per 10 MB
                    n_chunks = min(100, max(2, target))
                    # Offsets span 0 → (content_length - chunk) evenly.
                    offsets = [
                        round(i * (content_length - chunk) / (n_chunks - 1))
                        for i in range(n_chunks)
                    ]

                    remote_chunks: list[bytes] = []
                    range_supported = True
                    for i, off in enumerate(offsets):
                        print(f'  [pre-check] fetching chunk {i + 1}/{n_chunks}...', end='\r', flush=True)
                        data = _remote_range_bytes(url, headers, off, chunk)
                        if data is None:
                            range_supported = False
                            break
                        remote_chunks.append(data)

                    if range_supported and remote_chunks:
                        for entry, full in size_matched:
                            matched = True
                            with open(full, 'rb') as f:
                                for off, rc in zip(offsets, remote_chunks):
                                    f.seek(off)
                                    lc = f.read(len(rc))
                                    if lc != rc:
                                        matched = False
                                        break
                            if matched:
                                cname = _safe(entry)
                                reason = f'chunk match ({n_chunks} samples): {cname}'
                                return _decide_skip_or_replace(full, url, headers, content_length, reason)
                    # No chunk match found — fall through to duration pipeline.

    # --- 1b. Partial content probe for large non-video files (no size match) ---
    # For files > 50 MB where we couldn't match by name or size, sample the
    # first and last 64 KB via Range requests — cheap and definitive for
    # identical files regardless of what they're named.
    if content_length > _HASH_MAX_BYTES and head_name:
        _, head_ext = os.path.splitext(head_name)
        if not _is_video_filename(head_name):
            match = _remote_partial_match(url, headers, content_length, download_dir, head_ext)
            if match:
                match_path = os.path.join(download_dir, match)
                reason = f'partial content match with existing: {_safe(match)}'
                return _decide_skip_or_replace(match_path, url, headers, content_length, reason)

    # --- 2. ffprobe remote duration (video files only) ---
    _set_status('  [pre-check] probing remote duration...')
    url_dur = _remote_video_duration(url, headers)
    if url_dur is None:
        print('  [pre-check] duration unavailable — proceeding with download')
        return None  # can't probe — proceed with download
    print(f'  [pre-check] remote duration: {url_dur:.1f}s')

    duration_candidates: list[str] = []
    for entry in os.listdir(download_dir):
        if _is_temp_file(entry):
            continue
        full = os.path.join(download_dir, entry)
        if not os.path.isfile(full):
            continue
        if not _is_video_filename(entry):
            continue
        existing_dur = _video_duration(full)
        if existing_dur is not None and abs(url_dur - existing_dur) <= 1.0:
            duration_candidates.append(full)

    if not duration_candidates:
        print('  [pre-check] no duration match — proceeding with download', flush=True)
        return None  # no duration match — proceed with download

    # --- 3. Audio fingerprint ---
    # Stream 120 s of audio from the remote URL through fpcalc (~2.6 MB).
    # Confirms or eliminates duration candidates before the more expensive
    # visual check.  Inconclusive results (0.5 ≤ sim < 0.85) fall through.
    print('  [pre-check] audio fingerprint...', end='\r', flush=True)
    url_fp = _remote_audio_fingerprint(url, headers)
    if url_fp is not None:
        surviving: list[str] = []
        for candidate in duration_candidates:
            local_fp = _audio_fingerprint(candidate)
            if local_fp is None:
                surviving.append(candidate)
                continue
            sim = _fingerprint_similarity(url_fp, local_fp)
            cname = _safe(os.path.basename(candidate))
            print(f'  [pre-check] audio vs {cname}: {sim:.0%}', flush=True)
            if sim >= 0.85:
                reason = (f'audio fingerprint match: {cname} '
                          f'({url_dur:.1f}s, {sim:.0%})')
                return _decide_skip_or_replace(candidate, url, headers, content_length, reason)
            if sim >= 0.5:
                surviving.append(candidate)  # inconclusive — keep for visual check
            # sim < 0.5: clearly different audio — drop candidate
        duration_candidates = surviving

    if not duration_candidates:
        print('  [pre-check] audio ruled out all candidates — proceeding with download', flush=True)
        return None

    # --- 4. Visual frame sampling (large files only) ---
    # For files under 100 MB, the full download is cheap enough that we skip
    # the per-frame probing and let the post-download hash/AV check handle it.
    if content_length >= _VISUAL_PRECHECK_MIN_BYTES:
        for candidate in duration_candidates:
            cname = _safe(os.path.basename(candidate))
            print(f'  [pre-check] visually sampling against {cname} ({url_dur:.1f}s)...', flush=True)
            if _remote_visually_similar(url, headers, url_dur, candidate):
                reason = (f'visually similar to existing: {_safe(os.path.basename(candidate))} '
                          f'({url_dur:.1f}s)')
                return _decide_skip_or_replace(candidate, url, headers, content_length, reason)

    print('  [pre-check] no AV match — proceeding with download', flush=True)
    return None


def _direct_fetch(video_url: str, download_dir: str, temp_prefix: str, headers: dict[str, str]) -> bool:
    """Download *video_url* straight to *download_dir* using urllib, no browser needed.

    Writes to a .part file while in progress so that wait_for_download ignores
    it until the download is complete, then renames to the final temp name.
    This ensures an interrupted download is never mistaken for a finished one.
    The file extension is taken from the Content-Disposition header when present
    so that non-video files (e.g. .funscript, .zip) keep their original extension.
    """
    global _last_fetch_original_name, _last_download_skipped, _precheck_replace_target
    _last_download_skipped = False
    _precheck_replace_target = None

    # Pre-download duplicate check: HEAD + ffprobe remote duration.
    # Zero bandwidth cost — we only proceed with the full download if nothing
    # similar already exists in download_dir.
    skip_reason = _precheck_url(video_url, headers, download_dir)
    if skip_reason:
        print(f'  [pre-check] skipping — {skip_reason}')
        _last_download_skipped = True
        return False

    writing_path = os.path.join(download_dir, f'{temp_prefix}.part')
    req = urllib.request.Request(video_url, headers=headers)
    with urllib.request.urlopen(req) as response:
        ext = _ext_from_response(response, video_url)
        _last_fetch_original_name = _name_from_response(response, video_url)
        size_mb = int(response.headers.get('Content-Length', 0)) / 1024 / 1024
        if size_mb:
            print(f'  file size: {size_mb:.1f} MB')
        done_bytes = 0
        with open(writing_path, 'wb') as f:
            while chunk := response.read(65536):
                f.write(chunk)
                done_bytes += len(chunk)
                done_mb = done_bytes / (1 << 20)
                if size_mb:
                    _set_status(f'  downloading... {done_mb:.1f} / {size_mb:.1f} MB')
                else:
                    _set_status(f'  downloading... {done_mb:.1f} MB')
    final_temp = os.path.join(download_dir, f'{temp_prefix}{ext}')
    os.rename(writing_path, final_temp)
    final_temp = _fix_av_extension(final_temp)

    # Content-based duplicate check: compare the downloaded bytes against every
    # existing file with the same extension.  Catches cases where the remote
    # filename differs from the local copy (e.g. "_maxinterval" suffix) and
    # where Content-Length was absent so the pre-check couldn't compare sizes.
    existing_match = _file_matches_any_existing(final_temp, download_dir)
    if existing_match:
        existing_path = os.path.join(download_dir, existing_match)
        new_size = os.path.getsize(final_temp)
        old_size = os.path.getsize(existing_path)
        if new_size < old_size:
            # New file is smaller (better compressed) — replace the old one
            os.remove(existing_path)
            print(f'  [post-check] replacing {_safe(existing_match)} with smaller identical-content file '
                  f'({old_size // 1024} KB → {new_size // 1024} KB)')
        else:
            os.remove(final_temp)
            print(f'  [post-check] duplicate of existing file: {_safe(existing_match)} — removed')
            _last_download_skipped = True
            return False

    # If the pre-check flagged a file for replacement (same content, better quality
    # or smaller size), delete it now that the new download has succeeded.
    replace_target: str = _precheck_replace_target or ''
    if replace_target and os.path.isfile(replace_target):
        rname = _safe(os.path.basename(replace_target))
        os.remove(replace_target)
        print(f'  [pre-check] replaced {rname} with new download')
        _precheck_replace_target = None

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
            print('  [hanime1.me] timed out waiting for #downloadBtn')
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
            print('  [hanime1.me] timed out waiting for quality links on download page')
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


def download_eporner(driver, url: str, download_dir: str) -> bool:
    """Navigate to an eporner.com video page and download the best quality within MAX_RESOLUTION.

    eporner hides its download div (display:none) but Selenium can still read
    the href attributes.  AV1 links (.download-av1 a) are preferred over h264
    (.download-h264 a) when available.
    """
    driver.get(url)

    try:
        time.sleep(2)

        # Prefer AV1; fall back to h264 if none found.
        links = driver.find_elements(By.CSS_SELECTOR, '.download-av1 a')
        codec = 'av1'
        if not links:
            links = driver.find_elements(By.CSS_SELECTOR, '.download-h264 a')
            codec = 'h264'
        if not links:
            print('  [eporner.com] no download links found in #downloaddiv')
            return False

        best, resolution = _pick_best(
            links,
            lambda el: _parse_resolution((el.get_attribute('href') or '') + (el.text or '')),
        )
        rel_href = best.get_attribute('href') or ''
        if not rel_href:
            print('  [eporner.com] best link has no href')
            return False

        # hrefs are root-relative (/dload/...) — prepend the origin.
        video_url = rel_href if rel_href.startswith('http') else f'https://www.eporner.com{rel_href}'

        print(f'  [eporner.com] fetching {resolution}p ({codec})...')
        return _direct_fetch(video_url, download_dir, '_eporner_temp',
                             {'Referer': 'https://www.eporner.com/'})

    except Exception as e:
        print(f'  [eporner.com] handler error: {e}')

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
        print('  [rule34.xxx] fetching original...')
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


def _pixeldrain_headers() -> dict[str, str]:
    """Build request headers for the pixeldrain API, adding auth if a key is configured."""
    headers: dict[str, str] = {'Referer': 'https://pixeldrain.com/'}
    api_key = _get_secret('PIXELDRAIN_API_KEY').strip()
    if api_key:
        token = base64.b64encode(f':{api_key}'.encode()).decode()
        headers['Authorization'] = f'Basic {token}'
    return headers


def _expand_pixeldrain_list(url: str) -> list[str]:
    """
    If *url* is a pixeldrain list page (/l/<id>), fetch the list API and return
    individual single-file URLs (/u/<file_id>) for every item in the list.
    For single-file URLs (/u/<id>), return [url] unchanged.
    """
    path_parts = urlparse(url).path.strip('/').split('/')
    if not path_parts or path_parts[0] != 'l' or len(path_parts) < 2:
        return [url]

    list_id = path_parts[1]
    try:
        api_url = f'https://pixeldrain.com/api/list/{list_id}'
        req = urllib.request.Request(api_url, headers=_pixeldrain_headers())
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        files = data.get('files', [])
        if files:
            expanded = [f'https://pixeldrain.com/u/{f["id"]}' for f in files if f.get('id')]
            print(f'  [pixeldrain.com] list {list_id} expanded to {len(expanded)} file(s)')
            return expanded
        print(f'  [pixeldrain.com] list {list_id} is empty')
    except Exception as e:
        print(f'  [pixeldrain.com] could not expand list {list_id}: {e}')

    return [url]


def download_pixeldrain(_driver, url: str, download_dir: str) -> bool:
    """Download a pixeldrain.com file directly via its public API (no browser needed)."""
    try:
        # Page URL: /u/<id> → API URL: /api/file/<id>
        file_id = urlparse(url).path.rstrip('/').split('/')[-1]
        video_url = f'https://pixeldrain.com/api/file/{file_id}'
        print(f'  [pixeldrain.com] fetching {file_id}...')
        return _direct_fetch(video_url, download_dir, '_pixeldrain_temp', _pixeldrain_headers())

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
    except WebDriverException:
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
        except WebDriverException:
            # Fallback: pick the last submit button (search is usually first)
            submits = driver.find_elements(By.XPATH, '//button[@type="submit"]')
            submit = submits[-1] if submits else driver.find_element(By.XPATH, '//button[@type="submit"]')
        for submit_attempt in range(3):
            driver.execute_script('arguments[0].click()', submit)

            # Wait up to 10 s for the URL to change away from the login page.
            try:
                WebDriverWait(driver, 10).until(
                    lambda _: 'login' not in driver.current_url
                )
            except WebDriverException:
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
        print('  [iwara.tv] unexpected redirect, retrying navigation...')
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
    except WebDriverException:
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
        for _href in all_hrefs:
            print(f'    {_href}')
        return False

    max_res = int(os.getenv('MAX_RESOLUTION', '1080'))

    def _res_from_href(href: str) -> int:
        href_lower = href.lower()
        if 'preview' in href_lower:
            return 0
        for _kw in ('source',):
            if _kw in href_lower:
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
        except json.JSONDecodeError:
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


# ---------------------------------------------------------------------------
# spankbang.com handler
# ---------------------------------------------------------------------------

_spankbang_logged_in: bool = False


def _spankbang_normalize_url(url: str) -> str:
    """Replace any regional subdomain (ru., fr., de., …) with the main domain."""
    parsed = urlparse(url)
    netloc = parsed.netloc
    if netloc != 'spankbang.com' and netloc.endswith('.spankbang.com'):
        netloc = 'spankbang.com'
    return parsed._replace(netloc=netloc).geturl()


def _spankbang_login(driver) -> bool:
    """Log into spankbang.com via the modal overlay. Returns True if successful."""
    global _spankbang_logged_in
    if _spankbang_logged_in:
        return True

    username = _get_secret('SPANKBANG_USERNAME').strip()
    password = _get_secret('SPANKBANG_PASSWORD').strip()
    if not username or not password:
        print('  [spankbang.com] no credentials — set SPANKBANG_USERNAME and SPANKBANG_PASSWORD via setup_credentials.py')
        return False

    driver.get('https://spankbang.com/')
    time.sleep(2)

    try:
        wait = WebDriverWait(driver, 10)

        # Open the login modal via the header login button
        login_btn = wait.until(EC.element_to_be_clickable(
            (By.XPATH, '//*[@data-remodal-target="auth" or @href="#auth" or '
                       '(contains(@class,"login") and not(ancestor::form))]')
        ))
        driver.execute_script('arguments[0].click()', login_btn)

        # Wait for the modal form fields to be visible
        user_field = wait.until(EC.visibility_of_element_located((By.ID, 'log_username')))
        user_field.click()
        time.sleep(0.2)
        user_field.clear()
        for char in username:
            user_field.send_keys(char)
            time.sleep(0.05)

        pw_field = driver.find_element(By.ID, 'log_password')
        pw_field.click()
        time.sleep(0.2)
        for char in password:
            pw_field.send_keys(char)
            time.sleep(0.05)

        time.sleep(0.3)
        login_form = driver.find_element(By.ID, 'auth_login_form')
        submit = login_form.find_element(By.XPATH, './/button[@type="submit"]')
        driver.execute_script('arguments[0].click()', submit)

        # Success: modal closes (auth-remodal loses visibility) or a profile element appears
        def _logged_in(_driver):
            _modal = _driver.find_elements(By.ID, 'auth-remodal')
            if _modal and _modal[0].value_of_css_property('visibility') == 'hidden':
                return True
            if _driver.find_elements(By.XPATH, '//*[contains(@class,"user-nav") or contains(@class,"profile-btn") or contains(@href,"/users/")]'):
                return True
            return False

        try:
            WebDriverWait(driver, 10).until(_logged_in)
        except WebDriverException:
            pass

        # Verify: a logged-in page won't show the auth modal as visible
        modal = driver.find_elements(By.ID, 'auth-remodal')
        if modal and modal[0].value_of_css_property('visibility') == 'visible':
            print('  [spankbang.com] login failed — modal still open')
            return False

        print('  [spankbang.com] login successful')
        _spankbang_logged_in = True
        return True

    except Exception as e:
        print(f'  [spankbang.com] login error: {e}')
        return False


def download_spankbang(driver, url: str, download_dir: str) -> bool:
    """Download a spankbang.com video. Regional subdomains are normalised to
    spankbang.com automatically. Login is required and handled via keyring
    credentials (SPANKBANG_USERNAME / SPANKBANG_PASSWORD).
    """
    url = _spankbang_normalize_url(url)

    if not _spankbang_login(driver):
        return False

    driver.get(url)
    time.sleep(3)

    try:
        # SpankBang renders quality download links inside a .download section.
        # Clicking the download toggle reveals anchor elements with resolution
        # labels in their text and direct CDN .mp4 hrefs.
        try:
            toggle = driver.find_element(By.XPATH,
                '//*[contains(@class,"download") and '
                '(self::button or self::a or self::div) and '
                'not(contains(@href,".mp4"))]'
            )
            driver.execute_script('arguments[0].click()', toggle)
            time.sleep(1)
        except WebDriverException:
            pass

        # Collect all anchors that look like quality download links.
        links = driver.find_elements(By.XPATH,
            '//a[contains(@href,".mp4") or '
            '(contains(@class,"download") and @href and @href != "#")]'
        )

        if not links:
            print('  [spankbang.com] no download links found')
            return False

        best, resolution = _pick_best(
            links,
            lambda el: _parse_resolution((el.get_attribute('href') or '') + (el.text or '')),
        )
        video_url = best.get_attribute('href') or ''
        if not video_url or video_url == '#':
            print('  [spankbang.com] best link has no usable href')
            return False

        print(f'  [spankbang.com] fetching {resolution}p...')
        return _direct_fetch(video_url, download_dir, '_spankbang_temp',
                             {'Referer': 'https://spankbang.com/'})

    except Exception as e:
        print(f'  [spankbang.com] handler error: {e}')

    return False


# ---------------------------------------------------------------------------
# Yandex Disk handler
# ---------------------------------------------------------------------------

def download_yandex_disk(_driver, url: str, download_dir: str) -> bool:
    """Download a public Yandex Disk file via the public resources API (no browser needed).

    Works for both disk.yandex.com and disk.yandex.ru share links.
    The API accepts the full share URL as the public_key parameter and returns
    a pre-signed direct download URL.
    """
    try:
        api_url = (
            'https://cloud-api.yandex.net/v1/disk/public/resources/download'
            f'?public_key={quote(url, safe="")}'
        )
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        })
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())

        download_url = data.get('href')
        if not download_url:
            print(f'  [disk.yandex] API returned no href: {data}')
            return False

        print('  [disk.yandex] fetching...')
        return _direct_fetch(download_url, download_dir, '_yandex_temp',
                             {'Referer': 'https://disk.yandex.com/'})

    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        print(f'  [disk.yandex] HTTP {e.code}: {body}')
    except Exception as e:
        print(f'  [disk.yandex] handler error: {e}')

    return False


# Whether MEGAcmd is already logged in for this session.
_mega_logged_in: bool = False


def _mega_server_responding() -> bool:
    """Return True if the MEGAcmd server is responding to mega-whoami.

    Any exit code counts as responsive — the server runs but is not logged in
    returns non-zero, which is still proof the server is up.  Only a timeout
    or missing binary means the server is not ready.
    """
    mega_whoami = shutil.which('mega-whoami')
    if mega_whoami is None:
        return False
    try:
        subprocess.run([mega_whoami], capture_output=True, text=True, timeout=5)
        return True   # completed = server is up, regardless of exit code
    except subprocess.TimeoutExpired:
        return False


def _mega_ensure_server() -> bool:
    """Start the MEGAcmd background server if needed and wait until it responds.

    Polls _mega_server_responding() every second for up to 30 seconds after
    launching the server so that mega-login always finds a ready server and
    its full timeout budget is spent on the actual login, not server startup.

    Returns True if the server is ready (or already was), False if it never
    became ready within the wait window.
    """
    if _mega_server_responding():
        return True  # already up

    mega_cmd_server = shutil.which('mega-cmd-server')
    if mega_cmd_server is None:
        # No server binary — individual commands will try to start it themselves.
        return True

    print('  [mega.nz] starting MEGAcmd server...')
    subprocess.Popen(
        [mega_cmd_server],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1)
        if _mega_server_responding():
            return True

    print('  [mega.nz] server did not become ready in time')
    return False


def _mega_is_logged_in() -> bool:
    """Return True if MEGAcmd already has an active session.

    Checks the combined stdout+stderr of mega-whoami for the word 'account'
    (present when logged in) and absence of 'not logged in'.  Exit code is
    not reliable across MEGAcmd versions so we read the output instead.
    """
    mega_whoami = shutil.which('mega-whoami')
    if mega_whoami is None:
        return False
    try:
        r = subprocess.run([mega_whoami], capture_output=True, text=True, timeout=10)
        out = (r.stdout + r.stderr).lower()
        return 'not logged in' not in out and ('account' in out or '@' in out)
    except subprocess.TimeoutExpired:
        return False


def _mega_ensure_login() -> bool:
    """Log into MEGAcmd with keyring credentials if not already logged in.

    Ensures the MEGAcmd server is running first, then checks the current
    session by reading mega-whoami output (not just exit code).
    If no credentials are stored the function returns True so that
    public-link downloads can still proceed without an account.

    On any login failure the user is asked whether to retry with an MFA code.
    """
    global _mega_logged_in
    if _mega_logged_in:
        return True

    if not _mega_ensure_server():
        return False

    if _mega_is_logged_in():
        print('  [mega.nz] already logged in')
        _mega_logged_in = True
        return True

    email    = _get_secret('MEGA_EMAIL').strip()
    password = _get_secret('MEGA_PASSWORD').strip()

    if not email or not password:
        return True  # no credentials — proceed as anonymous (public links only)

    _mega_login_bin = shutil.which('mega-login')
    if _mega_login_bin is None:
        print('  [mega.nz] mega-login not found — cannot log in automatically')
        return True
    mega_login: str = _mega_login_bin  # definite str for closure capture

    def _run_login(extra_args: list[str]) -> 'subprocess.CompletedProcess[str] | None':
        try:
            return subprocess.run(
                [mega_login, email, password] + extra_args,
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            print('  [mega.nz] login timed out')
            return None

    print(f'  [mega.nz] logging in as {email}...')
    result = _run_login([])

    if result is None:
        return False

    # If the server reports a stuck/in-progress login state, wait and recheck.
    if result.returncode != 0:
        out = (result.stdout + result.stderr).lower()
        if 'not valid while login' in out or 'already' in out:
            print('  [mega.nz] server busy — waiting for previous login to settle...')
            time.sleep(5)
            if _mega_is_logged_in():
                print('  [mega.nz] already logged in')
                _mega_logged_in = True
                return True

        err = _safe((result.stderr or result.stdout).strip()) or '(no output)'
        print(f'  [mega.nz] login failed: {err}')
        code = input('  [mega.nz] Enter MFA code if required, or press Enter to abort: ').strip()
        if not code:
            return False
        result = _run_login([f'--auth-code={code}'])
        if result is None:
            return False
        if result.returncode != 0:
            err = _safe((result.stderr or result.stdout).strip()) or '(no output)'
            print(f'  [mega.nz] login failed: {err}')
            return False

    print('  [mega.nz] login successful')
    _mega_logged_in = True
    return True


def _mega_flatten_folders(download_dir: str, before: set[str]) -> None:
    """Move files from any new subdirectories created by mega-get into download_dir."""
    after = set(os.listdir(download_dir))
    new_dirs = [
        e for e in (after - before)
        if os.path.isdir(os.path.join(download_dir, e))
    ]
    for d in new_dirs:
        src_dir = os.path.join(download_dir, d)
        for root, _dirs, files in os.walk(src_dir):
            for fname in files:
                src = os.path.join(root, fname)
                dst = os.path.join(download_dir, fname)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
                else:
                    print(f'  [mega.nz] skipping duplicate: {_safe(fname)}')
        shutil.rmtree(src_dir, ignore_errors=True)
        print(f'  [mega.nz] flattened folder: {_safe(d)}')


def download_mega(_driver, url: str, download_dir: str) -> bool:
    """Download a mega.nz file using the MEGAcmd mega-get CLI tool.

    Requires MEGAcmd to be installed (https://mega.nz/cmd).
    Logs in automatically using MEGA_EMAIL / MEGA_PASSWORD from the keyring
    if credentials are stored; otherwise proceeds as anonymous (public links).
    mega-get is synchronous — the file is fully written before this returns.
    """
    mega_get = shutil.which('mega-get')
    if mega_get is None:
        print('  [mega.nz] mega-get not found — install MEGAcmd: https://mega.nz/cmd')
        return False

    if not _mega_ensure_login():
        return False

    before = set(os.listdir(download_dir))
    try:
        print('  [mega.nz] running mega-get...')
        _set_status('  [mega.nz] downloading — this may take several minutes...')
        result = subprocess.run(
            [mega_get, url, download_dir],
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                print(f'  [mega.nz] {_safe(line)}')
        if result.returncode != 0:
            err = _safe(result.stderr.strip()) if result.stderr else '(no output)'
            print(f'  [mega.nz] mega-get failed (exit {result.returncode}): {err}')
            return False
        _mega_flatten_folders(download_dir, before)
        return True

    except subprocess.TimeoutExpired:
        print('  [mega.nz] download timed out after 1 hour')
    except Exception as e:
        print(f'  [mega.nz] handler error: {e}')

    return False


def _e621_dismiss_tos(driver) -> None:
    """Check both ToS checkboxes and submit if the first-visit modal is present.

    Checkbox IDs are fixed in the e621 markup:
      #tos-age-checkbox   — "I am 18 years of age or older."
      #tos-terms-checkbox — "I have read and accept the Terms of Use."
    The submit button is scoped to .tos-modal-content so we don't accidentally
    click something else on the page.
    This is a no-op on subsequent visits once the cookie is set.
    """
    try:
        WebDriverWait(driver, 4).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.tos-modal-checkboxes'))
        )
    except TimeoutException:
        return  # modal not present

    try:
        for cb_id in ('tos-age-checkbox', 'tos-terms-checkbox'):
            cb = driver.find_element(By.ID, cb_id)
            if not cb.is_selected():
                driver.execute_script('arguments[0].click()', cb)
                time.sleep(0.2)

        # Click Accept specifically — Decline has id="tos-warning-decline" and
        # comes first in the DOM, so a generic button selector would click it.
        confirm = driver.find_element(By.ID, 'tos-warning-accept')
        driver.execute_script('arguments[0].click()', confirm)
        time.sleep(1)
        print('  [e621.net] ToS modal accepted')
    except Exception as e:
        print(f'  [e621.net] could not dismiss ToS modal: {e}')


def download_e621(driver, url: str, download_dir: str) -> bool:
    """Download the original video from an e621.net post page.

    e621 embeds videos in a <video> element whose data-file-url attribute
    points directly to the original CDN file — no quality-selector interaction
    needed.  Falls back to the <source src> child if data-file-url is absent.
    On first visit the site shows a ToS + age-verification modal; it is
    dismissed automatically before attempting to read the video element.
    """
    driver.get(url)
    time.sleep(2)

    _e621_dismiss_tos(driver)

    try:
        try:
            video_el = driver.find_element(By.CSS_SELECTOR, '#webm-video, video')
        except WebDriverException:
            print('  [e621.net] no video element found — post may be an image, not a video')
            return False

        # data-file-url is the cleanest path to the original file.
        video_url = video_el.get_attribute('data-file-url') or ''

        if not video_url:
            # Fall back to the <source> child element.
            try:
                source_el = video_el.find_element(By.TAG_NAME, 'source')
                video_url = source_el.get_attribute('src') or ''
            except WebDriverException:
                pass

        if not video_url:
            video_url = video_el.get_attribute('src') or ''

        if not video_url:
            print('  [e621.net] could not find video URL in page')
            return False

        print('  [e621.net] fetching original...')
        return _direct_fetch(video_url, download_dir, '_e621_temp',
                             {'Referer': 'https://e621.net/'})

    except Exception as e:
        print(f'  [e621.net] handler error: {e}')

    return False


def download_faptap(driver, url: str, download_dir: str) -> bool:
    """Follow the original-source link on a faptap.net video page and dispatch
    to the handler for that source domain.

    faptap.net is a video aggregator; each video page links back to the host
    site (e.g. spankbang.com, rule34video.com).  This handler finds that link
    and re-uses the existing per-domain handler so all quality selection,
    login, and download logic is inherited automatically.
    """
    driver.get(url)
    time.sleep(2)

    try:
        # faptap renders the source link as:
        #   <a href="https://..." target="_blank"><span>Source</span></a>
        # Primary selector targets the <span>Source</span> pattern; the broader
        # fallback catches any external anchor whose visible text or attributes
        # suggest it is the original source.
        candidates = driver.find_elements(By.XPATH,
            '//a[@href and .//span['
            '  contains(translate(normalize-space(.),"SOURCE","source"),"source")'
            ']]'
            ' | '
            '//a[@href and ('
            '  contains(translate(normalize-space(.),"SOURCE ORIGINAL","source original"),"source") or '
            '  contains(translate(normalize-space(.),"SOURCE ORIGINAL","source original"),"original")'
            ')]'
        )

        source_url = None
        for el in candidates:
            href = el.get_attribute('href') or ''
            if href.startswith('http') and 'faptap.net' not in href:
                source_url = href
                break

        if not source_url:
            print('  [faptap.net] no external source link found on page')
            all_external = [
                el.get_attribute('href') for el in driver.find_elements(By.XPATH, '//a[@href]')
                if (el.get_attribute('href') or '').startswith('http')
                and 'faptap.net' not in (el.get_attribute('href') or '')
            ]
            print(f'  [faptap.net] external hrefs on page: {all_external[:10]}')
            return False

        print(f'  [faptap.net] source link → {source_url}')

        try:
            source_domain = check_domain(source_url)
            handler = DOMAIN_HANDLERS[source_domain]
        except UnknownDomainError:
            handler = download_ytdlp
        return handler(driver, source_url, download_dir)

    except Exception as e:
        print(f'  [faptap.net] handler error: {e}')

    return False


def _ytdlp_cmd() -> list[str] | None:
    """Return the command prefix to invoke yt-dlp, or None if not available.

    Prefers `sys.executable -m yt_dlp` so the venv installation is found
    even when yt-dlp is not on PATH (e.g. the venv is not activated).
    Falls back to the yt-dlp binary on PATH for system-wide installs.
    """
    import importlib.util
    if importlib.util.find_spec('yt_dlp') is not None:
        return [sys.executable, '-m', 'yt_dlp']
    ytdlp = shutil.which('yt-dlp')
    return [ytdlp] if ytdlp else None


def download_ytdlp(_driver, url: str, download_dir: str) -> bool:
    """Generic video extractor using yt-dlp for sites without a dedicated handler.

    Selects the best available quality up to MAX_RESOLUTION and saves the
    result as _ytdlp_temp.<ext> so wait_for_download can find it and
    _save_downloaded can rename it to the correct basename.

    Install yt-dlp with:  pip install yt-dlp  or  pipx install yt-dlp
    """
    ytdlp_prefix = _ytdlp_cmd()
    if ytdlp_prefix is None:
        print('  [yt-dlp] not found — install with: pip install yt-dlp')
        return False

    max_res = _get_max_resolution()
    output_tmpl = os.path.join(download_dir, '_ytdlp_temp.%(ext)s')

    cmd = [
        *ytdlp_prefix,
        '--no-playlist',
        '--no-write-subs',
        '--no-write-auto-subs',
        '--no-keep-fragments',
        '--merge-output-format', 'mp4',
        '-f', (
            f'bestvideo[height<={max_res}][ext=mp4]+bestaudio[ext=m4a]'
            f'/bestvideo[height<={max_res}]+bestaudio'
            f'/best[height<={max_res}]/best'
        ),
        '-o', output_tmpl,
        url,
    ]

    print('  [yt-dlp] extracting video...')
    try:
        result = subprocess.run(cmd, timeout=3600)
        if result.returncode != 0:
            print(f'  [yt-dlp] failed (exit {result.returncode})')
            print('  [yt-dlp] try again after updating: \"pip install -U yt-dlp\". if this does not work then we are stuck, sorry...')
            return False
        return True
    except subprocess.TimeoutExpired:
        print('  [yt-dlp] timed out after 1 hour')
    except Exception as e:
        print(f'  [yt-dlp] error: {e}')

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
    'eporner.com':     download_eporner,
    'disk.yandex.com':   download_yandex_disk,
    'disk.yandex.ru':    download_yandex_disk,
    'mega.nz':           download_mega,
    'mega.co.nz':        download_mega,
    'rule34video.party': download_rule34video,
    'spankbang.com':     download_spankbang,
    'faptap.net':        download_faptap,
    'e621.net':          download_e621,
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


def _is_temp_file(filename: str) -> bool:
    """Return True if *filename* looks like an in-progress or leftover temp file."""
    if filename.endswith(('.part', '.crdownload', '.tmp')):
        return True
    stem = Path(filename).stem
    if stem.endswith('_temp'):
        return True
    # Double-extension temp: e.g. _iwara_temp.mp4 → stem still ends in _temp
    if Path(stem).stem.endswith('_temp'):
        return True
    return False


def _peek_is_video(path: str) -> bool:
    """Return True if the first bytes of *path* match a known video container.

    Checks magic bytes rather than file extension so misnamed files (e.g. a
    funscript served with a .mp4 Content-Disposition header) are not mistaken
    for videos.  Covers the containers we actually encounter: MP4/MOV, WebM,
    MKV, AVI, FLV, MPEG-TS, and raw MPEG.
    """
    try:
        with open(path, 'rb') as fh:
            h = fh.read(16)
        if len(h) < 8:
            return False
        if h[4:8] == b'ftyp':           # MP4 / MOV / M4V
            return True
        if h[:4] == b'\x1a\x45\xdf\xa3':  # WebM / MKV (EBML)
            return True
        if h[:4] == b'RIFF' and h[8:12] == b'AVI ':  # AVI
            return True
        if h[:3] == b'FLV':             # Flash Video
            return True
        if h[0] == 0x47:                # MPEG-TS sync byte
            return True
        if h[:3] == b'\x00\x00\x01' and h[3] in (0xb3, 0xba):  # MPEG PS/ES
            return True
        return False
    except OSError:
        return False


def _any_video_in_folder(folder: str) -> str | None:
    """Return the path of any complete video file in *folder*, or None.

    Temp/partial files (.part, .crdownload, .tmp, *_temp*) are excluded so a
    previously cancelled download does not falsely count as a finished video.
    Uses both MIME type (filename extension) and magic-byte inspection so
    files with a video extension that contain non-video data are not counted.
    """
    for f in os.listdir(folder):
        if _is_temp_file(f):
            continue
        full = os.path.join(folder, f)
        if not os.path.isfile(full):
            continue
        if _is_video_filename(f) and _peek_is_video(full):
            return full
    return None


def _video_duration(path: str) -> float | None:
    """Return the video duration in seconds using ffprobe, or None if unavailable."""
    ffprobe = shutil.which('ffprobe')
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, timeout=30,
        )
        text = result.stdout.strip()
        return float(text) if text else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _file_matches_any_existing(new_path: str, download_dir: str) -> str | None:
    """Return the name of an existing file in *download_dir* whose content is
    identical to *new_path*, or None if no match is found.

    Only compares files with the same extension (case-insensitive) and ignores
    temp/partial files.  Reads both files in chunks to avoid loading large
    files fully into memory.
    """
    _, new_ext = os.path.splitext(new_path)
    new_size = os.path.getsize(new_path)
    for entry in os.listdir(download_dir):
        if _is_temp_file(entry):
            continue
        full = os.path.join(download_dir, entry)
        if full == new_path or not os.path.isfile(full):
            continue
        _, entry_ext = os.path.splitext(entry)
        if entry_ext.lower() != new_ext.lower():
            continue
        if os.path.getsize(full) != new_size:
            continue
        # Same size and extension — compare content
        matched = True
        with open(new_path, 'rb') as f_new, open(full, 'rb') as f_existing:
            while True:
                a = f_new.read(65536)
                b = f_existing.read(65536)
                if a != b:
                    matched = False
                    break
                if not a:
                    break
        if matched:
            return entry
    return None


def _audio_fingerprint(path: str) -> list[int] | None:
    """Return a chromaprint fingerprint array for *path* using fpcalc.

    Analyses up to the first 120 seconds of audio so the check stays fast
    even for long files.  Returns None if fpcalc is not installed.
    """
    fpcalc = shutil.which('fpcalc')
    if fpcalc is None:
        return None
    try:
        result = subprocess.run(
            [fpcalc, '-raw', '-json', '-length', '120', path],
            capture_output=True, text=True, timeout=120,
        )
        data = json.loads(result.stdout)
        fp = data.get('fingerprint')
        return fp if isinstance(fp, list) else None
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None


def _fingerprint_similarity(fp1: list[int], fp2: list[int]) -> float:
    """Return bit-match similarity [0, 1] between two chromaprint fingerprint arrays.

    Each element is a 32-bit integer; similarity is the fraction of bits that
    match across the shorter of the two arrays.  A score ≥ 0.85 indicates the
    same underlying audio track (possibly at different bitrates or sample rates).
    """
    n = min(len(fp1), len(fp2))
    if n == 0:
        return 0.0
    matching = sum(32 - bin(a ^ b).count('1') for a, b in zip(fp1[:n], fp2[:n]))
    return matching / (n * 32)


def _format_ts(seconds: float) -> str:
    """Format *seconds* as HH:MM:SS.mmm for use as an ffmpeg -ss argument."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'


def _video_frame_hash(path: str, timestamp: str) -> bytes | None:
    """Extract a 32×32 grayscale frame at *timestamp* and return its raw pixels.

    Uses ffmpeg pipe output — no temp files needed.  Returns None if ffmpeg is
    unavailable or the timestamp is beyond the end of the video.
    """
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        return None
    try:
        result = subprocess.run(
            [ffmpeg, '-ss', timestamp, '-i', path,
             '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray',
             '-vf', 'scale=32:32', '-loglevel', 'error', 'pipe:1'],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0 and len(result.stdout) == 32 * 32:
            return result.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _frame_similarity(h1: bytes, h2: bytes) -> float:
    """Return pixel-wise similarity [0, 1] between two raw 32×32 grayscale frames."""
    if not h1 or not h2 or len(h1) != len(h2):
        return 0.0
    total_diff = sum(abs(a - b) for a, b in zip(h1, h2))
    return 1.0 - total_diff / (255 * len(h1))


def _videos_are_similar(path_a: str, path_b: str) -> bool:
    """Return True if *path_a* and *path_b* appear to contain the same AV content.

    Three-level comparison, from cheapest to most thorough:
      1. Duration gate  — if durations differ by > 3 s the files cannot be the
                          same content.
      2. Audio fingerprint (fpcalc/chromaprint) — compares up to 120 s of audio.
                          Score ≥ 0.85 → same; score < 0.5 → different.
      3. Video frame hash (ffmpeg) — compares 32×32 grayscale frames at 10 % and
                          50 % of the shorter clip's duration.
    Falls back gracefully when tools are missing.
    """
    dur_a = _video_duration(path_a)
    dur_b = _video_duration(path_b)

    # 1. Duration gate
    if dur_a is not None and dur_b is not None:
        if abs(dur_a - dur_b) > 1.0:
            return False

    # 2. Audio fingerprint
    fp_a = _audio_fingerprint(path_a)
    fp_b = _audio_fingerprint(path_b)
    if fp_a is not None and fp_b is not None:
        sim = _fingerprint_similarity(fp_a, fp_b)
        if sim >= 0.85:
            return True
        if sim < 0.5:
            return False
        # inconclusive — fall through to frame check

    # 3. Video frame comparison
    short_dur = min(d for d in (dur_a, dur_b) if d is not None) if (dur_a or dur_b) else 60.0
    timestamps = [_format_ts(short_dur * 0.10), _format_ts(short_dur * 0.50)]
    frame_sims = []
    for ts in timestamps:
        h_a = _video_frame_hash(path_a, ts)
        h_b = _video_frame_hash(path_b, ts)
        if h_a and h_b:
            frame_sims.append(_frame_similarity(h_a, h_b))

    if frame_sims:
        return all(s >= 0.85 for s in frame_sims)

    # Fallback: trust duration alone when no AV tools are available
    return False


def _is_av_similar(new_path: str, folder: str) -> str | None:
    """Return the path of an existing video in *folder* that is AV-similar to *new_path*.

    Uses duration, audio fingerprinting (fpcalc), and video frame hashing (ffmpeg)
    in order from cheapest to most thorough.  Returns None if no similar video is
    found or the folder contains no other video files.
    """
    for f in os.listdir(folder):
        if _is_temp_file(f):
            continue
        full = os.path.join(folder, f)
        if full == new_path or not os.path.isfile(full):
            continue
        if not _is_video_filename(f):
            continue
        if _videos_are_similar(new_path, full):
            return full
    return None


def _dedup_existing(base_path: str) -> int:
    """Hash every file under *base_path* and remove exact duplicates.

    For each set of identical files the oldest (earliest mtime) is kept;
    all others are deleted.  Returns the number of files removed.

    Controlled by the DEDUP_EXISTING env var (default 'true').
    Set DEDUP_EXISTING=false in .env to skip this scan.
    """
    print('\n[dedup] Scanning for duplicates (set DEDUP_EXISTING=false to skip)...')

    # Collect all candidate files first so we can show a total count.
    candidates: list[str] = []
    for root, dirs, files in os.walk(base_path):
        dirs.sort()
        for f in sorted(files):
            if _is_temp_file(f):
                continue
            full = os.path.join(root, f)
            if os.path.isfile(full):
                candidates.append(full)

    total = len(candidates)
    hash_to_paths: dict[str, list[str]] = {}

    completed = 0
    def _hash_one(fpath: str) -> tuple[str, str]:
        return fpath, _file_hash(fpath)

    _env_threads = os.getenv('DEDUP_THREADS', '').strip()
    max_workers = int(_env_threads) if _env_threads.isdigit() else max(1, (os.cpu_count() or 4) - 2)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_hash_one, f): f for f in candidates}
            try:
                for future in concurrent.futures.as_completed(futures):
                    path, h = future.result()
                    hash_to_paths.setdefault(h, []).append(path)
                    completed += 1
                    if completed % 25 == 0 or completed == total:
                        print(f'  hashing {completed}/{total}...', flush=True)
            except KeyboardInterrupt:
                done      = sum(1 for f in futures if f.done())
                running   = sum(1 for f in futures if f.running())
                pending   = sum(1 for f in futures if not f.done() and not f.running())
                print(f'\n[dedup] interrupted — '
                      f'{done} done, {running} running, {pending} pending '
                      f'({completed}/{total} hashed)')
                print('[dedup] cancelling remaining tasks...')
                for f in futures:
                    f.cancel()
                raise
    except KeyboardInterrupt:
        print('[dedup] hash scan aborted.')
        return 0

    removed = 0
    for paths in hash_to_paths.values():
        if len(paths) < 2:
            continue
        paths.sort(key=os.path.getmtime)   # oldest first
        keeper = paths[0]
        for dup in paths[1:]:
            print(f'  [dedup] keeping  {_safe(os.path.basename(keeper))}')
            print(f'  [dedup] removing {_safe(os.path.basename(dup))}  ({_safe(os.path.dirname(dup))})')
            try:
                os.remove(dup)
                removed += 1
            except OSError as e:
                print(f'  [dedup] could not remove: {e}')

    if removed:
        print(f'[dedup] done — removed {removed} duplicate(s) from {total} files scanned')
    else:
        print(f'[dedup] done — no duplicates found ({total} files scanned)')
    return removed


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


def _match_links_to_funscripts(links: list[str], funscript_paths: list[str]) -> dict[str, tuple[str, bool]]:
    """
    Fuzzy-match each link URL to the best-fitting funscript stem using token
    overlap.  Returns {link: (funscript_stem, is_real_match)}.

    Scoring: tokenise both the funscript name and the URL path (split on
    non-alphanumeric chars, ignore tokens ≤ 2 chars), then compute
    (matching tokens) / (total funscript tokens).  The funscript with the
    highest score wins; ties go to the first funscript.  If the best score
    is below 0.25 the link is left unmatched (falls back to first funscript)
    and is_real_match is False — callers can then keep the download's original
    filename instead of renaming to the funscript basename.
    """
    def tokenize(s: str) -> list[str]:
        return [t for t in re.split(r'[^a-z0-9]+', s.lower()) if len(t) > 2]

    stems = [Path(p).stem for p in funscript_paths]
    stem_tokens = {stem: set(tokenize(stem)) for stem in stems}
    fallback = stems[0]

    result: dict[str, tuple[str, bool]] = {}
    for link in links:
        url_tokens = set(tokenize(link))
        best_stem, best_score = fallback, 0.0
        for stem, fs_tokens in stem_tokens.items():
            if not fs_tokens:
                continue
            score = len(fs_tokens & url_tokens) / len(fs_tokens)
            if score > best_score:
                best_score, best_stem = score, stem
        is_real_match = best_score >= 0.25
        result[link] = (best_stem if is_real_match else fallback, is_real_match)
    return result


def collect_tasks(base_path: str, require_funscript: bool = True) -> tuple[list, list, list, list]:
    """
    Walk *base_path* looking for folders that contain a description.json and,
    when *require_funscript* is True, at least one .funscript.

    When *require_funscript* is False, folders without a funscript are still
    processed; the folder name is used as the download basename.

    Pixeldrain list URLs (/l/<id>) are automatically expanded into individual
    file URLs (/u/<file_id>) before tasks are created.

    Returns (tasks, failures, many_funscripts, manual_folders).
    Unsupported domains are added to failures instead of aborting the run.
    """
    tasks = []
    failures = []
    many_funscripts = []
    manual_folders = []

    axis_suffixes = ('.surge', '.pitch', '.roll', '.twist', '.sway')

    for root, dirs, files in os.walk(base_path):
        dirs.sort()  # visit subdirectories in alphabetical order
        if '.manual' in files:
            manual_folders.append(root)
            continue
        if 'description.json' not in files:
            continue

        all_funscripts = glob.glob(os.path.join(glob.escape(root), '*.funscript'))
        if all_funscripts:
            main_scripts = [fs for fs in all_funscripts
                            if not any(Path(fs).stem.endswith(s) for s in axis_suffixes)]
            if not main_scripts:
                main_scripts = all_funscripts
            if len(main_scripts) >= 3:
                many_funscripts.append({
                    'folder': root,
                    'count': len(main_scripts),
                    'funscripts': ', '.join(Path(fs).name for fs in sorted(main_scripts)),
                })
            funscript_basename = Path(main_scripts[0]).stem
        elif require_funscript:
            print(f"[SKIP] No .funscript in: {_safe(root)}")
            continue
        else:
            funscript_basename = os.path.basename(root)

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
            except UnknownDomainError:
                if _ytdlp_cmd() is not None:
                    print(f"  [yt-dlp] no dedicated handler for '{domain}' — will attempt generic extraction")
                else:
                    print(f"[ERROR] unsupported domain '{domain}': {link}")
                    print("        Install yt-dlp (pip install yt-dlp) to attempt generic extraction.")
                    failures.append({
                        'link': link,
                        'funscript_name': funscript_basename,
                        'save_directory': root,
                        'domain': domain,
                    })
                    continue

            # Expand pixeldrain list URLs into individual file URLs.
            if domain == 'pixeldrain.com' and '/l/' in urlparse(link).path:
                validated_links.extend(_expand_pixeldrain_list(link))
            else:
                validated_links.append(link)

        if not validated_links:
            continue

        # Always download every link using its original filename.
        # Funscript-to-video matching is handled separately by check_funscripts.py.
        tasks.append({
            'folder': root,
            'basename': funscript_basename,
            'links': validated_links,
        })

    return tasks, failures, many_funscripts, manual_folders


def _write_playlist(base_path: str, newly_downloaded: list[str] | None = None):
    """
    Scan *base_path* recursively for video files and write full_folder_playlist.m3u8.
    Files are sorted newest-first by modification time so the most recently
    downloaded videos appear at the top when opened in a media player.
    Temp files and the playlist itself are excluded.

    If *newly_downloaded* is provided, also write new_media_playlist.m3u8 containing
    only those files (in the same newest-first order).
    """
    def _write_m3u8(path: str, video_paths: list[str]):
        with open(path, 'w', encoding='utf-8') as fp:
            fp.write('#EXTM3U\n')
            for video_path in video_paths:
                title = Path(video_path).stem
                rel_path = os.path.relpath(video_path, base_path).replace('\\', '/')
                fp.write(f'#EXTINF:-1,{title}\n')
                fp.write(f'{rel_path}\n')

    video_files = []
    for root, dirs, files in os.walk(base_path):
        for f in files:
            # Skip temp files left by handlers
            if Path(f).stem.endswith('_temp'):
                continue
            full_path = os.path.join(root, f)
            if _is_video_filename(f):
                video_files.append(full_path)

    if not video_files:
        return

    # Newest downloads first
    video_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    playlist_path = os.path.join(base_path, 'full_folder_playlist.m3u8')
    _write_m3u8(playlist_path, video_files)
    print(f'\nPlaylist updated ({len(video_files)} videos): {playlist_path}')

    if newly_downloaded:
        new_sorted = sorted(
            (p for p in newly_downloaded if os.path.exists(p)),
            key=lambda p: os.path.getmtime(p),
            reverse=True,
        )
        if new_sorted:
            new_path = os.path.join(base_path, 'new_media_playlist.m3u8')
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


def _write_manual_folders(base_path: str, manual_folders: list):
    """Print and write the list of .manual folders to manual_folders.txt."""
    if not manual_folders:
        return
    txt_path = os.path.join(base_path, 'manual_folders.txt')
    with open(txt_path, 'w', encoding='utf-8') as f:
        for path in manual_folders:
            f.write(path + '\n')
    print(f"\nSkipped {len(manual_folders)} manual folder(s):")
    for path in manual_folders:
        print(f"  {path}")
    print(f"  (list written to: {txt_path})")


def _write_many_funscripts_csv(base_path: str, many_funscripts: list):
    """Write folders with 3+ main funscripts to many_funscripts.csv in *base_path*."""
    if not many_funscripts:
        return
    csv_path = os.path.join(base_path, 'many_funscripts.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['folder', 'count', 'funscripts'])
        writer.writeheader()
        writer.writerows(many_funscripts)
    print(f"\nFolders with 3+ funscripts ({len(many_funscripts)}) written to: {csv_path}")


_FOLDER_TITLE_RE = re.compile(r'^\[\d+]\s+\d{4}-\d{2}-\d{2}\s+(.+)$')

# Thresholds for routing failed links when a matching video is already present.
_MATCH_CONFIDENT  = 0.50   # skip failure entirely — content is clearly there
_MATCH_UNCERTAIN  = 0.25   # route to uncertain CSV — possible match but unsure


def _folder_title_tokens(folder: str) -> set[str]:
    """Return the tokenised title words from a Patreon folder name.

    Folder names are expected in the form '[id] YYYY-MM-DD Title text'.
    Returns an empty set when the format doesn't match or yields no tokens.
    """
    m = _FOLDER_TITLE_RE.match(os.path.basename(folder))
    if not m:
        return set()
    return {t for t in re.split(r'[^a-z0-9]+', m.group(1).lower()) if len(t) > 2}


def _best_video_match(folder: str) -> tuple[str, float]:
    """Find the existing video in *folder* whose stem best matches the folder title.

    Returns (filename, score).  Score is (overlapping tokens) / (title tokens).
    Requires at least 2 token matches to avoid false positives on short titles
    (e.g. collection posts whose title contains only a creator name).
    Returns ('', 0.0) when no usable match is found.
    """
    title_tokens = _folder_title_tokens(folder)
    if not title_tokens:
        return '', 0.0

    best_file, best_score = '', 0.0
    try:
        for fname in os.listdir(folder):
            if _is_temp_file(fname) or not _is_video_filename(fname):
                continue
            if not os.path.isfile(os.path.join(folder, fname)):
                continue
            stem_tokens = {t for t in re.split(r'[^a-z0-9]+',
                           Path(fname).stem.lower()) if len(t) > 2}
            overlap = len(title_tokens & stem_tokens)
            if overlap < 2:
                continue
            score = overlap / len(title_tokens)
            if score > best_score:
                best_score, best_file = score, fname
    except OSError:
        pass

    return best_file, best_score


def _triage_failure(entry: dict, folder: str,
                    failures: list, uncertain: list) -> None:
    """Route a failed-download entry to the appropriate list.

    Checks whether a video already present in *folder* plausibly matches the
    folder title:
      - score >= _MATCH_CONFIDENT : content is likely already downloaded — drop
      - score >= _MATCH_UNCERTAIN : possible match — add to *uncertain*
      - otherwise                 : genuine failure — add to *failures*
    """
    matched_file, score = _best_video_match(folder)
    if score >= _MATCH_CONFIDENT:
        print(f'  [skip-fail] existing video "{_safe(matched_file)}" '
              f'matches folder title ({int(score * 100)}%) — not logged as failure')
        return
    if score >= _MATCH_UNCERTAIN:
        print(f'  [uncertain] existing video "{_safe(matched_file)}" '
              f'may match ({int(score * 100)}%) — logged for manual review')
        uncertain.append({**entry, 'matched_video': matched_file,
                          'match_score': round(score, 3)})
        return
    failures.append(entry)


def _write_uncertain_csv(base_path: str, uncertain: list):
    """Write uncertain download entries to uncertain_downloads.csv in *base_path*."""
    if not uncertain:
        return
    csv_path = os.path.join(base_path, 'uncertain_downloads.csv')
    fieldnames = ['link', 'funscript_name', 'save_directory', 'domain',
                  'matched_video', 'match_score']
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(uncertain)
    print(f"\nUncertain downloads ({len(uncertain)}) written to: {csv_path}")


def _find_existing_by_hash(folder: str, file_hash: str, exclude: str) -> str | None:
    """Return the path of any file in *folder* whose SHA-256 matches *file_hash*.

    *exclude* is the path of the newly-downloaded temp file so it is not
    compared against itself.
    """
    for f in os.listdir(folder):
        full = os.path.join(folder, f)
        if full == exclude or not os.path.isfile(full) or _is_temp_file(f):
            continue
        if _file_hash(full) == file_hash:
            return full
    return None


def _save_downloaded(downloaded: str, folder: str,
                     newly_downloaded: list[str],
                     original_name: str | None = None) -> bool:
    """Hash *downloaded*, check for duplicates, then move into place.

    The file is kept under its own download name — no renaming to funscript
    basenames is performed.  Use original_name when the file on disk is still a
    temp/partial name and the real name is known from headers or a global.

    Checks (in order):
      1. Session hashes — same content already saved this run.
      2. Any file in *folder* with the same hash — catches duplicates.
      3. Name collision with different content — saved as [alt2], [alt3], etc.

    Returns True if the file was kept (and added to *newly_downloaded*).
    Always removes *downloaded* if the content is a duplicate.
    """
    new_hash = _file_hash(downloaded)

    ext = os.path.splitext(downloaded)[1]
    if original_name:
        dest_stem = Path(original_name).stem
        dest_name = _truncate_filename(dest_stem + ext)
    else:
        dest_name = _truncate_filename(os.path.basename(downloaded))
    dest_path = os.path.join(folder, dest_name)

    # --- 1. session-level dedup ---
    if new_hash in _session_hashes:
        prior = _session_hashes[new_hash]
        print(f'  [SKIP] identical to already-downloaded file: {_safe(os.path.basename(prior))}')
        os.remove(downloaded)
        return False

    # --- 2. folder-level dedup ---
    existing_match = _find_existing_by_hash(folder, new_hash, exclude=downloaded)
    if existing_match:
        os.remove(downloaded)
        print(f'  [SKIP] identical file already on disk: {_safe(os.path.basename(existing_match))}')
        _session_hashes[new_hash] = existing_match
        return False

    # --- 3. name collision with different content — keep both ---
    if os.path.exists(dest_path):
        counter = 2
        stem_base, ext2 = os.path.splitext(dest_name)
        while True:
            alt_name = _truncate_filename(f'{stem_base} [alt{counter}]{ext2}')
            alt_path = os.path.join(folder, alt_name)
            if not os.path.exists(alt_path):
                break
            counter += 1
        os.rename(downloaded, alt_path)
        print(f'  Saved as: {_safe(alt_name)} (name collision with different content)')
        _session_hashes[new_hash] = alt_path
        newly_downloaded.append(alt_path)
        return True

    os.rename(downloaded, dest_path)
    print(f'  Saved as: {_safe(dest_name)}')
    _session_hashes[new_hash] = dest_path
    newly_downloaded.append(dest_path)
    return True


# ---------------------------------------------------------------------------
# Progress tracking — lets the user resume after a crash or interruption.
# ---------------------------------------------------------------------------

class ProgressTracker:
    """Persist per-link download progress so an interrupted run can resume.

    State is written to *base_path*/.download_progress.json after every
    completed link.  A link is "completed" when it was successfully downloaded,
    found to be a duplicate/AV-similar file, or pre-checked as already present
    — i.e. it does not need to be attempted again.  Genuine failures are NOT
    marked done so they are retried on the next run.
    """

    _FILENAME = '.download_progress.json'

    def __init__(self, base_path: str):
        self._path = os.path.join(base_path, self._FILENAME)
        self._done: dict[str, set[str]] = {}   # folder → set of completed link URLs
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._done = {k: set(v) for k, v in data.items()}
            except (OSError, json.JSONDecodeError, ValueError):
                self._done = {}

    def has_progress(self) -> bool:
        return bool(self._done)

    def is_done(self, folder: str, link: str) -> bool:
        return link in self._done.get(folder, set())

    def mark_done(self, folder: str, link: str):
        self._done.setdefault(folder, set()).add(link)
        self._save()

    def _save(self):
        tmp = self._path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({k: list(v) for k, v in self._done.items()}, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            print(f'  [progress] could not save progress: {e}')

    def clear(self):
        self._done = {}
        try:
            if os.path.exists(self._path):
                os.remove(self._path)
        except OSError:
            pass


def find_and_download(base_path: str):
    ans = input("Download even without a funscript file? (y/n, default n): ").strip().lower()
    require_funscript = ans != 'y'

    # Deduplicate existing videos unless the user has opted out.
    dedup_existing = os.getenv('DEDUP_EXISTING', 'true').strip().lower() not in ('false', '0', 'no')
    if dedup_existing:
        _dedup_existing(base_path)

    tasks, failures, many_funscripts, manual_folders = collect_tasks(base_path, require_funscript=require_funscript)
    _write_many_funscripts_csv(base_path, many_funscripts)
    _write_manual_folders(base_path, manual_folders)

    if not tasks:
        print("No valid download tasks found.")
        _write_failures_csv(base_path, failures)
        return

    tracker = ProgressTracker(base_path)
    if tracker.has_progress():
        ans = input(
            '\nA previous session was interrupted. Resume where it left off? '
            '(y/n, default y): '
        ).strip().lower()
        if ans == 'n':
            tracker.clear()
            print('Starting fresh — all links will be re-attempted.')
        else:
            print('Resuming previous session — completed links will be skipped.')

    print(f"\nFound {len(tasks)} folder(s) to process:")
    for t in tasks:
        print(f"  {_safe(t['basename'])}")
        for link in t['links']:
            status = ' [done]' if tracker.is_done(t['folder'], link) else ''
            print(f"    -> {link}{status}")

    confirm = input("\nProceed with downloads? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Aborted.")
        return

    driver = setup_driver(tasks[0]['folder'])

    # State tracked so KeyboardInterrupt can finish/clean the active download.
    current_folder: str = tasks[0]['folder']
    current_before_files: set[str] = set()

    newly_downloaded: list[str] = []
    uncertain: list[dict] = []
    total = len(tasks)
    global _last_fetch_original_name, _last_download_skipped
    completed_cleanly = False
    try:
        for task_idx, task in enumerate(tasks, start=1):
            folder   = task['folder']
            basename = task['basename']
            links    = task['links']

            current_folder = folder

            print(f"\n[{task_idx}/{total}] {_safe(basename)}")
            _cleanup_temp_files(folder)

            # Health-check the browser before touching the task; restart if dead.
            driver = _ensure_driver_alive(driver, folder)
            set_download_dir(driver, folder)

            saved_for_folder = 0
            n_links = len(links)
            for link_idx, link in enumerate(links, start=1):
                _last_fetch_original_name = None   # reset before each attempt
                _last_download_skipped = False

                if tracker.is_done(folder, link):
                    print(f"  [resume] already done — skipping: {link}")
                    continue

                try:
                    domain  = check_domain(link)
                    handler = DOMAIN_HANDLERS[domain]
                except UnknownDomainError:
                    domain  = get_domain(link)
                    handler = download_ytdlp

                before_files: set[str] = set(os.listdir(str(folder)))
                current_before_files = before_files
                print(f"  [link {link_idx}/{n_links}] [{domain}] {link}")
                print("  Downloading...")

                # Health-check the browser before navigating; restart if dead.
                driver = _ensure_driver_alive(driver, folder)

                triggered = False
                _link_failed = False
                for _browser_attempt in range(2):
                    try:
                        triggered = handler(driver, link, folder)
                        break  # success — exit retry loop
                    except CloudflareBlockedError as cf_err:
                        print(f'  [cloudflare] {cf_err}')
                        answer = input('  Switch to windowed mode and retry? (y/n): ').strip().lower()
                        if answer != 'y':
                            _triage_failure(
                                {'link': link, 'funscript_name': basename,
                                 'save_directory': folder, 'domain': domain},
                                folder, failures, uncertain)
                            _link_failed = True
                            break
                        # Restart the driver in windowed mode for this run only.
                        try:
                            driver.quit()
                        except WebDriverException:
                            pass
                        os.environ['BROWSER_HEADLESS'] = 'false'
                        print('  Restarting browser in windowed mode...')
                        driver = setup_driver(folder)
                        set_download_dir(driver, folder)
                        before_files = set(os.listdir(str(folder)))
                        try:
                            triggered = handler(driver, link, folder)
                        except CloudflareBlockedError:
                            print('  Still blocked after switching to windowed mode.')
                            _triage_failure(
                                {'link': link, 'funscript_name': basename,
                                 'save_directory': folder, 'domain': domain},
                                folder, failures, uncertain)
                            _link_failed = True
                        break
                    except (WebDriverException, urllib3.exceptions.ReadTimeoutError) as wd_err:
                        if _browser_attempt == 0:
                            print(f'  [browser] frozen during navigation ({wd_err.__class__.__name__}) — killing and restarting...')
                            try:
                                driver.quit()
                            except WebDriverException:
                                pass
                            driver = setup_driver(folder)
                            set_download_dir(driver, folder)
                            before_files = set(os.listdir(str(folder)))
                            current_before_files = before_files
                            # loop continues → second attempt with fresh driver
                        else:
                            print(f'  [browser] retry also failed: {wd_err.__class__.__name__}')
                            _triage_failure(
                                {'link': link, 'funscript_name': basename,
                                 'save_directory': folder, 'domain': domain},
                                folder, failures, uncertain)
                            _link_failed = True
                            break

                if _link_failed:
                    continue

                if _last_download_skipped:
                    tracker.mark_done(folder, link)
                    continue  # pre-download check found a duplicate — not a failure

                if not triggered:
                    print("  Could not trigger download — check the handler for this domain.")
                    _triage_failure(
                        {'link': link, 'funscript_name': basename,
                         'save_directory': folder, 'domain': domain},
                        folder, failures, uncertain)
                    continue

                downloaded = wait_for_download(folder, before_files)

                if downloaded is None:
                    print("  Download did not complete.")
                    _triage_failure(
                        {'link': link, 'funscript_name': basename,
                         'save_directory': folder, 'domain': domain},
                        folder, failures, uncertain)
                    continue

                # AV similarity check: skip if an existing video in the folder
                # has the same duration (i.e. same content, different encoding).
                if _peek_is_video(downloaded):
                    similar = _is_av_similar(downloaded, folder)
                    if similar:
                        print(f'  [SKIP] AV-similar to existing video: {_safe(os.path.basename(similar))}')
                        try:
                            os.remove(downloaded)
                        except OSError:
                            pass
                        tracker.mark_done(folder, link)
                        continue

                # Determine the original filename for use when there is no
                # funscript match.  Browser-based handlers preserve the real
                # name in the downloaded path; _direct_fetch stores it globally.
                orig_basename = os.path.basename(downloaded)
                if not _is_temp_file(orig_basename):
                    original_name: str | None = Path(_decode_filename(orig_basename)).stem
                elif _last_fetch_original_name:
                    original_name = Path(_decode_filename(_last_fetch_original_name)).stem
                else:
                    original_name = None

                kept = _save_downloaded(downloaded, folder, newly_downloaded,
                                        original_name=original_name)
                tracker.mark_done(folder, link)
                if kept:
                    saved_for_folder += 1

            _cleanup_temp_files(folder)

        completed_cleanly = True

    except KeyboardInterrupt:
        print('\n\nInterrupted.')
        try:
            current_files = set(os.listdir(current_folder)) if os.path.isdir(current_folder) else set()
            new_files = current_files - current_before_files
            already_complete = [f for f in new_files if not f.endswith(('.part', '.crdownload', '.tmp'))]
            in_progress = any(f.endswith(('.part', '.crdownload', '.tmp')) for f in new_files)

            if already_complete:
                # Download finished before the interrupt landed — save it immediately.
                downloaded: str | None = os.path.join(current_folder, already_complete[0])
            elif in_progress:
                print('  Download in progress — waiting up to 120 s for it to finish...')
                print('  Press Ctrl+C again to cancel immediately and discard the partial download.')
                downloaded = wait_for_download(current_folder, current_before_files, timeout=120)
            else:
                downloaded = None

            if downloaded:
                orig = os.path.basename(downloaded)
                if _is_temp_file(orig) and _last_fetch_original_name:
                    orig = _last_fetch_original_name
                _save_downloaded(downloaded, current_folder, newly_downloaded,
                                 original_name=Path(_decode_filename(orig)).stem)
            else:
                if in_progress:
                    print('  Download did not complete in time — removing temp files.')
                _cleanup_temp_files(current_folder)
        except KeyboardInterrupt:
            print('\n  Cancelled — removing temp files.')
            _cleanup_temp_files(current_folder)

    finally:
        driver.quit()
        if completed_cleanly:
            tracker.clear()
        _write_failures_csv(base_path, failures)
        _write_uncertain_csv(base_path, uncertain)
        _write_many_funscripts_csv(base_path, many_funscripts)
        _write_playlist(base_path, newly_downloaded)


def main():
    base_path = input("Enter full file path to scan for downloads: ").strip()
    if not os.path.isdir(base_path):
        print(f"Directory not found: {base_path}")
        return
    find_and_download(base_path)


if __name__ == "__main__":
    main()
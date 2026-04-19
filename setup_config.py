#!/usr/bin/env python3
"""
Interactive setup for runtime settings, credentials, and I/O tuning.
Run this once (or again to change a value).

Settings are written to .env.
Secrets are stored in the OS keyring (Windows Credential Manager, macOS
Keychain, or Linux Secret Service); if the keyring is unavailable they
fall back to .env.

Press Enter at any prompt to keep the value shown in brackets.
"""
import concurrent.futures
import getpass
import hashlib
import json
import os
import platform
import shutil
import string
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

SERVICE   = 'patreon-funscript-video-downloader'
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

# Pseudo-filesystem types to ignore when listing drives on Linux/macOS
_PSEUDO_FS = {
    'tmpfs', 'devtmpfs', 'sysfs', 'proc', 'cgroup', 'cgroup2',
    'devpts', 'overlay', 'nsfs', 'pstore', 'securityfs', 'debugfs',
    'configfs', 'fusectl', 'hugetlbfs', 'mqueue', 'tracefs', 'bpf',
    'ramfs', 'efivarfs', 'autofs',
}

# Benchmark parameters
_BENCH_FILE_COUNT = 8
_BENCH_FILE_SIZE  = 16 * 1024 * 1024   # 16 MB per file → 128 MB total
_BENCH_CHUNK      = 1 << 20             # 1 MB read chunks
_BENCH_ROUNDS     = 2                   # timed rounds per thread count


# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def _read_env(key: str) -> str:
    """Return the current value of *key* from .env, or '' if absent."""
    if not os.path.exists(_ENV_PATH):
        return ''
    with open(_ENV_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith(f'{key}='):
                return line[len(key) + 1:].rstrip('\n')
    return ''


def _write_env(key: str, value: str, comment: str = '') -> None:
    """Write or update *key*=*value* in .env, preserving all other content."""
    lines: list[str] = []
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    for i, line in enumerate(lines):
        if line.startswith(f'{key}='):
            lines[i] = f'{key}={value}\n'
            break
    else:
        if lines and not lines[-1].endswith('\n'):
            lines.append('\n')
        if comment:
            lines.append(f'# {comment}\n')
        lines.append(f'{key}={value}\n')
    with open(_ENV_PATH, 'w', encoding='utf-8') as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Keyring helpers
# ---------------------------------------------------------------------------

try:
    import keyring as _keyring
    _KEYRING_OK = True
except ImportError:
    _KEYRING_OK = False


def _keyring_get(key: str) -> str:
    if _KEYRING_OK:
        try:
            return _keyring.get_password(SERVICE, key) or ''
        except Exception:
            pass
    return _read_env(key)


def _keyring_set(key: str, value: str) -> None:
    if _KEYRING_OK:
        try:
            _keyring.set_password(SERVICE, key, value)
            return
        except Exception as e:
            print(f'  WARNING: keyring unavailable ({e}) — saving to .env instead.')
    _write_env(key, value)


# ---------------------------------------------------------------------------
# Prompt helpers  (press Enter to keep the value shown in brackets)
# ---------------------------------------------------------------------------

def _ask(label: str, current: str) -> str:
    """Prompt with the current value in brackets; Enter keeps it."""
    hint  = f'[{current}]' if current else '[not set]'
    value = input(f'  {label} {hint}: ').strip()
    return value if value else current


def _ask_secret(label: str, current: str) -> str:
    """Like _ask but hides input; shows [set] or [not set] instead of the value."""
    hint  = '[set — Enter to keep]' if current else '[not set]'
    value = getpass.getpass(f'  {label} {hint}: ')
    return value if value else current


def _ask_bool(label: str, current: bool) -> bool:
    """Prompt for true/false; Enter keeps the current value."""
    hint = 'true' if current else 'false'
    raw  = input(f'  {label} [{hint}]: ').strip().lower()
    if raw in ('true', 'yes', '1'):
        return True
    if raw in ('false', 'no', '0'):
        return False
    return current


# ---------------------------------------------------------------------------
# Drive detection (for I/O benchmark)
# ---------------------------------------------------------------------------

def _list_drives() -> list[tuple[str, str]]:
    """Return a list of (label, path) for writable drives on this system."""
    drives: list[tuple[str, str]] = []

    if sys.platform == 'win32':
        for c in string.ascii_uppercase:
            path = f'{c}:\\'
            if os.path.exists(path) and os.access(path, os.W_OK):
                try:
                    import ctypes
                    buf = ctypes.create_unicode_buffer(261)
                    ctypes.windll.kernel32.GetVolumeInformationW(
                        path, buf, 261, None, None, None, None, 0)
                    label = f'{c}: {buf.value}' if buf.value else f'{c}:'
                except Exception:
                    label = f'{c}:'
                drives.append((label, path))
    else:
        seen: set[str] = set()
        try:
            with open('/proc/mounts', 'r') as f:
                mounts = f.readlines()
        except OSError:
            import subprocess
            try:
                out    = subprocess.check_output(['mount'], text=True)
                mounts = []
                for line in out.splitlines():
                    parts = line.split(' on ')
                    if len(parts) == 2:
                        mp  = parts[1].split(' (')[0].strip()
                        dev = parts[0].strip()
                        mounts.append(f'{dev} {mp} unknown')
            except Exception:
                mounts = []

        for line in mounts:
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mountpoint, fstype = parts[0], parts[1], parts[2]
            if fstype in _PSEUDO_FS:
                continue
            if device.startswith(('tmpfs', 'devtmpfs', 'none', 'udev')):
                continue
            mountpoint = mountpoint.replace('\\040', ' ')
            if mountpoint in seen:
                continue
            if not os.path.isdir(mountpoint) or not os.access(mountpoint, os.W_OK):
                continue
            seen.add(mountpoint)
            drives.append((f'{mountpoint}  [{device}]', mountpoint))

    return drives


# ---------------------------------------------------------------------------
# I/O benchmark
# ---------------------------------------------------------------------------

def _drop_cache(path: str) -> None:
    """Best-effort: release OS page-cache pages for *path* so subsequent
    reads come from disk rather than RAM.

    - Linux:  posix_fadvise(POSIX_FADV_DONTNEED)
    - macOS:  not possible after-the-fact; cache bypass is handled per-read
              in _hash_file_nocache via F_NOCACHE on the read fd instead.
    - Windows: no equivalent without FILE_FLAG_NO_BUFFERING (requires aligned
              buffers); silently skipped.
    """
    if sys.platform == 'linux':
        try:
            import ctypes
            libc = ctypes.CDLL('libc.so.6', use_errno=True)
            fd = os.open(path, os.O_RDONLY)
            try:
                libc.posix_fadvise(fd, 0, 0, 4)  # POSIX_FADV_DONTNEED = 4
            finally:
                os.close(fd)
        except Exception:
            pass


def _hash_file_nocache(path: str) -> str:
    """Hash *path*, bypassing the OS page cache where supported.

    - Linux:  cache was already dropped by _drop_cache before the round;
              read normally (pages won't be re-warmed between rounds because
              _drop_cache is called again before each round).
    - macOS:  sets F_NOCACHE on the read fd so the kernel skips caching.
    - Windows / other: falls back to normal buffered read.
    """
    h = hashlib.sha256()
    if sys.platform == 'darwin':
        try:
            import ctypes
            F_NOCACHE = 48
            libc = ctypes.CDLL('libc.dylib', use_errno=True)
            fd = os.open(path, os.O_RDONLY)
            libc.fcntl(fd, F_NOCACHE, 1)
            try:
                with os.fdopen(fd, 'rb', closefd=True) as f:
                    while chunk := f.read(_BENCH_CHUNK):
                        h.update(chunk)
            except Exception:
                os.close(fd)
                raise
            return h.hexdigest()
        except Exception:
            pass  # fall through to buffered read below
    with open(path, 'rb') as f:
        while chunk := f.read(_BENCH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _time_round(paths: list[str], workers: int) -> float:
    # Drop cache before each round so reads come from disk, not RAM.
    for p in paths:
        _drop_cache(p)
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_hash_file_nocache, paths))
    return time.perf_counter() - t0


def _run_benchmark(base_dir: str) -> int:
    """Benchmark hashing on *base_dir* and return the optimal thread count."""
    cpu      = os.cpu_count() or 4
    max_test = max(1, cpu - 2)
    total_mb = _BENCH_FILE_COUNT * _BENCH_FILE_SIZE // (1024 * 1024)

    print(f'  Writing {_BENCH_FILE_COUNT} × {_BENCH_FILE_SIZE // (1024*1024)} MB temp files '
          f'({total_mb} MB) to {base_dir} ...', flush=True)

    tmp_dir = tempfile.mkdtemp(prefix='dedup_bench_', dir=base_dir)
    paths: list[str] = []
    try:
        for i in range(_BENCH_FILE_COUNT):
            p = os.path.join(tmp_dir, f'bench_{i}.bin')
            with open(p, 'wb') as f:
                written = 0
                while written < _BENCH_FILE_SIZE:
                    block = os.urandom(min(64 * 1024, _BENCH_FILE_SIZE - written))
                    f.write(block)
                    written += len(block)
                f.flush()
                os.fsync(f.fileno())
            paths.append(p)

        print(f'  Testing 1 – {max_test} thread(s), {_BENCH_ROUNDS} rounds each...', flush=True)

        best_workers = 1
        best_time    = float('inf')
        no_gain      = 0

        for workers in range(1, max_test + 1):
            times   = [_time_round(paths, workers) for _ in range(_BENCH_ROUNDS)]
            elapsed = min(times)
            mb_s    = total_mb / elapsed
            print(f'    {workers:2d} thread(s):  {elapsed:.2f}s  ({mb_s:.0f} MB/s)')

            if elapsed < best_time:
                best_time    = elapsed
                best_workers = workers
                no_gain      = 0
            else:
                no_gain += 1

            if no_gain >= 2:
                break

        print(f'  → optimal for this drive: {best_workers} thread(s)')
        return best_workers

    finally:
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def _setup_benchmark() -> None:
    """Interactive drive selection and I/O benchmark."""
    existing = _read_env('DEDUP_THREADS')
    if existing:
        redo = _ask_bool(
            f'DEDUP_THREADS is already set to {existing}. Re-run benchmark?',
            current=False,
        )
        if not redo:
            return

    drives = _list_drives()
    if not drives:
        print('  No writable drives detected — skipping benchmark.')
        return

    print()
    print('  Available drives:')
    for i, (label, _) in enumerate(drives, start=1):
        print(f'    {i}) {label}')
    print(f'    A) Test all drives')
    print()

    while True:
        raw = input('  Select drive(s) to benchmark [1]: ').strip()
        if raw == '':
            raw = '1'
        if raw.upper() == 'A':
            selected = list(range(len(drives)))
            break
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(drives):
                selected = [idx]
                break
        except ValueError:
            pass
        print(f'  Please enter a number between 1 and {len(drives)}, or A for all.')

    results: list[tuple[str, int]] = []
    for idx in selected:
        label, path = drives[idx]
        print(f'\n  Benchmarking: {label}')
        optimal = _run_benchmark(path)
        results.append((label, optimal))

    print()
    if len(results) == 1:
        chosen = results[0][1]
    else:
        print('  Results summary:')
        for i, (label, threads) in enumerate(results, start=1):
            print(f'    {i}) {label}  →  {threads} thread(s)')
        print()
        print('  Which result should be saved?')
        print('  (Pick the drive your library lives on.')
        print('   Enter L to use the lowest / most conservative value.)')
        print()
        while True:
            raw = input(f'  Select [1–{len(results)}, L]: ').strip()
            if raw.upper() == 'L':
                chosen = min(t for _, t in results)
                break
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(results):
                    chosen = results[idx][1]
                    break
            except ValueError:
                pass
            print(f'  Please enter a number between 1 and {len(results)}, or L.')

    _write_env(
        'DEDUP_THREADS',
        str(chosen),
        comment='Optimal hashing thread count from I/O benchmark. Re-run setup to recalibrate.',
    )
    print(f'  Saved DEDUP_THREADS={chosen} to .env')


# ---------------------------------------------------------------------------
# ffmpeg portable binary setup
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_VENV_BIN = os.path.join(_SCRIPT_DIR, '.venv', 'Scripts' if sys.platform == 'win32' else 'bin')
_FFMPEG_GITHUB_API = 'https://api.github.com/repos/BtbN/ffmpeg-builds/releases/latest'

# BtbN asset name per platform/arch (GPL static builds, no external deps).
def _ffmpeg_asset_name() -> str | None:
    machine = platform.machine().lower()
    if sys.platform == 'linux':
        return ('ffmpeg-master-latest-linuxarm64-gpl.tar.xz'
                if ('aarch64' in machine or 'arm64' in machine)
                else 'ffmpeg-master-latest-linux64-gpl.tar.xz')
    if sys.platform == 'win32':
        return 'ffmpeg-master-latest-win64-gpl.zip'
    return None  # macOS: no BtbN builds; use brew


def _ffmpeg_in_venv() -> bool:
    suffix = '.exe' if sys.platform == 'win32' else ''
    return (os.path.isfile(os.path.join(_VENV_BIN, f'ffprobe{suffix}')) and
            os.path.isfile(os.path.join(_VENV_BIN, f'ffmpeg{suffix}')))


def _setup_ffmpeg() -> None:
    """Download portable ffmpeg/ffprobe binaries into .venv/bin (or Scripts on Windows)."""
    suffix = '.exe' if sys.platform == 'win32' else ''
    binaries = [f'ffmpeg{suffix}', f'ffprobe{suffix}']

    system_has = shutil.which('ffprobe') is not None
    venv_has = _ffmpeg_in_venv()

    if system_has and not venv_has:
        print(f'  ffprobe found on system PATH ({shutil.which("ffprobe")}) — no local copy needed.')
        return

    if venv_has:
        redo = _ask_bool('ffmpeg/ffprobe already installed in .venv. Re-download?', current=False)
        if not redo:
            return

    asset = _ffmpeg_asset_name()
    if asset is None:
        print('  macOS detected — install ffmpeg via Homebrew:  brew install ffmpeg')
        return

    if not os.path.isdir(_VENV_BIN):
        print(f'  .venv not found at {_VENV_BIN} — run: python -m venv .venv && pip install -r requirements.txt')
        return

    print(f'  Fetching latest ffmpeg release from GitHub...')
    req = urllib.request.Request(
        _FFMPEG_GITHUB_API,
        headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'patreon-downloader-setup'},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            release = json.loads(r.read())
    except Exception as e:
        print(f'  Could not reach GitHub API: {e}')
        return

    asset_url = next(
        (a['browser_download_url'] for a in release.get('assets', []) if a['name'] == asset),
        None,
    )
    if not asset_url:
        print(f'  Asset not found in latest release: {asset}')
        return

    tmp_path = os.path.join(tempfile.gettempdir(), asset)
    print(f'  Downloading {asset} ...')
    try:
        urllib.request.urlretrieve(asset_url, tmp_path)
    except Exception as e:
        print(f'  Download failed: {e}')
        return

    print('  Extracting binaries...')
    try:
        if asset.endswith('.tar.xz'):
            with tarfile.open(tmp_path, 'r:xz') as tar:
                for member in tar.getmembers():
                    name = os.path.basename(member.name)
                    if name in binaries:
                        dest = os.path.join(_VENV_BIN, name)
                        f = tar.extractfile(member)
                        if f:
                            with open(dest, 'wb') as out:
                                shutil.copyfileobj(f, out)
                            os.chmod(dest, 0o755)
                            print(f'  Installed: {dest}')
        elif asset.endswith('.zip'):
            with zipfile.ZipFile(tmp_path) as zf:
                for info in zf.infolist():
                    name = os.path.basename(info.filename)
                    if name in binaries:
                        dest = os.path.join(_VENV_BIN, name)
                        with zf.open(info) as src, open(dest, 'wb') as out:
                            shutil.copyfileobj(src, out)
                        print(f'  Installed: {dest}')
    except Exception as e:
        print(f'  Extraction failed: {e}')
        return
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    print('  ffmpeg and ffprobe installed in .venv.')


# ---------------------------------------------------------------------------
# Main setup
# ---------------------------------------------------------------------------

def main():
    print()
    print('==============================================')
    print('  Setup  (press Enter to keep current values)')
    print('==============================================')

    # -------------------------------------------------------------------------
    # Settings  (written to .env)
    # -------------------------------------------------------------------------
    print()
    print('--- Settings ---')

    headless = _ask_bool(
        'Run browser in headless mode? (true/false)',
        current=_read_env('BROWSER_HEADLESS').lower() not in ('', 'false', '0', 'no'),
    )
    _write_env('BROWSER_HEADLESS', 'true' if headless else 'false',
               comment='Run the browser in headless mode. Set to false if sites block automation.')

    while True:
        raw = _ask('Max download resolution (e.g. 1080, 720, 2160)',
                   current=_read_env('MAX_RESOLUTION') or '1080')
        try:
            if int(raw) > 0:
                _write_env('MAX_RESOLUTION', raw,
                           comment='Maximum resolution to download. Downloads highest quality up to this value.')
                break
        except ValueError:
            pass
        print('  Please enter a positive integer (e.g. 1080).')

    dedup = _ask_bool(
        'Scan for duplicate videos on startup and remove extras? (true/false)',
        current=_read_env('DEDUP_EXISTING').lower() not in ('false', '0', 'no'),
    )
    _write_env('DEDUP_EXISTING', 'true' if dedup else 'false',
               comment='Remove duplicate video files on startup. Set to false to skip (faster on large libraries).')

    # -------------------------------------------------------------------------
    # Credentials  (keyring, with .env fallback)
    # -------------------------------------------------------------------------
    print()
    print('--- Credentials ---')

    print()
    print('Pixeldrain  (API key at https://pixeldrain.com/user/api)')
    print('  Leave blank to download as anonymous (public files only).')
    api_key = _ask('API key', current=_keyring_get('PIXELDRAIN_API_KEY'))
    if api_key:
        _keyring_set('PIXELDRAIN_API_KEY', api_key)

    print()
    print('iwara.tv  (required for 18+ content; leave blank to skip)')
    email = _ask('Email', current=_keyring_get('IWARA_EMAIL'))
    if email:
        _keyring_set('IWARA_EMAIL', email)
        password = _ask_secret('Password', current=_keyring_get('IWARA_PASSWORD'))
        if password:
            _keyring_set('IWARA_PASSWORD', password)
    else:
        print('  Skipping iwara.tv.')

    print()
    print('mega.nz  (only needed for private/account links; leave blank to skip)')
    mega_email = _ask('Email', current=_keyring_get('MEGA_EMAIL'))
    if mega_email:
        _keyring_set('MEGA_EMAIL', mega_email)
        mega_password = _ask_secret('Password', current=_keyring_get('MEGA_PASSWORD'))
        if mega_password:
            _keyring_set('MEGA_PASSWORD', mega_password)
    else:
        print('  Skipping mega.nz.')

    print()
    print('spankbang.com  (required for all downloads; leave blank to skip)')
    sb_user = _ask('Username', current=_keyring_get('SPANKBANG_USERNAME'))
    if sb_user:
        _keyring_set('SPANKBANG_USERNAME', sb_user)
        sb_pass = _ask_secret('Password', current=_keyring_get('SPANKBANG_PASSWORD'))
        if sb_pass:
            _keyring_set('SPANKBANG_PASSWORD', sb_pass)
    else:
        print('  Skipping spankbang.com.')

    # -------------------------------------------------------------------------
    # ffmpeg binaries
    # -------------------------------------------------------------------------
    print()
    print('--- ffmpeg binaries ---')
    _setup_ffmpeg()

    # -------------------------------------------------------------------------
    # I/O benchmark
    # -------------------------------------------------------------------------
    print()
    print('--- Disk I/O benchmark ---')
    _setup_benchmark()

    print()
    print('Done.')
    print(f'  .env: {_ENV_PATH}')
    print()


if __name__ == '__main__':
    main()

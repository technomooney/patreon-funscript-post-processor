#!/usr/bin/env python3
"""
Benchmark local disk I/O to find the optimal number of hashing threads.

Lists available drives, lets the user pick one or test them all, then writes
the best result to .env as DEDUP_THREADS.

Run automatically by setup.sh / setup.bat, or manually any time the storage
setup changes (e.g. moving the library to a different drive).
"""
import concurrent.futures
import hashlib
import os
import string
import sys
import tempfile
import time

_ENV_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
_FILE_COUNT = 8
_FILE_SIZE  = 16 * 1024 * 1024   # 16 MB per file → 128 MB total
_CHUNK      = 1 << 20             # 1 MB read chunks (matches _file_hash in downloader)
_ROUNDS     = 2                   # timed rounds per thread count

# Pseudo-filesystem types / device prefixes to ignore on Linux/macOS
_PSEUDO_FS = {
    'tmpfs', 'devtmpfs', 'sysfs', 'proc', 'cgroup', 'cgroup2',
    'devpts', 'overlay', 'nsfs', 'pstore', 'securityfs', 'debugfs',
    'configfs', 'fusectl', 'hugetlbfs', 'mqueue', 'tracefs', 'bpf',
    'ramfs', 'efivarfs', 'autofs',
}


# ---------------------------------------------------------------------------
# Drive detection
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
        seen_paths: set[str] = set()
        try:
            with open('/proc/mounts', 'r') as f:
                mounts = f.readlines()
        except OSError:
            # macOS fallback: use mount command
            import subprocess
            try:
                out = subprocess.check_output(['mount'], text=True)
                # Format: device on mountpoint (type, ...)
                mounts = []
                for line in out.splitlines():
                    parts = line.split(' on ')
                    if len(parts) == 2:
                        mp = parts[1].split(' (')[0].strip()
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
            # Normalise path (unescape \040 → space etc.)
            mountpoint = mountpoint.replace('\\040', ' ')
            if mountpoint in seen_paths:
                continue
            if not os.path.isdir(mountpoint) or not os.access(mountpoint, os.W_OK):
                continue
            seen_paths.add(mountpoint)
            label = f'{mountpoint}  [{device}]'
            drives.append((label, mountpoint))

    return drives


# ---------------------------------------------------------------------------
# Core benchmark
# ---------------------------------------------------------------------------

def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _time_round(paths: list[str], workers: int) -> float:
    """Hash all *paths* with *workers* threads; return wall-clock seconds."""
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_hash_file, paths))
    return time.perf_counter() - t0


def run_benchmark(base_dir: str) -> int:
    """Run the I/O benchmark under *base_dir* and return the optimal thread count."""
    cpu      = os.cpu_count() or 4
    max_test = max(1, cpu - 2)
    total_mb = _FILE_COUNT * _FILE_SIZE // (1024 * 1024)

    print(f'  Writing {_FILE_COUNT} × {_FILE_SIZE // (1024 * 1024)} MB temp files '
          f'({total_mb} MB) to {base_dir} ...', flush=True)

    tmp_dir = tempfile.mkdtemp(prefix='dedup_bench_', dir=base_dir)
    paths: list[str] = []
    try:
        for i in range(_FILE_COUNT):
            p = os.path.join(tmp_dir, f'bench_{i}.bin')
            with open(p, 'wb') as f:
                written = 0
                while written < _FILE_SIZE:
                    block = os.urandom(min(64 * 1024, _FILE_SIZE - written))
                    f.write(block)
                    written += len(block)
                f.flush()
                os.fsync(f.fileno())
            paths.append(p)

        print(f'  Testing 1 – {max_test} thread(s), {_ROUNDS} rounds each...', flush=True)

        best_workers = 1
        best_time    = float('inf')
        no_gain      = 0

        for workers in range(1, max_test + 1):
            times   = [_time_round(paths, workers) for _ in range(_ROUNDS)]
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


# ---------------------------------------------------------------------------
# .env helper
# ---------------------------------------------------------------------------

def _write_env(key: str, value: str, comment: str = '') -> None:
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print()
    print('--- Disk I/O benchmark ---')
    print()

    drives = _list_drives()
    if not drives:
        print('  No writable drives detected — skipping benchmark.')
        sys.exit(0)

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

    results: list[tuple[str, int]] = []   # (label, optimal_threads)
    for idx in selected:
        label, path = drives[idx]
        print(f'\n  Benchmarking: {label}')
        optimal = run_benchmark(path)
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
        print('  (If your library is on a specific drive, pick that one.')
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
    print(f'\n  Saved DEDUP_THREADS={chosen} to .env')
    print()

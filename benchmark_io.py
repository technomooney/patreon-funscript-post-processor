#!/usr/bin/env python3
"""
Benchmark local disk I/O to find the optimal number of hashing threads.

Creates a set of temp files, times how long it takes to SHA-256 hash all of
them at increasing thread counts, and writes the best result to .env as
DEDUP_THREADS.

Run automatically by setup.sh / setup.bat, or manually any time the storage
setup changes (e.g. moving the library to a different drive).
"""
import concurrent.futures
import hashlib
import os
import tempfile
import time

_ENV_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
_FILE_COUNT = 8
_FILE_SIZE  = 16 * 1024 * 1024   # 16 MB per file → 128 MB total
_CHUNK      = 1 << 20             # 1 MB read chunks (matches _file_hash in downloader)
_ROUNDS     = 2                   # timed rounds per thread count


# ---------------------------------------------------------------------------
# Helpers
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


def _write_env(key: str, value: str, comment: str = '') -> None:
    lines: list[str] = []
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    for i, line in enumerate(lines):
        if line.startswith(f'{key}='):
            lines[i] = f'{key}={value}\n'
            return _flush(lines)
    if lines and not lines[-1].endswith('\n'):
        lines.append('\n')
    if comment:
        lines.append(f'# {comment}\n')
    lines.append(f'{key}={value}\n')
    _flush(lines)


def _flush(lines: list[str]) -> None:
    with open(_ENV_PATH, 'w', encoding='utf-8') as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

def run_benchmark() -> int:
    """Run the I/O benchmark and return the optimal thread count."""
    cpu       = os.cpu_count() or 4
    max_test  = max(1, cpu - 2)

    total_mb  = _FILE_COUNT * _FILE_SIZE // (1024 * 1024)
    print(f'  Creating {_FILE_COUNT} × {_FILE_SIZE // (1024 * 1024)} MB temp files '
          f'({total_mb} MB total)...', flush=True)

    tmp_dir = tempfile.mkdtemp(prefix='dedup_bench_')
    paths: list[str] = []
    try:
        # Write temp files and fsync each one so reads go to disk on the first round.
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
        prev_time    = float('inf')
        no_gain      = 0          # consecutive non-improving steps

        for workers in range(1, max_test + 1):
            times = [_time_round(paths, workers) for _ in range(_ROUNDS)]
            elapsed = min(times)          # best of N rounds
            mb_s    = total_mb / elapsed
            print(f'    {workers:2d} thread(s):  {elapsed:.2f}s  ({mb_s:.0f} MB/s)')

            if elapsed < best_time:
                best_time    = elapsed
                best_workers = workers
                no_gain      = 0
            else:
                no_gain += 1

            # Stop early if throughput has not improved for 2 consecutive steps —
            # adding more threads is making things worse.
            if no_gain >= 2:
                break

            prev_time = elapsed

        print(f'\n  Optimal thread count: {best_workers}')
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print()
    print('--- Disk I/O benchmark ---')
    optimal = run_benchmark()
    _write_env(
        'DEDUP_THREADS',
        str(optimal),
        comment='Optimal hashing thread count from I/O benchmark. Re-run setup to recalibrate.',
    )
    print(f'  Saved DEDUP_THREADS={optimal} to .env')
    print()

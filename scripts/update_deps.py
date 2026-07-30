"""Upgrade this project's pip dependencies, but only to versions that have
been published on PyPI for at least MIN_PACKAGE_AGE_DAYS.

A malicious release on PyPI is usually caught and pulled within days, so
refusing to install anything newer than that is a cheap mitigation against
fast-moving supply chain attacks (a compromised maintainer account or token
pushing a bad release) without giving up on updates entirely — it just adds
a cooldown window.

Run via the 'u' option in run.sh/run.bat, or directly:
    .venv/bin/python scripts/update_deps.py
"""
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request

MIN_PACKAGE_AGE_DAYS = 7

REQUIREMENTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'requirements.txt')


def _package_names() -> list[str]:
    """Return the bare package names listed in requirements.txt, ignoring version pins."""
    names = []
    with open(REQUIREMENTS_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            name = re.split(r'[=<>!~\[;]', line, maxsplit=1)[0].strip()
            if name:
                names.append(name)
    return names


# Only plain numeric dotted versions (e.g. "4.46.0") are candidates. This also
# weeds out legacy pre-PEP440 formats old packages like setuptools still have
# in their PyPI history (e.g. "0.6c11"), which aren't safely comparable.
_STABLE_VERSION_RE = re.compile(r'^\d+(\.\d+)*$')


def _version_key(version: str) -> tuple:
    """Best-effort sort key for PyPI version strings, without a packaging dependency."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r'[.\-+]', version))


def _best_version(name: str, min_age_days: int) -> str | None:
    """Return the newest stable, non-yanked version of *name* published at least
    *min_age_days* ago, or None. Pre-release/dev builds (yt-dlp publishes nightly
    dev builds to PyPI) and yanked releases are never candidates regardless of age.
    """
    url = f'https://pypi.org/pypi/{name}/json'
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=min_age_days)
    candidates: list[str] = []
    for version, files in data.get('releases', {}).items():
        if not files or not _STABLE_VERSION_RE.match(version):
            continue
        if any(f.get('yanked') for f in files):
            continue
        upload_times = [f['upload_time_iso_8601'] for f in files if f.get('upload_time_iso_8601')]
        if not upload_times:
            continue
        uploaded = min(datetime.datetime.fromisoformat(t.replace('Z', '+00:00')) for t in upload_times)
        if uploaded <= cutoff:
            candidates.append(version)

    if not candidates:
        return None
    return max(candidates, key=_version_key)


def main() -> None:
    pip_cmd = [sys.executable, '-m', 'pip', 'install', '--quiet']

    for name in _package_names():
        print(f'[update-deps] checking {name} (>= {MIN_PACKAGE_AGE_DAYS}d old)...')
        try:
            version = _best_version(name, MIN_PACKAGE_AGE_DAYS)
        except (OSError, json.JSONDecodeError) as e:
            print(f'[update-deps] could not check {name}: {e} — skipping')
            continue

        if version is None:
            print(f'[update-deps] no version of {name} is {MIN_PACKAGE_AGE_DAYS}+ days old — leaving as-is')
            continue

        print(f'[update-deps] installing {name}=={version}')
        result = subprocess.run(pip_cmd + [f'{name}=={version}'])
        if result.returncode != 0:
            print(f'[update-deps] failed to install {name}=={version}')


if __name__ == '__main__':
    main()

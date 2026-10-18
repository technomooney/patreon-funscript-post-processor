"""Hybrid creators: compare the Patreon downloads with a pixeldrain archive, and keep a ledger of Discord-only scripts.

For a creator whose scripts come from three places -- the Patreon downloader, one big pixeldrain /l/ list of loose
files, and a Discord channel that shares "secret" scripts the other two never carry -- this answers:

  1. Does the pixeldrain archive hold every funscript the Patreon downloads got? The archive is downloaded IN FULL
     into the creator's 'archive' subfolder (its links come from the creator's Discord channel and/or saved
     pixeldrain lists); the Patreon downloads are only the checklist. Matching is by the script's POINT DATA
     (funscript_utils.funscript_data), never by name, so a renamed or re-saved script still matches.
  2. Which Patreon posts carry a script the archive lacks? Those are listed with the post's own (non-pixeldrain)
     links, so the normal download (menu 5) can fetch their media. Nothing is downloaded or moved here.
  3. Which Patreon posts are "secret" posts -- a title but no links, no scripts, no media? Those are tracked in a
     ledger (library database) and matched against files fetched from the creator's Discord channel by the
     existing "Discord channel: download new pixeldrain links" plugin (default subfolder 'discord'), by
     funscript name; the ledger records what each secret post resolved to, or that it is still unresolved.

The fetching steps reuse the existing Discord and pixeldrain sync plugins (only-what-is-new, journaled for undo
by them). The audit itself is read-only: it writes only reports under _reports/ and rows in the library database.
"""
import csv
import hashlib
import json
import os
import re

import action_log
import creator_config
import creator_profiles
import funscript_utils as fu
import library_db
import pixeldrain_sync as ps

SCRIPT_ID = 'bekscript_hybrid_audit'
MENU_LABEL = 'BekScript: audit Patreon vs pixeldrain archive + Discord secret ledger'
MENU_DESCRIPTION = (
    'Checks, by funscript point data (not names), that the pixeldrain archive has every script the Patreon downloads\n'
    'got; lists posts whose script the archive lacks; tracks title-only "secret" posts against the Discord downloads.'
)
REPLACES_DOWNLOAD = False
DISCORD_DEST = 'discord'
ARCHIVE_DEST = 'archive'
_LEDGER_KEY = 'secret_post_ledger'
_TOOL = SCRIPT_ID
_MEDIA = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4v', '.wmv', '.zip', '.rar', '.7z'}


def fingerprint(path: str) -> str | None:
    """Stable hash of a funscript's playback data, or None if it is not a parseable funscript."""
    data = fu.funscript_data(path)
    if data is None:
        return None
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def norm(name: str) -> str:
    """Name reduced for loose matching of a post title to a file: lowercase alphanumerics only."""
    return re.sub(r'[^a-z0-9]+', '', os.path.splitext(name)[0].casefold())


# ---------------------------------------------------------------------------
# archive side
# ---------------------------------------------------------------------------

def archive_scripts(base_path: str) -> dict[str, list[str]]:
    """{fingerprint: [paths relative to base_path]} for every funscript in the downloaded archive folder."""
    top = os.path.join(base_path, ARCHIVE_DEST)
    found: dict[str, list[str]] = {}
    for cur, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in action_log.SKIP_DIRNAMES]
        for f in sorted(files):
            if f.lower().endswith('.funscript'):
                p = os.path.join(cur, f)
                found.setdefault(fingerprint(p), []).append(os.path.relpath(p, base_path))
    found.pop(None, None)       # unparseable files can never prove a match
    return found


# ---------------------------------------------------------------------------
# Patreon side
# ---------------------------------------------------------------------------

def scan_posts(base_path: str) -> dict[str, dict]:
    """{post folder (relative): {'scripts': [(rel path, fingerprint)], 'has_media': bool, 'links': [...]}} for
    every folder holding a description.json. The archive and Discord folders are not posts."""
    import downloadContent as dc
    posts: dict[str, dict] = {}
    protected = creator_config.protected_paths(base_path)
    for cur, dirs, files in os.walk(base_path):
        dirs.sort()
        dirs[:] = [d for d in dirs if d not in action_log.SKIP_DIRNAMES and not d.startswith('_') and d not in (DISCORD_DEST, ARCHIVE_DEST)]
        creator_config.prune(protected, cur, dirs)
        if 'description.json' not in files:
            continue
        rel = os.path.relpath(cur, base_path)
        try:
            entries = dc.extract_link_entries_from_description(os.path.join(cur, 'description.json'))
        except (OSError, ValueError):
            entries = []
        links = []
        for e in entries:
            domain = dc.get_domain(e['href'])
            if domain == 'pixeldrain.com' or any(domain == s or domain.endswith('.' + s) for s in dc.SKIP_DOMAINS):
                continue
            if e['href'] not in links:
                links.append(e['href'])
        posts[rel] = {'scripts': [], 'has_media': False, 'links': links}
        for sub, subdirs, subfiles in os.walk(cur):
            subdirs[:] = [d for d in subdirs if d not in action_log.SKIP_DIRNAMES]
            for f in subfiles:
                ext = os.path.splitext(f)[1].lower()
                if ext == '.funscript':
                    p = os.path.join(sub, f)
                    posts[rel]['scripts'].append((os.path.relpath(p, base_path), fingerprint(p)))
                elif ext in _MEDIA:
                    posts[rel]['has_media'] = True
    return posts


def gaps(posts: dict[str, dict], archive: dict[str, list[dict]]) -> list[dict]:
    """Every Patreon script whose point data the archive does not hold."""
    out = []
    for rel, p in sorted(posts.items()):
        for script, fp in p['scripts']:
            if fp is None:
                out.append({'post': rel, 'script': script, 'reason': 'unreadable funscript', 'links': p['links']})
            elif fp not in archive:
                out.append({'post': rel, 'script': script, 'reason': 'not in the archive', 'links': p['links']})
    return out


def secret_posts(posts: dict[str, dict]) -> list[str]:
    """Posts that are only a title: no links, no scripts, no media."""
    return sorted(rel for rel, p in posts.items() if not p['links'] and not p['scripts'] and not p['has_media'])


# ---------------------------------------------------------------------------
# Discord side
# ---------------------------------------------------------------------------

def discord_files(base_path: str) -> list[str]:
    """Funscripts under the Discord destination folder, relative to base_path."""
    top = os.path.join(base_path, DISCORD_DEST)
    out = []
    for cur, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in action_log.SKIP_DIRNAMES]
        out += [os.path.relpath(os.path.join(cur, f), base_path) for f in files
                if f.lower().endswith('.funscript')]
    return sorted(out)


def update_ledger(root: str, base_path: str, secrets: list[str], files: list[str]) -> dict[str, dict]:
    """Record each secret post and what it resolved to. A resolved entry is never reverted to unresolved, even
    if the Discord file is later moved or renamed -- that is the 'keep track' part."""
    ledger = library_db.tool_data_get(root, _TOOL, _LEDGER_KEY, {})
    by_norm: dict[str, list[str]] = {}
    for f in files:
        by_norm.setdefault(norm(os.path.basename(f)), []).append(f)
    for post in secrets:
        title = os.path.basename(post)
        entry = ledger.setdefault(post, {'title': title, 'status': 'unresolved', 'files': []})
        n = norm(title)
        hits = by_norm.get(n) or [f for k, v in by_norm.items() if n and (n in k or k in n) for f in v]
        if hits:
            entry['status'], entry['files'] = 'resolved', sorted(set(entry['files']) | set(hits))
    library_db.tool_data_set(root, _TOOL, _LEDGER_KEY, ledger, _TOOL)
    return ledger


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _csv(base_path: str, name: str, header: list[str], rows: list[list]) -> str:
    d = os.path.join(base_path, '_reports')
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def audit(base_path: str, root: str) -> dict:
    archive = archive_scripts(base_path)
    problems = [] if os.path.isdir(os.path.join(base_path, ARCHIVE_DEST)) else \
        [f'no "{ARCHIVE_DEST}" folder yet -- fetch the archive first, or every Patreon script will look missing']
    posts = scan_posts(base_path)
    missing = gaps(posts, archive)
    ledger = update_ledger(root, base_path, secret_posts(posts), discord_files(base_path))
    gap_csv = _csv(base_path, 'hybrid_missing_from_archive.csv', ['post', 'script', 'reason', 'post links'],
                   [[g['post'], g['script'], g['reason'], ' '.join(g['links'])] for g in missing])
    secret_csv = _csv(base_path, 'hybrid_secret_ledger.csv', ['post', 'status', 'discord files'],
                      [[p, e['status'], ' | '.join(e['files'])] for p, e in sorted(ledger.items())])
    return {'archive_scripts': sum(len(v) for v in archive.values()),
            'patreon_scripts': sum(len(p['scripts']) for p in posts.values()),
            'missing': missing, 'problems': problems, 'ledger': ledger, 'gap_csv': gap_csv, 'secret_csv': secret_csv}


def fetch_archive(base_path: str, interactive: bool) -> None:
    """Download whatever the archive is still missing: links from the Discord channel (whole history the first time,
    then only newer messages) and any saved pixeldrain lists, all into the 'archive' folder."""
    import discord_pixeldrain_links as dpl
    creator_key = os.path.basename(os.path.normpath(base_path))
    dpl.sync_channel(base_path, dest=ARCHIVE_DEST, dry_run=False, interactive=interactive)
    sources = [s if isinstance(s, dict) else {'url': s, 'dest': ''}
               for s in creator_profiles.get(creator_key).get(ps.SOURCES_KEY) or []]
    if sources:
        ps.sync_urls(base_path, [s['url'] for s in sources], dest=ARCHIVE_DEST, interactive=interactive)


def run(base_path: str, options: dict | None = None) -> None:
    interactive = options is None
    root = ps.resolve_root(base_path, interactive)
    if root is None:
        return
    fetch = (options or {}).get('fetch', True) if not interactive else input(
        f'  First fetch new archive links from the Discord channel into "{ARCHIVE_DEST}" (opens the browser)? (Y/n): '
    ).strip().lower() != 'n'
    if fetch:
        try:
            fetch_archive(base_path, interactive)
        finally:
            import discord_passwords
            discord_passwords.close()
    r = audit(base_path, root)
    print(f'  archive funscripts: {r["archive_scripts"]}   Patreon funscripts: {r["patreon_scripts"]}')
    print(f'  Patreon scripts missing from the archive: {len(r["missing"])}  ->  {r["gap_csv"]}')
    if r['missing']:
        print('  Run the normal download (menu 5) to fetch those posts\' media; they are listed with their links.')
    unresolved = sum(1 for e in r['ledger'].values() if e['status'] == 'unresolved')
    print(f'  secret (title-only) posts: {len(r["ledger"])}, still unresolved: {unresolved}  ->  {r["secret_csv"]}')
    for p in r['problems']:
        print(f'  ! {p}')


def setup_unattended(current: dict) -> dict:
    a = input(f'  fetch new archive links from Discord each run? (Enter keeps {current.get("fetch", True)}) y/n: ').strip().lower()
    return {**current, 'fetch': current.get('fetch', True) if not a else a == 'y'}

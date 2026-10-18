"""Pixeldrain repo sync: download only the files you do not already have.

Replaces a manual `rclone copy` of a pixeldrain repository. rclone decides what is missing by comparing file
names and sizes, so a file you later transcoded (smaller, maybe .mkv -> .mp4) looks "missing or different" and
gets fetched again, undoing the savings. This plugin keys on pixeldrain's own file id instead, and recognises a
transcoded file through the shared library database and the AV1 compressor's log (engine: pixeldrain_sync.py).

It prints a plan first (new / already have / ambiguous) and asks before downloading anything. Ambiguous files --
same name but a different size or extension -- are reported and never overwritten or re-fetched. New downloads
are sha256-verified, journaled for undo, and recorded in <library root>/.library-data/library.db as they finish.

Sources (pixeldrain /l/ lists and /u/ files) are saved per creator in the creator profile. /d/ filesystem shares
are not listed by this version.
"""
import os

import creator_profiles
import folder_log
import pixeldrain_sync as ps
import pixeldrain_util as pdu

SCRIPT_ID = ps.SCRIPT_ID
MENU_LABEL = 'Pixeldrain repo sync (download only what you do not have)'
MENU_DESCRIPTION = (
    'Lists the creator\'s saved pixeldrain list(s), shows what is new vs already owned (including files the AV1\n'
    'compressor transcoded), and downloads only the new ones. Never re-fetches or overwrites transcoded files.'
)
REPLACES_DOWNLOAD = False


def _sources(creator_key: str, options: dict | None) -> list[dict]:
    raw = (options or {}).get('sources') or creator_profiles.get(creator_key).get(ps.SOURCES_KEY) or []
    return [s if isinstance(s, dict) else {'url': s, 'dest': ''} for s in raw]


def run(base_path: str, options: dict | None = None) -> None:
    interactive = options is None
    creator_key = os.path.basename(os.path.normpath(base_path))
    root = ps.resolve_root(base_path, interactive)
    if root is None:
        return
    sources = _sources(creator_key, options)
    if interactive:
        for s in sources:
            print(f'  saved source: {s["url"]}  ->  {s.get("dest") or "(creator folder)"}')
        if not sources or input('  Add another pixeldrain list/file URL? (y/N): ').strip().lower() == 'y':
            while True:
                url = input('  pixeldrain URL (blank to finish): ').strip()
                if not url:
                    break
                if pdu.kind(url) not in ('list', 'file'):
                    print('  only pixeldrain /l/ lists and /u/ files are supported here.')
                    continue
                dest = input('  save into which subfolder of this creator folder? (blank = the folder itself): ').strip()
                sources.append({'url': url, 'dest': dest})
            profile = creator_profiles.load()
            profile.setdefault(creator_key, {})[ps.SOURCES_KEY] = sources
            creator_profiles.save(profile)
    if not sources:
        print('  No pixeldrain sources configured.')
        return

    items = ps.plan(root, base_path, sources)
    counts = ps.summarize(items)
    ps.print_plan(items, counts)
    dry = (options or {}).get('dry_run', False)
    if interactive:
        if not counts[ps.NEW] and not counts[ps.HAVE]:
            return
        if input(f'  Download {counts[ps.NEW]} new file(s) and record {counts[ps.HAVE]} you already have? (y/N): ').strip().lower() != 'y':
            print('  Nothing changed (dry run only).')
            return
    elif dry:
        return
    result = ps.execute(root, base_path, items)
    folder_log.append_run(base_path, SCRIPT_ID, **result, ambiguous=counts[ps.AMBIGUOUS])
    print(f'  downloaded {result["downloaded"]}, recorded {result["adopted"]} already owned, '
          f'{result["failed"]} failed, {counts[ps.AMBIGUOUS]} ambiguous left alone.')


def setup_unattended(current: dict) -> dict:
    """Ask for the pixeldrain sources for 'sa' (Enter keeps what is there)."""
    have = [s['url'] if isinstance(s, dict) else s for s in current.get('sources', [])]
    print(f'  current sources: {", ".join(have) or "(none -- the creator profile\'s saved sources are used)"}')
    raw = input('  pixeldrain list/file URLs, space-separated (Enter keeps current): ').strip()
    if not raw:
        return current
    return {**current, 'sources': [{'url': u, 'dest': ''} for u in raw.split() if pdu.kind(u) in ('list', 'file')]}

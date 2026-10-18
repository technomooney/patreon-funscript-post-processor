"""Per-creator Discord channel jobs: which channels a creator uses and what to do with each one's messages.

A creator can have any number of named channels (their real server/channel IDs live in the creator profile under
the channel's label, see creator_profiles.get_discord). What each channel is FOR is set per creator in
creator_config.json:

    "discord_channels": [
      {"channel": "archive", "method": "pixeldrain_links", "dir": "!archive", "mode": "latest"},
      {"channel": "secrets", "method": "pixeldrain_links", "dir": "!discord", "mode": "every"},
      {"channel": "files",   "method": "attachments", "dir": "!uploads", "exts": [".funscript", ".zip"]},
      {"channel": "links",   "method": "site_links", "dir": "!links"},
      {"channel": "pw",      "method": "passwords"}
    ]

methods
  pixeldrain_links  the channel's posts carry pixeldrain links (base64-hidden ones are decoded); they are synced into
                    <creator>/<dir> (discord_pixeldrain_sync: only what is new, compressed videos recognised).
                    mode "every"  = every message is its own link, all are fetched;
                    mode "latest" = a new post replaces the last, only the newest post's link is used.
  attachments       people upload files straight to the channel; the wanted extensions (default: funscript, zip, rar, 7z
                    and common video types) are downloaded into <creator>/<dir>, each attachment fetched once.
  site_links        links to sites the main downloader supports (mega, yandex, gofile, yt-dlp sites, ...) go through the
                    normal download engine, one subfolder of <creator>/<dir> per message.
  passwords         the channel posts archive passwords; they are read and remembered (creator_db) for extraction.

Nothing runs unless a creator lists it here (explicit opt-in, like every creator_config flag). The older single-channel
flag "discord_pixeldrain_links" still works and is read as one "every" job on the creator's original channel.
"""
import os

import creator_config
import progress

KEY = 'discord_channels'
METHODS = {
    'pixeldrain_links': 'pixeldrain links (every message a new link, or only the newest) -> download into a folder',
    'attachments': 'files uploaded straight to the channel -> download into a folder',
    'site_links': 'mega / other supported-site links -> the normal download engine, into a folder',
    'passwords': 'archive passwords -> remembered for extraction',
}
MODES = {'every': 'every message is its own link', 'latest': 'only the newest post\'s link counts'}


def normalize(raw: dict) -> dict | None:
    """One configured job, validated and filled in, or None when it is unusable."""
    if not isinstance(raw, dict) or raw.get('method') not in METHODS:
        return None
    channel = raw.get('channel') or None
    job = {'channel': channel, 'method': raw['method']}
    if raw['method'] in ('pixeldrain_links', 'attachments', 'site_links'):
        job['dir'] = raw.get('dir') or f'!{channel or "discord"}'
    if raw['method'] == 'pixeldrain_links':
        job['mode'] = raw.get('mode') if raw.get('mode') in MODES else 'every'
    if raw['method'] == 'attachments' and raw.get('exts'):
        job['exts'] = [str(e) for e in raw['exts']]
    return job


def jobs(base_path: str) -> list[dict]:
    """The creator's configured channel jobs ([] when none)."""
    cfg = creator_config.load(base_path)
    out = [j for j in (normalize(r) for r in cfg.get(KEY) or []) if j]
    if not out:
        legacy = cfg.get('discord_pixeldrain_links')       # the old single-channel opt-in
        if legacy:
            out.append({'channel': None, 'method': 'pixeldrain_links',
                        'dir': legacy if isinstance(legacy, str) else 'discord', 'mode': 'every'})
    return out


def describe(job: dict) -> str:
    where = f'"{job["channel"]}" channel' if job['channel'] else 'the creator\'s original channel'
    if job['method'] == 'passwords':
        return f'{where}: archive passwords'
    if job['method'] == 'attachments':
        return f'{where}: uploaded files -> {job["dir"]}/' + (f' ({", ".join(job["exts"])})' if job.get('exts') else '')
    if job['method'] == 'site_links':
        return f'{where}: mega / other site links -> {job["dir"]}/ (normal downloader)'
    return f'{where}: pixeldrain links -> {job["dir"]}/ ({MODES[job["mode"]]})'


def run_jobs(base_path: str, interactive: bool = False, dry_run: bool = False, job_list: list[dict] | None = None,
             methods: tuple[str, ...] | None = None, step=None) -> list[dict]:
    """Run the creator's channel jobs (or *job_list*). *methods* limits which kinds run. *step*(title) is called
    before each job so a caller can announce it. Returns one result per job."""
    import discord_pixeldrain_sync as dps
    results = []
    todo = [j for j in (jobs(base_path) if job_list is None else job_list) if methods is None or j['method'] in methods]
    try:
        for j in todo:
            (step or (lambda t: progress.say(f'\n{t}')))(describe(j))
            if j['method'] == 'pixeldrain_links':
                res = dps.sync_channel(base_path, dest=j['dir'], dry_run=dry_run, interactive=interactive,
                                       channel=j['channel'], latest_only=j['mode'] == 'latest')
            elif j['method'] == 'attachments':
                import discord_attachments
                res = discord_attachments.sync(base_path, j['dir'], channel=j['channel'], interactive=interactive,
                                               dry_run=dry_run, exts=j.get('exts'))
            elif j['method'] == 'site_links':
                import discord_site_links
                res = discord_site_links.sync(base_path, j['dir'], channel=j['channel'], interactive=interactive,
                                              dry_run=dry_run)
            else:
                import discord_history
                creator_key = os.path.basename(os.path.normpath(base_path))
                found = discord_history.password_history(creator_key, channel=j['channel'])
                res = {'passwords': len(found)}
            results.append({'job': j, 'result': res})
    finally:
        if todo and 'discord_browser' in __import__('sys').modules:
            __import__('sys').modules['discord_browser'].close()
    return results


def dirs_for(job_list: list[dict], mode: str) -> list[str]:
    """Folders of the pixeldrain-link jobs with the given mode ('latest' = archive-style, 'every' = per-post)."""
    return [j['dir'] for j in job_list if j['method'] == 'pixeldrain_links' and j['mode'] == mode]

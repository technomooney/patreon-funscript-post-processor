"""Tool ('t' menu): fetch new pixeldrain links from a creator's Discord channel and download what you don't have.

A thin wrapper over the core module discord_pixeldrain_sync.py, for running it by hand on any creator's folder
(reads base64-hidden links too). Creator pipelines that need it call the core module directly; this is the
manual entry point, e.g. for a first dry run.
"""
import os

import creator_profiles
import discord_pixeldrain_sync as dps

SCRIPT_ID = 'discord_pixeldrain_links'      # unchanged id: keeps existing undo journals / unattended configs working
MENU_LABEL = 'Discord links (download new pixeldrain links from a creator\'s channel, incl. base64)'
MENU_DESCRIPTION = (
    'Reads the creator\'s Discord channel (whole history the first time), decodes base64-hidden pixeldrain links,\n'
    'and downloads only what you do not already have. Browser login is manual, once; no token is used.'
)
REPLACES_DOWNLOAD = False
GENERIC_TOOL = True
DEST_KEY = 'discord_dest'


def run(base_path: str, options: dict | None = None) -> None:
    interactive = options is None
    creator_key = os.path.basename(os.path.normpath(base_path))
    dest = (options or {}).get('dest') or creator_profiles.get(creator_key).get(DEST_KEY) or dps.DEFAULT_DEST
    dry = (options or {}).get('dry_run', False)
    if interactive:
        answer = input(f'  Save into which subfolder of this creator folder? (Enter = "{dest}"): ').strip()
        if answer and answer != dest:
            dest = answer
            profiles = creator_profiles.load()
            profiles.setdefault(creator_key, {})[DEST_KEY] = dest
            creator_profiles.save(profiles)
        dry = input('  Dry run first (print what would be downloaded, change nothing)? (Y/n): ').strip().lower() != 'n'
    try:
        dps.sync_channel(base_path, dest=dest, dry_run=dry, interactive=interactive)
    finally:
        import discord_browser
        discord_browser.close()


def setup_unattended(current: dict) -> dict:
    dest = input(f'  subfolder to save into (Enter keeps "{current.get("dest", dps.DEFAULT_DEST)}"): ').strip()
    return {**current, 'dest': dest or current.get('dest', dps.DEFAULT_DEST)}

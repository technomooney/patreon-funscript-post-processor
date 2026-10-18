"""Per-creator configuration: Discord password-channel locations, archive-link
notes, funscript-source-link status, and any other creator-specific metadata
that isn't a secret.

Stored in the machine-local app database (app_db: <app home>/app.db, table
creator_profiles) -- user-specific config tied to the user's own Discord
memberships, so it lives in the user's data directory, not in the repo or in
any library. It used to be .creators.json at the repo root; that file is
copied in once (first load with an empty table) and then left untouched.
Secrets (if any ever need caching) belong in the OS keyring via
_get_secret()/keyring.set_password() in downloadContent.py, not here -- this
is for structured, non-secret metadata only.

Schema (informal, grows as needed):
{
  "<creator_key>": {
    "discord": {"guild_id": "...", "channel_id": "...", "note": "..."},
    "archive_links": {"note": "..."},
    "unattended": {"source": "...", "destination": "...", "steps": [...]}
  }
}
"""
import json
import os

import app_db
import legacy_app_data

_LEGACY_NAME = '.creators.json'
_IMPORTED_KEY = 'creators_json_imported'


def _import_legacy(con) -> None:
    """Copy the old repo-root .creators.json into the table, once. The file itself is left in place."""
    if app_db.get_meta(con, _IMPORTED_KEY):
        return
    path = legacy_app_data.legacy_path(_LEGACY_NAME)
    if os.path.isfile(path) and not app_db.load_profiles(con):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                app_db.save_profiles(con, data)
        except (OSError, json.JSONDecodeError):
            return      # unreadable: try again next time rather than marking it done
    app_db.set_meta(con, _IMPORTED_KEY, '1')
    con.commit()


def load() -> dict:
    """Return the full creator-profile dict, or {} if none exists yet."""
    con = app_db.connect()
    try:
        _import_legacy(con)
        return app_db.load_profiles(con)
    finally:
        con.close()


def save(profiles: dict) -> None:
    con = app_db.connect()
    try:
        _import_legacy(con)     # never let a first save race ahead of (and hide) the old file's contents
        app_db.save_profiles(con, profiles)
    finally:
        con.close()


def get(creator_key: str) -> dict:
    """Return the profile for *creator_key*, or {} if it isn't configured."""
    return load().get(creator_key, {})


def get_discord(creator_key: str, channel: str | None = None) -> dict | None:
    """The Discord channel config for *creator_key*: the original single channel (the password channel) when
    *channel* is None, else the named one in profile['discord_channels'] (e.g. 'archive', 'secrets')."""
    profile = get(creator_key)
    if channel is None:
        return profile.get('discord')
    return (profile.get('discord_channels') or {}).get(channel)


def _put_discord(creator_key: str, channel: str | None, cfg: dict) -> None:
    profiles = load()
    entry = profiles.setdefault(creator_key, {})
    if channel is None:
        entry['discord'] = cfg
    else:
        entry.setdefault('discord_channels', {})[channel] = cfg
    save(profiles)


def set_discord_channel(creator_key: str, guild_id: str, channel_id: str, note: str = '',
                        channel: str | None = None) -> None:
    """Record a Discord channel for *creator_key*: where its mega passwords get posted (channel=None), or one of
    its named channels (a creator can have several: one for an archive, one for secret posts, ...)."""
    cfg = {'guild_id': guild_id, 'channel_id': channel_id}
    if note:
        cfg['note'] = note
    _put_discord(creator_key, channel, cfg)


def set_discord_declined(creator_key: str, channel: str | None = None) -> None:
    """Record that *creator_key* (or one of its named channels) was offered Discord setup and declined it.

    Lets discord_browser.open_channel stop asking on every
    speculative lookup (e.g. every mega link with no inline password) for a
    creator who doesn't use Discord for this at all, while still allowing a
    genuinely confirmed need (an archive that actually turns out to require a
    password) to ask again via its own `force` path.
    """
    _put_discord(creator_key, channel, {'declined': True})


def get_unattended_config(creator_key: str) -> dict | None:
    """Return *creator_key*'s saved unattended-run config (source,
    destination, ordered steps with their default answers), or None if
    setup_unattended.py has never been run for this creator."""
    return get(creator_key).get('unattended')


def set_unattended_config(creator_key: str, config: dict) -> None:
    """Save *creator_key*'s unattended-run config -- overwrites whatever was
    there before in full (setup_unattended.py always writes the complete
    steps list, not a partial update)."""
    profiles = load()
    entry = profiles.setdefault(creator_key, {})
    entry['unattended'] = config
    save(profiles)

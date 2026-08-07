"""Per-creator configuration: Discord password-channel locations, archive-link
notes, funscript-source-link status, and any other creator-specific metadata
that isn't a secret.

Stored in .creators.json at the repo root (gitignored — this is user-specific
config tied to the user's own Discord memberships, not shared code, so it
never gets committed). Secrets (if any ever need caching) belong in the OS
keyring via _get_secret()/keyring.set_password() in downloadContent.py, not
here — this file is for structured, non-secret metadata only.

Schema (informal, grows as needed):
{
  "<creator_key>": {
    "discord": {"guild_id": "...", "channel_id": "...", "note": "..."},
    "archive_links": {"note": "..."}
  }
}
"""
import json
import os

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.creators.json'
)


def load() -> dict:
    """Return the full creator-profile dict, or {} if none exists yet / it's unreadable."""
    if not os.path.isfile(_PATH):
        return {}
    try:
        with open(_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(profiles: dict) -> None:
    with open(_PATH, 'w', encoding='utf-8') as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)
        f.write('\n')


def get(creator_key: str) -> dict:
    """Return the profile for *creator_key*, or {} if it isn't configured."""
    return load().get(creator_key, {})


def set_discord_channel(creator_key: str, guild_id: str, channel_id: str, note: str = '') -> None:
    """Record where *creator_key*'s mega passwords get posted in Discord."""
    profiles = load()
    entry = profiles.setdefault(creator_key, {})
    discord_cfg = {'guild_id': guild_id, 'channel_id': channel_id}
    if note:
        discord_cfg['note'] = note
    entry['discord'] = discord_cfg
    save(profiles)

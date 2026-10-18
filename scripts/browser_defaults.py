"""Launch options shared by every automated browser (the main downloader, the Discord reader, creator plugins).

Software rendering is the default: with the GPU on, pages (Discord's, confirmed live 2026-10-18) can render once and
then go blank in this Brave/Linux setup. Set BROWSER_DISABLE_GPU=false in .env to turn the GPU back on for all of
them. DISCORD_DISABLE_GPU, if set, overrides that for the Discord browser only (kept from the first fix).
"""
import os

_OFF = ('0', 'false', 'no')


def gpu_disabled(specific_env: str | None = None) -> bool:
    """True when the browser should run with --disable-gpu."""
    for name in (specific_env, 'BROWSER_DISABLE_GPU'):
        if name and os.getenv(name, '').strip():
            return os.getenv(name).strip().lower() not in _OFF
    return True


def apply(options, specific_env: str | None = None) -> None:
    """Add the shared default arguments to a ChromeOptions."""
    if gpu_disabled(specific_env):
        options.add_argument('--disable-gpu')

"""Launch options for the app's browser (see browser_session.py: one browser shared by the downloader, the Discord
reader and creator plugins, all started through downloadContent.setup_driver).

Software rendering is the default for all of it: with the GPU on, pages (Discord's, confirmed live 2026-10-18) can
render once and then go blank in this Brave/Linux setup. Set BROWSER_DISABLE_GPU=false in .env to turn the GPU
back on. The *specific_env* argument (formerly DISCORD_DISABLE_GPU for the separate Discord browser) is no longer
passed by anything, since Discord now runs in the shared browser.
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

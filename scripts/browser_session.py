"""The app's one browser: a single persistent profile shared by the downloader, the Discord reader and plugins.

Chromium lets only one browser process use a profile folder at a time, and the download browser stays open for a
whole run while Discord (passwords) and plugins need a browser in the middle of it. So there is exactly one
browser per process, started by downloadContent.setup_driver and registered here; everything else borrows it
through a named tab (use_tab) and hands it back (back_to_main) when done. The profile lives in
<app home>/profiles/app, so cookies and logins -- Discord, JOIGang, eroscripts (with its 2FA), iwara,
rule34video... -- survive between runs and browser restarts.

Set BROWSER_PERSISTENT_PROFILE=false to go back to a throwaway profile per run.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_paths
import legacy_app_data

PROFILE_NAME = 'app'

_driver = None
_main_handle: str | None = None       # the tab the downloader works in, while a borrowed tab is in front
_tabs: dict[str, str] = {}            # borrowed tab name -> window handle


def persistent_profile_enabled() -> bool:
    return os.getenv('BROWSER_PERSISTENT_PROFILE', 'true').strip().lower() not in ('false', '0', 'no')


def profile_dir() -> str:
    """The shared profile folder, created on demand. The first time, the old Discord profile (the one login that
    has to be done by hand) is copied in, so it is kept; the old folder is left untouched."""
    dest = os.path.join(app_paths.app_home(), 'profiles', PROFILE_NAME)
    if not os.path.exists(dest):
        seed = legacy_app_data.profile_dir('discord', '.discord_browser_profile')
        if os.path.isdir(seed) and os.listdir(seed):
            try:
                shutil.copytree(seed, dest, symlinks=True, ignore=shutil.ignore_patterns('Singleton*'))
                print(f'  [browser] shared profile seeded from the Discord profile -> {dest}')
            except (OSError, shutil.Error) as e:
                print(f'  [browser] could not seed the shared profile from {seed}: {e} -- starting a fresh one')
    os.makedirs(dest, exist_ok=True)
    return dest


def current():
    """The registered browser if it is still alive, else None."""
    global _driver
    if _driver is None:
        return None
    try:
        _ = _driver.window_handles
        return _driver
    except Exception:
        _driver = None
        _tabs.clear()
        return None


def register(driver) -> None:
    global _driver, _main_handle
    _driver = driver
    _tabs.clear()
    try:
        _main_handle = driver.current_window_handle
    except Exception:
        _main_handle = None


def get_driver(download_dir: str | None = None):
    """The shared browser, launching it (via downloadContent.setup_driver) if there isn't a live one."""
    driver = current()
    if driver is not None:
        return driver
    import tempfile
    import downloadContent as dc
    return dc.setup_driver(download_dir or tempfile.gettempdir())


def use_tab(name: str, driver=None):
    """Bring the tab called *name* to the front (opening it the first time), remembering the main tab to return
    to. Returns the driver."""
    global _main_handle
    driver = driver or get_driver()
    handles = set(driver.window_handles)
    if not _tabs and driver.current_window_handle in handles:
        _main_handle = driver.current_window_handle      # nothing borrowed yet: the front tab is the downloader's
    handle = _tabs.get(name)
    if handle not in handles:
        driver.switch_to.new_window('tab')
        handle = driver.current_window_handle
        _tabs[name] = handle
    else:
        driver.switch_to.window(handle)
    return driver


def back_to_main() -> None:
    """Return to the downloader's tab. Safe to call when nothing was borrowed or the browser is gone."""
    driver = current()
    if driver is None or _main_handle is None:
        return
    try:
        if _main_handle in driver.window_handles:
            driver.switch_to.window(_main_handle)
    except Exception:
        pass


def close_tab(name: str) -> None:
    """Close a borrowed tab and return to the main one (quits the browser if that tab was its only window)."""
    driver = current()
    handle = _tabs.pop(name, None)
    if driver is None or handle is None:
        return
    try:
        if handle in driver.window_handles:
            if len(driver.window_handles) <= 1:
                driver.quit()
                return
            driver.switch_to.window(handle)
            driver.close()
    except Exception:
        pass
    back_to_main()

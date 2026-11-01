"""Judge a failed link on a login-gated site from the page the app's own (logged-in) browser is already showing.

link_probe.py cannot trust yt-dlp or any anonymous answer for these sites: iwara answers an anonymous request for a
login-only video with the same 404 as for one that does not exist, and rule34video hides some videos behind a login.
But the handler has just opened the page in the shared browser (browser_session), which holds the user's logins, so
that page already says what the site shows a signed-in visitor. This module only READS it: no navigation, no extra
request, nothing injected into the page.

A "not found" only counts as permanent when the page proves the visitor was signed in; seen signed out it is
reported ("..._logged_out") but never permanent, because the video may simply be hidden from anonymous visitors.

What is read, per site (all observed live, 2026-11-01):
* iwara.tv: a single-page app that always loads with status 200. A missing video renders title 'Error' and the text
  'HTTP 404 - Not found'; an existing one (also a login-only one, when signed in) renders its title. Signed in =
  localStorage 'token' exists.
* rule34video.com: a removed video renders a 'Page not Found' page (title and a '404 / Page not found' heading),
  even for a signed-in user. Signed in = the header's #login button is absent.
* hanime.tv: a removed video is a real HTTP 404 with the title 'Page not found'. Signed in = no 'SIGN IN' in the
  header.
Other gated sites (joi.moe, the-joi-database.com, eroscripts, gofile) have no verified signature yet and stay
unknown.
"""
import re
from urllib.parse import urlparse

from link_probe import Verdict

_STATE_JS = r"""
var n = performance.getEntriesByType('navigation')[0];
var host = location.hostname;
var loggedIn = null;
if (/iwara\.tv$/.test(host)) loggedIn = !!window.localStorage.getItem('token');
else if (/rule34video\.com$/.test(host)) loggedIn = !document.getElementById('login');
else if (/hanime\.tv$/.test(host)) loggedIn = !/SIGN IN/.test((document.body && document.body.innerText) || '');
return {url: location.href, nav_status: n ? n.responseStatus : null, title: document.title || '',
        body: ((document.body && document.body.innerText) || '').slice(0, 2000), logged_in: loggedIn,
        ready: document.readyState};
"""

_IWARA_ID = re.compile(r'/video/([A-Za-z0-9]+)')
_R34_ID = re.compile(r'/video/(\d+)')


def _page_key(url: str):
    """What identifies the video a URL points at, so a page left over from some other link is never judged."""
    host = (urlparse(url).hostname or '').lower()
    path = urlparse(url).path
    if host.endswith('iwara.tv'):
        m = _IWARA_ID.search(path)
        return ('iwara', m.group(1)) if m else None
    if host.endswith('rule34video.com'):
        m = _R34_ID.search(path)
        return ('rule34video', m.group(1)) if m else None
    if host.endswith('hanime.tv'):
        return ('hanime.tv', path.rstrip('/'))
    return None


def _iwara_verdict(state: dict) -> Verdict | None:
    if state['title'].strip().lower() == 'error':
        m = re.search(r'HTTP (\d{3})', state['body'])
        status = int(m.group(1)) if m else None
        if status == 404:
            if state['logged_in']:
                return Verdict('iwara_not_found', 404, True)
            return Verdict('iwara_not_found_logged_out', 404, False)
        return Verdict(f'iwara_http_{status}' if status else 'iwara_error_page', status, False)
    return Verdict('iwara_alive', 200, False) if state['title'].strip() else None


def _rule34video_verdict(state: dict) -> Verdict | None:
    title = state['title'].lower()
    if 'page not found' in title or '404 / page not found' in state['body'].lower():
        if state['logged_in']:
            return Verdict('rule34video_not_found', 404, True)
        return Verdict('rule34video_not_found_logged_out', 404, False)
    return Verdict('page_alive', None, False) if title else None


def _hanimetv_verdict(state: dict) -> Verdict | None:
    if state['nav_status'] == 404 and 'page not found' in state['title'].lower():
        if state['logged_in']:
            return Verdict('hanimetv_not_found', 404, True)
        return Verdict('hanimetv_not_found_logged_out', 404, False)
    return None


def classify_page(url: str, state: dict) -> Verdict | None:
    """Verdict from what the page showed (see _STATE_JS for the keys), or None for a site with no signature or a
    page that is not the one the link points at."""
    key = _page_key(url)
    if key is None or key != _page_key(state.get('url', url)):
        return None
    if key[0] == 'iwara':
        return _iwara_verdict(state)
    if key[0] == 'rule34video':
        return _rule34video_verdict(state)
    return _hanimetv_verdict(state)


def verdict_from_current_page(driver, url: str) -> Verdict | None:
    """Read the page *driver* is on and judge *url* by it. Never raises, never navigates."""
    if _page_key(url) is None:
        return None
    try:
        return classify_page(url, driver.execute_script(_STATE_JS))
    except Exception:
        return None

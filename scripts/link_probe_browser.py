"""Check a failed link on a login-gated site through the app's own (logged-in) browser.

link_probe.py cannot ask these sites anonymously: iwara answers an anonymous request for a login-only video with the
same 404 as for a video that does not exist, and rule34video hides some videos behind a login. The shared browser
(browser_session) already holds the user's logins, so the page is opened there, in a borrowed tab, and judged by
what the site itself shows a signed-in visitor.

A "not found" only counts as permanent when the page proves the visitor was signed in; seen signed out it is
reported ("..._logged_out") but never permanent, because the video may simply be hidden from anonymous visitors.

What is read, per site (all observed live, 2026-11-01):
* iwara.tv: a single-page app. Its page always loads (status 200); the answer is the status of the API call it
  makes for the video, api.iwara.tv/video/<id>: 200 exists, 404 gone. Signed in = localStorage 'token' exists.
* rule34video.com: a removed video renders a 'Page not Found' page (title and a '404 / Page not found' heading),
  even for a signed-in user. Signed in = the header's #login button is absent.
* hanime.tv: a removed video is a real HTTP 404 with the title 'Page not found'. Signed in = no 'SIGN IN' in the
  header.
Other gated sites (joi.moe, the-joi-database.com, eroscripts, gofile) have no verified signature yet and stay
unknown.

The in-page hook below records every fetch/XHR status in memory (window.__probe) from the moment the page starts, so
nothing is written to disk and the browser's own log is not needed.
"""
import re
import time
from urllib.parse import urlparse

from link_probe import Verdict

HOOK = r"""
(function(){
  if (window.__probe) return;
  window.__probe = [];
  const of = window.fetch;
  window.fetch = function(){
    const u = (arguments[0] && arguments[0].url) || String(arguments[0]);
    return of.apply(this, arguments).then(r => { window.__probe.push({u: u, s: r.status}); return r; },
                                          e => { window.__probe.push({u: u, s: 0}); throw e; });
  };
  const oo = XMLHttpRequest.prototype.open, os_ = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u){ this.__u = u; return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(){
    this.addEventListener('loadend', () => window.__probe.push({u: String(this.__u), s: this.status}));
    return os_.apply(this, arguments);
  };
})();
"""

_STATE_JS = r"""
var n = performance.getEntriesByType('navigation')[0];
var host = location.hostname;
var loggedIn = null;
if (/iwara\.tv$/.test(host)) loggedIn = !!window.localStorage.getItem('token');
else if (/rule34video\.com$/.test(host)) loggedIn = !document.getElementById('login');
else if (/hanime\.tv$/.test(host)) loggedIn = !/SIGN IN/.test((document.body && document.body.innerText) || '');
return {nav_status: n ? n.responseStatus : null, title: document.title || '',
        body: ((document.body && document.body.innerText) || '').slice(0, 2000),
        calls: window.__probe || [], logged_in: loggedIn, ready: document.readyState};
"""

_IWARA_VIDEO = re.compile(r'/video/([A-Za-z0-9]+)')
_IWARA_API_VIDEO = re.compile(r'api\.iwara\.tv/video/([A-Za-z0-9]+)/?$')
_SETTLE_SECONDS = 15


def _iwara_verdict(url: str, state: dict) -> Verdict | None:
    m = _IWARA_VIDEO.search(urlparse(url).path)
    if not m:
        return None
    status = next((c['s'] for c in state['calls'] if (a := _IWARA_API_VIDEO.search(c['u'])) and a.group(1) == m.group(1)),
                  None)
    if status is None:
        return Verdict('iwara_no_answer', None, False)
    if status == 200:
        return Verdict('iwara_alive', 200, False)
    if status == 404:
        if state['logged_in']:
            return Verdict('iwara_not_found', 404, True)
        return Verdict('iwara_not_found_logged_out', 404, False)
    return Verdict(f'iwara_http_{status}', status, False)


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
    """Verdict from what the page showed (see _STATE_JS for the keys), or None for a site with no signature."""
    host = (urlparse(url).hostname or '').lower()
    if host.endswith('iwara.tv'):
        return _iwara_verdict(url, state)
    if host.endswith('rule34video.com'):
        return _rule34video_verdict(state)
    if host.endswith('hanime.tv'):
        return _hanimetv_verdict(state)
    return None


_TAB = 'link-probe'


def probe(driver, url: str) -> Verdict | None:
    """Open *url* in a borrowed tab of the shared browser and judge it. Never raises; the main tab is restored."""
    host = (urlparse(url).hostname or '').lower()
    if not host.endswith(('iwara.tv', 'rule34video.com', 'hanime.tv')):
        return None
    import browser_session
    try:
        browser_session.use_tab(_TAB, driver)
        try:
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': HOOK})
            driver.get(url)
            deadline = time.time() + _SETTLE_SECONDS
            state = driver.execute_script(_STATE_JS)
            while time.time() < deadline:
                # iwara's answer arrives after the page loads: wait for it, the others are ready with the page.
                if state['ready'] == 'complete' and (not host.endswith('iwara.tv') or classify_page(url, state)
                                                      and classify_page(url, state).reason != 'iwara_no_answer'):
                    break
                time.sleep(0.5)
                state = driver.execute_script(_STATE_JS)
            if state['ready'] == 'complete':
                time.sleep(1.5)   # let the page's own script finish rendering a not-found page
                state = driver.execute_script(_STATE_JS)
            return classify_page(url, state)
        finally:
            try:
                browser_session.close_tab(_TAB)
            finally:
                browser_session.back_to_main()
    except Exception:
        return None

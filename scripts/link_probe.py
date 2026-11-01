"""Ask a site whether a link that just failed is gone for good, so it is never attempted (and never stalls) again.

probe(url) returns a Verdict: the HTTP status (or MEGA API error) we got, a short reason, and `permanent`, True only
when the answer proves the link can never work. Anything uncertain (blocked, rate limited, timeout, unknown site)
is NOT permanent: a wrong "dead" verdict would hide a good link forever, a wrong "alive" costs one more attempt.

Only domains where the answer is trustworthy are probed:
* mega.nz: the public API answers a bare error number for a file/folder that does not exist (-9), is blocked by
  MEGA (-16) or has expired (-8), instantly -- the same code `mega-get` exits with, without waiting on it.
* rule34video.com: a removed video returns a real HTTP 404. A bot-looking User-Agent gets 403 from DDoS-Guard, so
  a browser UA is sent and a 403 is reported as "blocked", never as dead.
"""
import json
import re
import urllib.error
import urllib.request
from typing import NamedTuple
from urllib.parse import urlparse

_BROWSER_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
               'Chrome/130.0.0.0 Safari/537.36')
_TIMEOUT = 15
_BODY_BYTES = 20000
_MEGA_API = 'https://g.api.mega.co.nz/cs'

# MEGA SDK error -> (reason, permanent). Everything else is reported but retryable.
_MEGA_PERMANENT = {-9: 'mega_not_found', -16: 'mega_blocked', -8: 'mega_expired'}


class Verdict(NamedTuple):
    reason: str
    http_status: int | None
    permanent: bool


def _http_status(url: str) -> tuple[int | None, str]:
    """(status code, first bytes of the body) of a GET with a browser User-Agent. Status is None when the request
    itself failed (DNS, timeout...). The probe is anonymous, like nobody logged in."""
    req = urllib.request.Request(url, headers={'User-Agent': _BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status, ''
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(_BODY_BYTES).decode('utf-8', 'replace')
        except OSError:
            return e.code, ''
    except (urllib.error.URLError, OSError, ValueError):
        return None, ''


def _mega_api(payload: dict, node: str | None = None):
    """POST one command to MEGA's public API. Returns the first response item, or None on a transport failure."""
    url = f'{_MEGA_API}?id=1' + (f'&n={node}' if node else '')
    req = urllib.request.Request(url, data=json.dumps([payload]).encode(),
                                 headers={'Content-Type': 'application/json', 'User-Agent': _BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace'))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if isinstance(data, list) and data:
        return data[0]
    # A bare number (not wrapped in a list) is also how MEGA reports a request-level error such as -3 or -4.
    return data if isinstance(data, int) else None


def _mega_ref(url: str) -> tuple[str, str] | None:
    """('file'|'folder', id) from a mega.nz link in the new (/file/ID#KEY) or legacy (#!ID!KEY / #F!ID!KEY) form."""
    m = re.search(r'mega\.(?:nz|io)/(file|folder)/([A-Za-z0-9_-]{8})', url)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r'mega\.(?:nz|io)/#(F?)!([A-Za-z0-9_-]{8})', url)
    if m:
        return ('folder' if m.group(1) else 'file'), m.group(2)
    return None


def _probe_mega(url: str) -> Verdict | None:
    ref = _mega_ref(url)
    if ref is None:
        return None
    kind, node = ref
    answer = _mega_api({'a': 'g', 'p': node}) if kind == 'file' else _mega_api({'a': 'f', 'c': 1, 'r': 1}, node)
    if answer is None:
        return Verdict('mega_api_unreachable', None, False)
    if isinstance(answer, int) and answer < 0:
        return Verdict(_MEGA_PERMANENT.get(answer, f'mega_api_{-answer}'), answer, answer in _MEGA_PERMANENT)
    return Verdict('mega_alive', None, False)


_NOT_FOUND_TITLE = re.compile(r'<title>\s*(?:404|410)\b[^<]*not found|<title>[^<]*not found', re.I)


def _probe_http_page(url: str) -> Verdict:
    status, body = _http_status(url)
    if status is None:
        return Verdict('unreachable', None, False)
    if status in (404, 410):
        # Only a plain "404 Not Found" page proves the video is gone. A page for something hidden from an
        # anonymous visitor (private / members-only / login-gated) must never be mistaken for it: the downloader
        # may be logged in and see it fine.
        if _NOT_FOUND_TITLE.search(body):
            return Verdict(f'http_{status}', status, True)
        return Verdict(f'http_{status}_unrecognised', status, False)
    if status in (401, 403):
        return Verdict(f'blocked_{status}', status, False)
    if status == 429:
        return Verdict('rate_limited', status, False)
    if status >= 500:
        return Verdict(f'http_{status}', status, False)
    return Verdict('page_alive', status, False)


_PAGE_PROBED_HOSTS = ('rule34video.com',)


def probe(url: str) -> Verdict | None:
    """Verdict for *url*, or None when this site is not probed. Never raises."""
    try:
        host = (urlparse(url).hostname or '').lower()
        if host.endswith(('mega.nz', 'mega.io')):
            return _probe_mega(url)
        if host.endswith(_PAGE_PROBED_HOSTS):
            return _probe_http_page(url)
    except Exception:
        return None
    return None


def classify_entry(entry: dict) -> dict:
    """Add reason / http_status / permanent to a failure row in place (when the link's site is probed)."""
    verdict = probe(entry.get('link', ''))
    if verdict is not None:
        entry['reason'] = verdict.reason
        entry['http_status'] = verdict.http_status
        entry['permanent'] = verdict.permanent
    return entry

"""Ask a site whether a link that just failed is gone for good, so it is never attempted (and never stalls) again.

probe(url) returns a Verdict: the HTTP status (or MEGA API error) we got, a short reason, and `permanent`, True only
when the answer proves the link can never work. Anything uncertain (blocked, rate limited, timeout, unsupported,
a login wall) is NOT permanent: a wrong "dead" verdict would hide a good link forever, a wrong "alive" costs one
more attempt. Three tiers, most exact first:

1. Sites with an API that says it outright: mega.nz (the public API's -9 / -16 / -8, the same numbers `mega-get`
   exits with, instantly), pixeldrain (`not_found`), Yandex Disk (`NotFoundError`).
2. rule34video.com: a plain "404 Not Found" page. A bot-looking User-Agent gets 403 from DDoS-Guard, so a browser UA
   is sent and 403 is "blocked", never dead.
3. Every other site that is not login-gated (including domains with no dedicated handler): yt-dlp in simulate mode,
   classified by its own error message (HTTP 404/410, "has been removed", ...). Cloudflare challenges, 403s,
   "Unsupported URL" and anything mentioning login/private/age/members are never permanent.

Login-gated sites are never probed (GATED_HOSTS): iwara.tv, for example, answers an anonymous request for a
login-only video with the same 404 (errors.notFound) as for one that does not exist, and its pages are a
single-page app returning 200 for anything. Without the user's session there is no signal, so those stay unknown.
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
_YTDLP_TIMEOUT = 90
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

# Sites whose content (all or some) needs the user's login, so an anonymous probe cannot tell "gone" from "hidden".
# gofile.io is here too: its API needs a guest token plus a token read from the page, which only the handler has.
GATED_HOSTS = ('iwara.tv', 'hanime.tv', 'the-joi-database.com', 'joi.moe', 'eroscripts.com', 'gofile.io')


def _get_json(url: str) -> tuple[int | None, object]:
    """(status, parsed JSON body or None) of an anonymous GET; status None when the request itself failed."""
    req = urllib.request.Request(url, headers={'User-Agent': _BROWSER_UA, 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            status, raw = resp.status, resp.read(_BODY_BYTES)
    except urllib.error.HTTPError as e:
        try:
            status, raw = e.code, e.read(_BODY_BYTES)
        except OSError:
            return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None
    try:
        return status, json.loads(raw.decode('utf-8', 'replace'))
    except ValueError:
        return status, None


def _probe_pixeldrain(url: str) -> Verdict | None:
    m = re.search(r'pixeldrain\.(?:com|net)/(u|l)/([A-Za-z0-9_-]+)', url)
    if not m:
        return None   # /d/ filesystem paths and anything unusual: no clear answer
    api = f'https://pixeldrain.com/api/{"file" if m.group(1) == "u" else "list"}/{m.group(2)}'
    status, body = _get_json(api + ('/info' if m.group(1) == 'u' else ''))
    if status is None:
        return Verdict('unreachable', None, False)
    value = body.get('value') if isinstance(body, dict) else None
    if status == 404 and value == 'not_found':
        return Verdict('pixeldrain_not_found', 404, True)
    if status == 200:
        return Verdict('pixeldrain_alive', 200, False)
    return Verdict(f'pixeldrain_{value or status}', status, False)


def _probe_yandex(url: str) -> Verdict:
    from urllib.parse import quote
    status, body = _get_json(
        'https://cloud-api.yandex.net/v1/disk/public/resources?public_key=' + quote(url, safe=''))
    if status is None:
        return Verdict('unreachable', None, False)
    error = body.get('error') if isinstance(body, dict) else None
    if status == 404 and error == 'NotFoundError':
        return Verdict('yandex_not_found', 404, True)
    if status == 200:
        return Verdict('yandex_alive', 200, False)
    return Verdict(f'yandex_{error or status}', status, False)


# yt-dlp error text. Gate words are checked first and always win: a login/age/region/bot wall is not a removal.
_YTDLP_NOT_PERMANENT = re.compile(
    r'log ?in|sign ?in|private|members|premium|subscri|age[- ]?(restrict|verif|gate)|confirm your age|cookies|'
    r'cloudflare|captcha|anti-bot|HTTP Error (401|403|429|5\d\d)|too many requests|unsupported url|piracy|'
    r'geo|country|region|not available in your|rate.?limit|timed out|connection|ssl|certificate|impersonat', re.I)
_YTDLP_GONE = re.compile(
    r'HTTP Error (404|410)|has been (removed|deleted|taken down|terminated)|was (removed|deleted)|'
    r'does(?: not|n.t) exist|no longer (?:exists|available)', re.I)


def classify_ytdlp_error(text: str) -> Verdict:
    """Verdict from yt-dlp's stderr: only an explicit removal/404 with no login-or-block wording is permanent."""
    errors = [ln for ln in text.splitlines() if 'ERROR' in ln] or [text.strip()]
    line = errors[-1].strip()
    status = None
    m = re.search(r'HTTP Error (\d{3})', line)
    if m:
        status = int(m.group(1))
    if _YTDLP_NOT_PERMANENT.search(line):
        return Verdict('ytdlp_blocked' if status in (401, 403, 429) or 'cloudflare' in line.lower() else 'ytdlp_unclear',
                       status, False)
    if _YTDLP_GONE.search(line):
        return Verdict(f'ytdlp_http_{status}' if status else 'ytdlp_removed', status, True)
    return Verdict('ytdlp_unclear', status, False)


def _probe_ytdlp(url: str, ytdlp: list[str]) -> Verdict:
    import subprocess
    try:
        result = subprocess.run([*ytdlp, '-s', '--no-playlist', '--no-warnings', url],
                                capture_output=True, text=True, timeout=_YTDLP_TIMEOUT)
    except (subprocess.SubprocessError, OSError):
        return Verdict('ytdlp_unreachable', None, False)
    if result.returncode == 0:
        return Verdict('ytdlp_alive', None, False)
    return classify_ytdlp_error(result.stderr or result.stdout)


def probe(url: str, ytdlp: list[str] | None = None) -> Verdict | None:
    """Verdict for *url*, or None when nothing trustworthy can be asked. *ytdlp* is the command prefix for yt-dlp
    (None disables that tier). Never raises."""
    try:
        host = (urlparse(url).hostname or '').lower()
        if host.endswith(GATED_HOSTS):
            return None
        if host.endswith(('mega.nz', 'mega.io', 'mega.co.nz')):
            return _probe_mega(url)
        if host.endswith(('pixeldrain.com', 'pixeldrain.net')):
            return _probe_pixeldrain(url)
        if host.endswith(('disk.yandex.com', 'disk.yandex.ru')):
            return _probe_yandex(url)
        if host.endswith(_PAGE_PROBED_HOSTS):
            return _probe_http_page(url)
        if ytdlp:
            return _probe_ytdlp(url, ytdlp)
    except Exception:
        return None
    return None


def classify_entry(entry: dict, ytdlp: list[str] | None = None) -> dict:
    """Add reason / http_status / permanent to a failure row in place (when something trustworthy can be asked)."""
    verdict = probe(entry.get('link', ''), ytdlp)
    if verdict is not None:
        entry['reason'] = verdict.reason
        entry['http_status'] = verdict.http_status
        entry['permanent'] = verdict.permanent
    return entry

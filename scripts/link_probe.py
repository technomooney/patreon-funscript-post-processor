"""Turn what a failed download attempt already told us into a verdict: gone for good, or worth another try.

Nothing here talks to a site. The download attempt is the one and only request; when it fails, the handler passes
the server's own answer (mega-get's exit code, an HTTP status and error value, yt-dlp's error text, what the
logged-in browser is showing) to a classifier below, and the result is stored with the failed link
(report_store.record_outcome) so later runs can skip links proven dead and nobody asks the server a second time.

A Verdict has the status we saw (HTTP status, or the negated MEGA error), a short reason, and `permanent`, True only
when the answer proves the link can never work. Anything uncertain (blocked, rate limited, timeout, unsupported, a
login wall, a login-gated site's answer to a visitor who may not have been signed in) is NOT permanent: a wrong
"dead" verdict would hide a good link forever, a wrong "alive" costs one more attempt.
"""
import re
from typing import NamedTuple
from urllib.parse import urlparse


class Verdict(NamedTuple):
    reason: str
    http_status: int | None
    permanent: bool


# Sites whose content (all or some) needs the user's login, so a yt-dlp/anonymous answer cannot tell "gone" from
# "hidden". iwara.tv answers an anonymous request for a login-only video with the same 404 (errors.notFound) as
# for one that does not exist. A not-found from these only counts when the logged-in browser saw it
# (link_probe_browser).
GATED_HOSTS = ('iwara.tv', 'hanime.tv', 'the-joi-database.com', 'joi.moe', 'eroscripts.com', 'gofile.io',
               'rule34video.com', 'rule34video.party')


def is_gated(url: str) -> bool:
    return (urlparse(url).hostname or '').lower().endswith(GATED_HOSTS)


# MEGA SDK error -> reason, for the ones that mean the link can never work. mega-get exits with the negated SDK
# error, so exit 9 is API_ENOENT (-9). Everything else is reported but retryable.
_MEGA_PERMANENT = {9: 'mega_not_found', 16: 'mega_blocked', 8: 'mega_expired'}
_MEGA_RATE_LIMIT = 6   # API_ETOOMANY: "Too many requests for this resource", the exit-6 case


def mega_exit_verdict(exit_code: int | None) -> Verdict:
    """Verdict from mega-get's process exit code. -1 is our own stall kill (no progress for a minute), which is
    deliberately not a verdict: nobody knows why the link stalled."""
    if exit_code is None:
        return Verdict('mega_unknown', None, False)
    if exit_code == -1:
        return Verdict('mega_stalled', None, False)
    if exit_code in _MEGA_PERMANENT:
        return Verdict(_MEGA_PERMANENT[exit_code], -exit_code, True)
    if exit_code == _MEGA_RATE_LIMIT:
        return Verdict('mega_too_many_requests', -exit_code, False)
    return Verdict(f'mega_exit_{exit_code}', -exit_code if exit_code > 0 else None, False)


def classify_api_error(site: str, status: int | None, value: str | None) -> Verdict:
    """Verdict from a site API's HTTP status plus its error string (pixeldrain `value`, Yandex `error`). Only an
    explicit 404 with a not-found error string is permanent."""
    if status is None:
        return Verdict(f'{site}_unreachable', None, False)
    value = (value or '').strip()
    if status == 404 and value.lower().endswith(('not_found', 'notfounderror')):
        return Verdict(f'{site}_not_found', 404, True)
    return Verdict(f'{site}_{value or status}', status, False)


# yt-dlp error text. Gate words are checked first and always win: a login/age/region/bot wall is not a removal.
_YTDLP_NOT_PERMANENT = re.compile(
    r'log ?in|sign ?in|private|members|premium|subscri|age[- ]?(restrict|verif|gate)|confirm your age|cookies|'
    r'cloudflare|captcha|anti-bot|HTTP Error (401|403|429|5\d\d)|too many requests|unsupported url|piracy|'
    r'geo|country|region|not available in your|rate.?limit|timed out|connection|ssl|certificate|impersonat', re.I)
_YTDLP_GONE = re.compile(
    r'HTTP Error (404|410)|has been (removed|deleted|taken down|terminated)|was (removed|deleted)|'
    r'does(?: not|n.t) exist|no longer (?:exists|available)', re.I)


def classify_ytdlp_error(text: str, url: str = '') -> Verdict:
    """Verdict from yt-dlp's stderr: only an explicit removal/404 with no login-or-block wording is permanent,
    and never for a login-gated site, since yt-dlp runs without the user's login."""
    errors = [ln for ln in (text or '').splitlines() if 'ERROR' in ln] or [(text or '').strip()]
    line = errors[-1].strip()
    status = None
    m = re.search(r'HTTP Error (\d{3})', line)
    if m:
        status = int(m.group(1))
    if _YTDLP_NOT_PERMANENT.search(line):
        return Verdict('ytdlp_blocked' if status in (401, 403, 429) or 'cloudflare' in line.lower() else 'ytdlp_unclear',
                       status, False)
    if _YTDLP_GONE.search(line):
        return Verdict(f'ytdlp_http_{status}' if status else 'ytdlp_removed', status, not (url and is_gated(url)))
    return Verdict('ytdlp_unclear', status, False)

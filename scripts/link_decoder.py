"""Find links that creators hide as base64 text ("aHR0cHM6Ly9waXhlbGRyYWluLmNvbS9sL...") and decode them.

Used by the core link extraction (downloadContent._walk_description) so EVERY creator and every text source
(post descriptions, Discord messages, ...) benefits -- not a per-creator hack.

False positives are the risk (any long word is "base64-shaped"), so a candidate only counts when ALL of these hold:
  * at least MIN_LEN base64 characters, a run of the base64 / URL-safe alphabet with optional '=' padding;
  * it decodes cleanly (standard or URL-safe alphabet, missing padding repaired) to strict UTF-8 text with no
    control characters;
  * the decoded text contains an http(s) URL (or a bare 'host.tld/path'), after at most MAX_LAYERS decodings;
  * and, when the caller passes `accept`, that URL passes it (the downloader passes "is a supported domain").
Plain, already-visible URLs are blanked out of the text first so path segments are never mistaken for base64.

Standard library only.
"""
import base64
import binascii
import re

MIN_LEN = 24
MAX_LAYERS = 2

_CANDIDATE = re.compile(r'(?<![A-Za-z0-9+/=_\-])[A-Za-z0-9+/_\-]{%d,}={0,2}(?![A-Za-z0-9+/=_\-])' % MIN_LEN)
_VISIBLE_URL = re.compile(r'https?://\S+', re.I)
_URL = re.compile(r'https?://[^\s<>"\'\\]+', re.I)
_BARE_URL = re.compile(r'(?<![\w.@/])((?:[a-z0-9-]+\.)+[a-z]{2,})(/[^\s<>"\'\\]*)', re.I)
_TRAILING = '.,;:!?)]}>\'"'


def decode_candidate(s: str) -> str | None:
    """Base64-decode *s* (standard or URL-safe alphabet, padding optional) to clean UTF-8 text, else None."""
    s = s.strip()
    if '-' in s or '_' in s:
        if '+' in s or '/' in s:
            return None                     # mixed alphabets: not real base64
        s = s.replace('-', '+').replace('_', '/')
    s = s.rstrip('=')
    if len(s) % 4 == 1:
        return None                         # impossible base64 length
    s += '=' * (-len(s) % 4)
    try:
        raw = base64.b64decode(s, validate=True)
        text = raw.decode('utf-8')          # strict: garbage almost never survives this
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not text or any((not c.isprintable()) and c not in '\r\n\t' for c in text):
        return None
    return text


def _urls_in(text: str) -> list[str]:
    urls = [u.rstrip(_TRAILING) for u in _URL.findall(text)]
    if not urls:    # scheme-less, e.g. 'pixeldrain.com/l/abc'
        urls = ['https://' + m.group(1) + m.group(2).rstrip(_TRAILING) for m in _BARE_URL.finditer(text)]
    return [u for u in urls if '.' in u.split('//', 1)[-1].split('/', 1)[0]]


def find_encoded_urls(text: str, accept=None) -> list[dict]:
    """Every URL hidden as base64 in *text*, in order, de-duplicated.

    Returns [{'href': url, 'encoded': the base64 string, 'layers': how many decodings it took}].
    *accept(url) -> bool* lets the caller keep only URLs it can use (e.g. supported domains)."""
    found: list[dict] = []
    seen: set[str] = set()
    scrubbed = _VISIBLE_URL.sub(' ', text or '')
    for m in _CANDIDATE.finditer(scrubbed):
        encoded = m.group(0)
        current, layers = encoded, 0
        while layers < MAX_LAYERS:
            decoded = decode_candidate(current)
            if decoded is None:
                break
            layers += 1
            urls = _urls_in(decoded)
            if urls:
                for url in urls:
                    if url in seen or (accept is not None and not accept(url)):
                        continue
                    seen.add(url)
                    found.append({'href': url, 'encoded': encoded, 'layers': layers})
                break
            current = decoded.strip()       # maybe encoded twice
    return found

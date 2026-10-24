"""Diagnostic log for failed direct HTTP downloads (403s and friends).

A bare "HTTP Error 403" doesn't say whether the request lacked a cookie, carried a bot-looking User-Agent, or
hit an expired signed link. Every failure is appended to <app home>/download_debug/fetch_failures.log with the
URL, the request headers actually sent, and the server's response headers/body snippet. Header *values* for
Cookie/Authorization are never written -- only the cookie names and lengths -- so the log is safe to paste.
"""
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_paths

_SECRET_HEADERS = {'cookie', 'authorization', 'x-api-key'}
_BODY_SNIPPET = 300


def log_path() -> str:
    path = os.path.join(app_paths.app_home(), 'download_debug')
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, 'fetch_failures.log')


def _describe_header(name: str, value: str) -> str:
    if name.lower() == 'cookie':
        names = [c.split('=', 1)[0].strip() for c in value.split(';') if c.strip()]
        return f'<{len(names)} cookies: {", ".join(names)}>'
    if name.lower() in _SECRET_HEADERS:
        return f'<redacted, {len(value)} chars>'
    return value


def log_fetch_failure(url: str, headers: dict[str, str], exc: BaseException, label: str = 'GET') -> str | None:
    """Append one failure record and print a one-line summary. Returns the log path, or None if it couldn't be
    written. Never raises, so it can sit inside an except block without masking the real error."""
    try:
        host = urlparse(url).netloc
        code = getattr(exc, 'code', None)
        lines = [
            f'--- {time.strftime("%Y-%m-%d %H:%M:%S")} {label} {code or type(exc).__name__}',
            f'url: {url}',
            f'host: {host}',
            f'error: {exc}',
            'request headers sent:',
        ]
        lines += [f'  {k}: {_describe_header(k, v)}' for k, v in headers.items()]
        if not any(k.lower() == 'user-agent' for k in headers):
            lines.append('  (no User-Agent set -- urllib sends "Python-urllib/x.y")')
        if not any(k.lower() == 'cookie' for k in headers):
            lines.append('  (no Cookie sent)')
        resp_headers = getattr(exc, 'headers', None)
        if resp_headers:
            lines.append('response headers:')
            lines += [f'  {k}: {v}' for k, v in resp_headers.items()]
        try:
            body = exc.read(_BODY_SNIPPET) if hasattr(exc, 'read') else b''
            if body:
                lines.append(f'response body (first {_BODY_SNIPPET} bytes): {body.decode("utf-8", "replace")!r}')
        except Exception:
            pass
        path = log_path()
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n\n')
        print(f'  [diagnostic] {label} {code or type(exc).__name__} from {host} -- details in {path}', flush=True)
        return path
    except Exception:
        return None

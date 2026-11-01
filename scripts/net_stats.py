"""What the downloader asks of the network, counted in memory: requests, bytes, time and answers, per host and per link.

Nothing here changes behaviour. `urlopen` is a drop-in for urllib.request.urlopen that notes the host, the HTTP status (or
the error), how long the request was open and how many bytes were read through it. Tools that run as separate processes
(mega-get, yt-dlp, ffmpeg/ffprobe) are timed by their callers through `timed_process`, which records their exit code
instead of a status. A link being worked on (`begin_link`) collects its own totals, so a finished link can say what it
cost: one download plus N small checks.

The numbers exist to answer questions like "does the slowdown follow request volume or a fixed per-connection cap" and
"did that link really cost one download", and they are written to the databases by run_stats.
"""
import contextlib
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse


def host_of(url: str) -> str:
    """The host of a URL, or of a bare hostname like 'mega.nz'."""
    return (urlparse(url if '://' in url else '//' + url).hostname or 'unknown').lower().removeprefix('www.')


@dataclass
class HostStat:
    requests: int = 0
    bytes: int = 0
    seconds: float = 0.0          # time requests were open (their transfers included)
    ok: int = 0                   # 2xx answers, or a tool exiting 0
    redirects: int = 0            # 3xx
    client_errors: int = 0        # 4xx
    forbidden: int = 0            # of which 403
    rate_limited: int = 0         # 429, or mega-get exit 6
    server_errors: int = 0        # 5xx
    errors: int = 0               # no answer at all: timeout, refused, DNS, a tool failing without a status
    slowest_seconds: float = 0.0


@dataclass
class LinkStat:
    folder: str
    url: str
    host: str
    started_at: str
    requests: int = 0
    bytes: int = 0
    seconds: float = 0.0
    precheck_seconds: float = 0.0
    download_seconds: float = 0.0
    precheck_result: str = ''
    outcome: str = ''
    hosts: set = field(default_factory=set)


def _classify(stat: HostStat, status: int | None, error: bool, kind: str, rate_limit_exit: bool = False) -> None:
    if error or status is None:
        stat.errors += 1
        return
    if kind == 'process':
        if status == 0:
            stat.ok += 1
        elif rate_limit_exit:
            stat.rate_limited += 1
        else:
            stat.errors += 1
        return
    if 200 <= status < 300:
        stat.ok += 1
    elif 300 <= status < 400:
        stat.redirects += 1
    elif status == 429:
        stat.rate_limited += 1
        stat.client_errors += 1
    elif 400 <= status < 500:
        stat.client_errors += 1
        if status == 403:
            stat.forbidden += 1
    elif status >= 500:
        stat.server_errors += 1


class Collector:
    def __init__(self):
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.hosts: dict[str, HostStat] = {}
            self.links: list[LinkStat] = []
            self._current: LinkStat | None = None
            self.started = time.time()

    # -- recording -------------------------------------------------------------------------------------------
    def record(self, host: str, *, status: int | None = None, nbytes: int = 0, seconds: float = 0.0,
               error: bool = False, kind: str = 'http', rate_limit_exit: bool = False) -> None:
        with self._lock:
            stat = self.hosts.setdefault(host, HostStat())
            stat.requests += 1
            stat.bytes += nbytes
            stat.seconds += seconds
            stat.slowest_seconds = max(stat.slowest_seconds, seconds)
            _classify(stat, status, error, kind, rate_limit_exit)
            if self._current is not None:
                self._current.requests += 1
                self._current.bytes += nbytes
                self._current.seconds += seconds
                self._current.hosts.add(host)

    def add_bytes(self, host: str, nbytes: int) -> None:
        """Bytes that arrived after the request was recorded (a streamed body)."""
        with self._lock:
            self.hosts.setdefault(host, HostStat()).bytes += nbytes
            if self._current is not None:
                self._current.bytes += nbytes

    def add_seconds(self, host: str, seconds: float) -> None:
        with self._lock:
            stat = self.hosts.setdefault(host, HostStat())
            stat.seconds += seconds
            stat.slowest_seconds = max(stat.slowest_seconds, seconds)
            if self._current is not None:
                self._current.seconds += seconds

    # -- per link ---------------------------------------------------------------------------------------------
    def begin_link(self, folder: str, url: str, outcome_of=None) -> None:
        """Start collecting for a link; the previous link (if any) is finished first, its outcome read through
        *outcome_of(folder, url)* because the loop that works on links only knows it as it moves on."""
        with self._lock:
            self.end_link(outcome_of)
            self._current = LinkStat(folder=folder, url=url, host=host_of(url), started_at=time.strftime('%Y-%m-%dT%H:%M:%S'))

    def note(self, **values) -> None:
        """Attach facts to the current link: precheck_result='size shortcut', precheck_seconds=..., download_seconds=..."""
        with self._lock:
            if self._current is None:
                return
            for key, value in values.items():
                if key.endswith('_seconds'):
                    setattr(self._current, key, getattr(self._current, key) + value)
                else:
                    setattr(self._current, key, value)

    def end_link(self, outcome_of=None) -> LinkStat | None:
        with self._lock:
            link = self._current
            self._current = None
            if link is None:
                return None
            if outcome_of is not None:
                link.outcome = outcome_of(link.folder, link.url) or link.outcome
            self.links.append(link)
            return link

    def take_finished_links(self) -> list[LinkStat]:
        with self._lock:
            links, self.links = self.links, []
            return links

    # -- reading ----------------------------------------------------------------------------------------------
    def snapshot_hosts(self) -> dict[str, HostStat]:
        with self._lock:
            return {h: HostStat(**vars(s)) for h, s in self.hosts.items()}

    def totals(self) -> HostStat:
        total = HostStat()
        with self._lock:
            for s in self.hosts.values():
                for name in vars(total):
                    if name == 'slowest_seconds':
                        total.slowest_seconds = max(total.slowest_seconds, s.slowest_seconds)
                    else:
                        setattr(total, name, getattr(total, name) + getattr(s, name))
        return total


collector = Collector()


class _CountingResponse:
    """A response that counts the bytes read through it and records its open time when closed."""
    def __init__(self, resp, host: str, started: float, col: Collector):
        self._resp, self._host, self._started, self._col = resp, host, started, col
        self._closed = False

    def read(self, *args):
        data = self._resp.read(*args)
        if data:
            self._col.add_bytes(self._host, len(data))
        return data

    def _finish(self) -> None:
        if not self._closed:
            self._closed = True
            self._col.add_seconds(self._host, time.perf_counter() - self._started)

    def close(self):
        self._finish()
        return self._resp.close()

    def __enter__(self):
        self._resp.__enter__()
        return self

    def __exit__(self, *exc):
        self._finish()
        return self._resp.__exit__(*exc)

    def __iter__(self):
        return iter(self._resp)

    def __getattr__(self, name):
        return getattr(self._resp, name)


def urlopen(url, *args, **kwargs):
    """urllib.request.urlopen, counted. Raises exactly what it raises."""
    target = url.full_url if isinstance(url, urllib.request.Request) else str(url)
    host = host_of(target)
    started = time.perf_counter()
    try:
        resp = urllib.request.urlopen(url, *args, **kwargs)
    except urllib.error.HTTPError as e:
        collector.record(host, status=e.code, seconds=time.perf_counter() - started)
        raise
    except (urllib.error.URLError, OSError, ValueError):
        collector.record(host, error=True, seconds=time.perf_counter() - started)
        raise
    elapsed = time.perf_counter() - started
    collector.record(host, status=getattr(resp, 'status', None) or resp.getcode(), seconds=0.0)
    collector.add_seconds(host, elapsed)
    return _CountingResponse(resp, host, time.perf_counter(), collector)


class _ProcessResult:
    exit_code: int | None = None


@contextlib.contextmanager
def timed_process(url_or_host: str, kind: str, rate_limit_exit: int | None = None):
    """Time a separate network tool (mega-get, yt-dlp, ffmpeg, ffprobe) working on *url_or_host*.

        with timed_process(url, 'mega-get', rate_limit_exit=6) as run:
            result = subprocess.run(...)
            run.exit_code = result.returncode

    Records one request against the host whose status is the exit code (0 = ok), and the time it ran. An exception leaves
    exit_code None and counts as an error."""
    host = host_of(url_or_host)
    result = _ProcessResult()
    started = time.perf_counter()
    try:
        yield result
    finally:
        elapsed = time.perf_counter() - started
        collector.record(host, status=result.exit_code, seconds=elapsed, kind='process',
                         error=result.exit_code is None,
                         rate_limit_exit=rate_limit_exit is not None and result.exit_code == rate_limit_exit)

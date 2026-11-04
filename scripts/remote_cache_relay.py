"""A local relay that lets ffmpeg/ffprobe read a remote MP4 without fetching its index (moov box) over and over.

Every ffmpeg or ffprobe run on a remote MP4 starts by reading the WHOLE index, which grows with the video's length
(~86 KB per minute: ~5 MB for an hour, ~15 MB for three hours). A pre-check that runs one ffmpeg for the audio
fingerprint, another for each sampled frame and another for the quality probe would therefore fetch it again and again,
from a CDN that throttles to ~100-150 KB/s and may rate limit the address. Pointing those runs at this relay instead
(a tiny HTTP server on 127.0.0.1) means:

* the index is fetched from the remote server ONCE, in a single request, when the relay starts;
* every later run reads the index from memory, and only the sample data it really needs is requested remotely;
* that sample data is fetched in small aligned blocks and remembered (bounded LRU), so reading the same part again costs
  no request, and reads are never extended beyond the blocks actually asked for.

Only 127.0.0.1 is bound, ranged GET/HEAD are served exactly as a normal file server would (206 + Content-Range), and any
remote failure ends the local connection so the ffmpeg run fails the way it would have against the real server.
"""
import collections
import http.server
import re
import secrets
import socketserver
import threading
import time
import urllib.error
import urllib.request

import net_stats

BLOCK = 128 * 1024            # remote data is fetched and remembered in blocks of this size
SERVE_CHUNK = 1024 * 1024     # a response is produced piece by piece, so a client that stops early costs nothing more
POLL_INTERVAL = 0.02          # serve_forever's tick: shutdown() waits for one, and the default (0.5 s) made every close slow
MAX_CACHED_BLOCKS = 512       # 64 MB of remembered sample data; the index is kept separately and never evicted
FILL_MIN_RUN_BLOCKS = 8        # a fill request is never shrunk below this (1 MB)
FILL_MAX_FAILURES = 3          # requests in a row that deliver nothing before the fill gives up
INDEX_LIMIT = 96 * 1024 * 1024  # an index bigger than this is not worth holding in memory: no relay


class RemoteError(Exception):
    pass


def default_fetch(url: str, headers: dict, start: int, end: int, timeout: float = 60) -> bytes:
    """Bytes start..end (inclusive) of *url* through one ranged request."""
    req = urllib.request.Request(url, headers={**headers, 'Range': f'bytes={start}-{end}'})
    want = end - start + 1
    try:
        with net_stats.urlopen(req, timeout=timeout) as resp:
            if resp.status != 206 and not (resp.status == 200 and start == 0):
                raise RemoteError(f'the server ignored the Range header (HTTP {resp.status})')
            data = bytearray()
            while len(data) < want:
                piece = resp.read(min(1024 * 1024, want - len(data)))
                if not piece:
                    break
                data += piece
    except (urllib.error.URLError, OSError) as e:
        raise RemoteError(f'{type(e).__name__}: {e}') from e
    if len(data) != want:
        raise RemoteError(f'short read: wanted {want} bytes, got {len(data)}')
    return bytes(data)


def default_stream(url: str, headers: dict, start: int, end: int, timeout: float = 60):
    """Bytes start..end (inclusive) of *url* through ONE ranged request, yielded in BLOCK-sized pieces as they arrive
    (the last one may be shorter). *start* must be a multiple of BLOCK. Closing the generator closes the connection."""
    req = urllib.request.Request(url, headers={**headers, 'Range': f'bytes={start}-{end}'})
    want = end - start + 1
    got = 0
    try:
        with net_stats.urlopen(req, timeout=timeout) as resp:
            if resp.status != 206 and not (resp.status == 200 and start == 0):
                raise RemoteError(f'the server ignored the Range header (HTTP {resp.status})')
            while got < want:
                piece = bytearray()
                while len(piece) < min(BLOCK, want - got):
                    part = resp.read(min(BLOCK, want - got) - len(piece))
                    if not part:
                        raise RemoteError(f'short read: wanted {want} bytes, got {got + len(piece)}')
                    piece += part
                got += len(piece)
                yield bytes(piece)
    except (urllib.error.URLError, OSError) as e:
        raise RemoteError(f'{type(e).__name__}: {e}') from e


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class CacheRelay:
    def __init__(self, url: str, headers: dict, content_length: int, index_span: tuple[int, int] | None = None,
                 content_type: str = 'video/mp4', fetch=default_fetch, filename: str = 'relay.mp4',
                 max_blocks: int | None = MAX_CACHED_BLOCKS, stream=default_stream,
                 wait_ahead_mb: float = 2, stall_seconds: float = 15, retry_pause: float = 1.0):
        self.url, self.headers = url, dict(headers)
        self.content_length = content_length
        self.index_span = index_span                  # (offset, size) of the moov box, or None
        self.content_type = content_type
        self.filename = filename                      # the last part of the local URL: ffmpeg takes a hint from its extension
        self._fetch = fetch
        self._stream = stream
        self.wait_ahead_blocks = int(wait_ahead_mb * 1024 * 1024) // BLOCK   # 0 = never wait for the fill
        self.stall_seconds = stall_seconds
        self.retry_pause = retry_pause                # between a failed fill request and the next one
        self.max_blocks = max_blocks                  # remembered sample blocks kept; None = no limit
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)  # signalled whenever the fill stores a block or ends
        self._index: bytes | None = None
        self._blocks: collections.OrderedDict[int, bytes] = collections.OrderedDict()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._fill_thread: threading.Thread | None = None
        self._fill_cancel = threading.Event()
        self._fill_running = False
        self._fill_next = 0              # the next block the fill will store ...
        self._fill_run_last = -1         # ... and the last block of the request it is reading
        self.last_progress = time.monotonic()
        self.fill_requests = 0
        self.fill_bytes = 0
        self.fill_error = ''
        self.fill_continuations = 0      # requests that carried on after a stream was cut or refused
        self.fill_run_cap: int | None = None   # blocks per request once the server has shown it will not do longer ones
        self.remote_requests = 0
        self.remote_bytes = 0
        self.index_bytes = 0
        self.local_url = ''
        self._token = secrets.token_urlsafe(12)       # the relay serves your logged-in session: only whoever got the URL may read

    # -- remote side ------------------------------------------------------------------------------------------
    def _remote(self, start: int, end: int) -> bytes:
        data = self._fetch(self.url, self.headers, start, end)
        self.remote_requests += 1
        self.remote_bytes += len(data)
        return data

    def _remote_without_index(self, start: int, end: int) -> bytes:
        """Bytes start..end (inclusive) where any part inside the already held index comes from memory, not the server."""
        if not self.index_span or self._index is None:
            return self._remote(start, end)
        i_start, i_size = self.index_span
        i_end = i_start + i_size - 1
        if end < i_start or start > i_end:
            return self._remote(start, end)
        out = bytearray()
        if start < i_start:
            out += self._remote(start, i_start - 1)
        out += self._index[max(start, i_start) - i_start:min(end, i_end) - i_start + 1]
        if end > i_end:
            out += self._remote(i_end + 1, end)
        return bytes(out)

    def _ensure_index(self) -> bytes:
        if self._index is None:
            offset, size = self.index_span
            self._index = self._remote(offset, offset + size - 1)
            self.index_bytes = len(self._index)
        return self._index

    def _read_blocks(self, start: int, end: int) -> bytes:
        first, last = start // BLOCK, end // BLOCK
        self._wait_for_fill(first, last)
        missing = [b for b in range(first, last + 1) if b not in self._blocks]
        i = 0
        while i < len(missing):                       # one request per run of neighbouring missing blocks
            j = i
            while j + 1 < len(missing) and missing[j + 1] == missing[j] + 1:
                j += 1
            run_start = missing[i] * BLOCK
            run_end = min((missing[j] + 1) * BLOCK - 1, self.content_length - 1)
            data = self._remote_without_index(run_start, run_end)
            for n, b in enumerate(range(missing[i], missing[j] + 1)):
                self._blocks[b] = data[n * BLOCK:(n + 1) * BLOCK]
            i = j + 1
        out = bytearray()
        for b in range(first, last + 1):
            self._blocks.move_to_end(b)
            out += self._blocks[b]
        while self.max_blocks is not None and len(self._blocks) > self.max_blocks:
            self._blocks.popitem(last=False)
        offset = start - first * BLOCK
        return bytes(out[offset:offset + (end - start + 1)])

    # -- background fill --------------------------------------------------------------------------------------
    def _fill_active(self) -> bool:
        return self._fill_running and not self._fill_cancel.is_set()

    def _wait_for_fill(self, first: int, last: int) -> None:
        """Blocks the fill is about to deliver (within wait_ahead of where it is, in its current request) are waited
        for rather than requested again. The wait is a STALL timeout: it ends only when the fill has delivered no
        data at all for stall_seconds (each arriving block restarts the clock), however long the wait has been."""
        if self.wait_ahead_blocks <= 0:
            return
        for b in range(first, last + 1):
            while b not in self._blocks and self._fill_active() and \
                    self._fill_next <= b <= self._fill_run_last and b - self._fill_next < self.wait_ahead_blocks:
                if time.monotonic() - self.last_progress > self.stall_seconds:
                    return
                self._cond.wait(timeout=min(0.2, self.stall_seconds))

    def start_fill(self, priority_ranges: list[tuple[int, int]]) -> None:
        """Start filling the whole file into memory in the background: first the byte ranges in *priority_ranges*
        (start, end inclusive; in that order), then every remaining gap from the start of the file onwards. Each run of
        missing blocks is ONE streaming request. Nothing is evicted while filling."""
        if self._fill_thread is not None or self.content_length <= 0:
            return
        last_block = (self.content_length - 1) // BLOCK
        plan = [(max(0, a) // BLOCK, min(b, self.content_length - 1) // BLOCK) for a, b in priority_ranges if b >= a]
        plan.append((0, last_block))
        self.max_blocks = None
        self._fill_running = True
        self.last_progress = time.monotonic()
        self._fill_thread = threading.Thread(target=self._fill_main, args=(plan,), daemon=True, name='cache-relay-fill')
        self._fill_thread.start()

    def _fill_main(self, plan: list[tuple[int, int]]) -> None:
        try:
            for first, last in plan:
                if self._fill_cancel.is_set():
                    return
                self._fill_range(first, last)
        except (RemoteError, OSError) as e:
            self.fill_error = str(e)
        finally:
            with self._cond:
                self._fill_running = False
                self._cond.notify_all()

    def _fill_range(self, first: int, last: int) -> None:
        """Fill every missing block in first..last. A request that is cut short or refused is not the end: the blocks it
        delivered are kept and the next request carries on from the first missing one, at the length the server has shown
        it will serve (cut after N blocks -> N from then on; refused outright -> half as many, down to a floor). The fill
        only gives up after FILL_MAX_FAILURES requests in a row that deliver nothing."""
        b = first
        failures = 0
        while b <= last and not self._fill_cancel.is_set():
            with self._lock:
                while b <= last and b in self._blocks:
                    b += 1
                if b > last:
                    return
                run_last = b
                while run_last + 1 <= last and run_last + 1 not in self._blocks and \
                        (self.fill_run_cap is None or run_last + 1 - b < self.fill_run_cap):
                    run_last += 1
                self._fill_next, self._fill_run_last = b, run_last
                self.last_progress = time.monotonic()
                self.fill_requests += 1
            end = min((run_last + 1) * BLOCK - 1, self.content_length - 1)
            n = b
            error = ''
            pieces = self._stream(self.url, self.headers, b * BLOCK, end)
            try:
                for piece in pieces:
                    if self._fill_cancel.is_set():
                        return
                    with self._cond:
                        if n not in self._blocks:              # an on-demand read may have stored it already
                            self._blocks[n] = piece
                        self.fill_bytes += len(piece)
                        n += 1
                        self._fill_next = n
                        self.last_progress = time.monotonic()
                        self._cond.notify_all()
            except (RemoteError, OSError) as e:
                error = str(e) or type(e).__name__
            finally:
                close = getattr(pieces, 'close', None)
                if close:
                    close()
            if n == run_last + 1 and not error:
                failures = 0
                b = run_last + 1
                continue
            # cut short, refused or ended early
            error = error or f'the stream ended after {n - b} of {run_last + 1 - b} blocks'
            self.fill_continuations += 1
            if n > b:
                failures = 0
                self.fill_run_cap = max(FILL_MIN_RUN_BLOCKS, n - b)      # the length the server was willing to serve
            else:
                failures += 1
                if failures >= FILL_MAX_FAILURES:
                    raise RemoteError(error)
                self.fill_run_cap = max(FILL_MIN_RUN_BLOCKS, (run_last + 1 - b) // 2)
            if self._fill_cancel.wait(self.retry_pause):
                return
            b = n

    def _block_from_index(self, b: int) -> bytes | None:
        if not self.index_span or self._index is None:
            return None
        i_start, i_size = self.index_span
        lo, hi = b * BLOCK, min((b + 1) * BLOCK, self.content_length)
        if lo >= i_start and hi <= i_start + i_size:
            return self._index[lo - i_start:hi - i_start]
        return None

    def iter_blocks(self):
        """Every block of the file in order, as the fill delivers them; each is dropped from memory once handed over.
        Raises RemoteError when the fill is over (or stalls) before a block arrives."""
        for b in range((self.content_length + BLOCK - 1) // BLOCK):
            with self._cond:
                while True:
                    if b in self._blocks:
                        data = self._blocks.pop(b)
                        break
                    data = self._block_from_index(b)
                    if data is not None:
                        break
                    if not self._fill_active():
                        raise RemoteError(self.fill_error or f'the background fill did not deliver block {b}')
                    if time.monotonic() - self.last_progress > self.stall_seconds:
                        raise RemoteError(f'no data for {self.stall_seconds:g} s while waiting for block {b}')
                    self._cond.wait(timeout=min(0.2, self.stall_seconds))
            yield data

    def read(self, start: int, end: int) -> bytes:
        """Bytes start..end (inclusive) of the remote file, from memory where already held."""
        end = min(end, self.content_length - 1)
        with self._lock:
            out = bytearray()
            pos = start
            while pos <= end:
                if self.index_span:
                    i_start, i_size = self.index_span
                    i_end = i_start + i_size - 1
                    if i_start <= pos <= i_end:
                        take = min(end, i_end)
                        out += self._ensure_index()[pos - i_start:take - i_start + 1]
                        pos = take + 1
                        continue
                    seg_end = min(end, i_start - 1) if pos < i_start else end
                else:
                    seg_end = end
                out += self._read_blocks(pos, seg_end)
                pos = seg_end + 1
            return bytes(out)

    # -- local side -------------------------------------------------------------------------------------------
    def start(self) -> str | None:
        """Start serving. With a known index it is fetched once first; without one (any other container) the last block
        of the file is read instead, which proves the server can do ranged reads and is usually wanted anyway (the
        index of an MKV or AVI sits at the end), and is kept. Returns the local URL, or None when no relay can be
        used (no size, an index too large to hold, or the first remote read failed)."""
        if self.content_length <= 0:
            return None
        if self.index_span and not (0 < self.index_span[1] <= INDEX_LIMIT):
            return None
        try:
            with self._lock:
                if self.index_span:
                    self._ensure_index()
                else:
                    self._read_blocks(max(0, self.content_length - BLOCK), self.content_length - 1)
        except (RemoteError, OSError):
            return None
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def _span(self):
                total = relay.content_length
                m = re.fullmatch(r'bytes=(\d*)-(\d*)', (self.headers.get('Range') or '').strip())
                if not m or (m.group(1) == '' and m.group(2) == ''):
                    return 0, total - 1, False
                a, b = m.group(1), m.group(2)
                if a == '':
                    return max(0, total - int(b)), total - 1, True
                start = int(a)
                end = total - 1 if b == '' else min(int(b), total - 1)
                return start, end, True

            def _respond(self, head_only):
                if not self.path.startswith(f'/{relay._token}/'):
                    self.send_response(404)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                start, end, ranged = self._span()
                if start > end or start >= relay.content_length:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{relay.content_length}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                self.send_response(206 if ranged else 200)
                self.send_header('Content-Type', relay.content_type)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(end - start + 1))
                if ranged:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{relay.content_length}')
                self.end_headers()
                if head_only:
                    return
                pos = start
                while pos <= end:
                    stop = min(end, pos + SERVE_CHUNK - 1)
                    try:
                        data = relay.read(pos, stop)
                    except (RemoteError, OSError):
                        self.close_connection = True      # the client sees a short read, as with a failing server
                        return
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        self.close_connection = True
                        return
                    pos = stop + 1

            def do_GET(self):
                self._respond(False)

            def do_HEAD(self):
                self._respond(True)

        self._server = _Server(('127.0.0.1', 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={'poll_interval': POLL_INTERVAL},
                                        daemon=True, name='cache-relay')
        self._thread.start()
        self.local_url = f'http://127.0.0.1:{self._server.server_address[1]}/{self._token}/{self.filename}'
        return self.local_url

    def close(self) -> None:
        self._fill_cancel.set()
        with self._cond:
            self._cond.notify_all()
        if self._fill_thread is not None:
            self._fill_thread.join(timeout=5)
            self._fill_thread = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._blocks.clear()
        self._index = None

    def summary(self) -> str:
        text = (f'{self.remote_requests} remote request(s), {self.remote_bytes / 1048576:.1f} MB '
                f'(index {self.index_bytes / 1048576:.1f} MB fetched once)')
        if self.fill_requests:
            text += f'; background fill: {self.fill_requests} request(s), {self.fill_bytes / 1048576:.1f} MB'
            if self.fill_continuations:
                text += f' ({self.fill_continuations} carried on after a cut or refused stream)'
        return text

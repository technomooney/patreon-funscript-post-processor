"""The relay fetches a remote MP4's index once and serves it, plus remembered sample blocks, to ffmpeg/ffprobe."""
import http.client
import http.server
import os
import random
import shutil
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import remote_cache_relay as rcr

MB = 1024 * 1024


class FakeOrigin:
    """Stands in for the CDN: serves ranges of a bytes object and counts the requests it gets."""
    def __init__(self, data: bytes):
        self.data = data
        self.calls: list[tuple[int, int]] = []
        self.fail = False

    def fetch(self, url, headers, start, end, timeout=60):
        self.calls.append((start, end))
        if self.fail:
            raise rcr.RemoteError('boom')
        return self.data[start:end + 1]


def make_relay(size=3 * MB, index_span=(1 * MB, 600 * 1024), origin=None):
    origin = origin or FakeOrigin(random.Random(7).randbytes(size))
    relay = rcr.CacheRelay('https://cdn.example/x.mp4', {'Referer': 'r'}, len(origin.data), index_span, fetch=origin.fetch)
    return relay, origin


class CacheLogicTests(unittest.TestCase):
    def test_reads_return_exactly_the_remote_bytes_across_the_index_boundaries(self):
        relay, origin = make_relay()
        relay.start()
        self.addCleanup(relay.close)
        for a, b in ((0, 99), (1 * MB - 50, 1 * MB + 50), (1 * MB, 1 * MB + 600 * 1024 - 1), (1 * MB + 600 * 1024 - 10, 1 * MB + 600 * 1024 + 10),
                     (0, 3 * MB - 1), (2 * MB + 5, 2 * MB + 5)):
            self.assertEqual(relay.read(a, b), origin.data[a:b + 1], (a, b))

    def test_the_index_is_fetched_once_however_many_times_it_is_read(self):
        relay, origin = make_relay()
        relay.start()
        self.addCleanup(relay.close)
        for _ in range(6):
            relay.read(1 * MB, 1 * MB + 600 * 1024 - 1)
            relay.read(1 * MB + 100, 1 * MB + 5000)
        self.assertEqual(origin.calls.count((1 * MB, 1 * MB + 600 * 1024 - 1)), 1)
        self.assertEqual(len(origin.calls), 1)                     # nothing else was asked for
        self.assertEqual(relay.index_bytes, 600 * 1024)

    def test_a_block_that_overlaps_the_index_does_not_fetch_the_index_again(self):
        relay, origin = make_relay(index_span=(1 * MB + 5000, 300 * 1024))
        relay.start()
        self.addCleanup(relay.close)
        before = len(origin.calls)
        got = relay.read(1 * MB - 100, 1 * MB + 20000)                 # blocks on both sides of the index start
        self.assertEqual(got, origin.data[1 * MB - 100:1 * MB + 20001])
        i_start, i_end = 1 * MB + 5000, 1 * MB + 5000 + 300 * 1024 - 1
        for a, b in origin.calls[before:]:
            self.assertTrue(b < i_start or a > i_end, (a, b))           # nothing asked for lies inside the index
        got = relay.read(1 * MB + 5000 + 300 * 1024 - 50, 1 * MB + 5000 + 300 * 1024 + 50)
        self.assertEqual(got, origin.data[i_end - 49:i_end + 52])
        for a, b in origin.calls[before:]:
            self.assertTrue(b < i_start or a > i_end, (a, b))

    def test_data_blocks_are_remembered(self):
        relay, origin = make_relay()
        relay.start()
        self.addCleanup(relay.close)
        relay.read(2 * MB, 2 * MB + 1000)
        n = len(origin.calls)
        relay.read(2 * MB + 10, 2 * MB + 900)
        relay.read(2 * MB, 2 * MB + 1000)
        self.assertEqual(len(origin.calls), n)

    def test_only_the_blocks_asked_for_are_fetched_and_neighbours_share_a_request(self):
        relay, origin = make_relay()
        relay.start()
        self.addCleanup(relay.close)
        before = len(origin.calls)
        relay.read(2 * MB + 10, 2 * MB + 3 * rcr.BLOCK - 10)       # touches 3 neighbouring blocks
        new = origin.calls[before:]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0][1] - new[0][0] + 1, 3 * rcr.BLOCK)

    def test_the_cache_is_bounded_but_the_index_stays(self):
        relay, origin = make_relay(size=100 * MB, index_span=(0, 400 * 1024))
        relay.start()
        self.addCleanup(relay.close)
        for i in range(rcr.MAX_CACHED_BLOCKS + 50):
            relay.read(10 * MB + i * rcr.BLOCK, 10 * MB + i * rcr.BLOCK)
        self.assertLessEqual(len(relay._blocks), rcr.MAX_CACHED_BLOCKS)
        index_calls = [c for c in origin.calls if c[0] == 0 and c[1] == 400 * 1024 - 1]
        relay.read(0, 100)
        self.assertEqual(len([c for c in origin.calls if c[0] == 0 and c[1] == 400 * 1024 - 1]), len(index_calls))

    def test_no_relay_without_a_known_index_or_when_it_is_too_big_or_unfetchable(self):
        data = random.Random(1).randbytes(MB)
        for span in (None, (0, 0), (0, rcr.INDEX_LIMIT + 1)):
            relay = rcr.CacheRelay('u', {}, len(data), span, fetch=FakeOrigin(data).fetch)
            self.assertIsNone(relay.start())
        origin = FakeOrigin(data)
        origin.fail = True
        self.assertIsNone(rcr.CacheRelay('u', {}, len(data), (0, 1000), fetch=origin.fetch).start())


class HttpLayerTests(unittest.TestCase):
    def setUp(self):
        self.relay, self.origin = make_relay()
        self.url = self.relay.start()
        self.addCleanup(self.relay.close)
        self.port = int(self.url.split(':')[2].split('/')[0])
        self.path = '/' + self.url.split('/', 3)[3]

    def request(self, method='GET', rng=None, path=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request(method, path or self.path, headers={'Range': rng} if rng else {})
        resp = conn.getresponse()
        body = resp.read() if method == 'GET' else b''
        conn.close()
        return resp, body

    def test_ranged_get(self):
        resp, body = self.request(rng='bytes=100-299')
        self.assertEqual((resp.status, resp.getheader('Content-Range'), resp.getheader('Accept-Ranges')),
                         (206, f'bytes 100-299/{3 * MB}', 'bytes'))
        self.assertEqual(body, self.origin.data[100:300])

    def test_open_ended_and_suffix_ranges(self):
        resp, body = self.request(rng=f'bytes={3 * MB - 1000}-')
        self.assertEqual(body, self.origin.data[-1000:])
        resp, body = self.request(rng='bytes=-500')
        self.assertEqual((resp.status, body), (206, self.origin.data[-500:]))

    def test_plain_get_head_and_bad_range(self):
        resp, body = self.request()
        self.assertEqual((resp.status, len(body)), (200, 3 * MB))
        self.assertEqual(body, self.origin.data)
        resp, _ = self.request('HEAD')
        self.assertEqual((resp.status, resp.getheader('Content-Length')), (200, str(3 * MB)))
        resp, _ = self.request(rng=f'bytes={10 * MB}-')
        self.assertEqual(resp.status, 416)

    def test_a_remote_failure_gives_the_client_a_short_read_not_wrong_data(self):
        self.origin.fail = True
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('GET', self.path, headers={'Range': 'bytes=2000000-2001000'})
        resp = conn.getresponse()
        with self.assertRaises(http.client.IncompleteRead):
            resp.read()
        conn.close()

    def test_a_request_without_the_secret_in_the_url_is_refused(self):
        for path in ('/relay.mp4', '/wrong-token/relay.mp4', '/'):
            resp, body = self.request(path=path)
            self.assertEqual((resp.status, body), (404, b''), path)
        resp, _ = self.request(rng='bytes=0-9')
        self.assertEqual(resp.status, 206)

    def test_each_relay_has_its_own_secret(self):
        other, _ = make_relay()
        other_url = other.start()
        self.addCleanup(other.close)
        self.assertNotEqual(self.url.split('/')[3], other_url.split('/')[3])

    def test_close_is_quick(self):
        import time
        t = time.perf_counter()
        self.relay.close()
        self.assertLess(time.perf_counter() - t, 0.3)

    def test_close_frees_the_port(self):
        self.relay.close()
        with self.assertRaises(OSError):
            http.client.HTTPConnection('127.0.0.1', self.port, timeout=2).request('GET', '/')


class DefaultFetchTests(unittest.TestCase):
    def serve(self, data, honour_range=True):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get('Range')
                outer.seen_headers = dict(self.headers)
                if rng and honour_range:
                    a, b = rng.split('=')[1].split('-')
                    body = data[int(a):int(b) + 1]
                    self.send_response(206)
                else:
                    body = data
                    self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = socketserver.TCPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f'http://127.0.0.1:{srv.server_address[1]}/f.mp4'

    def test_fetches_a_range_with_the_given_headers(self):
        data = random.Random(2).randbytes(10000)
        url = self.serve(data)
        self.assertEqual(rcr.default_fetch(url, {'Referer': 'https://x/'}, 100, 599), data[100:600])
        self.assertEqual(self.seen_headers['Referer'], 'https://x/')

    def test_a_server_that_ignores_range_is_an_error_not_wrong_data(self):
        url = self.serve(random.Random(2).randbytes(10000), honour_range=False)
        with self.assertRaises(rcr.RemoteError):
            rcr.default_fetch(url, {}, 5000, 5999)
        self.assertEqual(len(rcr.default_fetch(url, {}, 0, 99)), 100)        # a 200 at offset 0 is fine

    def test_connection_failure_is_a_remote_error(self):
        with self.assertRaises(rcr.RemoteError):
            rcr.default_fetch('http://127.0.0.1:1/x.mp4', {}, 0, 9, timeout=2)


def mp4_layout(data: bytes):
    pos, boxes = 0, {}
    while pos + 8 <= len(data):
        size, kind = struct.unpack('>I4s', data[pos:pos + 8])
        if size == 1:
            size = struct.unpack('>Q', data[pos + 8:pos + 16])[0]
        boxes[kind.decode('latin1')] = (pos, size)
        pos += size
    return boxes


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs ffmpeg and ffprobe')
class RealFfmpegThroughRelayTests(unittest.TestCase):
    def make_mp4(self, tmp, name, faststart):
        path = os.path.join(tmp, name)
        cmd = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=duration=40:size=320x240:rate=15',
               '-f', 'lavfi', '-i', 'sine=frequency=440:duration=40', '-c:v', 'mpeg4', '-q:v', '5', '-c:a', 'aac',
               *(['-movflags', '+faststart'] if faststart else []), '-y', path]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            self.skipTest('ffmpeg could not make a test file')
        return path

    def run_case(self, faststart):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_mp4(tmp, 'v.mp4', faststart)
            with open(path, 'rb') as fh:
                data = fh.read()
            layout = mp4_layout(data)
            at_start = layout['moov'][0] < layout['mdat'][0]
            self.assertEqual(at_start, faststart)
            origin = FakeOrigin(data)
            relay = rcr.CacheRelay('https://cdn.example/v.mp4', {}, len(data), layout['moov'], fetch=origin.fetch)
            url = relay.start()
            self.addCleanup(relay.close)
            self.assertEqual(len(origin.calls), 1)                              # the index, once
            direct = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', path],
                                    capture_output=True, text=True).stdout.strip()
            durations = []
            for _ in range(3):
                r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', url],
                                   capture_output=True, text=True, timeout=60)
                durations.append(r.stdout.strip())
            self.assertEqual(durations, [direct] * 3)
            index_fetches = [c for c in origin.calls if c == (layout['moov'][0], layout['moov'][0] + layout['moov'][1] - 1)]
            self.assertEqual(len(index_fetches), 1)                             # three ffprobe runs, one index fetch
            frame = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '20', '-i', url, '-frames:v', '1', '-f', 'rawvideo',
                                    '-pix_fmt', 'gray', '-vf', 'scale=32:32', 'pipe:1'], capture_output=True, timeout=60)
            self.assertEqual(len(frame.stdout), 32 * 32)
            local_frame = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '20', '-i', path, '-frames:v', '1', '-f', 'rawvideo',
                                          '-pix_fmt', 'gray', '-vf', 'scale=32:32', 'pipe:1'], capture_output=True, timeout=60)
            self.assertEqual(frame.stdout, local_frame.stdout)                  # the same frame as reading the file directly
            n = len(origin.calls)
            subprocess.run(['ffmpeg', '-v', 'error', '-ss', '20', '-i', url, '-frames:v', '1', '-f', 'null', '-'], capture_output=True, timeout=60)
            self.assertEqual([c for c in origin.calls[n:] if c == layout['moov'][:1] + (0,)], [])
            self.assertEqual(len([c for c in origin.calls if c[1] - c[0] + 1 == layout['moov'][1]]), 1)

    def test_index_at_the_start(self):
        self.run_case(faststart=True)

    def test_index_at_the_end(self):
        self.run_case(faststart=False)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs ffmpeg and ffprobe')
class PrecheckEndToEndTests(unittest.TestCase):
    """The whole pre-check against a real loopback 'CDN': a re-encode of a video already in the folder is recognised
    while the remote index is requested exactly once."""
    def make(self, path, quality, faststart):
        cmd = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=duration=40:size=320x240:rate=15',
               '-f', 'lavfi', '-i', 'sine=frequency=440:duration=40', '-c:v', 'mpeg4', '-q:v', str(quality), '-c:a', 'aac',
               *(['-movflags', '+faststart'] if faststart else []), '-y', path]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            self.skipTest('ffmpeg could not make a test file')

    def origin(self, path):
        with open(path, 'rb') as fh:
            data = fh.read()
        outer = self
        outer.requests = []

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Type', 'video/mp4')
                self.end_headers()

            def do_GET(self):
                rng = self.headers.get('Range')
                if rng:
                    a, b = rng.split('=')[1].split('-')
                    a = int(a)
                    b = min(int(b), len(data) - 1) if b else len(data) - 1
                    outer.requests.append((a, b))
                    body = data[a:b + 1]
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes {a}-{b}/{len(data)}')
                else:
                    outer.requests.append((0, len(data) - 1))
                    body = data
                    self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Type', 'video/mp4')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f'http://127.0.0.1:{srv.server_address[1]}/get/clip_1080p.mp4', data

    def run_case(self, faststart, audio):
        import downloadContent as dc
        from unittest import mock
        with tempfile.TemporaryDirectory() as lib, tempfile.TemporaryDirectory() as remote_dir:
            self.make(os.path.join(lib, 'Original Name.mp4'), 8, faststart)      # what is already in the folder
            remote = os.path.join(remote_dir, 'clip.mp4')
            self.make(remote, 3, faststart)                                       # same content, other bytes, bigger
            url, data = self.origin(remote)
            span = mp4_layout(data)['moov']
            patches = [mock.patch.object(dc, '_VISUAL_PRECHECK_MIN_BYTES', 0)]
            if not audio:
                patches.append(mock.patch.object(dc, '_remote_audio_fingerprint', return_value=None))
            import contextlib, io
            out = io.StringIO()
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                stack.enter_context(contextlib.redirect_stdout(out))
                result = dc._precheck_url(url, {}, lib)
            text = out.getvalue()
            self.assertIsNotNone(result, text)
            self.assertIn('Original Name.mp4', result)
            if audio:        # the audio stage may be unavailable (no fpcalc); the visual one then settles it
                self.assertTrue('audio fingerprint match' in result or 'visually similar' in result, result)
            else:
                self.assertIn('visually similar', result)
            index_range = (span[0], span[0] + span[1] - 1)
            full_index_reads = [r for r in self.requests if r[0] <= index_range[0] and r[1] >= index_range[1]
                                and (r[1] - r[0] + 1) < len(data)]
            self.assertEqual(len(full_index_reads), 1, f'index reads: {full_index_reads}\n{text}')
            self.assertIn('reading the index once', text)
            self.assertIn('fetched once', text)
            return text

    def test_audio_match_with_the_index_at_the_end(self):
        self.run_case(faststart=False, audio=True)

    def test_visual_match_with_the_index_at_the_start(self):
        text = self.run_case(faststart=True, audio=False)
        self.assertEqual(text.count('match ('), 6)          # all the sampled frames of a 40 s clip compared, none re-read the index

    def test_visual_match_with_the_index_at_the_end(self):
        self.run_case(faststart=False, audio=False)


if __name__ == '__main__':
    unittest.main()

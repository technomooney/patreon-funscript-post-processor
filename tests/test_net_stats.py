"""net_stats counts requests, bytes, time and answers per host and per link, without changing what urlopen does."""
import http.server
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import net_stats as ns


class Origin(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        code = {'/ok': 200, '/missing': 404, '/limited': 429, '/forbidden': 403, '/broken': 503}.get(self.path, 200)
        if self.path == '/slow':
            time.sleep(0.2)
        body = b'x' * 5000 if code == 200 else b'no'
        self.send_response(code)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NetStatsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.srv.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        ns.collector.reset()

    def host(self):
        return ns.collector.hosts['127.0.0.1']

    def test_host_of_handles_urls_and_bare_hosts(self):
        self.assertEqual(ns.host_of('https://www.Mega.nz/file/x'), 'mega.nz')
        self.assertEqual(ns.host_of('mega.nz'), 'mega.nz')
        self.assertEqual(ns.host_of('http://127.0.0.1:8080/x'), '127.0.0.1')

    def test_a_successful_request_counts_status_bytes_and_time(self):
        with ns.urlopen(self.base + '/ok') as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(len(resp.read()), 5000)
        h = self.host()
        self.assertEqual((h.requests, h.ok, h.bytes), (1, 1, 5000))
        self.assertGreater(h.seconds, 0)

    def test_it_behaves_like_urlopen_for_the_callers(self):
        req = urllib.request.Request(self.base + '/ok', headers={'X-A': 'b'})
        with ns.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.getcode(), 200)
            self.assertIn('Content-Length', resp.headers)
            self.assertTrue(resp.geturl().endswith('/ok'))
            chunks = []
            while True:
                part = resp.read(1000)
                if not part:
                    break
                chunks.append(part)
        self.assertEqual(b''.join(chunks), b'x' * 5000)
        self.assertEqual(self.host().bytes, 5000)

    def test_error_statuses_are_counted_and_still_raised(self):
        for path, attr in (('/missing', 'client_errors'), ('/forbidden', 'forbidden'), ('/limited', 'rate_limited'),
                           ('/broken', 'server_errors')):
            with self.assertRaises(urllib.error.HTTPError):
                ns.urlopen(self.base + path)
            self.assertGreaterEqual(getattr(self.host(), attr), 1, path)
        h = self.host()
        self.assertEqual((h.requests, h.ok, h.client_errors, h.forbidden, h.rate_limited, h.server_errors),
                         (4, 0, 3, 1, 1, 1))          # 403 and 429 are also 4xx

    def test_no_answer_at_all_counts_as_an_error(self):
        with self.assertRaises(urllib.error.URLError):
            ns.urlopen('http://127.0.0.1:1/x', timeout=2)
        self.assertEqual((self.host().requests, self.host().errors), (1, 1))

    def test_slowest_and_seconds_follow_the_request_duration(self):
        with ns.urlopen(self.base + '/slow') as resp:
            resp.read()
        self.assertGreaterEqual(self.host().slowest_seconds, 0.2)

    def test_a_process_is_timed_against_its_host_with_the_exit_code_as_status(self):
        with ns.timed_process('https://mega.nz/file/x', 'mega-get', rate_limit_exit=6) as run:
            run.exit_code = 6
        with ns.timed_process('mega.nz', 'mega-get', rate_limit_exit=6) as run:
            run.exit_code = 0
        with ns.timed_process('mega.nz', 'mega-get', rate_limit_exit=6) as run:
            run.exit_code = 9
        with self.assertRaises(RuntimeError):
            with ns.timed_process('mega.nz', 'mega-get', rate_limit_exit=6):
                raise RuntimeError('boom')                # exit_code never set
        m = ns.collector.hosts['mega.nz']
        self.assertEqual((m.requests, m.ok, m.rate_limited, m.errors), (4, 1, 1, 2))

    def test_a_link_collects_its_own_cost_and_outcome(self):
        ns.collector.begin_link('/lib/a', self.base + '/one')
        with ns.urlopen(self.base + '/ok') as resp:
            resp.read()
        ns.collector.note(precheck_result='size shortcut', precheck_seconds=1.5)
        ns.collector.note(precheck_seconds=0.5, download_seconds=2.0)
        statuses = {('/lib/a', self.base + '/one'): 'downloaded'}
        ns.collector.begin_link('/lib/b', self.base + '/two', lambda f, u: statuses.get((f, u)))
        with ns.urlopen(self.base + '/ok') as resp:
            resp.read()
        ns.collector.end_link(lambda f, u: 'failed')
        first, second = ns.collector.take_finished_links()
        self.assertEqual((first.folder, first.requests, first.bytes, first.outcome, first.precheck_result),
                         ('/lib/a', 1, 5000, 'downloaded', 'size shortcut'))
        self.assertAlmostEqual(first.precheck_seconds, 2.0)
        self.assertEqual(first.download_seconds, 2.0)
        self.assertEqual((second.folder, second.requests, second.outcome), ('/lib/b', 1, 'failed'))
        self.assertEqual(ns.collector.links, [])

    def test_requests_outside_a_link_still_count_for_the_host(self):
        with ns.urlopen(self.base + '/ok') as resp:
            resp.read()
        self.assertEqual(self.host().requests, 1)
        self.assertEqual(ns.collector.take_finished_links(), [])

    def test_totals_add_up_hosts(self):
        ns.collector.record('a.com', status=200, nbytes=10, seconds=1)
        ns.collector.record('b.com', status=500, nbytes=5, seconds=3)
        t = ns.collector.totals()
        self.assertEqual((t.requests, t.bytes, t.ok, t.server_errors, t.slowest_seconds), (2, 15, 1, 1, 3))


if __name__ == '__main__':
    unittest.main()

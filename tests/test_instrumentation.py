"""The downloader's network/run statistics: what each instrumented call records, and a whole run end to end."""
import contextlib
import http.server
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import downloadContent as dc  # noqa: E402
import library_db  # noqa: E402
import mega_cooldown  # noqa: E402
import net_stats  # noqa: E402
import remote_cache_relay as rcr  # noqa: E402
import run_stats  # noqa: E402


class Origin(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = bytes(range(256)) * 1600       # 400 KB
        rng = self.headers.get('Range')
        if rng:
            a, b = rng.split('=')[1].split('-')
            part = body[int(a):int(b) + 1]
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {a}-{b}/{len(body)}')
        else:
            part = body
            self.send_response(200)
        self.send_header('Content-Length', str(len(part)))
        self.end_headers()
        self.wfile.write(part)


class CallSiteTests(unittest.TestCase):
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
        net_stats.collector.reset()

    def local(self):
        return net_stats.collector.hosts['127.0.0.1']

    def test_ranged_reads_are_counted_with_their_bytes(self):
        data = dc._remote_range_bytes(self.base + '/f.mp4', {}, 100, 500)
        self.assertEqual(len(data), 500)
        self.assertEqual((self.local().requests, self.local().ok, self.local().bytes), (1, 1, 500))

    def test_the_relays_remote_fetches_are_counted_against_the_real_host(self):
        total = 256 * 1600
        relay = rcr.CacheRelay(self.base + '/f.mp4', {}, total, (300_000, 3000))
        relay.start()
        self.addCleanup(relay.close)
        relay.read(0, 100)
        relay.read(300_500, 300_600)
        self.assertEqual(self.local().requests, 2)               # the index once, one data block
        self.assertGreaterEqual(self.local().bytes, 3000)

    def test_mega_get_runs_are_timed_with_their_exit_codes(self):
        cd = mega_cooldown.Cooldown([300, 900])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(dc, '_mega_cooldown', cd), \
                mock.patch.object(dc.shutil, 'which', return_value='/usr/bin/mega-get'), \
                mock.patch.object(dc, '_mega_ensure_login', return_value=True), \
                mock.patch.object(dc, '_set_status'), \
                contextlib.redirect_stdout(io.StringIO()):
            for code in (0, 6):
                with mock.patch.object(dc.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, '', '')):
                    cd._until = 0
                    try:
                        dc.download_mega(None, 'https://mega.nz/file/AbCdEfGh#k', tmp)
                    except dc.MegaRateLimitedError:
                        pass
        m = net_stats.collector.hosts['mega.nz']
        self.assertEqual((m.requests, m.ok, m.rate_limited), (2, 1, 1))

    def test_yt_dlp_runs_are_timed_against_the_video_host(self):
        done = subprocess.CompletedProcess([], 1, None, 'ERROR: x: HTTP Error 404: Not Found\n')
        with mock.patch.object(dc.subprocess, 'run', return_value=done), contextlib.redirect_stderr(io.StringIO()):
            dc._run_ytdlp_download(['yt-dlp', 'https://www.eporner.com/video-x/'], 'https://www.eporner.com/video-x/')
        h = net_stats.collector.hosts['eporner.com']
        self.assertEqual((h.requests, h.ok, h.errors), (1, 0, 1))

    def test_ffmpeg_on_the_local_relay_or_a_local_file_is_not_a_host(self):
        for target in ('http://127.0.0.1:5555/t/relay.mp4', 'local'):
            with net_stats.timed_process(target, 'ffmpeg') as run:
                run.exit_code = 0
        self.assertEqual(net_stats.collector.hosts, {})

    def test_precheck_categories(self):
        for reason, label in ((None, 'no match'), ('already exists: a.mp4', 'name match'),
                              ('hash match: a.mp4', 'hash match'), ('chunk match (12 samples): a.mp4', 'chunk match'),
                              ("identical to existing a.mp4 (same size, start, end and sampled blocks match)", 'size shortcut'),
                              ('audio fingerprint match: a.mp4 (60.0s, 97%)', 'audio match'),
                              ('visually similar to existing: a.mp4 (60.0s)', 'visual match'),
                              ('something else', 'other')):
            self.assertEqual(dc._precheck_category(reason), label)

    def test_direct_fetch_notes_the_download_time_without_the_precheck_time(self):
        net_stats.collector.begin_link('/f', 'https://x.example/v')

        def inner(*a, **k):
            net_stats.collector.note(precheck_seconds=0.2)
            import time
            time.sleep(0.3)
            return True
        with mock.patch.object(dc, '_direct_fetch_inner', inner):
            self.assertTrue(dc._direct_fetch('u', '/tmp', 'x', {}))
        link = net_stats.collector.end_link()
        self.assertAlmostEqual(link.precheck_seconds, 0.2, places=2)
        self.assertGreaterEqual(link.download_seconds, 0.08)
        self.assertLess(link.download_seconds, 0.2)             # the 0.2 s of pre-check is not counted as download


class WholeRunTests(unittest.TestCase):
    """find_and_download with the browser and the site handler faked: the run, its hosts and its links are recorded."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self._tmp.name)
        self._env = {k: os.environ.get(k) for k in ('LIBRARY_TOOLS_HOME', 'ACTION_LOG_POINTER_PATH')}
        os.environ['LIBRARY_TOOLS_HOME'] = os.path.join(base, 'home')
        os.environ['ACTION_LOG_POINTER_PATH'] = os.path.join(base, 'pointer.json')
        library_db._roots.clear()
        self.root = library_db.use_root(os.path.join(base, 'lib'))
        self.post = os.path.join(self.root, 'post one')
        os.makedirs(self.post)
        net_stats.collector.reset()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def run_downloader(self, handler, links):
        tasks = [{'folder': self.post, 'basename': 'Post One', 'links': links}]
        out = io.StringIO()
        with mock.patch.object(dc, 'setup_driver', return_value=mock.MagicMock()), \
                mock.patch.object(dc, '_ensure_driver_alive', side_effect=lambda d, f: d), \
                mock.patch.object(dc, 'set_download_dir'), \
                mock.patch.object(dc, 'check_domain', return_value='example.com'), \
                mock.patch.dict(dc.DOMAIN_HANDLERS, {'example.com': handler}), \
                mock.patch.object(dc, 'get_domain', return_value='example.com'), \
                mock.patch.object(dc.fetch_diagnostics, 'start_capture', return_value=lambda: None), \
                contextlib.redirect_stdout(out):
            dc.find_and_download(self.root, tasks=tasks, failures=[], resume=False, auto_confirm=True)
        return out.getvalue()

    def test_a_failing_link_is_recorded_as_a_completed_run_with_its_cost(self):
        def handler(driver, url, folder):
            net_stats.collector.record('example.com', status=404, seconds=0.4)
            return False
        text = self.run_downloader(handler, ['https://example.com/v/1', 'https://example.com/v/2'])
        (run,) = run_stats.read_runs(self.root)
        self.assertEqual((run['status'], run['script'], run['tasks'], run['links_seen'], run['failed']),
                         ('completed', 'downloadContent', 1, 2, 2), text)
        self.assertIsNotNone(run['ended_at'])
        self.assertRegex(run['code_version'], r'^[0-9a-f]{10} py3\.\d+$')
        (host,) = run_stats.read_hosts(self.root, run['run_id'])
        self.assertEqual((host['host'], host['requests'], host['client_errors']), ('example.com', 2, 2))
        links = {l['url']: l for l in run_stats.read_links(self.root, run['run_id'])}
        self.assertEqual(set(links), {'https://example.com/v/1', 'https://example.com/v/2'})
        self.assertTrue(all(l['outcome'] == 'failed' and l['requests'] == 1 for l in links.values()), links)
        con = sqlite3.connect(os.path.join(os.environ['LIBRARY_TOOLS_HOME'], 'app.db'))
        self.assertEqual(con.execute('select status, tasks, links_seen, failed from run_rollup').fetchall(), [('completed', 1, 2, 2)])
        con.close()

    def test_a_crash_inside_the_run_still_closes_the_record(self):
        def handler(driver, url, folder):
            raise KeyboardInterrupt
        try:
            self.run_downloader(handler, ['https://example.com/v/1'])
        except KeyboardInterrupt:
            pass
        (run,) = run_stats.read_runs(self.root)
        self.assertIn(run['status'], ('interrupted', 'stopped'))
        self.assertIsNotNone(run['ended_at'])


if __name__ == '__main__':
    unittest.main()

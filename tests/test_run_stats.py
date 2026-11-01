"""run_stats: run history, per-host and per-link network cost, in the library DB with a rollup in the app DB."""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import library_db  # noqa: E402
import net_stats  # noqa: E402
import run_stats  # noqa: E402


class RunStatsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self._tmp.name)
        library_db._roots.clear()
        self.root = library_db.use_root(os.path.join(base, 'lib'))
        self.post = os.path.join(self.root, 'post one')
        os.makedirs(self.post)
        self.app_db = os.path.join(base, 'app.db')
        self.col = net_stats.Collector()
        self.addCleanup(self._tmp.cleanup)           # registered first, so it runs after the finish() cleanups below

    def recorder(self, **kw):
        return run_stats.RunRecorder(self.root, kw.pop('script', 'downloadContent'), collector=self.col,
                                     app_db_path=self.app_db, settings=kw.pop('settings', {'MAX_RESOLUTION': '1080'}),
                                     version=kw.pop('version', 'abc123 py3.14'))

    def app(self, sql):
        con = sqlite3.connect(self.app_db)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def test_a_started_run_leaves_a_running_row_so_a_killed_run_is_visible(self):
        rec = self.recorder()
        rid = rec.start()
        (row,) = run_stats.read_runs(self.root)
        self.assertEqual((row['run_id'], row['status'], row['script'], row['code_version']), (rid, 'running', 'downloadContent', 'abc123 py3.14'))
        self.assertIn('"MAX_RESOLUTION": "1080"', row['settings_json'])
        self.assertEqual(self.app("select status, library_path from run_rollup"), [('running', self.root)])

    def backdate(self, run_id, seconds):
        """Pretend a run last said it was alive `seconds` ago."""
        then = time.time() - seconds
        past = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(then))
        con = sqlite3.connect(library_db.db_path(self.root))
        con.execute('UPDATE downloader_run_stats SET updated_at=?, updated_epoch=? WHERE run_id=?', (past, then, run_id))
        con.commit()
        con.close()
        return past

    def test_a_run_that_stopped_saying_it_is_alive_is_marked_interrupted_by_the_next_run(self):
        dead = self.recorder()
        dead.start()
        last_seen = self.backdate(dead.run_id, 600)               # killed ten minutes ago: no finish, no more heartbeats
        time.sleep(1.01)
        nxt = self.recorder()
        nxt.start()
        self.addCleanup(nxt.finish, 'completed')
        rows = {r['run_id']: r for r in run_stats.read_runs(self.root)}
        self.assertEqual(rows[dead.run_id]['status'], 'interrupted')
        self.assertEqual(rows[dead.run_id]['ended_at'], last_seen)                # it ended when it was last seen
        self.assertIn('no sign of life', rows[dead.run_id]['note'])
        self.assertEqual(rows[nxt.run_id]['status'], 'running')
        self.assertEqual(self.app("select status from run_rollup where run_id='%s'" % dead.run_id), [('interrupted',)])

    def test_a_run_that_is_still_alive_in_another_terminal_is_left_alone(self):
        live = self.recorder()
        live.start()
        self.addCleanup(live.finish, 'completed')
        time.sleep(1.01)
        other = self.recorder()
        other.start()
        self.addCleanup(other.finish, 'completed')
        rows = {r['run_id']: r['status'] for r in run_stats.read_runs(self.root)}
        self.assertEqual(rows[live.run_id], 'running')

    def test_a_dead_run_reads_as_interrupted_even_before_any_next_run(self):
        dead = self.recorder()
        dead.start()
        self.backdate(dead.run_id, 600)
        (row,) = run_stats.read_runs(self.root)
        self.assertEqual(row['status'], 'interrupted')
        self.assertIn('no sign of life', row['note'])
        self.assertIsNotNone(row['ended_at'])

    def test_liveness_does_not_depend_on_the_local_clock_text(self):
        """Local-time text is ambiguous for an hour when the clocks change; the verdict comes from epoch seconds."""
        now = 1_800_000_000.0
        self.assertFalse(run_stats._is_stale(now - 60, now))
        self.assertTrue(run_stats._is_stale(now - 600, now))
        self.assertTrue(run_stats._is_stale(None, now))
        for tz in ('America/Denver', 'Europe/Berlin', 'UTC'):
            old = os.environ.get('TZ')
            os.environ['TZ'] = tz
            time.tzset()
            try:
                self.assertFalse(run_stats._is_stale(now - 60, now), tz)
                self.assertTrue(run_stats._is_stale(now - 600, now), tz)
            finally:
                if old is None:
                    os.environ.pop('TZ', None)
                else:
                    os.environ['TZ'] = old
                time.tzset()

    def test_runs_sort_by_when_they_really_started(self):
        a, b = self.recorder(), self.recorder()
        a.start()
        time.sleep(0.02)
        b.start()
        self.addCleanup(a.finish, 'completed')
        self.addCleanup(b.finish, 'completed')
        con = sqlite3.connect(library_db.db_path(self.root))
        con.execute("UPDATE downloader_run_stats SET started_at='2026-11-01T01:30:00'")       # the repeated hour: same text
        con.commit()
        con.close()
        self.assertEqual([r['run_id'] for r in run_stats.read_runs(self.root)], [b.run_id, a.run_id])

    def test_a_fresh_running_row_still_reads_as_running(self):
        live = self.recorder()
        live.start()
        self.addCleanup(live.finish, 'completed')
        (row,) = run_stats.read_runs(self.root)
        self.assertEqual(row['status'], 'running')

    def test_the_heartbeat_keeps_a_long_quiet_run_alive_and_stops_when_it_finishes(self):
        rec = run_stats.RunRecorder(self.root, 'downloadContent', collector=self.col, app_db_path=self.app_db, heartbeat_seconds=0.05)
        rec.start()
        first = run_stats.read_runs(self.root)[0]['updated_at']
        time.sleep(1.2)                                                       # e.g. a long mega pause: no links finish
        later = run_stats.read_runs(self.root)[0]
        self.assertGreater(later['updated_at'], first)
        self.assertGreaterEqual(later['duration_s'], 1.0)
        rec.finish('completed', {})
        self.assertFalse(rec._beat.is_alive())
        done = run_stats.read_runs(self.root)[0]
        time.sleep(0.3)
        self.assertEqual(run_stats.read_runs(self.root)[0]['updated_at'], done['updated_at'])
        self.assertEqual(done['status'], 'completed')

    def test_counts_are_saved_with_every_flush_so_a_killed_run_keeps_them(self):
        counts = {'tasks': 7, 'links_seen': 3, 'downloaded': 2, 'failed': 1, 'bytes_saved': 99}
        rec = run_stats.RunRecorder(self.root, 'downloadContent', collector=self.col, app_db_path=self.app_db, counts_fn=lambda: counts)
        rec.start()
        self.col.record('a.example', status=200, nbytes=5, seconds=1)
        for _ in range(run_stats.FLUSH_EVERY_LINKS):
            rec.link_finished()                                                # ... and then the process is killed
        (row,) = run_stats.read_runs(self.root)
        self.assertEqual((row['tasks'], row['links_seen'], row['downloaded'], row['failed'], row['bytes_saved'], row['requests']),
                         (7, 3, 2, 1, 99, 1))
        self.assertEqual(self.app('select tasks, downloaded from run_rollup'), [(7, 2)])

    def test_a_crash_is_recorded_with_its_error(self):
        rec = self.recorder()
        rec.start()
        rec.finish('crashed', {'tasks': 2}, note='RuntimeError: disk gone')
        (row,) = run_stats.read_runs(self.root)
        self.assertEqual((row['status'], row['note'], row['tasks']), ('crashed', 'RuntimeError: disk gone', 2))
        self.assertIsNotNone(row['ended_at'])

    def test_pauses_count_in_the_run_time_but_not_in_the_per_host_request_time(self):
        rec = self.recorder()
        rec.start()
        self.col.record('a.example', status=200, nbytes=1048576, seconds=2.0)        # a 2 s request ...
        time.sleep(1.2)                                                               # ... and a pause (cooldown, waiting)
        rec.finish('completed', {})
        (run,) = run_stats.read_runs(self.root)
        (host,) = run_stats.read_hosts(self.root, rec.run_id)
        self.assertGreaterEqual(run['duration_s'], 1.2)
        self.assertAlmostEqual(host['seconds'], 2.0)                                  # speed = bytes / this, unaffected by the pause

    def test_an_interrupted_runs_flushed_numbers_still_count_in_the_totals(self):
        first = self.recorder()
        first.start()
        self.col.record('a.example', status=200, nbytes=100, seconds=1)
        first.flush()                                                                 # saved, then killed
        self.backdate(first.run_id, 600)
        time.sleep(1.01)
        second = self.recorder()
        second.start()                                                                # a new run starts with fresh counters
        self.col.record('a.example', status=200, nbytes=50, seconds=1)
        second.finish('completed', {})
        self.assertEqual([h['bytes'] for h in run_stats.read_hosts(self.root, first.run_id)], [100])
        self.assertEqual([h['bytes'] for h in run_stats.read_hosts(self.root, second.run_id)], [50])
        (total,) = run_stats.read_hosts(self.root)
        self.assertEqual((total['requests'], total['bytes']), (2, 150))                # the killed run still counts

    def test_finish_stores_status_counts_totals_hosts_and_links(self):
        rec = self.recorder()
        rec.start()
        self.col.begin_link(self.post, 'https://rule34video.com/video/1/x')
        self.col.record('rule34video.com', status=200, nbytes=5_000, seconds=2.0)
        self.col.record('cdn.example', status=429, seconds=0.5)
        self.col.note(precheck_result='size shortcut', precheck_seconds=3.0, download_seconds=0.0)
        self.col.begin_link(self.post, 'https://disk.yandex.com/d/abc', lambda f, u: 'skipped_duplicate')   # closes the first
        self.col.record('disk.yandex.com', status=200, nbytes=9_000, seconds=1.0)
        rec.finish('completed', {'tasks': 5, 'links_seen': 9, 'downloaded': 3, 'failed': 1, 'bytes_saved': 123456},
                   outcome_of=lambda f, u: 'downloaded')                                                  # closes the last
        (run,) = run_stats.read_runs(self.root)
        self.assertEqual((run['status'], run['tasks'], run['links_seen'], run['downloaded'], run['failed'], run['bytes_saved']),
                         ('completed', 5, 9, 3, 1, 123456))
        self.assertEqual((run['requests'], run['bytes_transferred']), (3, 14_000))
        self.assertIsNotNone(run['ended_at'])
        self.assertGreater(run['duration_s'], 0)
        hosts = {h['host']: h for h in run_stats.read_hosts(self.root, rec.run_id)}
        self.assertEqual(set(hosts), {'rule34video.com', 'cdn.example', 'disk.yandex.com'})
        self.assertEqual((hosts['cdn.example']['rate_limited'], hosts['rule34video.com']['bytes']), (1, 5_000))
        links = {l['url']: l for l in run_stats.read_links(self.root, rec.run_id)}
        self.assertEqual(links['https://rule34video.com/video/1/x']['precheck_result'], 'size shortcut')
        self.assertEqual(links['https://rule34video.com/video/1/x']['outcome'], 'skipped_duplicate')
        self.assertEqual(links['https://disk.yandex.com/d/abc']['outcome'], 'downloaded')
        self.assertEqual(links['https://disk.yandex.com/d/abc']['folder'], 'post one')      # relative to the library

    def test_links_that_touched_no_network_and_were_skipped_get_no_row(self):
        rec = self.recorder()
        rec.start()
        self.col.begin_link(self.post, 'https://a.example/1')
        self.col.begin_link(self.post, 'https://a.example/2', lambda f, u: 'previously_done')
        self.col.begin_link(self.post, 'https://a.example/3', lambda f, u: 'skipped_known_failure')
        rec.finish('completed', {})
        self.assertEqual(run_stats.read_links(self.root, rec.run_id), [])

    def test_flush_midway_keeps_the_numbers_so_far(self):
        rec = self.recorder()
        rec.start()
        self.col.begin_link(self.post, 'https://a.example/1')
        self.col.record('a.example', status=200, nbytes=10, seconds=1)
        self.col.end_link(lambda f, u: 'downloaded')
        rec.flush({'downloaded': 1})
        (run,) = run_stats.read_runs(self.root)
        self.assertEqual((run['status'], run['downloaded'], run['requests']), ('running', 1, 1))
        self.assertEqual(len(run_stats.read_links(self.root, rec.run_id)), 1)

    def test_flush_happens_every_so_many_finished_links(self):
        rec = self.recorder()
        rec.start()
        self.col.record('a.example', status=200, nbytes=1, seconds=0.1)
        for _ in range(run_stats.FLUSH_EVERY_LINKS - 1):
            rec.link_finished()
        self.assertEqual(run_stats.read_hosts(self.root, rec.run_id), [])
        rec.link_finished()
        self.assertEqual(len(run_stats.read_hosts(self.root, rec.run_id)), 1)

    def test_the_app_database_gets_a_rollup_for_comparing_libraries(self):
        rec = self.recorder()
        rec.start()
        self.col.record('mega.nz', status=6, seconds=1.0, kind='process', rate_limit_exit=True)
        rec.finish('stopped', {'tasks': 2, 'downloaded': 1, 'failed': 0})
        self.assertEqual(self.app('select status, tasks, downloaded, requests from run_rollup'), [('stopped', 2, 1, 1)])
        self.assertEqual(self.app('select host, requests, rate_limited from host_rollup'), [('mega.nz', 1, 1)])
        (lib_id,) = self.app('select library_id from run_rollup')[0]
        self.assertEqual(lib_id, library_db.library_id(self.root))

    def test_hosts_add_up_over_runs_when_no_run_is_given(self):
        for _ in range(2):
            rec = self.recorder()
            rec.start()
            self.col.record('a.example', status=200, nbytes=100, seconds=1)
            rec.finish('completed', {})
            time.sleep(1.01)
        (host,) = run_stats.read_hosts(self.root)
        self.assertEqual((host['host'], host['requests'], host['bytes']), ('a.example', 2, 200))

    def test_nothing_is_written_outside_a_library_and_nothing_raises(self):
        plain = os.path.join(self._tmp.name, 'plain')
        os.makedirs(plain)
        rec = run_stats.RunRecorder(plain, 'x', collector=self.col, app_db_path=self.app_db)
        rec.start()
        rec.flush()
        rec.finish('completed', {})
        self.assertEqual(run_stats.read_runs(plain), [])
        self.assertFalse(os.path.exists(os.path.join(plain, library_db.DATA_DIRNAME)))

    def test_a_broken_app_database_does_not_stop_the_library_record(self):
        rec = run_stats.RunRecorder(self.root, 'x', collector=self.col, app_db_path='/nonexistent-dir/app.db')
        rec.start()
        rec.finish('completed', {'tasks': 1})
        (run,) = run_stats.read_runs(self.root)
        self.assertEqual((run['status'], run['tasks']), ('completed', 1))

    def test_code_version_changes_with_the_code(self):
        with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
            f.write('a = 1')
        self.addCleanup(os.remove, f.name)
        v1 = run_stats.code_version(f.name)
        with open(f.name, 'w') as fh:
            fh.write('a = 2')
        self.assertNotEqual(v1, run_stats.code_version(f.name))
        self.assertRegex(v1, r'^[0-9a-f]{10} py3\.\d+$')


if __name__ == '__main__':
    unittest.main()

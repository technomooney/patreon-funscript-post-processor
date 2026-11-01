"""A mega rate limit (exit 6) pauses every mega-get; nothing asks MEGA again until the wait is over."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import downloadContent as dc
import mega_cooldown

URL = 'https://mega.nz/file/AbCdEfGh#key'


def finished(code, stdout='', stderr=''):
    return subprocess.CompletedProcess(['mega-get'], code, stdout, stderr)


class ForegroundTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.cd = mega_cooldown.Cooldown([300, 900])
        for p in (mock.patch.object(dc, '_mega_cooldown', self.cd),
                  mock.patch.object(dc.shutil, 'which', return_value='/usr/bin/mega-get'),
                  mock.patch.object(dc, '_mega_ensure_login', return_value=True),
                  mock.patch.object(dc, '_set_status')):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()
        dc._note_failure(None)

    def test_exit_6_starts_a_pause_and_defers(self):
        with mock.patch.object(dc.subprocess, 'run', return_value=finished(6)) as run:
            with self.assertRaises(dc.MegaRateLimitedError):
                dc.download_mega(None, URL, self.dir)
        self.assertEqual(run.call_count, 1)
        self.assertGreater(self.cd.remaining(), 0)

    def test_nothing_is_asked_of_mega_while_the_pause_lasts(self):
        self.cd.hit()
        with mock.patch.object(dc.subprocess, 'run') as run:
            with self.assertRaises(dc.MegaRateLimitedError):
                dc.download_mega(None, URL, self.dir)
        run.assert_not_called()

    def test_a_limit_right_after_a_pause_means_that_wait_was_not_enough(self):
        with mock.patch.object(dc.subprocess, 'run', return_value=finished(6)):
            with self.assertRaises(dc.MegaRateLimitedError):
                dc.download_mega(None, URL, self.dir)            # first limit: tests a 5 minute pause
            self.cd._until = 0                                    # the pause ran out
            self.cd._hit_at -= 300
            with self.assertRaises(dc.MegaRateLimitedError):
                dc.download_mega(None, URL, self.dir)            # still limited
        self.assertEqual(self.cd.bad, 300)
        self.assertEqual(round(self.cd.remaining()), 900)         # so it now waits the next level

    def test_success_clears_the_pause_and_records_the_wait_that_worked(self):
        self.cd.hit()
        self.cd._until = 0
        self.cd._hit_at -= 300
        with mock.patch.object(dc.subprocess, 'run', return_value=finished(0)):
            self.assertTrue(dc.download_mega(None, URL, self.dir))
        self.assertEqual(self.cd.good, 300)
        self.assertEqual(self.cd.remaining(), 0)

    def test_a_dead_link_is_a_failure_not_a_rate_limit_and_shows_the_limit_has_cleared(self):
        self.cd.hit()
        self.cd._until = 0
        self.cd._hit_at -= 300
        with mock.patch.object(dc.subprocess, 'run', return_value=finished(9, stderr='Failed to get public node: Not found')):
            self.assertFalse(dc.download_mega(None, URL, self.dir))
        self.assertEqual(self.cd.remaining(), 0)
        self.assertEqual(self.cd.good, 300)             # MEGA answered normally, so that wait was enough
        self.assertEqual((dc._last_failure.reason, dc._last_failure.permanent), ('mega_not_found', True))

    def test_a_timeout_or_stall_says_nothing_about_the_limit(self):
        self.cd.hit()
        self.cd._until = 0
        for code in (-1, 1, 2):
            with mock.patch.object(dc.subprocess, 'run', return_value=finished(code)):
                dc.download_mega(None, URL, self.dir)
        self.assertIsNone(self.cd.good)                 # still waiting for a real answer

    def test_only_one_mega_get_runs_at_a_time(self):
        started = threading.Event()
        with mock.patch.object(dc.subprocess, 'run', side_effect=lambda *a, **k: started.set() or finished(0)):
            with dc._mega_get_lock:                          # e.g. the worker is in the middle of a download
                t = threading.Thread(target=dc.download_mega, args=(None, URL, self.dir))
                t.start()
                time.sleep(0.3)
                self.assertFalse(started.is_set())           # still waiting its turn
            t.join(5)
        self.assertTrue(started.is_set())

    def test_a_limit_hit_while_waiting_for_the_lock_is_respected(self):
        with mock.patch.object(dc.subprocess, 'run') as run:
            with dc._mega_get_lock:
                result = {}

                def go():
                    try:
                        dc.download_mega(None, URL, self.dir)
                    except dc.MegaRateLimitedError:
                        result['deferred'] = True
                t = threading.Thread(target=go)
                t.start()
                time.sleep(0.3)
                self.cd.hit()                                # the worker got rate limited meanwhile
            t.join(5)
        self.assertTrue(result.get('deferred'))
        run.assert_not_called()


class MaxWaitTests(unittest.TestCase):
    def test_the_end_of_run_wait_is_a_positive_number_of_seconds(self):
        self.assertIsInstance(dc._MEGA_MAX_WAIT, int)
        self.assertGreater(dc._MEGA_MAX_WAIT, 0)


if __name__ == '__main__':
    unittest.main()

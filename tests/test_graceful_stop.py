import os
import signal
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import downloadContent as dc


class MegaWorkerAbortTests(unittest.TestCase):
    def test_abort_drops_queued_retries_and_stops_quickly(self):
        w = dc._MegaWorker()
        w._thread.start()                       # skip start(): it would open a terminal window
        for i in range(3):
            w.submit(f'https://mega.nz/file/{i}', '/nonexistent', f'job{i}', [3600])
        t0 = time.time()
        dropped = w.abort()
        self.assertLess(time.time() - t0, 10)
        self.assertGreaterEqual(len(dropped), 3)    # the queued ones plus the one the worker already holds
        self.assertTrue(all(j['link'].startswith('https://mega.nz/file/') for j in dropped))
        self.assertFalse(w._thread.is_alive())
        self.assertEqual(w.drain_results(), [])


class MegaWorkerWaitTests(unittest.TestCase):
    def test_wait_all_gives_up_after_its_timeout_and_reports_it(self):
        w = dc._MegaWorker()
        w._thread.start()
        w.submit('https://mega.nz/file/x', '/nonexistent', 'job', [3600])
        try:
            t0 = time.time()
            self.assertFalse(w.wait_all(timeout=1))
            self.assertLess(time.time() - t0, 5)
        finally:
            w.abort()

    def test_wait_all_returns_true_when_nothing_is_pending(self):
        w = dc._MegaWorker()
        self.assertTrue(w.wait_all(timeout=1))


class GracefulSigintTests(unittest.TestCase):
    def test_first_sets_flag_second_raises(self):
        prev = dc._install_graceful_sigint()
        try:
            self.assertFalse(dc._stop_after_current.is_set())
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(dc._stop_after_current.is_set())
            with self.assertRaises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGINT)
        finally:
            signal.signal(signal.SIGINT, prev)
            dc._stop_after_current.clear()


if __name__ == '__main__':
    unittest.main()

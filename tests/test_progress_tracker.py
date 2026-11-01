import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import downloadContent as dc
import library_db
import report_store


class ProgressTrackerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        library_db._roots.clear()
        self.dir = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        self.a = os.path.join(self.dir, 'a')
        self.b = os.path.join(self.dir, 'b')

    def tearDown(self):
        self._tmp.cleanup()

    def test_folder_done_survives_reload(self):
        dc.ProgressTracker(self.dir).mark_folder_done(self.a)
        again = dc.ProgressTracker(self.dir)
        self.assertTrue(again.is_folder_done(self.a))
        self.assertFalse(again.is_folder_done(self.b))

    def test_failed_link_is_not_done_even_when_its_folder_is(self):
        t = dc.ProgressTracker(self.dir)
        t.mark_done(self.a, 'good')
        t.mark_folder_done(self.a)
        again = dc.ProgressTracker(self.dir)
        self.assertTrue(again.is_done(self.a, 'good'))
        self.assertFalse(again.is_done(self.a, 'bad'))

    def test_links_are_written_immediately(self):
        dc.ProgressTracker(self.dir).mark_done(self.a, 'l1')
        self.assertTrue(dc.ProgressTracker(self.dir).is_done(self.a, 'l1'))

    def test_folders_alone_count_as_progress_and_clear_removes_everything(self):
        t = dc.ProgressTracker(self.dir)
        t.mark_folder_done(self.a)
        self.assertTrue(dc.ProgressTracker(self.dir).has_progress())
        t.clear()
        self.assertFalse(dc.ProgressTracker(self.dir).has_progress())

    def test_nothing_is_stored_outside_a_library(self):
        plain = os.path.join(self._tmp.name, 'plain')
        os.makedirs(plain)
        t = dc.ProgressTracker(plain)
        t.mark_done(plain, 'l')
        self.assertFalse(dc.ProgressTracker(plain).has_progress())
        self.assertEqual(os.listdir(plain), [])


class RecordingListTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        library_db._roots.clear()
        self.dir = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        os.makedirs(os.path.join(self.dir, 'p'))

    def tearDown(self):
        self._tmp.cleanup()

    def test_rows_are_recorded_when_added_not_at_the_end(self):
        failures = dc._RecordingList(self.dir, 'failed')
        failures.append({'link': 'http://x/1', 'save_directory': os.path.join(self.dir, 'p'), 'domain': 'x'})
        self.assertEqual(report_store.failed_links(self.dir), {'http://x/1'})
        self.assertEqual(len(failures), 1)           # still a normal list for the existing code

    def test_initial_rows_and_extend_are_recorded_and_kind_is_kept(self):
        row = lambda n: {'link': f'http://x/{n}', 'save_directory': self.dir}
        unsure = dc._RecordingList(self.dir, 'uncertain', [row(1)])
        unsure.extend([row(2)])
        self.assertEqual({r['url'] for r in report_store.outcomes(self.dir, 'uncertain')}, {'http://x/1', 'http://x/2'})
        self.assertEqual(report_store.failed_links(self.dir), set())


if __name__ == '__main__':
    unittest.main()

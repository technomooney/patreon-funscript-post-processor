import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import downloadContent as dc


class ProgressTrackerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, '.download_progress.json')

    def tearDown(self):
        self._tmp.cleanup()

    def test_folder_done_survives_reload_with_its_report_rows(self):
        t = dc.ProgressTracker(self.dir)
        failed = [{'link': 'bad', 'save_directory': '/a'}]
        unsure = [{'link': 'maybe', 'save_directory': '/a'}]
        t.mark_folder_done('/a', failed, unsure)

        again = dc.ProgressTracker(self.dir)
        self.assertTrue(again.is_folder_done('/a'))
        self.assertFalse(again.is_folder_done('/b'))
        self.assertEqual(again.folder_rows('/a'), (failed, unsure))

    def test_failed_link_is_not_done_even_when_its_folder_is(self):
        t = dc.ProgressTracker(self.dir)
        t.mark_done('/a', 'good')
        t.mark_folder_done('/a', [{'link': 'bad', 'save_directory': '/a'}], [])
        self.assertTrue(t.is_done('/a', 'good'))
        self.assertFalse(t.is_done('/a', 'bad'))

    def test_loads_the_original_links_only_format(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'/a': ['l1', 'l2']}, f)
        t = dc.ProgressTracker(self.dir)
        self.assertTrue(t.is_done('/a', 'l2'))
        self.assertFalse(t.is_folder_done('/a'))
        self.assertTrue(t.has_progress())

    def test_folders_alone_count_as_progress_and_clear_removes_everything(self):
        t = dc.ProgressTracker(self.dir)
        t.mark_folder_done('/a', [], [])
        self.assertTrue(dc.ProgressTracker(self.dir).has_progress())
        t.clear()
        self.assertFalse(dc.ProgressTracker(self.dir).has_progress())
        self.assertFalse(os.path.exists(self.path))

    def test_unreadable_file_means_no_progress(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('[1, 2]')
        self.assertFalse(dc.ProgressTracker(self.dir).has_progress())


if __name__ == '__main__':
    unittest.main()

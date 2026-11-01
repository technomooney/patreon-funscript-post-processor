"""report_store: link outcomes (with HTTP status / permanence) and the former CSV reports, kept in the library DB."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import library_db  # noqa: E402
import report_store  # noqa: E402


class ReportStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._tmp.name)
        library_db._roots.clear()
        self.root = library_db.use_root(os.path.join(self.base, 'lib'))
        self.post = os.path.join(self.root, 'post one')
        os.makedirs(self.post)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_library_stores_nothing(self):
        plain = os.path.join(self.base, 'plain')
        os.makedirs(plain)
        self.assertFalse(report_store.record_outcome(plain, 'http://x/1', plain))
        self.assertFalse(report_store.replace_report(plain, 'r', [{'a': 1}]))
        self.assertEqual(report_store.outcomes(plain), [])
        self.assertEqual(report_store.read_report(plain, 'r'), [])
        self.assertFalse(os.path.exists(os.path.join(plain, library_db.DATA_DIRNAME)))

    def test_outcome_roundtrip_has_absolute_save_directory(self):
        report_store.record_outcome(self.root, 'http://x/1', self.post, domain='x', reason='http_404', http_status=404)
        (row,) = report_store.outcomes(self.root, 'failed')
        self.assertEqual(row['save_directory'], self.post)
        self.assertEqual((row['http_status'], row['reason'], row['attempts']), (404, 'http_404', 1))

    def test_repeat_bumps_attempts_and_permanent_sticks(self):
        report_store.record_outcome(self.root, 'http://x/1', self.post, permanent=True, reason='gone')
        report_store.record_outcome(self.root, 'http://x/1', self.post, permanent=False, reason='timeout')
        (row,) = report_store.outcomes(self.root)
        self.assertEqual((row['attempts'], row['permanent'], row['reason']), (2, 1, 'timeout'))
        self.assertEqual(report_store.known_dead_links(self.root), {'http://x/1'})

    def test_same_url_in_two_folders_is_two_rows(self):
        other = os.path.join(self.root, 'post two')
        os.makedirs(other)
        report_store.record_outcome(self.root, 'http://x/1', self.post)
        report_store.record_outcome(self.root, 'http://x/1', other)
        self.assertEqual(len(report_store.outcomes(self.root)), 2)

    def test_forget_and_kind_filter(self):
        report_store.record_outcome(self.root, 'http://x/1', self.post, 'failed')
        report_store.record_outcome(self.root, 'http://x/2', self.post, 'uncertain', matched_video='a.mp4', match_score=0.6)
        self.assertEqual([r['url'] for r in report_store.outcomes(self.root, 'uncertain')], ['http://x/2'])
        report_store.forget(self.root, 'http://x/1', self.post)
        self.assertEqual([r['url'] for r in report_store.outcomes(self.root)], ['http://x/2'])

    def test_bad_kind_rejected(self):
        with self.assertRaises(ValueError):
            report_store.record_outcome(self.root, 'u', self.post, 'weird')

    def test_report_replaced_whole_and_ordered(self):
        report_store.replace_report(self.root, 'many_funscripts', [{'n': 1}, {'n': 2}, {'n': 3}])
        report_store.replace_report(self.root, 'many_funscripts', [{'n': 9, 'name': 'é'}])
        self.assertEqual(report_store.read_report(self.root, 'many_funscripts'), [{'n': 9, 'name': 'é'}])
        report_store.replace_report(self.root, 'many_funscripts', [])
        self.assertEqual(report_store.report_names(self.root), [])

    def test_record_rows_and_forget_many(self):
        rows = [{'link': 'http://x/1', 'save_directory': self.post, 'domain': 'x', 'funscript_name': 'a'},
                {'link': 'http://x/2', 'save_directory': self.post}, {'save_directory': self.post}]
        self.assertEqual(report_store.record_rows(self.root, rows), 2)
        self.assertEqual(report_store.failed_links(self.root), {'http://x/1', 'http://x/2'})
        report_store.forget_many(self.root, [('http://x/1', self.post)])
        self.assertEqual(report_store.failed_links(self.root), {'http://x/2'})


if __name__ == '__main__':
    unittest.main()

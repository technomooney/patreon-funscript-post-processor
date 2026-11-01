"""generate_audit_report shows the reports kept in the library DB, and never needs a loose CSV."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import generate_audit_report  # noqa: E402
import library_db  # noqa: E402
import report_store  # noqa: E402


class AuditReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        library_db._roots.clear()
        self.root = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        self.post = os.path.join(self.root, 'post')
        os.makedirs(self.post)

    def tearDown(self):
        self._tmp.cleanup()

    def test_db_reports_and_link_outcomes_are_rendered(self):
        report_store.record_outcome(self.root, 'http://x/1', self.post, domain='x', reason='mega_not_found',
                                    http_status=-9, permanent=True)
        report_store.replace_report(self.root, 'funscript_check',
                                    [{'folder': self.post, 'issue': 'video_without_funscript', 'file': 'a <b>.mp4'}])
        html = generate_audit_report.generate(self.root)
        self.assertIn('mega_not_found', html)
        self.assertIn('video_without_funscript', html)
        self.assertIn('a &lt;b&gt;.mp4', html)      # escaped
        self.assertNotIn('a <b>.mp4', html)

    def test_loose_csvs_are_not_read_any_more(self):
        os.makedirs(os.path.join(self.root, '_reports'))
        with open(os.path.join(self.root, '_reports', 'funscript_check.csv'), 'w') as f:
            f.write('folder,issue\n/x,STALE_LOOSE_CSV_ROW\n')
        self.assertNotIn('STALE_LOOSE_CSV_ROW', generate_audit_report.generate(self.root))


if __name__ == '__main__':
    unittest.main()

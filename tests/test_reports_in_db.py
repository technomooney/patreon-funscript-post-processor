"""The check/fix tools store their reports in the library DB and leave no loose CSV files behind."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import check_funscripts  # noqa: E402
import fix_garbled_names  # noqa: E402
import library_db  # noqa: E402
import report_store  # noqa: E402


class ReportsInDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        library_db._roots.clear()
        self.root = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        self.post = os.path.join(self.root, 'post')
        os.makedirs(self.post)

    def tearDown(self):
        self._tmp.cleanup()

    def _quiet(self, fn, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a, **kw)

    def test_check_funscripts_report_goes_to_db(self):
        with open(os.path.join(self.post, 'video one.mp4'), 'wb') as f:
            f.write(b'x' * 10)
        self._quiet(check_funscripts.run, self.root, False)
        rows = report_store.read_report(self.root, 'funscript_check')
        self.assertEqual([(r['issue'], r['file']) for r in rows], [('video_without_funscript', 'video one.mp4')])
        self.assertFalse(os.path.exists(os.path.join(self.root, '_reports')))

    def test_a_clean_rerun_clears_the_stale_report(self):
        report_store.replace_report(self.root, 'funscript_check', [{'issue': 'old'}])
        self._quiet(check_funscripts.run, self.root, False)   # nothing in the library: no findings
        self.assertEqual(report_store.read_report(self.root, 'funscript_check'), [])

    def test_fix_garbled_names_reports_go_to_db(self):
        with open(os.path.join(self.post, 'script.txt'), 'w') as f:   # funscript content, wrong extension
            f.write('{"version": "1.0", "actions": [{"at": 0, "pos": 10}, {"at": 500, "pos": 90}]}')
        self._quiet(fix_garbled_names.run, self.root, True)
        rows = report_store.read_report(self.root, 'funscript_renames')
        self.assertEqual([r['status'] for r in rows], ['would rename'])
        self.assertFalse(os.path.exists(os.path.join(self.root, '_reports')))

if __name__ == '__main__':
    unittest.main()

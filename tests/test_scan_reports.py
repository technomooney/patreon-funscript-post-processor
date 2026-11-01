"""The reports a run replaces: an empty result clears the old one, and special runs never wipe a full scan's."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import downloadContent as dc
import library_db
import report_store


class ScanReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        library_db._roots.clear()
        self.root = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))

    def tearDown(self):
        self._tmp.cleanup()

    def quiet(self, fn, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a, **kw)

    def names(self):
        return set(report_store.report_names(self.root))

    def test_a_full_scan_stores_its_lists_and_a_later_empty_one_clears_them(self):
        self.quiet(dc._write_scan_reports, self.root, [{'folder': '/a', 'count': 3}], ['/m'], ['/c'], False, False)
        self.assertEqual(self.names(), {'many_funscripts', 'manual_folders', 'consolidated_folders'})
        self.quiet(dc._write_scan_reports, self.root, [], [], [], False, False)
        self.assertEqual(self.names(), set())

    def test_a_special_run_leaves_the_full_scans_reports_alone(self):
        self.quiet(dc._write_scan_reports, self.root, [{'folder': '/a', 'count': 3}], ['/m'], ['/c'], False, False)
        self.quiet(dc._write_scan_reports, self.root, [], [], [], False, True)       # prebuilt tasks, nothing scanned
        self.assertEqual(self.names(), {'many_funscripts', 'manual_folders', 'consolidated_folders'})

    def test_mega_error6_is_replaced_even_when_the_run_had_none(self):
        self.quiet(dc._write_mega_error6_csv, self.root, [{'link': 'http://x', 'save_directory': '/a'}])
        self.assertEqual(self.names(), {'mega_error6'})
        self.quiet(dc._write_mega_error6_csv, self.root, [])
        self.assertEqual(self.names(), set())

    def test_each_special_run_has_its_own_mega_error6_report(self):
        self.quiet(dc._write_mega_error6_csv, self.root, [{'link': 'http://x', 'save_directory': '/a'}], 'mega_error6')
        self.quiet(dc._write_mega_error6_csv, self.root, [], 'mega_error6_funscript_metadata')
        self.assertEqual(self.names(), {'mega_error6'})


if __name__ == '__main__':
    unittest.main()

import io
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import fetch_diagnostics as fd


class FetchDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get('LIBRARY_TOOLS_HOME')
        os.environ['LIBRARY_TOOLS_HOME'] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop('LIBRARY_TOOLS_HOME', None)
        else:
            os.environ['LIBRARY_TOOLS_HOME'] = self.old
        self.tmp.cleanup()

    def test_cookie_values_never_logged(self):
        e = urllib.error.HTTPError('https://h/x', 403, 'Forbidden', {}, io.BytesIO(b'no'))
        path = fd.log_fetch_failure('https://h/x', {'Cookie': 'sid=TOPSECRET; b=2'}, e)
        text = open(path).read()
        self.assertNotIn('TOPSECRET', text)
        self.assertIn('sid', text)

    def test_link_failure_keeps_final_progress_line_only(self):
        stop = fd.start_capture()
        try:
            fd.reset_capture()
            sys.stdout.write('  [x] oops\n  downloading 1\r  downloading 2\n')
            path = fd.log_link_failure({'link': 'u', 'domain': 'd'})
        finally:
            stop()
        text = open(path).read()
        self.assertIn('[x] oops', text)
        self.assertIn('downloading 2', text)
        self.assertNotIn('downloading 1', text)


if __name__ == '__main__':
    unittest.main()

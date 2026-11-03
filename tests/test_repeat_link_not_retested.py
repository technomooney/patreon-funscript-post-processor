import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import downloadContent as dc  # noqa: E402


class RepeatLinkTests(unittest.TestCase):
    def setUp(self):
        dc._links_handled.clear()
        self.addCleanup(dc._links_handled.clear)

    def test_a_skipped_link_reached_again_is_not_probed_again(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(dc, '_precheck_url', return_value='already exists: a.mp4') as pre:
                self.assertFalse(dc._direct_fetch_inner('https://cdn.example/a.mp4#x', d, 't1', {}))
                self.assertFalse(dc._direct_fetch_inner('https://cdn.example/a.mp4', d, 't2', {}))
            self.assertEqual(pre.call_count, 1)

    def test_the_same_link_for_another_folder_is_still_tested(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2, \
                contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(dc, '_precheck_url', return_value='already exists: a.mp4') as pre:
                dc._direct_fetch_inner('https://cdn.example/a.mp4', d1, 't1', {})
                dc._direct_fetch_inner('https://cdn.example/a.mp4', d2, 't2', {})
            self.assertEqual(pre.call_count, 2)


if __name__ == '__main__':
    unittest.main()

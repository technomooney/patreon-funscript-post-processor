import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import check_funscripts as c  # noqa: E402


def _touch(folder, name):
    with open(os.path.join(folder, name), 'wb') as fh:
        fh.write(b'x')


class NameMatchIgnoresPunctuation(unittest.TestCase):
    def _check(self, video, script):
        with tempfile.TemporaryDirectory() as d:
            _touch(d, video)
            _touch(d, script)
            return c._check_folder(d)

    def test_apostrophe_difference_still_matches(self):
        r = self._check("Don't Stop.mp4", 'Dont Stop.funscript')
        self.assertEqual(r.unmatched_videos, [])
        self.assertEqual(r.orphan_scripts, [])

    def test_case_underscore_and_curly_quote(self):
        r = self._check('Don’t_Stop Now.mp4', 'dont stop now (SMOOTH).funscript')
        self.assertEqual(r.unmatched_videos, [])

    def test_different_names_still_unmatched(self):
        r = self._check('Alpha beta.mp4', 'Gamma delta.funscript')
        self.assertEqual(len(r.unmatched_videos), 1)
        self.assertEqual(len(r.orphan_scripts), 1)

    def test_orphan_keeps_real_name(self):
        r = self._check('Alpha beta.mp4', "Gamma's delta.funscript")
        self.assertEqual(r.orphan_scripts[0]['base'], "Gamma's delta")


if __name__ == '__main__':
    unittest.main()

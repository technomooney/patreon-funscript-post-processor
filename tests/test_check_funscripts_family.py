import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import check_funscripts as c  # noqa: E402


class LabelFolding(unittest.TestCase):
    def test_prefix_folded_into_existing_tag(self):
        self.assertEqual(c._family_script_name('hard video one (smooth).funscript', 'video one', 'hard'),
                         'video one (smooth - hard).funscript')

    def test_prefix_without_tag(self):
        self.assertEqual(c._family_script_name('HARD Goat.funscript', 'Goat', 'HARD'),
                         'Goat (HARD).funscript')

    def test_axis_and_multi_tags_kept(self):
        self.assertEqual(c._family_script_name('HARD x (max interval) (SMOOTH).roll.funscript', 'x', 'HARD'),
                         'x (max interval) (SMOOTH - HARD).roll.funscript')

    def test_family_detection(self):
        self.assertEqual(c._script_family(['HARD Goat', 'Goat']),
                         ('Goat', {'HARD Goat': 'HARD'}))
        # trailing resolution tags are not labels: stays ambiguous
        self.assertIsNone(c._script_family(['Goat', 'Goat_540p']))
        self.assertIsNone(c._script_family(['Alpha beta', 'Gamma delta']))


class LoneVideoFamilyRename(unittest.TestCase):
    def test_family_renames_video_and_scripts(self):
        with tempfile.TemporaryDirectory() as d:
            names = ['goat-slug_1080p.mp4', 'HARD Goat.funscript',
                     'Goat (SMOOTH).funscript', 'Goat (max interval).funscript']
            for n in names:
                open(os.path.join(d, n), 'wb').write(b'x')
            with mock.patch.object(c, '_video_duration', return_value=100.0), \
                 mock.patch.object(c, '_funscript_duration', return_value=100.0), \
                 mock.patch.object(c.action_log, 'record'):
                r = c._check_folder(d, do_rename=True)
            self.assertEqual(sorted(os.listdir(d)),
                             ['Goat (HARD).funscript', 'Goat (SMOOTH).funscript',
                              'Goat (max interval).funscript', 'Goat.mp4'])
            self.assertEqual(r.unmatched_videos, [])

    def test_unrelated_names_still_ambiguous(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ['v.mp4', 'Alpha.funscript', 'Beta.funscript']:
                open(os.path.join(d, n), 'wb').write(b'x')
            with mock.patch.object(c, '_video_duration', return_value=100.0), \
                 mock.patch.object(c, '_funscript_duration', return_value=100.0):
                c._check_folder(d, do_rename=True)
            self.assertIn('v.mp4', os.listdir(d))


if __name__ == '__main__':
    unittest.main()

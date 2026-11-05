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


class LoneVideoOrphanLabels(unittest.TestCase):
    def _run(self, names, do_rename=True):
        with tempfile.TemporaryDirectory() as d:
            for n in names:
                with open(os.path.join(d, n), 'wb') as fh:
                    fh.write(b'x')
            with mock.patch.object(c, '_video_duration', return_value=100.0), \
                 mock.patch.object(c, '_funscript_duration', return_value=100.0), \
                 mock.patch.object(c.action_log, 'record'):
                r = c._check_folder(d, do_rename=do_rename)
            return sorted(os.listdir(d)), r

    def test_real_case_prefix_and_truncated_name(self):
        files, r = self._run([
            'krystal-hmv-conchita-warning_1080p.mp4',
            'krystal-hmv-conchita-warning_1080p (max interval).funscript',
            'krystal-hmv-conchita-warning (SMOOTH).funscript',
            'HARD krystal-hmv-conchita-warning.funscript'])
        self.assertEqual(files, [
            'krystal-hmv-conchita-warning (HARD).funscript',
            'krystal-hmv-conchita-warning (SMOOTH).funscript',
            'krystal-hmv-conchita-warning (max interval).funscript',
            'krystal-hmv-conchita-warning.mp4'])
        self.assertEqual(r.orphan_scripts, [])

    def test_prefix_folded_into_existing_tag(self):
        files, _ = self._run(['some long video name_1080p.mp4', 'some long video name_1080p (max interval).funscript',
                              'hard some long video name (smooth).funscript'])
        self.assertIn('some long video name (smooth - hard).funscript', files)

    def test_dry_run_changes_nothing(self):
        names = ['krystal-hmv-conchita-warning_1080p.mp4',
                 'krystal-hmv-conchita-warning_1080p (max interval).funscript',
                 'HARD krystal-hmv-conchita-warning.funscript']
        files, r = self._run(names, do_rename=False)
        self.assertEqual(files, sorted(names))
        self.assertEqual(len(r.reverse_renamed), 3)   # video + script lose _1080p, HARD folded in

    def test_unrelated_orphan_left_alone(self):
        files, r = self._run(['long video name here.mp4', 'long video name here.funscript',
                              'something completely different.funscript'])
        self.assertIn('something completely different.funscript', files)
        self.assertEqual(len(r.orphan_scripts), 1)


class ResolutionTags(unittest.TestCase):
    def test_strip_res(self):
        for before, after in (('x_1080p', 'x'), ('x 480p', 'x'), ('x-720p', 'x'), ('x_2160P', 'x'),
                              ('1080p', '1080p'), ('[1080p-HEVC] x', '[1080p-HEVC] x'), ('x 4k', 'x 4k')):
            self.assertEqual(c._strip_res(before), after)

    def test_names_lose_resolution_and_keep_tags_and_axes(self):
        with tempfile.TemporaryDirectory() as d:
            names = ['vid_1080p.mp4', 'vid_1080p (SMOOTH).funscript', 'vid_540p (max interval).roll.funscript', 'vid_1080p.gif']
            for n in names:
                with open(os.path.join(d, n), 'wb') as fh:
                    fh.write(b'x')
            with mock.patch.object(c.action_log, 'record'):
                c._check_folder(d, do_rename=True)
            self.assertEqual(sorted(os.listdir(d)), ['vid (SMOOTH).funscript', 'vid (max interval).roll.funscript',
                                                      'vid.mp4', 'vid_1080p.gif'])   # only videos/scripts are touched

    def test_taken_name_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ['vid_1080p.mp4', 'vid.mp4']:
                with open(os.path.join(d, n), 'wb') as fh:
                    fh.write(b'a' if n == 'vid.mp4' else b'b')
            with mock.patch.object(c.action_log, 'record'):
                c._check_folder(d, do_rename=True)
            self.assertEqual(sorted(os.listdir(d)), ['vid.mp4', 'vid_1080p.mp4'])

    def test_resolution_difference_no_longer_blocks_a_match(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ['vid_1080p.mp4', 'vid.funscript']:
                with open(os.path.join(d, n), 'wb') as fh:
                    fh.write(b'x')
            r = c._check_folder(d, do_rename=False)
            self.assertEqual(r.unmatched_videos, [])
            self.assertEqual(r.orphan_scripts, [])


if __name__ == '__main__':
    unittest.main()

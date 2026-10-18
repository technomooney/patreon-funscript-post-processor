import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import action_log  # noqa: E402
import creator_config  # noqa: E402
import library_db  # noqa: E402
import loose_archive as la  # noqa: E402


def fs(path, points):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump({'actions': [{'at': a, 'pos': p} for a, p in points]}, f)


def blob(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


class LooseArchiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        self.base = os.path.join(self.root, 'Bek')
        self.lib = os.path.join(self.base, '!archive')
        os.makedirs(self.lib)
        # the archive: one script, and one video recorded in the library database with its sha256
        fs(os.path.join(self.lib, 'Monthly', 'a.funscript'), [(0, 1), (100, 90)])
        fs(os.path.join(self.lib, 'Monthly', 'a.roll.funscript'), [(0, 5), (100, 50)])
        self.vid = b'archive video bytes'
        blob(os.path.join(self.lib, 'Monthly', 'a.mp4'), self.vid)
        library_db.add_file(self.root, 't', folder=library_db.rel_folder(self.root, os.path.join(self.lib, 'Monthly')),
                            orig_name='a.mp4', cur_name='a.mp4', sha256=la._sha256(os.path.join(self.lib, 'Monthly', 'a.mp4')))
        # video checks without ffprobe/fpcalc: duration = bytes length, "same content" = same first byte, height = len % 100
        self._saved = (la._duration, la._similar, la._height, la._max_res, la._closer)
        la._duration = lambda p: float(os.path.getsize(p))
        la._similar = lambda a, b: open(a, 'rb').read(1) == open(b, 'rb').read(1)
        la._height = lambda p: 1080 if b'HI' in open(p, 'rb').read() else 480
        la._max_res = lambda: 1080
        la._closer = lambda post_h, arch_h, mx: abs(post_h - mx) < abs(arch_h - mx)
        self.post = os.path.join(self.base, '[1] A post')
        os.makedirs(self.post)
        with open(os.path.join(self.post, 'description.json'), 'w') as f:
            f.write('{}')

    def tearDown(self):
        la._duration, la._similar, la._height, la._max_res, la._closer = self._saved
        self._tmp.cleanup()

    def adopt(self):
        action_log.start('t', self.base, journal='t')
        try:
            idx = la.build_index(self.lib, self.root)
            return la.adopt_post(self.base, self.root, self.lib, '[1] A post', idx)
        finally:
            action_log.finish()

    def test_duplicates_trashed_new_files_adopted_description_kept(self):
        fs(os.path.join(self.post, 'Renamed.funscript'), [(0, 1), (100, 90)])          # same data, other name
        fs(os.path.join(self.post, 'New.funscript'), [(0, 7), (100, 70)])               # not in the archive
        blob(os.path.join(self.post, 'dup.mp4'), self.vid)                              # same bytes as the archive video
        blob(os.path.join(self.post, 'fresh.mp4'), b'only in the post')
        out = self.adopt()
        self.assertEqual(sorted(out['trashed']), ['Renamed.funscript', 'dup.mp4'])
        self.assertEqual(sorted(out['adopted']), ['New.funscript', 'fresh.mp4'])
        adopted = os.path.join(self.lib, la.ADOPT_DIRNAME, '[1] A post')
        self.assertEqual(sorted(os.listdir(adopted)), ['New.funscript', 'fresh.mp4'])
        self.assertEqual(os.listdir(self.post), ['description.json'])
        self.assertTrue(os.path.isdir(os.path.join(self.base, '.trash')))               # trashed, not deleted

    def test_a_reencoded_copy_of_the_same_content_is_a_duplicate_not_a_new_file(self):
        # "archive video bytes" (19 bytes) vs a post copy that is NOT byte-identical but has the same duration/content
        blob(os.path.join(self.post, 'reencoded.mp4'), b'a' + b'x' * 18)               # same first byte + same length
        out = self.adopt()
        self.assertEqual((out['trashed'], out['adopted']), (['reencoded.mp4'], []))

    def test_a_different_video_of_another_length_is_adopted(self):
        blob(os.path.join(self.post, 'other.mp4'), b'a' + b'x' * 40)                   # same first byte but other duration
        self.assertEqual(self.adopt()['adopted'], ['other.mp4'])

    def test_a_post_copy_that_fits_max_resolution_better_is_kept_and_flagged_archive_untouched(self):
        blob(os.path.join(self.post, 'hi.mp4'), b'a' + b'HI' + b'x' * 16)              # same content/length, 1080p vs 480p
        out = self.adopt()
        self.assertEqual((out['trashed'], out['adopted']), ([], ['hi.mp4']))
        self.assertEqual(len(out['better_than_archive']), 1)
        with open(os.path.join(self.lib, 'Monthly', 'a.mp4'), 'rb') as f:
            self.assertEqual(f.read(), self.vid)                                      # the archive copy is untouched

    def test_an_axis_file_is_not_a_duplicate_of_a_main_script_with_the_same_points(self):
        fs(os.path.join(self.post, 'x.roll.funscript'), [(0, 1), (100, 90)])           # a.funscript's points, but .roll
        out = self.adopt()
        self.assertEqual(out['adopted'], ['x.roll.funscript'])

    def test_a_post_only_copy_is_never_trashed_because_of_its_own_database_record(self):
        p = os.path.join(self.post, 'mine.mp4')
        blob(p, b'only copy')
        library_db.add_file(self.root, 't', folder=library_db.rel_folder(self.root, self.post), orig_name='mine.mp4',
                            cur_name='mine.mp4', sha256=la._sha256(p))
        out = self.adopt()
        self.assertEqual((out['trashed'], out['adopted']), ([], ['mine.mp4']))

    def test_an_unreadable_script_is_left_in_place(self):
        blob(os.path.join(self.post, 'broken.funscript'), b'not json')
        out = self.adopt()
        self.assertEqual(out['left'], ['broken.funscript'])
        self.assertTrue(os.path.isfile(os.path.join(self.post, 'broken.funscript')))

    def test_settings_are_opt_in_and_post_folders_skip_the_library(self):
        self.assertIsNone(la.settings(self.base))
        creator_config.save(self.base, {'pixeldrain_archive': {'dir': '!archive'}})
        self.assertEqual(la.settings(self.base), {'dir': '!archive', 'discord_channel': 'archive', 'latest_only': True})
        with open(os.path.join(self.lib, 'description.json'), 'w') as f:
            f.write('{}')                                                               # inside the library: not a post
        self.assertEqual(la.post_folders(self.base, self.lib), ['[1] A post'])


if __name__ == '__main__':
    unittest.main()

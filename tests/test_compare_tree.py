"""compare_tree: finds renamed posts/files after a reset + redownload and puts the old names back (journalled)."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import action_log  # noqa: E402
import compare_tree as ct  # noqa: E402
import library_db  # noqa: E402
import reset_library as rs  # noqa: E402


class CompareTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = library_db.use_root(os.path.realpath(self._tmp.name))
        self._ptr = os.environ.get('ACTION_LOG_POINTER_PATH')
        os.environ['ACTION_LOG_POINTER_PATH'] = os.path.join(self.root, 'ptr.json')
        self.w('C/[11111] 2024 Old Name/clip one.mp4', 'a' * 100)
        self.w('C/[11111] 2024 Old Name/clip two.mp4', 'b' * 200)
        self.w('C/[22222] stays/x.mp4', 'x')
        self.w('C/[33333] dropped/y.mp4', 'y')
        rs.apply(self.root, self.plan(), progress=lambda *_: None)

    def tearDown(self):
        if self._ptr is None:
            os.environ.pop('ACTION_LOG_POINTER_PATH', None)
        else:
            os.environ['ACTION_LOG_POINTER_PATH'] = self._ptr
        self._tmp.cleanup()

    def plan(self):
        return rs.record_plan(self.root, rs.build_plan(self.root))

    def w(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(text)

    def result(self, **kw):
        v = ct.list_versions(self.root)[-1]
        new = {r: (k, s) for r, k, s, _m in rs.iter_tree(self.root)}
        return ct.compare(ct.load_snapshot(self.root, v), new, **kw)

    def redownload_with_other_names(self):
        self.w('C/[11111] 2024 Different Title/clip 1.mp4', 'a' * 100)
        self.w('C/[11111] 2024 Different Title/clip 2.mp4', 'b' * 200)
        self.w('C/[22222] stays/x.mp4', 'x')

    def test_finds_renamed_post_and_files(self):
        self.redownload_with_other_names()
        r = self.result()
        self.assertEqual(r['dir_renames'], [{'from': 'C/[11111] 2024 Different Title', 'to': 'C/[11111] 2024 Old Name'}])
        self.assertEqual({(f['from'], f['to']) for f in r['file_renames']}, {('clip 1.mp4', 'clip one.mp4'), ('clip 2.mp4', 'clip two.mp4')})
        self.assertIn('C/[33333] dropped', r['missing'])

    def test_restore_is_dry_run_until_applied_then_undoable(self):
        self.redownload_with_other_names()
        r = self.result()
        before = {p for p, *_ in rs.iter_tree(self.root)}
        self.assertEqual(ct.restore(self.root, r, apply=False, log=lambda *_: None), 3)
        self.assertEqual(before, {p for p, *_ in rs.iter_tree(self.root)})
        self.assertEqual(ct.restore(self.root, r, apply=True, log=lambda *_: None), 3)
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'C/[11111] 2024 Old Name/clip one.mp4')))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'C/[11111] 2024 Different Title')))
        self.assertTrue(action_log.read_last(self.root))

    def test_ambiguous_ids_and_existing_targets_are_left_alone(self):
        self.w('C/[11111] copy A/clip 1.mp4', 'a' * 100)
        self.w('C/[11111] copy B/clip 1.mp4', 'a' * 100)
        self.assertEqual(self.result()['dir_renames'], [])

    def test_scoped_snapshot_is_found_from_the_whole_library_and_from_the_folder(self):
        self.assertEqual(ct.version_scope(self.root, ct.list_versions(self.root)[-1]), '')
        self.assertEqual(ct.region_for('C', 'C'), 'C')
        self.assertEqual(ct.region_for('C', ''), 'C')
        self.assertEqual(ct.region_for('', 'C/x'), 'C/x')
        self.assertIsNone(ct.region_for('C', 'D'))

    def test_filters(self):
        self.redownload_with_other_names()
        self.assertEqual(self.result(only='no-such-text')['dir_renames'], [])
        self.assertEqual(len(self.result(creator='C')['dir_renames']), 1)


if __name__ == '__main__':
    unittest.main()

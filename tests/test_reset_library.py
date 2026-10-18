"""Reset: .manual folders and hidden/config entries survive, everything else goes, a snapshot is written first,
the database forgets the removed area, a wrong/stale token is refused and an interrupted apply resumes."""
import gzip
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import library_db  # noqa: E402
import reset_library as rs  # noqa: E402


class ResetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = library_db.use_root(os.path.realpath(self._tmp.name))
        self.w('Creator/creator_config.json', '{}')
        self.w('Creator/.hidden', 'h')
        self.w('Creator/[1] post one/video.mp4', 'v' * 10)
        self.w('Creator/[1] post one/.folder_log.json', '[]')
        self.w('Creator/[2] manual post/.manual', '')
        self.w('Creator/[2] manual post/keep.mp4', 'k')
        self.w('Creator/Collection/[3] nested/.manual', '')
        self.w('Creator/Collection/[3] nested/keep2.mp4', 'k')
        self.w('Creator/Collection/loose.zip', 'z')
        self.w('Creator/Collection/[4] gone/a.mp4', 'a')
        self.w('Other/[5] post/b.mp4', 'b')
        self.w('Pize/_pixeldrain/.manifest.json', '{}')
        self.w('Pize/_pixeldrain/pack.zip', 'p')
        self.w('.trash/old.mp4', 't')
        self.w('top-level-note.txt', 'n')
        library_db.add_run(self.root, 'Creator/[1] post one', 'x', {'script': 'x'}, 't')
        library_db.add_run(self.root, 'Creator/[2] manual post', 'x', {'script': 'x'}, 't')
        library_db.add_file(self.root, 't', folder='Creator/[1] post one', orig_name='video.mp4', cur_name='video.mp4',
                            pixeldrain_id='GONE')
        library_db.add_file(self.root, 't', folder='Creator/[2] manual post', orig_name='keep.mp4', cur_name='keep.mp4',
                            pixeldrain_id='KEEP')
        library_db.upsert_link(self.root, 'GONE', 't', status='done')
        library_db.upsert_link(self.root, 'KEEP', 't', status='done')
        library_db.tool_data_set(self.root, 'pize_pixeldrain_archives', 'legacy_manifest_extra:Pize', {'a': 1})

    def tearDown(self):
        self._tmp.cleanup()

    def w(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(text)

    def exists(self, rel):
        return os.path.exists(os.path.join(self.root, rel))

    def tree(self):
        return {r for r, _k, _s, _m in rs.iter_tree(self.root)}

    def plan_and_token(self):
        plan = rs.build_plan(self.root)
        return plan, rs.record_plan(self.root, plan)

    def test_plan_changes_nothing_and_lists_manual_folders(self):
        before = self.tree()
        plan, _ = self.plan_and_token()
        self.assertEqual(before, self.tree())
        self.assertEqual([m['path'] for m in plan['manual']],
                         sorted([os.path.join('Creator', '[2] manual post'), os.path.join('Creator', 'Collection', '[3] nested')]))
        html_ = rs.render_plan_html(plan)
        self.assertIn('[2] manual post', html_)
        self.assertIn(plan['token'], html_)

    def test_apply_removes_everything_except_manual_and_protected_entries(self):
        _, token = self.plan_and_token()
        rs.apply(self.root, token, progress=lambda *_: None)
        for keep in ('Creator/creator_config.json', 'Creator/.hidden', 'Creator/[2] manual post/keep.mp4',
                     'Creator/[2] manual post/.manual', 'Creator/Collection/[3] nested/keep2.mp4', '.trash/old.mp4',
                     'top-level-note.txt', '.library-data/library.db'):
            self.assertTrue(self.exists(keep), keep)
        for gone in ('Creator/[1] post one', 'Creator/Collection/loose.zip', 'Creator/Collection/[4] gone', 'Other/[5] post',
                     'Pize/_pixeldrain'):
            self.assertFalse(self.exists(gone), gone)

    def test_snapshot_and_database_copy_are_saved_before_removal(self):
        before = self.tree()
        _, token = self.plan_and_token()
        result = rs.apply(self.root, token, progress=lambda *_: None)
        v = result['version']
        with gzip.open(os.path.join(v, 'tree.jsonl.gz'), 'rt', encoding='utf-8') as f:
            snap = {json.loads(l)['p'] for l in f}
        self.assertEqual(snap, before)
        self.assertTrue(os.path.isfile(os.path.join(v, 'library.db')))
        self.assertTrue(os.path.isfile(os.path.join(v, 'logs', 'Creator', '[1] post one', '.folder_log.json')))
        self.assertTrue(os.path.isfile(os.path.join(v, 'logs', 'Pize', '_pixeldrain', '.manifest.json')))

    def test_database_forgets_removed_area_but_keeps_manual(self):
        _, token = self.plan_and_token()
        rs.apply(self.root, token, progress=lambda *_: None)
        self.assertEqual(library_db.runs_for(self.root, 'Creator/[1] post one'), [])
        self.assertEqual(len(library_db.runs_for(self.root, 'Creator/[2] manual post')), 1)
        self.assertIsNone(library_db.get_link(self.root, 'GONE'))
        self.assertIsNotNone(library_db.get_link(self.root, 'KEEP'))
        self.assertIsNone(library_db.tool_data_get(self.root, 'pize_pixeldrain_archives', 'legacy_manifest_extra:Pize'))

    def test_bad_stale_and_reused_tokens_are_refused(self):
        with self.assertRaises(rs.TokenError):
            rs.apply(self.root, 'nope', progress=lambda *_: None)
        _, token = self.plan_and_token()
        self.w('Other/[6] new post/c.mp4', 'c')                          # library changed after the report
        with self.assertRaises(rs.TokenError):
            rs.apply(self.root, token, progress=lambda *_: None)
        self.assertTrue(self.exists('Other/[5] post/b.mp4'))
        _, token2 = self.plan_and_token()
        rs.apply(self.root, token2, progress=lambda *_: None)
        with self.assertRaises(rs.TokenError):
            rs.apply(self.root, token2, progress=lambda *_: None)

    def test_interrupted_apply_resumes_with_the_same_token(self):
        _, token = self.plan_and_token()
        real = rs.shutil.rmtree
        calls = {'n': 0}

        def flaky(path, *a, **k):
            calls['n'] += 1
            if calls['n'] == 2:
                raise KeyboardInterrupt
            return real(path, *a, **k)
        with mock.patch.object(rs.shutil, 'rmtree', flaky):
            with self.assertRaises(KeyboardInterrupt):
                rs.apply(self.root, token, progress=lambda *_: None)
        self.assertEqual(len(os.listdir(os.path.join(self.root, '.library-data', 'previous'))), 1)
        rs.apply(self.root, token, progress=lambda *_: None)             # resumes; no second snapshot
        self.assertEqual(len(os.listdir(os.path.join(self.root, '.library-data', 'previous'))), 1)
        self.assertFalse(self.exists('Other/[5] post'))
        self.assertTrue(self.exists('Creator/[2] manual post/keep.mp4'))

    def test_tool_written_manual_plus_consolidated_is_not_a_human_marker(self):
        self.w('Pize/[9] emptied by tool/.manual', '')
        self.w('Pize/[9] emptied by tool/.consolidated', '')
        self.assertNotIn(os.path.join('Pize', '[9] emptied by tool'), rs.find_manual_folders(self.root))
        _, token = self.plan_and_token()
        rs.apply(self.root, token, progress=lambda *_: None)
        self.assertFalse(self.exists('Pize/[9] emptied by tool'))
        self.assertTrue(self.exists('Creator/[2] manual post/keep.mp4'))

    def test_marked_root_removes_nothing(self):
        self.w('.manual', '')
        self.assertEqual(rs.build_plan(self.root)['entries'], [])


if __name__ == '__main__':
    unittest.main()

"""Migration: dry run writes nothing, apply is all-or-nothing and idempotent, legacy files are moved (never deleted) into .library-data/legacy."""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import app_db  # noqa: E402
import folder_log  # noqa: E402
import library_db  # noqa: E402
import migrate_library_data as mig  # noqa: E402


def snapshot(root, include_data=False):
    """path -> sha256 of every file outside .library-data (or inside it too), to prove nothing was lost."""
    out = {}
    for dp, dn, fn in os.walk(root):
        if not include_data:
            dn[:] = [d for d in dn if d != '.library-data']
        for f in fn:
            p = os.path.join(dp, f)
            with open(p, 'rb') as fh:
                out[p] = hashlib.sha256(fh.read()).hexdigest()
    return out


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self._tmp.name)
        self.root = os.path.join(base, 'Patreon')
        self._env = os.environ.get('LIBRARY_TOOLS_HOME')
        os.environ['LIBRARY_TOOLS_HOME'] = os.path.join(base, 'home')
        library_db._roots.clear()
        folder_log._roots.clear() if hasattr(folder_log, '_roots') else None
        self._build_legacy_library()

    def tearDown(self):
        if self._env is None:
            os.environ.pop('LIBRARY_TOOLS_HOME', None)
        else:
            os.environ['LIBRARY_TOOLS_HOME'] = self._env
        self._tmp.cleanup()

    def _w(self, rel, text='', mode='w'):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode) as f:
            f.write(text)

    def _build_legacy_library(self):
        # downloader's per-folder log
        self._w('creator/post1/.folder_log.json', json.dumps([{'timestamp': '2026-01-01T00:00:00', 'script': 'fix_garbled_names'}]))
        # compressor's single log (+ a transcoded file on disk)
        self._w('creator/vids/a.mp4', 'x')
        self._w('.av1-compressor/library_log.jsonl', json.dumps(
            {'timestamp': '2026-02-01T00:00:00', 'script': 'compress_library', 'folder': 'creator/vids', 'file': 'a.mkv',
             'status': 'compressed', 'original_size': 1000, 'compressed_size': 300, 'final_file': 'a.mp4'}) + '\n' + json.dumps(
            {'timestamp': '2026-02-02T00:00:00', 'script': 'compress_library', 'folder': 'creator/vids', 'file': 'gone.mkv',
             'status': 'compressed', 'original_size': 5, 'compressed_size': 1, 'final_file': 'gone.mp4'}) + '\n')
        # history an older compressor moved away from the downloader
        self._w('.av1-compressor/legacy_folder_logs/creator/post2/.folder_log.json',
                json.dumps([{'timestamp': '2025-12-01T00:00:00', 'script': 'check_funscripts'}]))
        # Pize manifest
        self._w('pize/_pixeldrain/.manifest.json', json.dumps({
            'links': {'https://pixeldrain.com/u/ABC': {'status': 'done', 'file_id': 'ABC', 'remote_sha256': 'H',
                                                       'archive_name': 'pack.zip', 'packages': ['p1']}},
            'packages': {'p1': {'links': ['https://pixeldrain.com/u/ABC']}}, 'posts': {'x': {'status': 'emptied'}}}))
        self._w('creator/post1/video.mp4', 'v')

    def run_main(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mig.main([self.root, *args])
        return code, buf.getvalue()

    def test_dry_run_writes_nothing_at_all(self):
        before = snapshot(self.root)
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn('Dry run', out)
        self.assertIn('would import: 4 run record(s), 1 pixeldrain link(s), 2 compressed-file identity row(s)', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.library-data')))
        self.assertEqual(before, snapshot(self.root))
        self.assertFalse(os.path.exists(os.path.join(os.environ['LIBRARY_TOOLS_HOME'], 'app.db')))

    def test_apply_imports_everything_and_moves_legacy_files_into_the_data_dir(self):
        before = snapshot(self.root)
        code, out = self.run_main('--apply')
        self.assertEqual(code, 0)
        self.assertIn('moved 3 legacy file(s)', out)
        after = snapshot(self.root)
        legacy = os.path.join(self.root, '.library-data', 'legacy')
        moved = {
            os.path.join(self.root, 'creator/post1/.folder_log.json'): os.path.join(legacy, 'folder_logs/creator/post1/.folder_log.json'),
            os.path.join(self.root, '.av1-compressor/library_log.jsonl'): os.path.join(legacy, '.av1-compressor/library_log.jsonl'),
            os.path.join(self.root, '.av1-compressor/legacy_folder_logs/creator/post2/.folder_log.json'):
                os.path.join(legacy, '.av1-compressor/legacy_folder_logs/creator/post2/.folder_log.json'),
        }
        everything = snapshot(self.root, include_data=True)
        for old, new in moved.items():
            self.assertNotIn(old, after)                         # no loose log file left behind
            self.assertEqual(everything[new], before[old])       # byte-identical, just relocated
        for path in before:                                      # everything else (media, Pize manifest) untouched
            if path not in moved:
                self.assertEqual(after[path], before[path])
        with open(os.path.join(legacy, 'moved.json'), encoding='utf-8') as f:
            self.assertEqual({e['from'] for e in json.load(f)}, set(moved))
        root = self.root
        self.assertEqual(len(library_db.runs_for(root, 'creator/post1')), 1)
        self.assertEqual(len(library_db.runs_for(root, 'creator/post2')), 1)       # recovered from parked logs
        self.assertEqual(len(library_db.runs_for(root, 'creator/vids')), 2)
        link = library_db.get_link(root, 'ABC')
        self.assertEqual((link['status'], link['remote_sha256'], link['remote_name']), ('done', 'H', 'pack.zip'))
        f = library_db.find_files(root, orig_name='a.mkv')[0]
        self.assertEqual((f['cur_name'], f['orig_size'], f['cur_size'], f['codec']), ('a.mp4', 1000, 300, 'av1'))
        self.assertEqual(library_db.tool_data_get(root, 'pize_pixeldrain_archives', 'legacy_manifest_extra:pize')['posts'],
                         {'x': {'status': 'emptied'}})
        self.assertIn('compressed_file_missing', out)                            # gone.mp4 is not on disk
        self.assertEqual([l['path'] for l in app_db.libraries()], [root])

    def test_apply_twice_adds_nothing(self):
        self.run_main('--apply')
        with library_db.db(self.root) as con:
            counts = [con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('runs', 'files', 'pixeldrain_links')]
        _, out = self.run_main('--apply')
        self.assertIn('nothing new to import', out)
        self.assertNotIn('moved', out)
        with library_db.db(self.root) as con:
            self.assertEqual(counts, [con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                                      for t in ('runs', 'files', 'pixeldrain_links')])

    def test_both_tools_see_the_same_history_after_migration(self):
        self.run_main('--apply')
        d = os.path.join(self.root, 'creator', 'post1')
        folder_log._roots.clear() if hasattr(folder_log, '_roots') else None
        library_db._roots.clear()
        self.assertEqual([r['script'] for r in folder_log.read(d)], ['fix_garbled_names'])   # not shown twice
        self.assertTrue(folder_log.has_run(os.path.join(self.root, 'creator', 'post2'), 'check_funscripts'))

    def test_nested_library_data_and_bad_json_are_reported_not_guessed(self):
        os.makedirs(os.path.join(self.root, 'creator', '.library-data'))
        self._w('broken/.folder_log.json', '{not json')
        _, out = self.run_main()
        self.assertIn('nested_library_data', out)
        self.assertIn('unreadable', out)
        self.run_main('--apply')
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'broken', '.folder_log.json')))   # unread -> not moved

    def test_children_flag_gives_every_folder_its_own_database(self):
        self._w('Other/post/.folder_log.json', json.dumps([{'timestamp': '2026-03-01T00:00:00', 'script': 'check_funscripts'}]))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(mig.main([self.root, '--children', '--apply']), 0)
        for creator in ('creator', 'pize', 'Other'):
            self.assertTrue(os.path.isfile(os.path.join(self.root, creator, '.library-data', 'library.db')), creator)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.library-data')))     # the parent has none
        library_db._roots.clear()
        self.assertEqual(len(library_db.runs_for(os.path.join(self.root, 'Other'), 'post')), 1)

    def test_a_parent_library_data_is_reported(self):
        os.makedirs(os.path.join(self.root, '.library-data'))
        buf = io.StringIO()
        with redirect_stdout(buf):
            mig.main([os.path.join(self.root, 'creator')])
        self.assertIn('parent_library_data', buf.getvalue())

    def test_interrupted_apply_leaves_the_database_untouched(self):
        plan = mig.build_plan(self.root)
        plan['files'].append({'folder': 'x', 'orig_name': 'boom', 'cur_name': 'boom', 'orig_size': 1, 'cur_size': 1,
                              'codec': 'av1', 'state': 'compressed', 'bogus_extra_key_is_fine': 1})
        plan['links'].append({'link_id': 'L', 'source_url': 'u'})          # missing keys -> KeyError mid-transaction
        with self.assertRaises(KeyError):
            mig.apply_plan(plan)
        with library_db.db(self.root) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()

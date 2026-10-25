"""folder_log wrapper: legacy behaviour outside a library root, DB inside, legacy history merged and never lost."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import folder_log  # noqa: E402
import library_db  # noqa: E402


class FolderLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._tmp.name)
        self._old = os.environ.pop('FORCE_RERUN', None)
        library_db._roots.clear()

    def tearDown(self):
        if self._old is not None:
            os.environ['FORCE_RERUN'] = self._old
        self._tmp.cleanup()

    def test_no_root_uses_legacy_file_and_creates_no_db(self):
        d = os.path.join(self.base, 'plain')
        os.makedirs(d)
        folder_log.append_run(d, 's', n=1)
        self.assertTrue(folder_log.has_run(d, 's'))
        self.assertTrue(os.path.isfile(os.path.join(d, '.folder_log.json')))
        self.assertFalse(os.path.exists(os.path.join(d, '.library-data')))

    def test_declared_root_writes_db_not_files(self):
        root = library_db.use_root(os.path.join(self.base, 'lib'))
        d = os.path.join(root, 'creator', 'post')
        os.makedirs(d)
        folder_log.append_run(d, 's', n=1)
        self.assertEqual([r['n'] for r in folder_log.read(d)], [1])
        self.assertFalse(os.path.exists(os.path.join(d, '.folder_log.json')))
        self.assertFalse(folder_log.has_run(os.path.join(root, 'creator'), 's'))

    def test_legacy_history_is_merged_not_lost_and_not_duplicated(self):
        root = library_db.use_root(os.path.join(self.base, 'lib'))
        d = os.path.join(root, 'post')
        os.makedirs(d)
        legacy = [{'timestamp': '2020-01-01T00:00:00', 'script': 'old', 'x': 1}]
        with open(os.path.join(d, '.folder_log.json'), 'w') as f:
            json.dump(legacy, f)
        self.assertTrue(folder_log.has_run(d, 'old'))
        # the same record imported into the DB must not show twice
        library_db.add_run(root, 'post', 'old', legacy[0])
        folder_log.append_run(d, 'new')
        scripts = [r['script'] for r in folder_log.read(d)]
        self.assertEqual(sorted(scripts), ['new', 'old'])
        self.assertTrue(os.path.isfile(os.path.join(d, '.folder_log.json')))   # never deleted

    def test_compressor_log_is_visible(self):
        root = library_db.use_root(os.path.join(self.base, 'lib'))
        d = os.path.join(root, 'v')
        os.makedirs(d)
        os.makedirs(os.path.join(root, '.av1-compressor'))
        with open(os.path.join(root, '.av1-compressor', 'library_log.jsonl'), 'w') as f:
            f.write(json.dumps({'timestamp': 't', 'script': 'compress_library', 'folder': 'v', 'file': 'a.mp4'}) + '\n')
            f.write(json.dumps({'timestamp': 't', 'script': 'compress_library', 'folder': 'other'}) + '\n')
        recs = folder_log.read(d)
        self.assertEqual([r['file'] for r in recs], ['a.mp4'])
        self.assertTrue(folder_log.has_run(d, 'compress_library'))

    def test_logs_parked_by_old_compressor_are_found(self):
        root = library_db.use_root(os.path.join(self.base, 'lib'))
        parked = os.path.join(root, '.av1-compressor', 'legacy_folder_logs', 'post')
        os.makedirs(parked)
        with open(os.path.join(parked, '.folder_log.json'), 'w') as f:
            json.dump([{'timestamp': '1', 'script': 'fix_garbled_names'}], f)
        d = os.path.join(root, 'post')
        os.makedirs(d)
        self.assertTrue(folder_log.has_run(d, 'fix_garbled_names'))

    def test_force_rerun(self):
        root = library_db.use_root(os.path.join(self.base, 'lib'))
        folder_log.append_run(root, 's')
        os.environ['FORCE_RERUN'] = 'true'
        try:
            self.assertFalse(folder_log.has_run(root, 's'))
            folder_log.append_run(root, 's2')
            self.assertTrue([r for r in folder_log.read(root) if r.get('force_rerun')])
        finally:
            del os.environ['FORCE_RERUN']

    def test_remove_records(self):
        root = library_db.use_root(os.path.join(self.base, 'lib'))
        folder_log.append_run(root, 'a')
        folder_log.append_run(root, 'b')
        self.assertEqual(folder_log.remove_records(root, lambda r: r['script'] == 'a'), 1)
        self.assertEqual([r['script'] for r in folder_log.read(root)], ['b'])

    def test_ensure_library_creates_db_and_keeps_legacy_history(self):
        post = os.path.join(self.base, 'post'); os.makedirs(post)
        folder_log.append_run(post, 'old')                       # no library yet -> legacy json
        folder_log._migrated.clear()
        root = folder_log.ensure_library(self.base)
        self.assertEqual(root, self.base)
        # the old json was imported and filed away, not left beside the post
        self.assertFalse(os.path.exists(os.path.join(post, '.folder_log.json')))
        self.assertEqual(len(library_db.runs_for(self.base, 'post')), 1)
        self.assertTrue(os.path.isfile(library_db.db_path(self.base)))
        self.assertEqual([r['script'] for r in folder_log.read(post)], ['old'])
        folder_log.append_run(post, 'new')                       # now goes to the DB
        self.assertEqual(sorted(r['script'] for r in folder_log.read(post)), ['new', 'old'])

    def test_ensure_library_leaves_a_parent_of_libraries_alone(self):
        child = os.path.join(self.base, 'creator'); os.makedirs(child)
        library_db.use_root(child)
        library_db._roots.clear()
        self.assertIsNone(folder_log.ensure_library(self.base))
        self.assertFalse(os.path.exists(os.path.join(self.base, library_db.DATA_DIRNAME)))
        self.assertEqual(folder_log.ensure_library(child), child)    # already a library: unchanged


if __name__ == '__main__':
    unittest.main()

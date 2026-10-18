"""Run: python -m unittest discover -s tests -v   (from the repo root). Uses temp dirs only."""
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import app_paths  # noqa: E402
import library_db  # noqa: E402


def _writer(root, n):
    for i in range(n):
        library_db.add_run(root, 'f', 's', {'i': i, 'timestamp': 't'})


class LibraryDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = library_db.use_root(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_db_with_library_id_and_version(self):
        self.assertTrue(os.path.isfile(library_db.db_path(self.root)))
        self.assertTrue(library_db.library_id(self.root))
        with library_db.db(self.root) as con:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], library_db.SCHEMA_VERSION)
            self.assertEqual(con.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_library_id_stable_across_reopen(self):
        a = library_db.library_id(self.root)
        library_db.use_root(self.root)
        self.assertEqual(a, library_db.library_id(self.root))

    def test_refuses_newer_schema(self):
        con = sqlite3.connect(library_db.db_path(self.root))
        con.execute(f'PRAGMA user_version={library_db.SCHEMA_VERSION + 1}')
        con.close()
        with self.assertRaises(library_db.SchemaTooNew):
            library_db.connect(self.root)

    def test_find_root_walks_up(self):
        sub = os.path.join(self.root, 'a', 'b')
        os.makedirs(sub)
        self.assertEqual(library_db.find_root(sub), self.root)
        self.assertEqual(library_db.rel_folder(self.root, sub), os.path.join('a', 'b'))
        self.assertEqual(library_db.rel_folder(self.root, self.root), '')

    def test_runs_roundtrip_remove_and_order(self):
        library_db.add_run(self.root, 'x', 's1', {'script': 's1', 'n': 1})
        library_db.add_run(self.root, 'x', 's2', {'script': 's2', 'n': 2})
        library_db.add_run(self.root, 'y', 's1', {'script': 's1', 'n': 3})
        self.assertEqual([r['n'] for r in library_db.runs_for(self.root, 'x')], [1, 2])
        self.assertEqual(library_db.remove_runs(self.root, 'x', lambda r: r['script'] == 's1'), 1)
        self.assertEqual([r['n'] for r in library_db.runs_for(self.root, 'x')], [2])
        self.assertEqual(len(library_db.runs_for(self.root, 'y')), 1)

    def test_per_tool_migration_runs_once(self):
        stmts = ['CREATE TABLE IF NOT EXISTS tool_a_things (id INTEGER PRIMARY KEY, v TEXT)']
        self.assertTrue(library_db.register_migration(self.root, 'tool_a', 1, stmts))
        self.assertFalse(library_db.register_migration(self.root, 'tool_a', 1, stmts))
        # another tool's track is independent
        self.assertTrue(library_db.register_migration(self.root, 'tool_b', 1, stmts))

    def test_tool_data_and_meta(self):
        library_db.tool_data_set(self.root, 'tool_a', 'k', {'a': [1, 2]})
        self.assertEqual(library_db.tool_data_get(self.root, 'tool_a', 'k'), {'a': [1, 2]})
        self.assertIsNone(library_db.tool_data_get(self.root, 'tool_a', 'nope'))
        library_db.set_meta(self.root, 'x', '1')
        library_db.set_meta(self.root, 'x', '2')
        self.assertEqual(library_db.get_meta(self.root, 'x'), '2')

    def test_write_transaction_rolls_back(self):
        with self.assertRaises(ValueError):
            with library_db.db(self.root, write=True) as con:
                con.execute("INSERT INTO meta(key, value) VALUES('boom', '1')")
                raise ValueError
        self.assertIsNone(library_db.get_meta(self.root, 'boom'))

    def test_two_processes_can_write_concurrently(self):
        ctx = multiprocessing.get_context('spawn')
        procs = [ctx.Process(target=_writer, args=(self.root, 40)) for _ in range(3)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(len(library_db.runs_for(self.root, 'f')), 120)

    def test_files_and_links_helpers(self):
        fid = library_db.add_file(self.root, 't', folder='a', orig_name='x.mkv', cur_name='x.mkv', orig_size=10,
                                  pixeldrain_id='P1', sha256='h')
        library_db.update_file(self.root, fid, 't', cur_name='x.mp4', cur_size=4, state='compressed')
        rows = library_db.find_files(self.root, pixeldrain_id='P1')
        self.assertEqual([(r['orig_name'], r['cur_name'], r['cur_size'], r['state']) for r in rows],
                         [('x.mkv', 'x.mp4', 4, 'compressed')])
        self.assertEqual(library_db.find_files(self.root, orig_name='nope'), [])
        library_db.upsert_link(self.root, 'P1', 't', status='pending', remote_size=10)
        library_db.upsert_link(self.root, 'P1', 'u', status='done', file_id=fid)
        link = library_db.get_link(self.root, 'P1')
        self.assertEqual((link['status'], link['remote_size'], link['file_id'], link['updated_by']), ('done', 10, fid, 'u'))
        self.assertIsNone(library_db.get_link(self.root, 'nope'))
        with self.assertRaises(ValueError):
            library_db.add_file(self.root, 't', bogus=1)
        with self.assertRaises(ValueError):
            library_db.find_files(self.root, **{'x; DROP TABLE files': 1})

    def test_all_shared_tables_exist(self):
        with library_db.db(self.root) as con:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ('meta', 'schema_migrations', 'tool_data', 'runs', 'files', 'pixeldrain_links',
                  'discord_messages', 'markers', 'drift'):
            self.assertIn(t, names)


class AppPathsTests(unittest.TestCase):
    def test_override_env(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get('LIBRARY_TOOLS_HOME')
            os.environ['LIBRARY_TOOLS_HOME'] = os.path.join(d, 'home')
            try:
                self.assertEqual(app_paths.app_home(), os.path.join(d, 'home'))
                self.assertTrue(os.path.isdir(app_paths.profiles_dir('discord')))
                self.assertEqual(app_paths.app_db_path(), os.path.join(d, 'home', 'app.db'))
            finally:
                if old is None:
                    del os.environ['LIBRARY_TOOLS_HOME']
                else:
                    os.environ['LIBRARY_TOOLS_HOME'] = old


class SiblingParityTests(unittest.TestCase):
    """library_db.py / app_paths.py are shared verbatim with the AV1 compressor repo."""

    def test_identical_to_compressor_copy_when_present(self):
        sibling = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'Av1LibraryCompression', 'scripts')
        for name in ('library_db.py', 'app_paths.py'):
            other = os.path.join(sibling, name)
            if not os.path.isfile(other):
                self.skipTest(f'{name} not present in sibling repo (yet)')
            with open(os.path.join(os.path.dirname(HERE), 'scripts', name), 'rb') as a, open(other, 'rb') as b:
                self.assertEqual(a.read(), b.read(), f'{name} drifted from the compressor copy')


if __name__ == '__main__':
    unittest.main()

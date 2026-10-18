"""App-level data now lives in the user's data dir. Legacy repo-root files are COPIED in once and never touched.

Uses a fake repo dir + a temp LIBRARY_TOOLS_HOME, so the real profiles/passwords are never read or written."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import action_log  # noqa: E402
import app_db  # noqa: E402
import app_paths  # noqa: E402
import creator_db  # noqa: E402
import creator_profiles  # noqa: E402
import legacy_app_data  # noqa: E402


class AppDataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(base, 'repo')
        self.home = os.path.join(base, 'home')
        os.makedirs(self.repo)
        self._saved = {k: os.environ.get(k) for k in ('LIBRARY_TOOLS_HOME', 'CREATOR_DB_PATH', 'ACTION_LOG_POINTER_PATH')}
        os.environ['LIBRARY_TOOLS_HOME'] = self.home
        os.environ.pop('CREATOR_DB_PATH', None)
        os.environ.pop('ACTION_LOG_POINTER_PATH', None)
        self._repo_dir = legacy_app_data.REPO_DIR
        self._override = creator_db._DB_OVERRIDE
        legacy_app_data.REPO_DIR = self.repo
        creator_db._DB_OVERRIDE = None

    def tearDown(self):
        legacy_app_data.REPO_DIR = self._repo_dir
        creator_db._DB_OVERRIDE = self._override
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_profiles_import_legacy_once_and_leave_file(self):
        path = os.path.join(self.repo, '.creators.json')
        with open(path, 'w') as f:
            json.dump({'pize': {'discord': {'guild_id': '1', 'channel_id': '2'}}}, f)
        self.assertEqual(creator_profiles.get('pize')['discord']['channel_id'], '2')
        creator_profiles.set_discord_channel('other', '3', '4')
        # editing the old file afterwards must not resurrect or overwrite anything
        with open(path, 'w') as f:
            json.dump({'pize': {'discord': {'guild_id': 'X', 'channel_id': 'X'}}}, f)
        self.assertEqual(creator_profiles.get('pize')['discord']['channel_id'], '2')
        self.assertEqual(creator_profiles.get('other')['discord']['guild_id'], '3')
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.isfile(app_paths.app_db_path()))

    def test_first_save_does_not_hide_legacy_profiles(self):
        with open(os.path.join(self.repo, '.creators.json'), 'w') as f:
            json.dump({'old': {'x': 1}}, f)
        creator_profiles.save({'old': {'x': 1}, 'new': {'y': 2}})
        self.assertEqual(sorted(creator_profiles.load()), ['new', 'old'])

    def test_creator_db_imports_legacy_passwords_once(self):
        legacy = os.path.join(self.repo, '.creator_data.db')
        con = sqlite3.connect(legacy)
        con.execute('CREATE TABLE passwords (creator_key TEXT NOT NULL, password TEXT NOT NULL, source TEXT NOT NULL '
                    "DEFAULT 'discord', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, "
                    'confirmed_working INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (creator_key, password))')
        con.execute("INSERT INTO passwords VALUES('pize','pw-old','discord','2026-01-01','2026-01-02',1)")
        con.commit()
        con.close()
        self.assertIn('pw-old', creator_db.get_password_history('pize'))
        creator_db.record_passwords('pize', ['pw-new'])
        self.assertEqual(sorted(creator_db.get_password_history('pize')), ['pw-new', 'pw-old'])
        self.assertTrue(os.path.isfile(legacy))
        # lives inside the shared app database
        con = app_db.connect()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM passwords").fetchone()[0], 2)
        con.close()

    def test_creator_db_override_path_skips_import(self):
        creator_db._DB_OVERRIDE = os.path.join(self.repo, 'throwaway.db')
        creator_db.record_passwords('pize', ['p'])
        self.assertFalse(os.path.exists(app_paths.app_db_path()))

    def test_pointer_copied_once_and_override_wins(self):
        with open(os.path.join(self.repo, '.last_action_pointer.json'), 'w') as f:
            json.dump({'root_dir': '/somewhere'}, f)
        p = action_log._pointer_path()
        self.assertTrue(p.startswith(self.home))
        self.assertEqual(action_log._resolve_root_dir(None), '/somewhere')
        os.environ['ACTION_LOG_POINTER_PATH'] = os.path.join(self.repo, 'x.json')
        self.assertEqual(action_log._pointer_path(), os.path.join(self.repo, 'x.json'))

    def test_browser_profile_copied_without_singleton_locks(self):
        src = os.path.join(self.repo, '.discord_browser_profile', 'Default')
        os.makedirs(src)
        with open(os.path.join(src, 'Cookies'), 'w') as f:
            f.write('session')
        os.symlink('host-1234', os.path.join(self.repo, '.discord_browser_profile', 'SingletonLock'))
        dest = legacy_app_data.profile_dir('discord', '.discord_browser_profile')
        self.assertEqual(dest, os.path.join(self.home, 'profiles', 'discord'))
        self.assertTrue(os.path.isfile(os.path.join(dest, 'Default', 'Cookies')))
        self.assertFalse(os.path.lexists(os.path.join(dest, 'SingletonLock')))
        self.assertTrue(os.path.isdir(src))                       # original untouched
        self.assertEqual(legacy_app_data.profile_dir('discord', '.discord_browser_profile'), dest)   # idempotent

    def test_library_registry_tracks_moves(self):
        app_db.register_library('abc', '/old/path', 'main')
        app_db.register_library('abc', '/new/path')
        libs = app_db.libraries()
        self.assertEqual([(l['path'], l['nickname']) for l in libs], [('/new/path', 'main')])


if __name__ == '__main__':
    unittest.main()

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import browser_session as bs


class FakeDriver:
    def __init__(self):
        self.handles = ['main']
        self.current = 'main'
        self.dead = False
        self.quit_called = False
        self.switch_to = self      # selenium's driver.switch_to.window()/new_window() land on this object
        self._n = 0

    @property
    def window_handles(self):
        if self.dead:
            raise RuntimeError('gone')
        return list(self.handles)

    @property
    def current_window_handle(self):
        return self.current

    def new_window(self, kind):
        self._n += 1
        h = f'tab{self._n}'
        self.handles.append(h)
        self.current = h

    def window(self, h):
        self.current = h

    def close(self):
        self.handles.remove(self.current)

    def quit(self):
        self.quit_called = True
        self.dead = True


class TabTests(unittest.TestCase):
    def setUp(self):
        self.d = FakeDriver()
        bs.register(self.d)

    def test_borrow_and_return(self):
        bs.use_tab('discord')
        self.assertEqual(self.d.current, 'tab1')
        bs.back_to_main()
        self.assertEqual(self.d.current, 'main')
        bs.use_tab('discord')                      # same tab again, not a new one
        self.assertEqual(self.d.current, 'tab1')
        self.assertEqual(len(self.d.handles), 2)

    def test_two_borrowers_keep_separate_tabs_and_one_main(self):
        bs.use_tab('discord')
        bs.use_tab('joigang')
        self.assertEqual(len(self.d.handles), 3)
        bs.back_to_main()
        self.assertEqual(self.d.current, 'main')

    def test_close_tab_returns_to_main(self):
        bs.use_tab('discord')
        bs.close_tab('discord')
        self.assertEqual(self.d.handles, ['main'])
        self.assertEqual(self.d.current, 'main')
        self.assertFalse(self.d.quit_called)

    def test_dead_browser_is_forgotten(self):
        self.d.dead = True
        self.assertIsNone(bs.current())
        bs.back_to_main()                          # must not raise

    def test_profile_dir_is_in_app_home_and_seeded_once(self):
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get('LIBRARY_TOOLS_HOME')
            os.environ['LIBRARY_TOOLS_HOME'] = home
            try:
                seed = os.path.join(home, 'profiles', 'discord')
                os.makedirs(seed)
                open(os.path.join(seed, 'Cookies'), 'w').close()
                open(os.path.join(seed, 'SingletonLock'), 'w').close()
                dest = bs.profile_dir()
                self.assertEqual(dest, os.path.join(home, 'profiles', 'app'))
                self.assertTrue(os.path.exists(os.path.join(dest, 'Cookies')))
                self.assertFalse(os.path.exists(os.path.join(dest, 'SingletonLock')))
            finally:
                if old is None:
                    os.environ.pop('LIBRARY_TOOLS_HOME', None)
                else:
                    os.environ['LIBRARY_TOOLS_HOME'] = old


class ResetTests(unittest.TestCase):
    def test_false_wipes_profile_once_and_flips_back_to_true(self):
        with tempfile.TemporaryDirectory() as home:
            env_file = os.path.join(home, '.env')
            with open(env_file, 'w') as f:
                f.write('A=1\nBROWSER_PERSISTENT_PROFILE=false\nB=2\n')
            saved = (os.environ.get('LIBRARY_TOOLS_HOME'), os.environ.get('BROWSER_PERSISTENT_PROFILE'), bs._ENV_PATH)
            os.environ['LIBRARY_TOOLS_HOME'] = home
            os.environ['BROWSER_PERSISTENT_PROFILE'] = 'false'
            bs._ENV_PATH = env_file
            try:
                dest = os.path.join(home, 'profiles', 'app')
                os.makedirs(dest)
                open(os.path.join(dest, 'Cookies'), 'w').close()
                self.assertEqual(bs.profile_dir(), dest)
                self.assertFalse(os.path.exists(os.path.join(dest, 'Cookies')))      # wiped
                self.assertEqual(os.environ['BROWSER_PERSISTENT_PROFILE'], 'true')
                self.assertEqual(open(env_file).read(), 'A=1\nBROWSER_PERSISTENT_PROFILE=true\nB=2\n')
                open(os.path.join(dest, 'Cookies'), 'w').close()
                bs.profile_dir()                                                      # second launch: kept
                self.assertTrue(os.path.exists(os.path.join(dest, 'Cookies')))
            finally:
                home_old, flag_old, bs._ENV_PATH = saved
                for k, v in (('LIBRARY_TOOLS_HOME', home_old), ('BROWSER_PERSISTENT_PROFILE', flag_old)):
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


if __name__ == '__main__':
    unittest.main()

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


if __name__ == '__main__':
    unittest.main()

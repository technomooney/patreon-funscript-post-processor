import base64
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'scripts')
sys.path.insert(0, SCRIPTS)

import discord_history as dh  # noqa: E402
import discord_parser  # noqa: E402
import discord_store as store  # noqa: E402
import library_db  # noqa: E402


def msg(i, text='', **kw):
    return dict({'id': str(i), 'ts': '', 'author': 'a', 'text': text, 'raw': text, 'hrefs': []}, **kw)


class FakeChannel:
    """A virtualized list: shows a window of `window` messages; each scroll_up moves the window to older ones."""

    def __init__(self, total=100, window=20, step=15):
        self.all = [msg(1000 + i) for i in range(total)]     # oldest first
        self.window, self.step = window, step
        self.top = max(0, total - window)
        self.scrolls = 0

    def read_rows(self):
        return list(self.all[self.top:self.top + self.window])

    def scroll_up(self):
        self.scrolls += 1
        self.top = max(0, self.top - self.step)


class CollectHistoryTests(unittest.TestCase):
    def run_collect(self, ch, **kw):
        return dh.collect_history(ch.read_rows, ch.scroll_up, **kw)

    def test_reads_whole_channel_newest_first(self):
        ch = FakeChannel(total=100)
        rows = self.run_collect(ch)
        self.assertEqual(len(rows), 100)
        self.assertEqual([r['id'] for r in rows][:2], ['1099', '1098'])
        self.assertEqual(rows[-1]['id'], '1000')

    def test_stops_at_top_via_stall_limit(self):
        ch = FakeChannel(total=10, window=20)
        self.run_collect(ch, stall_limit=3)
        self.assertEqual(ch.scrolls, 3)

    def test_max_messages_keeps_newest(self):
        rows = self.run_collect(FakeChannel(total=100), max_messages=30)
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]['id'], '1099')
        self.assertEqual(rows[-1]['id'], '1070')

    def test_since_id_returns_only_newer_and_stops_early(self):
        ch = FakeChannel(total=100)
        rows = self.run_collect(ch, since_id='1080')
        self.assertEqual([r['id'] for r in rows][-1], '1081')
        self.assertEqual(len(rows), 19)
        self.assertLess(ch.scrolls, 3)          # did not walk to the top of the channel

    def test_since_id_newest_returns_nothing(self):
        self.assertEqual(self.run_collect(FakeChannel(total=50), since_id='1049'), [])

    def test_ignores_rows_without_numeric_id(self):
        rows = dh.collect_history(lambda: [msg('abc'), msg(5), {'text': 'no id'}], lambda: None, stall_limit=1)
        self.assertEqual([r['id'] for r in rows], ['5'])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = library_db.use_root(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_incremental_bookkeeping(self):
        self.assertIsNone(store.newest_stored_id(self.root, 'c'))
        self.assertEqual(store.save_messages(self.root, 'c', [msg(10, 'a'), msg(30, 'b'), msg(20, 'c')]), 3)
        self.assertEqual(store.newest_stored_id(self.root, 'c'), '30')
        self.assertEqual([m['message_id'] for m in store.unprocessed(self.root, 'c')], ['10', '20', '30'])
        store.mark_processed(self.root, 'c', 20)
        self.assertEqual([m['message_id'] for m in store.unprocessed(self.root, 'c')], ['10', '30'])
        # re-saving keeps the processed flag and adds only genuinely new messages
        self.assertEqual(store.save_messages(self.root, 'c', [msg(20, 'c'), msg(40, 'd')]), 1)
        self.assertEqual([m['message_id'] for m in store.unprocessed(self.root, 'c')], ['10', '30', '40'])
        # creators are separate
        self.assertIsNone(store.newest_stored_id(self.root, 'other'))

    def test_annotated_hidden_links_survive_storage(self):
        url = 'https://pixeldrain.com/l/AbCd1234'
        enc = base64.b64encode(url.encode()).decode()
        store.save_messages(self.root, 'c', discord_parser.annotate([msg(1, text='click to reveal', raw='click to reveal ' + enc)]))
        self.assertEqual(store.unprocessed(self.root, 'c')[0]['decoded'], [url])

    def test_decode_links_reads_spoiler_text_too(self):
        url = 'https://pixeldrain.com/l/AbCd1234'
        enc = base64.b64encode(url.encode()).decode()
        m = msg(1, text='click to reveal', raw='click to reveal ' + enc)
        self.assertEqual([f['href'] for f in discord_parser.decode_links(m)], [url])


class PasswordParsingSeparationTests(unittest.TestCase):
    def test_passwords_parse_without_a_browser_and_from_stored_messages(self):
        import tempfile
        import sys
        import creator_db
        import discord_passwords as dpw
        import subprocess
        out = subprocess.run([sys.executable, '-I', '-c',
                              'import sys; sys.path.insert(0, %r); import discord_passwords, discord_parser, '
                              'discord_store, discord_history; print("selenium" in sys.modules)' % SCRIPTS],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), 'False', out.stderr)
        self.assertEqual(dpw.passwords_from_texts(['The password is currently set as pize2026080101.'])[0],
                         ['pize2026080101'])
        with tempfile.TemporaryDirectory() as tmp:
            root = library_db.use_root(os.path.join(os.path.realpath(tmp), 'lib'))
            store.save_messages(root, 'c', [
                {'id': '1', 'ts': '', 'author': 'a', 'text': 'pw: oldpass99', 'raw': '', 'hrefs': []},
                {'id': '2', 'ts': '', 'author': 'a', 'text': 'The password is currently set as newpass77', 'raw': '', 'hrefs': []}])
            recorded = []
            old = creator_db.record_passwords
            creator_db.record_passwords = lambda k, p: recorded.append((k, list(p)))
            try:
                got = discord_parser.record_passwords_from_stored(root, 'c')
            finally:
                creator_db.record_passwords = old
            self.assertEqual(got, ['newpass77', 'oldpass99'])
            self.assertEqual(recorded, [('c', ['newpass77', 'oldpass99'])])


class PasswordScanTests(unittest.TestCase):
    """password_history reads the whole chat once, then only what is newer; older passwords stay on file."""

    def setUp(self):
        import creator_db
        self._tmp = tempfile.TemporaryDirectory()
        self._old = (creator_db._DB_OVERRIDE, dh.fetch_channel_history)
        creator_db._DB_OVERRIDE = os.path.join(self._tmp.name, 'c.db')
        self.calls = []
        chat = [msg(i, f'The password is currently set as pass{i:04d}') for i in range(1, 121)]   # 120 passwords

        def fake(creator, since_message_id=None, max_messages=None, force=False):
            self.calls.append((since_message_id, max_messages))
            since = int(since_message_id) if since_message_id else 0
            rows = [m for m in reversed(chat) if int(m['id']) > since]
            return rows[:max_messages] if max_messages else rows
        dh.fetch_channel_history = fake
        self.chat = chat

    def tearDown(self):
        import creator_db
        creator_db._DB_OVERRIDE, dh.fetch_channel_history = self._old
        self._tmp.cleanup()

    def test_no_cap_then_incremental_then_rescan(self):
        import creator_db
        got = dh.password_history('c')
        self.assertEqual(len(got), 120)                       # all 120, not the newest 50
        self.assertEqual(self.calls[-1], (None, None))
        self.assertEqual(creator_db.get_discord_scan('c'), '120')

        self.chat.append(msg(121, 'The password is currently set as pass0121'))
        got = dh.password_history('c')
        self.assertEqual(self.calls[-1], ('120', None))        # only newer messages were read
        self.assertEqual(len(got), 121)                        # but older passwords are still on file
        self.assertEqual(creator_db.get_discord_scan('c'), '121')

        dh.password_history('c', rescan=True)
        self.assertEqual(self.calls[-1], (None, None))

    def test_capped_peek_never_moves_the_position(self):
        import creator_db
        dh.password_history('c', max_messages=5)
        self.assertIsNone(creator_db.get_discord_scan('c'))

    def test_unreadable_channel_keeps_what_is_on_file_and_position(self):
        import creator_db
        dh.password_history('c')
        dh.fetch_channel_history = lambda *a, **k: None
        self.assertEqual(len(dh.password_history('c')), 120)
        self.assertEqual(creator_db.get_discord_scan('c'), '120')


class AgeGateTests(unittest.TestCase):
    class Button:
        def __init__(self, shown=True):
            self.shown, self.clicked = shown, False

        def is_displayed(self):
            return self.shown

    class Driver:
        def __init__(self, text, buttons):
            self.text, self.buttons, self.clicked = text, buttons, []

        def execute_script(self, script, *args):
            if args:
                args[0].clicked = True
                return None
            return self.text

        def find_elements(self, by, xpath):
            return self.buttons

    def test_clicks_continue_only_on_the_age_restricted_prompt(self):
        import discord_browser
        btn = self.Button()
        self.assertTrue(discord_browser.dismiss_age_gate(self.Driver('This channel is age-restricted. Continue', [btn])))
        self.assertTrue(btn.clicked)
        other = self.Button()
        self.assertFalse(discord_browser.dismiss_age_gate(self.Driver('Welcome back! Continue', [other])))
        self.assertFalse(other.clicked)
        hidden = self.Button(shown=False)
        self.assertFalse(discord_browser.dismiss_age_gate(self.Driver('NSFW channel', [hidden])))
        self.assertFalse(hidden.clicked)


class OpenChannelTests(unittest.TestCase):
    """The channel is retried once through the Discord home page when it comes up empty."""

    class Driver:
        def __init__(self, shows_after_home):
            self.visits, self.shows_after_home = [], shows_after_home

        def get(self, url):
            self.visits.append(url)

        def execute_script(self, *a, **k):
            return ''

        def find_elements(self, by, xpath):
            ready = (not self.shows_after_home) or any('@me' in v for v in self.visits)
            return [object()] if ready else []

    def run_open(self, driver):
        import discord_browser as db
        import creator_profiles
        saved = (db._discord_driver, db.ensure_login, db.settle, creator_profiles.get_discord, db.time.sleep, db.time.time)
        clock = iter(range(0, 10000, 5))
        db._discord_driver = lambda: driver
        db.ensure_login = lambda d: True
        db.settle = lambda d, s=None: driver.visits.append('settled')
        creator_profiles.get_discord = lambda c, ch=None: {'guild_id': '1', 'channel_id': '2'}
        db.time.sleep = lambda s: None
        db.time.time = lambda: next(clock)          # fake clock so the 20 s waits finish instantly
        try:
            return db.open_channel('c')
        finally:
            (db._discord_driver, db.ensure_login, db.settle, creator_profiles.get_discord,
             db.time.sleep, db.time.time) = saved

    def test_loads_first_time(self):
        d = self.Driver(shows_after_home=False)
        self.assertIsNotNone(self.run_open(d))
        self.assertEqual(d.visits, ['https://discord.com/channels/1/2'])

    def test_retries_once_via_home_page_when_the_channel_comes_up_empty(self):
        d = self.Driver(shows_after_home=True)
        self.assertIsNotNone(self.run_open(d))
        self.assertEqual(d.visits, ['https://discord.com/channels/1/2', 'https://discord.com/channels/@me', 'settled',
                                    'https://discord.com/channels/1/2'])

    def test_gives_up_after_the_retry(self):
        class Never(self.Driver):
            def find_elements(self, by, xpath):
                return []
        self.assertIsNone(self.run_open(Never(False)))


class BrowserDefaultsTests(unittest.TestCase):
    def test_software_rendering_is_the_default_and_can_be_turned_off(self):
        import browser_defaults as bd
        names = ('BROWSER_DISABLE_GPU', 'DISCORD_DISABLE_GPU')
        saved = {n: os.environ.pop(n, None) for n in names}
        try:
            self.assertTrue(bd.gpu_disabled())
            os.environ['BROWSER_DISABLE_GPU'] = 'false'
            self.assertFalse(bd.gpu_disabled())
            self.assertFalse(bd.gpu_disabled('DISCORD_DISABLE_GPU'))
            os.environ['DISCORD_DISABLE_GPU'] = 'true'              # the specific setting wins for its own browser
            self.assertTrue(bd.gpu_disabled('DISCORD_DISABLE_GPU'))
            self.assertFalse(bd.gpu_disabled())

            class Opts:
                args = []

                def add_argument(self, a):
                    self.args.append(a)
            bd.apply(Opts(), 'DISCORD_DISABLE_GPU')
            self.assertEqual(Opts.args, ['--disable-gpu'])
        finally:
            for n, v in saved.items():
                os.environ.pop(n, None)
                if v is not None:
                    os.environ[n] = v


class ProgressTests(unittest.TestCase):
    def test_collect_history_reports_messages_seen_after_each_scroll(self):
        import progress as pr
        seen = []
        ch = [[msg(3), msg(4)], [msg(1), msg(2), msg(3), msg(4)], [msg(1), msg(2), msg(3), msg(4)]]
        calls = iter(ch + [ch[-1]] * 20)
        dh.collect_history(lambda: next(calls), lambda: None, stall_limit=1, progress=seen.append)
        self.assertEqual(seen[:2], [2, 4])

    def test_sizes_read_naturally(self):
        import progress as pr
        self.assertEqual([pr.size(n) for n in (None, 500, 2048, 5 * 1024 ** 3)], ['? size', '500 B', '2.0 KB', '5.0 GB'])


if __name__ == '__main__':
    unittest.main()

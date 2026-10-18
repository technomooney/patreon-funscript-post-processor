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


if __name__ == '__main__':
    unittest.main()

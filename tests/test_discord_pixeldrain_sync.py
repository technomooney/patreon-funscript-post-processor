import base64
import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'scripts')
sys.path.insert(0, SCRIPTS)

import discord_history as dh  # noqa: E402
import discord_store as store  # noqa: E402
import library_db  # noqa: E402
import pixeldrain_sync as ps  # noqa: E402
import pixeldrain_util as pdu  # noqa: E402

import discord_pixeldrain_sync as plug  # noqa: E402


def b64(s):
    return base64.b64encode(s.encode()).decode()


def msg(i, text='', hrefs=()):
    return {'id': str(i), 'ts': '', 'author': 'a', 'text': text, 'raw': text, 'hrefs': list(hrefs)}


class DiscordPluginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lib = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        self.base = os.path.join(self.lib, 'creator')
        os.makedirs(self.base)
        self.calls = []
        self.failing = False
        self._old = (ps.sync_urls, pdu.list_entries, pdu.download_file)

        def fake_sync(base_path, urls, dest='', dry_run=False, interactive=False):
            self.calls.append((list(urls), dest, dry_run))
            r = {'new': len(urls), 'have': 0, 'ambiguous': 0, 'unreadable': 0, 'adopted': 0,
                 'downloaded': 0 if self.failing else len(urls), 'failed': len(urls) if self.failing else 0}
            return r
        ps.sync_urls = fake_sync

    def tearDown(self):
        ps.sync_urls, pdu.list_entries, pdu.download_file = self._old
        self._tmp.cleanup()

    def run_pass(self, rows, **kw):
        seen = {}
        def fetch(creator, since_message_id=None):
            seen['since'] = since_message_id
            return rows
        out = plug.sync_channel(self.base, fetch=fetch, **kw)
        return out, seen

    def test_base64_and_visible_links_are_collected_and_synced(self):
        hidden = 'https://pixeldrain.com/l/Hidden01'
        visible = 'https://pixeldrain.com/u/Visible1'
        rows = [msg(30, hrefs=[visible, 'https://discord.com/channels/1/2']), msg(10, text=f'drop: {b64(hidden)}'),
                msg(20, text='hello')]
        out, seen = self.run_pass(rows)
        self.assertIsNone(seen['since'])
        self.assertEqual(self.calls[0][0], [hidden, visible])           # oldest message first
        self.assertEqual(self.calls[0][1], 'discord')
        self.assertEqual(out['failed'], 0)
        self.assertEqual(store.unprocessed(self.lib, 'creator'), [])

    def test_second_pass_is_incremental_and_does_not_resync_old_links(self):
        self.run_pass([msg(10, hrefs=['https://pixeldrain.com/u/Old00001'])])
        self.calls.clear()
        out, seen = self.run_pass([msg(20, text='no links here')])
        self.assertEqual(seen['since'], '10')
        self.assertEqual(self.calls, [])
        self.assertEqual(out['links'], 0)

    def test_failed_links_keep_messages_unprocessed_for_retry(self):
        self.failing = True
        self.run_pass([msg(10, hrefs=['https://pixeldrain.com/u/Fail0001'])])
        self.assertEqual(len(store.unprocessed(self.lib, 'creator')), 1)
        self.failing = False
        self.calls.clear()
        self.run_pass([])                                               # nothing new, but the old message is retried
        self.assertEqual(self.calls[0][0], ['https://pixeldrain.com/u/Fail0001'])
        self.assertEqual(store.unprocessed(self.lib, 'creator'), [])

    def test_dry_run_stores_and_downloads_nothing(self):
        rows = [msg(10, text=b64('https://pixeldrain.com/l/Hidden01'))]
        out, _ = self.run_pass(rows, dry_run=True)
        self.assertEqual(self.calls[0][2], True)
        self.assertIsNone(store.newest_stored_id(self.lib, 'creator'))

    def test_unreadable_channel_changes_nothing(self):
        self.assertIsNone(plug.sync_channel(self.base, fetch=lambda c, since_message_id=None: None))
        self.assertEqual(self.calls, [])

    def test_two_channels_keep_separate_positions_and_flags(self):
        seen = {}

        def fetch(creator, since_message_id=None, channel=None):
            seen[channel] = since_message_id
            return {'archive': [msg(500, hrefs=['https://pixeldrain.com/l/Arch0001'])],
                    'secrets': [msg(7, hrefs=['https://pixeldrain.com/u/Secret01'])]}[channel]
        plug.sync_channel(self.base, dest='archive', fetch=fetch, channel='archive')
        plug.sync_channel(self.base, dest='discord', fetch=fetch, channel='secrets')
        self.assertEqual(seen, {'archive': None, 'secrets': None})       # secrets did not inherit archive's id 500
        self.assertEqual([c[1] for c in self.calls], ['archive', 'discord'])
        plug.sync_channel(self.base, dest='archive', fetch=lambda c, since_message_id=None, channel=None: seen.update(
            second=since_message_id) or [], channel='archive')
        self.assertEqual(seen['second'], '500')
        self.assertIsNone(store.newest_stored_id(self.lib, 'creator'))   # the original channel is untouched
        self.assertEqual(store.newest_stored_id(self.lib, 'creator#secrets'), '7')

    def test_latest_only_uses_the_newest_post_and_falls_back_when_it_is_unreadable(self):
        rows = [msg(10, hrefs=['https://pixeldrain.com/d/Old00001']), msg(20, hrefs=['https://pixeldrain.com/d/New00001'])]
        out, _ = self.run_pass(rows, latest_only=True)
        self.assertEqual([c[0] for c in self.calls], [['https://pixeldrain.com/d/New00001']])   # old one never touched
        self.assertEqual(store.unprocessed(self.lib, 'creator'), [])                          # both marked done

        self.calls.clear()
        self.failing = True                                    # every sync of the newest fails ...
        results = iter([True, False])
        real = ps.sync_urls

        def flaky(base_path, urls, **kw):
            self.failing = next(results)
            return real(base_path, urls, **kw)
        ps.sync_urls = flaky
        self.run_pass([msg(30, hrefs=['https://pixeldrain.com/d/Old00002']),
                       msg(40, hrefs=['https://pixeldrain.com/d/Bad00002'])], latest_only=True)
        self.assertEqual([c[0][0] for c in self.calls], ['https://pixeldrain.com/d/Bad00002', 'https://pixeldrain.com/d/Old00002'])

    def test_only_pixeldrain_lists_files_and_folder_shares_are_synced(self):
        self.run_pass([msg(10, hrefs=['https://example.com/a', 'https://pixeldrain.com/d/Share001',
                                      'https://pixeldrain.com/api/x'])])
        self.assertEqual(self.calls[0][0], ['https://pixeldrain.com/d/Share001'])


if __name__ == '__main__':
    unittest.main()

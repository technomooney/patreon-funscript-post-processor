"""Channel jobs: configuration, dispatch, and the attachment / site-link methods. Discord and the network are faked."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import creator_config  # noqa: E402
import discord_attachments as da  # noqa: E402
import discord_channels as dcs  # noqa: E402
import discord_site_links as dsl  # noqa: E402
import discord_store as store  # noqa: E402
import library_db  # noqa: E402


def msg(i, text='', hrefs=()):
    return {'id': str(i), 'ts': '', 'author': 'a', 'text': text, 'raw': text, 'hrefs': list(hrefs)}


def att(chan, aid, name, sig='ex=1'):
    return f'https://cdn.discordapp.com/attachments/{chan}/{aid}/{name}?{sig}'


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lib = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        self.base = os.path.join(self.lib, 'creator')
        os.makedirs(self.base)

    def tearDown(self):
        self._tmp.cleanup()


class JobConfigTests(Base):
    def test_jobs_are_validated_filled_in_and_legacy_flag_still_works(self):
        self.assertEqual(dcs.jobs(self.base), [])
        creator_config.save(self.base, {'discord_channels': [
            {'channel': 'archive', 'method': 'pixeldrain_links', 'mode': 'latest'},
            {'channel': 'files', 'method': 'attachments', 'dir': '!up', 'exts': ['.funscript']},
            {'channel': 'x', 'method': 'bogus'}, 'junk', {'channel': 'pw', 'method': 'passwords'}]})
        got = dcs.jobs(self.base)
        self.assertEqual([j['method'] for j in got], ['pixeldrain_links', 'attachments', 'passwords'])
        self.assertEqual(got[0], {'channel': 'archive', 'method': 'pixeldrain_links', 'dir': '!archive', 'mode': 'latest'})
        self.assertEqual(got[1]['exts'], ['.funscript'])
        self.assertEqual(dcs.dirs_for(got, 'latest'), ['!archive'])
        creator_config.save(self.base, {'discord_pixeldrain_links': 'extra'})
        self.assertEqual(dcs.jobs(self.base), [{'channel': None, 'method': 'pixeldrain_links', 'dir': 'extra', 'mode': 'every'}])

    def test_run_jobs_dispatches_each_method_with_its_own_settings(self):
        import discord_pixeldrain_sync as dps
        import discord_history as dh
        calls = []
        old = (dps.sync_channel, da.sync, dsl.sync, dh.password_history)
        dps.sync_channel = lambda b, **kw: calls.append(('links', kw['dest'], kw['channel'], kw['latest_only'])) or {}
        da.sync = lambda b, d, **kw: calls.append(('files', d, kw['channel'], kw['exts'])) or {}
        dsl.sync = lambda b, d, **kw: calls.append(('site', d, kw['channel'])) or {}
        dh.password_history = lambda c, **kw: calls.append(('pw', kw['channel'])) or ['p']
        try:
            jobs = [dcs.normalize(r) for r in [
                {'channel': 'a', 'method': 'pixeldrain_links', 'mode': 'latest', 'dir': '!a'},
                {'channel': 'f', 'method': 'attachments', 'dir': '!f', 'exts': ['.zip']},
                {'channel': 's', 'method': 'site_links', 'dir': '!s'}, {'channel': 'p', 'method': 'passwords'}]]
            dcs.run_jobs(self.base, job_list=jobs, step=lambda t: None)
            self.assertEqual(calls, [('links', '!a', 'a', True), ('files', '!f', 'f', ['.zip']), ('site', '!s', 's'), ('pw', 'p')])
            calls.clear()
            dcs.run_jobs(self.base, job_list=jobs, methods=('passwords',), step=lambda t: None)
            self.assertEqual(calls, [('pw', 'p')])
        finally:
            dps.sync_channel, da.sync, dsl.sync, dh.password_history = old


class AttachmentTests(Base):
    def run_sync(self, rows, dl, **kw):
        def fetch(creator, since_message_id=None, channel=None):
            return rows(since_message_id) if callable(rows) else rows
        return da.sync(self.base, '!up', channel='files', fetch=fetch, fetch_download=dl, **kw)

    def test_only_wanted_extensions_are_taken_each_once_and_recorded(self):
        got = []

        def dl(url, dest):
            got.append(os.path.basename(dest))
            with open(dest, 'wb') as f:
                f.write(b'12345')
            return 5
        rows = [msg(10, hrefs=[att(1, 111, 'a.funscript'), att(1, 112, 'shot.png'), att(1, 113, 'Pack%20One.zip')])]
        out = self.run_sync(rows, dl)
        self.assertEqual((out['downloaded'], sorted(got)), (2, ['Pack One.zip', 'a.funscript']))
        self.assertTrue(os.path.isfile(os.path.join(self.base, '!up', 'a.funscript')))
        self.assertEqual(library_db.get_link(self.lib, 'dc:111')['status'], 'done')
        self.assertEqual(store.unprocessed(self.lib, 'creator#files'), [])
        # the same attachment seen again (message re-stored as unprocessed) is not fetched again
        got.clear()
        out2 = self.run_sync([msg(20, hrefs=[att(1, 111, 'a.funscript')])], dl)
        self.assertEqual((out2['downloaded'], got), (0, []))

    def test_a_name_already_on_disk_is_never_overwritten(self):
        os.makedirs(os.path.join(self.base, '!up'))
        with open(os.path.join(self.base, '!up', 'a.funscript'), 'w') as f:
            f.write('mine')
        out = self.run_sync([msg(10, hrefs=[att(1, 111, 'a.funscript')])], lambda u, d: self.fail('no download'))
        self.assertEqual(out['ambiguous'], 1)
        self.assertEqual(open(os.path.join(self.base, '!up', 'a.funscript')).read(), 'mine')

    def test_an_expired_link_is_refreshed_by_reading_the_message_again(self):
        urls = []

        def dl(url, dest):
            urls.append(url)
            if 'old' in url:
                raise da.Expired('403')
            with open(dest, 'wb') as f:
                f.write(b'x')
            return 1

        def rows(since):
            return [msg(10, hrefs=[att(1, 111, 'a.funscript', 'sig=old' if since is None else 'sig=new')])]
        out = self.run_sync(rows, dl)
        self.assertEqual((out['downloaded'], out['failed']), (1, 0))
        self.assertTrue(urls[0].endswith('sig=old') and urls[1].endswith('sig=new'))

    def test_a_failed_download_keeps_its_message_for_retry(self):
        def dl(url, dest):
            raise RuntimeError('boom')
        out = self.run_sync([msg(10, hrefs=[att(1, 111, 'a.funscript')])], dl)
        self.assertEqual(out['failed'], 1)
        self.assertEqual(len(store.unprocessed(self.lib, 'creator#files')), 1)


class SiteLinkTests(Base):
    def test_other_site_links_become_one_task_per_message_and_pixeldrain_is_left_to_its_method(self):
        import base64
        seen = {}
        hidden = base64.b64encode(b'https://mega.nz/folder/AbC#key').decode()
        rows = [msg(10, text=f'New pack pw: hunter22 {hidden}', hrefs=['https://www.gofile.io/d/xyz', 'https://pixeldrain.com/l/abc',
                                                                       'https://discord.com/channels/1/2']),
                msg(20, text='just chat')]

        def fetch(creator, since_message_id=None, channel=None):
            return rows

        def download(base_path, tasks=None, **kw):
            seen['tasks'] = tasks
        out = dsl.sync(self.base, '!links', channel='links', fetch=fetch, download=download,
                       is_supported=lambda u: 'mega.nz' in u or 'gofile' in u)
        self.assertEqual(out['tasks'], 1)
        task = seen['tasks'][0]
        self.assertEqual(sorted(task['links']), ['https://mega.nz/folder/AbC#key', 'https://www.gofile.io/d/xyz'])
        self.assertTrue(os.path.basename(task['folder']).startswith('[10] New pack'))
        self.assertEqual(store.unprocessed(self.lib, 'creator#links'), [])


if __name__ == '__main__':
    unittest.main()

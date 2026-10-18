"""The rebuild tool must never lose data: every promise in its docstring has a test. Network and downloads are faked."""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import library_db  # noqa: E402
import rebuild_library as rb  # noqa: E402


def sha(b):
    return hashlib.sha256(b).hexdigest()


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        os.makedirs(os.path.join(self.root, 'c'))
        self.remote = {}          # link id -> info dict | 'gone' | None
        self.content = {}         # link id -> bytes served
        self.downloads = []
        self.fail_download = set()
        self.files = {}

    def tearDown(self):
        self._tmp.cleanup()

    # -- fixtures
    def add(self, lid, name, data=b'old', state='downloaded', codec=None, folder='c', extra=None, remote=True,
            listed=False):
        path = os.path.join(self.root, folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        fid = library_db.add_file(self.root, 't', folder=folder, orig_name=name, cur_name=name, orig_size=len(data),
                                  cur_size=len(data), pixeldrain_id=lid, state=state, codec=codec)
        library_db.upsert_link(self.root, lid, 't', source_url=f'https://pixeldrain.com/u/{lid}', status='done',
                               remote_name=name, file_id=fid, extra_json=json.dumps(extra) if extra else None)
        new = b'NEW-' + lid.encode()
        self.content[lid] = new
        if remote:
            self.remote[lid] = {'id': lid, 'name': name, 'size': len(new), 'sha256': sha(new),
                                'url': f'https://pixeldrain.com/u/{lid}'}
        self.files[lid] = path
        return fid

    def plan(self, **kw):
        return rb.build_plan(self.root, list_entries=lambda url: None, file_info=lambda i: self.remote.get(i),
                             hash_file=kw.get('hash_file', lambda p: 'no-match'))

    def download(self, entry, dest_dir, scratch_root):
        self.downloads.append(entry['id'])
        if entry['id'] in self.fail_download:
            return None, 'download failed'
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, entry['name'])
        with open(path, 'wb') as f:
            f.write(self.content.get(entry['id'], b'NEW-' + entry['id'].encode()))
        return path, 'ok'

    def make_token(self, plan=None):
        plan = plan or self.plan()
        rb.record_plan(self.root, plan, os.path.join(self.root, 'r.html'))
        return plan['token']

    def apply(self, token, decisions=None, **kw):
        return rb.apply(self.root, token, decisions, list_entries=lambda u: kw.get('listing'),
                        file_info=lambda i: self.remote.get(i), download=self.download,
                        free_bytes=kw.get('free_bytes'))

    def read(self, lid):
        return self.rd(self.files[lid])

    @staticmethod
    def rd(path):
        with open(path, 'rb') as f:
            return f.read()

    def verdicts(self, plan):
        return {i['link_id']: i['verdict'] for i in plan['items']}

    # -- planning
    def test_every_kind_of_item_is_classified(self):
        self.add('LIVE', 'live.mkv')
        self.add('SAME', 'same.mkv', data=b'NEW-SAME')
        self.add('COMP', 'comp.mp4', state='compressed', codec='av1')
        self.add('MAN', 'man.mkv', folder='m')
        open(os.path.join(self.root, 'm', '.manual'), 'w').close()
        self.add('DEAD', 'dead.mkv')
        self.remote['DEAD'] = 'gone'
        self.add('UNK', 'unk.mkv')
        self.remote['UNK'] = None
        self.add('PIZE', 'p.zip', extra={'packages': ['p1']})
        library_db.add_file(self.root, 't', folder='orphans', orig_name='o.mp4', cur_name='o.mp4')
        library_db.upsert_link(self.root, 'NOFILE', 't', source_url='u', status='done')
        plan = self.plan(hash_file=lambda p: sha(b'NEW-SAME'))
        self.assertEqual(self.verdicts(plan), {'COMP': 'transcoded', 'DEAD': 'dead', 'LIVE': 'live', 'MAN': 'manual',
                                               'NOFILE': 'missing_local', 'PIZE': 'pize_archive', 'SAME': 'identical',
                                               'UNK': 'unverified'})
        self.assertIn('orphans', plan['no_source'])
        self.assertIn({'path': 'm', 'kind': '.manual'}, plan['manual_locations'])
        html_ = rb.render_plan_html(dict(plan, token='T', expires_at='x'))
        self.assertIn('Marked folders (1)', html_)

    def test_planning_changes_no_library_file_and_report_is_escaped(self):
        self.add('LIVE', '<script>alert(1)</script>.mkv')
        self.remote['LIVE']['name'] = '<script>alert(1)</script>.mkv'
        before = {p: self.rd(p) for p in self.files.values()}
        plan = self.plan()
        token = self.make_token(plan)
        page = rb.render_plan_html(plan)
        self.assertIn(token, page)
        self.assertNotIn('<script>alert(1)', page)
        self.assertEqual(before, {p: self.rd(p) for p in self.files.values()})
        self.assertEqual(self.downloads, [])

    # -- token gate
    def test_apply_refuses_unknown_expired_and_stale_tokens(self):
        self.add('LIVE', 'live.mkv')
        with self.assertRaises(rb.TokenError):
            self.apply('nope')
        token = self.make_token()
        library_db.update_file(self.root, library_db.find_files(self.root, pixeldrain_id='LIVE')[0]['id'], 't', cur_size=999)
        with self.assertRaisesRegex(rb.TokenError, 'changed since'):
            self.apply(token)
        self.assertEqual((self.downloads, self.read('LIVE')), ([], b'old'))
        token2 = self.make_token()
        with library_db.db(self.root, write=True) as con:
            con.execute('UPDATE downloader_rebuild_runs SET expires_at=? WHERE token=?', ('2000-01-01T00:00:00', token2))
        with self.assertRaisesRegex(rb.TokenError, 'expired'):
            self.apply(token2)

    # -- the main path
    def test_live_item_is_replaced_old_goes_to_trash_and_others_are_untouched(self):
        self.add('LIVE', 'live.mkv')
        self.add('COMP', 'comp.mp4', data=b'small', state='compressed', codec='av1')
        self.add('DEAD', 'dead.mkv')
        self.remote['DEAD'] = 'gone'
        self.add('MAN', 'man.mkv', folder='m')
        open(os.path.join(self.root, 'm', '.manual'), 'w').close()
        out = self.apply(self.make_token())
        self.assertEqual(self.downloads, ['LIVE'])
        self.assertEqual(self.read('LIVE'), b'NEW-LIVE')
        self.assertEqual(self.rd(os.path.join(self.root, '.trash', 'c', 'live.mkv')), b'old')
        for lid, data in (('COMP', b'small'), ('DEAD', b'old'), ('MAN', b'old')):
            self.assertEqual(self.read(lid), data, lid)
        row = library_db.find_files(self.root, pixeldrain_id='LIVE')[0]
        self.assertEqual((row['cur_size'], row['sha256'], row['state']), (8, sha(b'NEW-LIVE'), 'rebuilt'))
        self.assertTrue(os.path.isfile(out['_report']))
        with open(os.path.join(self.root, '.last_action.rebuild_library.json')) as f:
            self.assertEqual([e['op'] for e in json.load(f)['entries']], ['soft_delete', 'create_marker'])

    def test_link_dying_after_the_plan_keeps_the_old_data(self):
        self.add('LIVE', 'live.mkv')
        token = self.make_token()
        self.remote['LIVE'] = 'gone'
        out = self.apply(token)
        self.assertEqual((out.get('kept_old'), self.downloads, self.read('LIVE')), (1, [], b'old'))

    def test_failed_download_keeps_old_and_a_second_apply_retries(self):
        self.add('LIVE', 'live.mkv')
        token = self.make_token()
        self.fail_download.add('LIVE')
        out = self.apply(token)
        self.assertEqual((out.get('failed'), self.read('LIVE')), (1, b'old'))
        self.assertFalse(os.path.exists(os.path.join(self.root, '.trash', 'c', 'live.mkv')))
        self.fail_download.clear()
        self.apply(token)
        self.assertEqual(self.read('LIVE'), b'NEW-LIVE')

    def test_interrupt_then_resume_does_not_redo_finished_items(self):
        self.add('A', 'a.mkv')
        self.add('B', 'b.mkv')
        token = self.make_token()
        real = self.download

        def interrupting(entry, dest, scratch):
            if entry['id'] == 'B':
                raise KeyboardInterrupt
            return real(entry, dest, scratch)
        with self.assertRaises(KeyboardInterrupt):
            rb.apply(self.root, token, None, list_entries=lambda u: None, file_info=lambda i: self.remote.get(i),
                     download=interrupting)
        self.assertEqual((self.read('A'), self.read('B')), (b'NEW-A', b'old'))
        self.downloads.clear()
        out = self.apply(token)
        self.assertEqual((self.downloads, self.read('B'), out.get('swapped')), (['B'], b'NEW-B', 2))

    def test_a_finished_run_cannot_be_used_to_redo_everything_after_the_library_changed(self):
        self.add('LIVE', 'live.mkv')
        token = self.make_token()
        self.apply(token)
        self.downloads.clear()
        self.apply(token)                       # resuming a finished run is a harmless no-op: nothing is pending
        self.assertEqual(self.downloads, [])

    def test_file_edited_after_the_plan_is_not_replaced(self):
        self.add('LIVE', 'live.mkv')
        token = self.make_token()
        with open(self.files['LIVE'], 'wb') as f:
            f.write(b'my own precious edit')
        out = self.apply(token)
        self.assertEqual((out.get('failed'), self.read('LIVE')), (1, b'my own precious edit'))
        self.assertFalse(os.path.exists(os.path.join(self.root, '.trash', 'c', 'live.mkv')))

    def test_insufficient_space_refuses_before_anything_happens(self):
        self.add('LIVE', 'live.mkv')
        token = self.make_token()
        with self.assertRaisesRegex(rb.TokenError, 'free space'):
            self.apply(token, free_bytes=lambda p: 1)
        self.assertEqual((self.downloads, self.read('LIVE')), ([], b'old'))

    # -- decisions
    def test_replacement_link_for_a_dead_item_is_used_and_checked(self):
        self.add('DEAD', 'dead.mkv')
        self.remote['DEAD'] = 'gone'
        token = self.make_token()
        new = {'id': 'NEWID', 'name': 'dead.mkv', 'size': 9, 'sha256': sha(b'NEW-NEWID'), 'url': 'u'}
        self.content['NEWID'] = b'NEW-NEWID'
        dec = {'token': token, 'items': {'DEAD': {'action': 'replace', 'url': 'https://pixeldrain.com/u/NEWID'}}}
        out = rb.apply(self.root, token, dec, list_entries=lambda u: [new], file_info=lambda i: self.remote.get(i),
                       download=self.download)
        self.assertEqual((out.get('swapped'), self.read('DEAD')), (1, b'NEW-NEWID'))

    def test_bad_replacement_link_keeps_the_old_data(self):
        self.add('DEAD', 'dead.mkv')
        self.remote['DEAD'] = 'gone'
        token = self.make_token()
        dec = {'token': token, 'items': {'DEAD': {'action': 'replace', 'url': 'https://example.com/x'}}}
        out = self.apply(token, dec)
        self.assertEqual((out.get('kept_old'), self.read('DEAD')), (1, b'old'))

    def test_decisions_for_another_plan_are_ignored(self):
        self.add('DEAD', 'dead.mkv')
        self.remote['DEAD'] = 'gone'
        token = self.make_token()
        out = self.apply(token, {'token': 'someone-elses', 'items': {'DEAD': {'action': 'replace', 'url': 'x'}}})
        self.assertEqual((self.downloads, self.read('DEAD')), ([], b'old'))

    def test_cli_plan_then_confirm(self):
        import io
        from contextlib import redirect_stdout
        self.add('LIVE', 'live.mkv')
        orig = (rb.pdu.list_entries, rb.pdu.file_info, rb.pdu.download_file)
        rb.pdu.list_entries = lambda u: None
        rb.pdu.file_info = lambda i: self.remote.get(i)
        rb.pdu.download_file = self.download
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(rb.main([self.root]), 0)
            token = buf.getvalue().split('--confirm ')[1].split()[0]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(rb.main([self.root, '--confirm', token, '--yes']), 0)
                self.assertEqual(rb.main([self.root, '--confirm', 'bad-token', '--yes']), 2)
        finally:
            rb.pdu.list_entries, rb.pdu.file_info, rb.pdu.download_file = orig
        self.assertEqual(self.read('LIVE'), b'NEW-LIVE')


if __name__ == '__main__':
    unittest.main()

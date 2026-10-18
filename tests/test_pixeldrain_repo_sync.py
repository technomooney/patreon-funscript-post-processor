"""The point of this plugin: a transcoded file must never be re-downloaded. Network is faked; temp dirs only."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'scripts')
sys.path.insert(0, SCRIPTS)

import folder_log  # noqa: E402
import library_db  # noqa: E402
import pixeldrain_util as pdu  # noqa: E402

spec = importlib.util.spec_from_file_location('pixeldrain_repo_sync',
                                              os.path.join(SCRIPTS, 'creator_scripts', 'pixeldrain_repo_sync.py'))
prs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prs)


def remote(i, name, size=1000, sha=None):
    return {'id': i, 'name': name, 'size': size, 'sha256': sha, 'url': f'https://pixeldrain.com/u/{i}'}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lib = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        self.base = os.path.join(self.lib, 'creator')
        os.makedirs(self.base)
        self._old = {k: getattr(pdu, k) for k in ('list_entries', 'download_file')}
        self._env = os.environ.get('LIBRARY_TOOLS_HOME')
        os.environ['LIBRARY_TOOLS_HOME'] = os.path.join(self._tmp.name, 'home')
        self.remotes = []
        self.downloaded = []
        pdu.list_entries = lambda url: list(self.remotes)

        def fake_download(entry, dest_dir, scratch_root):
            self.downloaded.append(entry['id'])
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, entry['name'])
            with open(path, 'wb') as f:
                f.write(b'x' * entry['size'])
            return path, 'ok'
        pdu.download_file = fake_download
        self.src = [{'url': 'https://pixeldrain.com/l/LIST', 'dest': 'repo'}]
        self.dest = os.path.join(self.base, 'repo')

    def tearDown(self):
        for k, v in self._old.items():
            setattr(pdu, k, v)
        if self._env is None:
            os.environ.pop('LIBRARY_TOOLS_HOME', None)
        else:
            os.environ['LIBRARY_TOOLS_HOME'] = self._env
        self._tmp.cleanup()

    def sync(self):
        items = prs.plan(self.lib, self.base, self.src)
        return items, prs.execute(self.lib, self.base, items)

    def test_new_files_are_downloaded_once_then_skipped(self):
        self.remotes = [remote('A', 'a.mkv'), remote('B', 'b.mkv')]
        _, r1 = self.sync()
        self.assertEqual((r1['downloaded'], sorted(self.downloaded)), (2, ['A', 'B']))
        self.downloaded.clear()
        items, r2 = self.sync()
        self.assertEqual(self.downloaded, [])
        self.assertEqual(prs.summarize(items)[prs.HAVE], 2)

    def test_transcoded_file_is_not_redownloaded(self):
        """The reported bug: a.mkv was transcoded to a.mp4 (smaller); a new sync must not fetch a.mkv again."""
        self.remotes = [remote('A', 'a.mkv', size=1000)]
        self.sync()
        os.remove(os.path.join(self.dest, 'a.mkv'))                 # the compressor replaced it...
        with open(os.path.join(self.dest, 'a.mp4'), 'wb') as f:
            f.write(b'y' * 300)
        self.downloaded.clear()
        items, _ = self.sync()
        self.assertEqual(self.downloaded, [])
        self.assertEqual(items[0]['verdict'], prs.HAVE)

    def test_transcode_recognised_from_compressor_log_without_any_db_record(self):
        """Files fetched by the old rclone flow: only the compressor's own log knows about them."""
        os.makedirs(self.dest)
        with open(os.path.join(self.dest, 'a.mp4'), 'wb') as f:
            f.write(b'y')
        folder_log.append_run(self.dest, 'compress_library', file='a.mkv', status='compressed',
                              original_size=1000, compressed_size=300, final_file='a.mp4')
        self.remotes = [remote('A', 'a.mkv', size=1000)]
        items, _ = self.sync()
        self.assertEqual((items[0]['verdict'], self.downloaded), (prs.HAVE, []))
        self.assertEqual(library_db.get_link(self.lib, 'A')['status'], 'done')
        self.assertEqual(library_db.find_files(self.lib, pixeldrain_id='A')[0]['cur_name'], 'a.mp4')

    def test_ambiguous_cases_are_left_alone(self):
        os.makedirs(self.dest)
        for name, data in (('same.mkv', b'x' * 5), ('stem.mp4', b'x')):
            with open(os.path.join(self.dest, name), 'wb') as f:
                f.write(data)
        folder_log.append_run(self.dest, 'compress_library', file='logged.mkv', status='compressed',
                              original_size=111, final_file='logged.mp4')
        self.remotes = [remote('S', 'same.mkv', 1000), remote('T', 'stem.mkv', 1000), remote('L', 'logged.mkv', 1000)]
        items, r = self.sync()
        self.assertEqual([i['verdict'] for i in items], [prs.AMBIGUOUS] * 3)
        self.assertEqual((self.downloaded, r['downloaded']), ([], 0))
        self.assertEqual(open(os.path.join(self.dest, 'same.mkv'), 'rb').read(), b'x' * 5)   # untouched

    def test_recognised_by_content_hash_or_original_name_and_size_elsewhere(self):
        library_db.add_file(self.lib, 't', folder='other', orig_name='moved.mkv', cur_name='moved.mp4',
                            orig_size=1000, sha256='H1')
        library_db.add_file(self.lib, 't', folder='other', orig_name='x.mkv', cur_name='x.mp4', orig_size=7, sha256='H2')
        self.remotes = [remote('A', 'renamed.mkv', 1000, sha='H2'), remote('B', 'moved.mkv', 1000)]
        items, _ = self.sync()
        self.assertEqual([i['verdict'] for i in items], [prs.HAVE, prs.HAVE])
        self.assertEqual(self.downloaded, [])

    def test_dry_plan_changes_nothing(self):
        self.remotes = [remote('A', 'a.mkv')]
        items = prs.plan(self.lib, self.base, self.src)
        self.assertEqual(prs.summarize(items)[prs.NEW], 1)
        self.assertIsNone(library_db.get_link(self.lib, 'A'))
        self.assertFalse(os.path.exists(self.dest))

    def test_failed_download_is_recorded_and_retried_next_time(self):
        self.remotes = [remote('A', 'a.mkv')]
        real = pdu.download_file
        pdu.download_file = lambda e, d, s: (None, 'download failed')
        _, r = self.sync()
        self.assertEqual((r['failed'], library_db.get_link(self.lib, 'A')['status']), (1, 'failed'))
        pdu.download_file = real
        _, r = self.sync()
        self.assertEqual((r['downloaded'], library_db.get_link(self.lib, 'A')['status']), (1, 'done'))

    def test_unreadable_source_is_reported_not_treated_as_empty(self):
        pdu.list_entries = lambda url: None
        items, r = self.sync()
        self.assertEqual((items[0]['verdict'], r['downloaded']), ('unreadable', 0))

    def test_downloads_are_journaled_for_undo(self):
        self.remotes = [remote('A', 'a.mkv')]
        self.sync()
        with open(os.path.join(self.base, '.last_action.pixeldrain_repo_sync.json')) as f:
            journal = json.load(f)
        self.assertEqual([e['op'] for e in journal['entries']], ['create_marker'])

    def test_sync_urls_refuses_without_a_library_root_when_unattended(self):
        bare = os.path.join(self._tmp.name, 'nolib', 'c')
        os.makedirs(bare)
        self.assertIsNone(prs.sync_urls(bare, ['https://pixeldrain.com/l/X'], interactive=False))
        self.assertFalse(os.path.exists(os.path.join(bare, '.library-data')))


class UtilTests(unittest.TestCase):
    def test_kind_and_ids_and_safe_name(self):
        self.assertEqual(pdu.kind('https://pixeldrain.com/l/abc'), 'list')
        self.assertEqual(pdu.kind('https://www.pixeldrain.com/u/abc'), 'file')
        self.assertEqual(pdu.kind('https://pixeldrain.com/d/abc/x'), 'filesystem')
        self.assertEqual(pdu.kind('https://example.com/u/abc'), '')
        self.assertEqual(pdu.file_id('https://pixeldrain.com/u/abc/'), 'abc')
        self.assertEqual(pdu.safe_name('../../etc/passwd'), 'passwd')
        self.assertEqual(pdu.safe_name('a/b\\c.mp4'), 'c.mp4')
        self.assertEqual(pdu.safe_name('..'), 'file')


if __name__ == '__main__':
    unittest.main()

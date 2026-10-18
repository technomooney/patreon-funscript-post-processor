import hashlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import check_library_drift as cli  # noqa: E402
import library_db  # noqa: E402
import library_drift as ld  # noqa: E402


def tree_hash(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != '.library-data']
        for f in fn:
            p = os.path.join(dp, f)
            with open(p, 'rb') as fh:
                out[p] = hashlib.sha256(fh.read()).hexdigest()
    return out


class DriftTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        os.makedirs(os.path.join(self.root, 'c'))

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, name, size, record=True, record_size=None):
        with open(os.path.join(self.root, 'c', name), 'wb') as f:
            f.write(b'x' * size)
        if record:
            return library_db.add_file(self.root, 't', folder='c', orig_name=name, cur_name=name,
                                       cur_size=record_size if record_size is not None else size)

    def kinds(self):
        return sorted((f['kind'], os.path.basename(f['path'])) for f in ld.open_findings(self.root))

    def test_clean_library_has_no_findings(self):
        self.make('a.mp4', 10)
        r = ld.scan(self.root)
        self.assertEqual((r['findings'], ld.open_findings(self.root)), ([], []))

    def test_missing_changed_and_untracked_not_counted_as_drift(self):
        self.make('gone.mp4', 10)
        os.remove(os.path.join(self.root, 'c', 'gone.mp4'))
        self.make('edited.mp4', 20, record_size=10)           # user replaced it with a bigger file
        self.make('new.mp4', 5, record=False)                 # simply not in the database yet
        r = ld.scan(self.root)
        self.assertEqual(self.kinds(), [('changed', 'edited.mp4'), ('missing', 'gone.mp4')])
        self.assertEqual(r['untracked_in_tracked_folders'], 1)

    def test_renamed_suspected_when_an_unrecorded_file_has_the_same_size(self):
        self.make('old.mp4', 77)
        os.rename(os.path.join(self.root, 'c', 'old.mp4'), os.path.join(self.root, 'c', 'renamed.mp4'))
        ld.scan(self.root)
        f = ld.open_findings(self.root)[0]
        self.assertEqual((f['kind'], f['detail']['candidates']), ('renamed_suspected', ['renamed.mp4']))
        self.assertEqual(ld.accept(self.root, f['id']), 'recorded the rename to renamed.mp4')
        self.assertEqual(library_db.find_files(self.root, orig_name='old.mp4')[0]['cur_name'], 'renamed.mp4')
        ld.scan(self.root)
        self.assertEqual(ld.open_findings(self.root), [])

    def test_accept_changed_adopts_new_size_and_missing_marks_intentional(self):
        self.make('edited.mp4', 20, record_size=10)
        self.make('gone.mp4', 3)
        os.remove(os.path.join(self.root, 'c', 'gone.mp4'))
        ld.scan(self.root)
        for f in ld.open_findings(self.root):
            ld.accept(self.root, f['id'])
        ld.scan(self.root)
        self.assertEqual(ld.open_findings(self.root), [])
        row = library_db.find_files(self.root, orig_name='edited.mp4')[0]
        self.assertEqual((row['cur_size'], row['state']), (20, 'manual'))
        self.assertEqual(library_db.find_files(self.root, orig_name='gone.mp4')[0]['state'], 'missing')

    def test_finding_resolves_itself_when_the_disk_matches_again(self):
        self.make('a.mp4', 10, record_size=99)
        ld.scan(self.root)
        self.assertEqual(len(ld.open_findings(self.root)), 1)
        self.make('a.mp4', 99, record=False)
        ld.scan(self.root)
        self.assertEqual(ld.open_findings(self.root), [])

    def test_markers_are_listed_and_changes_after_the_first_scan_are_reported(self):
        for rel in ('c', ''):
            open(os.path.join(self.root, rel, '.manual'), 'w').close()
        open(os.path.join(self.root, 'c', '.consolidated'), 'w').close()
        r = ld.scan(self.root)
        self.assertEqual(sorted((m['path'], m['kind']) for m in r['markers']),
                         [('', '.manual'), ('c', '.consolidated'), ('c', '.manual')])
        self.assertEqual(r['findings'], [])                    # existing markers are not "news" on the first scan
        os.makedirs(os.path.join(self.root, 'd'))
        open(os.path.join(self.root, 'd', '.manual'), 'w').close()
        os.remove(os.path.join(self.root, 'c', '.manual'))
        r = ld.scan(self.root)
        self.assertEqual(sorted((f['kind'], f['folder']) for f in r['findings']),
                         [('marker_added', 'd'), ('marker_removed', 'c')])

    def test_scan_never_modifies_a_library_file(self):
        self.make('a.mp4', 10, record_size=3)
        self.make('b.mp4', 4)
        open(os.path.join(self.root, 'c', '.manual'), 'w').close()
        before = tree_hash(self.root)
        ld.scan(self.root)
        ld.scan(self.root)
        self.assertEqual(before, tree_hash(self.root))

    def test_needs_attention(self):
        self.make('ok.mp4', 10)
        self.make('edited.mp4', 20, record_size=10)
        self.assertIsNone(ld.needs_attention(self.root, 'c', 'ok.mp4'))
        self.assertIn('changed', ld.needs_attention(self.root, 'c', 'edited.mp4'))
        self.assertIsNone(ld.needs_attention(self.root, 'c', 'unrecorded.mp4'))
        open(os.path.join(self.root, 'c', '.manual'), 'w').close()
        self.assertIn('.manual', ld.needs_attention(self.root, 'c', 'ok.mp4'))
        os.makedirs(os.path.join(self.root, 'c', 'sub'))
        self.assertTrue(ld.has_manual_marker(self.root, os.path.join(self.root, 'c', 'sub')))   # inherited by subfolders
        self.assertFalse(ld.has_manual_marker(self.root, os.path.join(self.root, 'other')))

    def test_cli_report_accept_and_csv(self):
        self.make('edited.mp4', 20, record_size=10)
        csv_path = os.path.join(self._tmp.name, 'out.csv')
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main([self.root, '--csv', csv_path]), 0)
        self.assertIn('changed', buf.getvalue())
        with open(csv_path) as fh:
            self.assertIn('edited.mp4', fh.read())
        with redirect_stdout(io.StringIO()):
            cli.main([self.root, '--accept-all', 'changed'])
        self.assertEqual(ld.open_findings(self.root), [])

    def test_cli_refuses_a_library_without_a_database(self):
        bare = os.path.join(self._tmp.name, 'bare')
        os.makedirs(bare)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([bare]), 2)
        self.assertFalse(os.path.exists(os.path.join(bare, '.library-data')))


if __name__ == '__main__':
    unittest.main()

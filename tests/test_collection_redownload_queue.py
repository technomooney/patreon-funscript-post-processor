"""The flagged-collection redownload queue lives in the library DB and keeps its add/dedupe/remove behaviour."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import collection_redownload_queue as q  # noqa: E402
import library_db  # noqa: E402


class QueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        library_db._roots.clear()
        self.root = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))

    def tearDown(self):
        self._tmp.cleanup()

    def _add(self, stem):
        with contextlib.redirect_stdout(io.StringIO()):
            q.add(self.root, '/f', stem, 'http://v/' + stem, 480, 1080, 'creator')

    def test_add_dedupes_and_survives_reload(self):
        self._add('a')
        self._add('a')
        self._add('b')
        self.assertEqual([e['stem'] for e in q.load(self.root)], ['a', 'b'])

    def test_remove_drops_only_the_given_pairs(self):
        self._add('a')
        self._add('b')
        q.remove(self.root, {('/f', 'a')})
        self.assertEqual([e['stem'] for e in q.load(self.root)], ['b'])
        q.remove(self.root, {('/f', 'b')})
        self.assertEqual(q.load(self.root), [])

    def test_no_file_is_written(self):
        self._add('a')
        self.assertFalse(os.path.exists(os.path.join(self.root, '_reports')))


if __name__ == '__main__':
    unittest.main()

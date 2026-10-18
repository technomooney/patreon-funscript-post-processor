import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'scripts')
sys.path.insert(0, SCRIPTS)

import library_db  # noqa: E402
import pixeldrain_util as pdu  # noqa: E402

spec = importlib.util.spec_from_file_location('hyb', os.path.join(SCRIPTS, 'creator_scripts', 'bekscript_hybrid_audit.py'))
hyb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hyb)


def fs(path, points):
    with open(path, 'w') as f:
        json.dump({'actions': [{'at': a, 'pos': p} for a, p in points]}, f)


def desc(folder, href=None):
    os.makedirs(folder, exist_ok=True)
    doc = {'type': 'doc', 'content': []}
    if href:
        doc['content'].append({'type': 'paragraph', 'content': [
            {'type': 'text', 'text': 'x', 'marks': [{'type': 'link', 'attrs': {'href': href}}]}]})
    with open(os.path.join(folder, 'description.json'), 'w') as f:
        json.dump(doc, f)


class HybridTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lib = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        self.base = os.path.join(self.lib, 'Bek')
        os.makedirs(self.base)
        self._old = pdu.list_entries

    def tearDown(self):
        pdu.list_entries = self._old
        self._tmp.cleanup()

    def test_match_by_data_not_name_and_secret_ledger(self):
        a = os.path.join(self.base, 'Post A')
        desc(a, 'https://example.com/v')
        fs(os.path.join(a, 'Renamed.funscript'), [(0, 1), (100, 90)])      # in archive under another name
        b = os.path.join(self.base, 'Post B')
        desc(b, 'https://example.com/w')
        fs(os.path.join(b, 'B.funscript'), [(0, 5), (100, 50)])            # not in archive
        desc(os.path.join(self.base, 'Secret Thing'))                       # title only
        os.makedirs(os.path.join(self.base, 'discord'))
        fs(os.path.join(self.base, 'discord', 'Secret Thing.funscript'), [(0, 9)])

        archive_data = {'f1': [(0, 1), (100, 90)]}
        pdu.list_entries = lambda url: [{'id': 'f1', 'name': 'Totally Different.funscript', 'url': 'u'}]

        def fake_dl(entry, dest, scratch):
            p = os.path.join(dest, entry['name'])
            fs(p, archive_data[entry['id']])
            return p, 'ok'

        r = hyb.audit(self.base, [{'url': 'https://pixeldrain.com/l/abc'}], self.lib, download=fake_dl)
        self.assertEqual([g['post'] for g in r['missing']], ['Post B'])
        self.assertEqual(r['ledger']['Secret Thing']['status'], 'resolved')
        # cached: second run must not download again
        r2 = hyb.audit(self.base, [{'url': 'https://pixeldrain.com/l/abc'}], self.lib,
                       download=lambda *a: self.fail('should use cache'))
        self.assertEqual(len(r2['missing']), 1)


if __name__ == '__main__':
    unittest.main()

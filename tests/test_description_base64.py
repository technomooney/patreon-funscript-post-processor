"""Base64-hidden links are found by the core description parser, for any creator.

Needs the project's venv (downloadContent imports selenium); skipped when those imports are unavailable."""
import base64
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, 'scripts'))

try:
    import downloadContent as dc
except ImportError as e:           # pragma: no cover
    dc = None
    _WHY = str(e)


def _b64(s):
    return base64.b64encode(s.encode()).decode()


def _doc(*texts, link=None):
    content = [{'type': 'paragraph', 'content': [{'type': 'text', 'text': t}]} for t in texts]
    if link:
        content.append({'type': 'paragraph', 'content': [
            {'type': 'text', 'text': 'normal', 'marks': [{'type': 'link', 'attrs': {'href': link}}]}]})
    return {'type': 'doc', 'content': content}


@unittest.skipIf(dc is None, 'downloadContent not importable here (run with the project venv)')
class DescriptionBase64Tests(unittest.TestCase):
    def entries(self, doc):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(doc, f)
        try:
            return dc.extract_link_entries_from_description(f.name)
        finally:
            os.unlink(f.name)

    def test_hidden_pixeldrain_link_found_and_tagged(self):
        url = 'https://pixeldrain.com/l/AbCd1234'
        out = self.entries(_doc('Download:', _b64(url)))
        self.assertEqual([(e['href'], e.get('decoded_from_base64')) for e in out], [(url, True)])

    def test_unsupported_domain_ignored(self):
        self.assertEqual(self.entries(_doc(_b64('https://totally-unknown-site.example/abc/def'))), [])

    def test_normal_links_unchanged_and_not_duplicated(self):
        url = 'https://pixeldrain.com/u/Same1234'
        out = self.entries(_doc(_b64(url), link=url))
        self.assertEqual([e['href'] for e in out], [url])
        self.assertNotIn('decoded_from_base64', out[0])


if __name__ == '__main__':
    unittest.main()

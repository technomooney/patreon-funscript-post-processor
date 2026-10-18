import base64
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import link_decoder as ld  # noqa: E402


def b64(s, urlsafe=False, pad=True):
    f = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    out = f(s.encode()).decode()
    return out if pad else out.rstrip('=')


PD = 'https://pixeldrain.com/l/AbCd1234'


class DecoderTests(unittest.TestCase):
    def hrefs(self, text, accept=None):
        return [f['href'] for f in ld.find_encoded_urls(text, accept)]

    def test_plain_base64(self):
        self.assertEqual(self.hrefs(f'Link: {b64(PD)} enjoy'), [PD])

    def test_missing_padding_and_urlsafe(self):
        url = 'https://pixeldrain.com/u/Zz??~x'          # bytes that need '+' '/' in standard base64
        self.assertEqual(self.hrefs(b64(url, pad=False)), [url])
        self.assertEqual(self.hrefs(b64(url, urlsafe=True)), [url])

    def test_double_encoded(self):
        out = ld.find_encoded_urls(b64(b64(PD)))
        self.assertEqual([(f['href'], f['layers']) for f in out], [(PD, 2)])

    def test_bare_host_gets_scheme(self):
        self.assertEqual(self.hrefs(b64('pixeldrain.com/l/xYz98765')), ['https://pixeldrain.com/l/xYz98765'])

    def test_multiple_links_in_one_string_and_dedupe(self):
        both = b64(PD + '\nhttps://mega.nz/folder/AAA#BBB')
        text = f'{both} again {both}'
        self.assertEqual(self.hrefs(text), [PD, 'https://mega.nz/folder/AAA#BBB'])

    def test_accept_filter(self):
        enc = b64('https://example.org/x/y/z')
        self.assertEqual(self.hrefs(enc), ['https://example.org/x/y/z'])
        self.assertEqual(self.hrefs(enc, accept=lambda u: 'pixeldrain' in u), [])

    def test_punctuation_around_candidate(self):
        self.assertEqual(self.hrefs(f'({b64(PD)}).'), [PD])
        self.assertEqual(self.hrefs(f'PW:abc "{b64(PD, pad=False)}",'), [PD])

    def test_false_positives_rejected(self):
        for text in (
            'Supercalifragilisticexpialidocious_is_a_long_word',
            'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789',
            'thisisaverylongwordwithnospacesatall',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'https://pixeldrain.com/u/AbCdEfGhIjKlMnOpQrStUvWxYz',   # a normal visible link
            b64('just some friendly text, no link in here at all'),    # valid base64, no URL inside
            base64.b64encode(os.urandom(48)).decode(),                 # binary garbage
            'short=',
            '',
        ):
            self.assertEqual(self.hrefs(text), [], text)

    def test_mixed_alphabet_rejected(self):
        self.assertIsNone(ld.decode_candidate('abcd+efgh_ijkl-mnop/qrst'))

    def test_visible_url_not_mistaken_for_base64(self):
        text = 'see https://pixeldrain.com/u/abcdefghijklmnopqrstuvwxyz0123 now'
        self.assertEqual(self.hrefs(text), [])


if __name__ == '__main__':
    unittest.main()

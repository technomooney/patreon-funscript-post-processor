"""eroscripts thread URL and link extraction. Needs the project's venv (downloadContent imports selenium)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import downloadContent as dc


class ThreadIdTests(unittest.TestCase):
    def test_thread_urls(self):
        for url, want in [
            ('https://discuss.eroscripts.com/t/some-slug/333149', 333149),
            ('https://discuss.eroscripts.com/t/some-slug/333149/12', 333149),
            ('https://discuss.eroscripts.com/t/333149', 333149),
            ('https://discuss.eroscripts.com/t/some-slug/333149?u=x', 333149),
        ]:
            self.assertEqual(dc._eroscripts_thread_id(url), want, url)

    def test_non_thread_urls(self):
        for url in ['https://discuss.eroscripts.com/u/someone', 'https://discuss.eroscripts.com/c/scripts/5',
                    'https://discuss.eroscripts.com/', 'https://example.com/t/slug/5']:
            self.assertIsNone(dc._eroscripts_thread_id(url), url)


class VideoLinkTests(unittest.TestCase):
    HTML = '''
      <a href="https://discuss.eroscripts.com/u/x">me</a>
      <a href="https://eroscripts-discourse.eroscripts.com/original/a.jpeg">img</a>
      <a href="https://www.patreon.com/x/posts/1">patreon</a>
      <a href="https://rule34video.com/video/1/abc/">r34</a>
      <a href="https://pixeldrain.com/u/abc">pd</a>
      <a href="https://mega.nz/file/abc#key">mega</a>
      <a href="https://pixeldrain.com/u/abc">dup</a>
      <a href="https://unknown-host.example/v.mp4">unknown</a>
      <a href="https://pixeldrain.com/api/file/a.zip">zip</a>
      <a href="/relative">rel</a>'''

    def test_keeps_known_hosts_priority_first_no_dupes_or_internal(self):
        got = dc._eroscripts_video_links(self.HTML)
        self.assertEqual(got, ['https://pixeldrain.com/u/abc', 'https://mega.nz/file/abc#key',
                               'https://rule34video.com/video/1/abc/'])

    def test_empty(self):
        self.assertEqual(dc._eroscripts_video_links(''), [])


if __name__ == '__main__':
    unittest.main()


class TotpTests(unittest.TestCase):
    def test_rfc6238_vectors(self):
        secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'     # ASCII "12345678901234567890"
        self.assertEqual(dc._totp_code(secret, at=59, digits=8), '94287082')
        self.assertEqual(dc._totp_code(secret, at=1111111109, digits=8), '07081804')
        self.assertEqual(dc._totp_code('gezd gnbv-gy3t qojq gezd gnbv gy3t qojq', at=59), '287082')


class CodeSearchTests(unittest.TestCase):
    def test_code_from_folder_name(self):
        f = dc._eroscripts_code_for_folder
        self.assertEqual(f('[155541226] 2026-04-13 FC-2026-114', True), 'FC-2026-114')
        self.assertEqual(f('[153299454] 2026-03-17 FC-2026-76,5', True), 'FC-2026-76,5')
        self.assertEqual(f('[155471310] 2026-04-12 FC-2026-111 (Premium request)', True), 'FC-2026-111')
        self.assertIsNone(f('[152412958] 2026-03-06 Welcome to my Patreon!', True))
        self.assertIsNone(f('[1] 2026-03-06 FC-2026-114', False))
        self.assertEqual(f('post AB12 here', r'\b([A-Z]{2}\d{2})\b'), 'AB12')

    def test_search_url_round_trip(self):
        url = dc._eroscripts_search_url('FC-2026-76,5')
        self.assertEqual(dc._eroscripts_search_code(url), 'FC-2026-76,5')
        self.assertIsNone(dc._eroscripts_search_code('https://discuss.eroscripts.com/t/slug/12'))
        self.assertIsNone(dc._eroscripts_thread_id(url))

    def test_title_match_is_exact_token(self):
        m = dc._eroscripts_title_has_code
        self.assertTrue(m('Widowmaker’s Work Affairs Part 2 Reinamation FC-2026-86', 'FC-2026-86'))
        self.assertTrue(m('FC-2026-86 something', 'FC-2026-86'))
        self.assertFalse(m('Thing FC-2026-865', 'FC-2026-86'))
        self.assertFalse(m('Thing FC-2026-86,5', 'FC-2026-86'))
        self.assertTrue(m('Thing FC-2026-86,5', 'FC-2026-86,5'))
        self.assertFalse(m('Thing FC-2026-8', 'FC-2026-86'))

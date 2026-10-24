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

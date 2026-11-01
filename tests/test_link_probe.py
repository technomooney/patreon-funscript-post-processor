"""link_probe: turn what a failed attempt already learned into a verdict. Nothing here talks to a site."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import link_probe as lp  # noqa: E402


class MegaExitTests(unittest.TestCase):
    def test_gone_blocked_and_expired_are_permanent(self):
        for code, reason in ((9, 'mega_not_found'), (16, 'mega_blocked'), (8, 'mega_expired')):
            self.assertEqual(lp.mega_exit_verdict(code), (reason, -code, True))

    def test_exit_6_is_a_retryable_rate_limit(self):
        self.assertEqual(lp.mega_exit_verdict(6), ('mega_too_many_requests', -6, False))

    def test_everything_else_is_not_permanent(self):
        for code in (3, 4, 17, 18, 19, 24, 100):
            self.assertFalse(lp.mega_exit_verdict(code).permanent, code)

    def test_our_own_stall_kill_and_unknown_are_not_a_verdict(self):
        self.assertEqual(lp.mega_exit_verdict(-1), ('mega_stalled', None, False))
        self.assertFalse(lp.mega_exit_verdict(None).permanent)


class ApiErrorTests(unittest.TestCase):
    def test_only_an_explicit_404_not_found_is_permanent(self):
        self.assertEqual(lp.classify_api_error('pixeldrain', 404, 'not_found'), ('pixeldrain_not_found', 404, True))
        self.assertTrue(lp.classify_api_error('pixeldrain', 404, 'file_not_found').permanent)
        self.assertEqual(lp.classify_api_error('yandex', 404, 'NotFoundError'), ('yandex_not_found', 404, True))
        self.assertTrue(lp.classify_api_error('yandex', 404, 'DiskNotFoundError').permanent)

    def test_other_answers_are_not(self):
        for status, value in ((429, 'too_many'), (403, 'forbidden'), (500, None), (404, None), (404, 'something_else'),
                              (None, None)):
            self.assertFalse(lp.classify_api_error('pixeldrain', status, value).permanent, (status, value))
        self.assertEqual(lp.classify_api_error('yandex', 429, 'TooManyRequestsError').reason,
                         'yandex_TooManyRequestsError')


class YtdlpTests(unittest.TestCase):
    def test_explicit_removals_are_permanent(self):
        for text in ('ERROR: [Eporner] x: Unable to download webpage: HTTP Error 404: Not Found (caused by <HTTPError 404>)',
                     'ERROR: [site] This video has been removed by the uploader',
                     'ERROR: [site] 12: Video does not exist',
                     'ERROR: [generic] Got HTTP Error 410: Gone'):
            self.assertTrue(lp.classify_ytdlp_error(text, 'https://example.com/v').permanent, text)

    def test_walls_and_unclear_errors_never_are(self):
        for text in ('ERROR: [generic] Got HTTP Error 403 caused by Cloudflare anti-bot challenge',
                     'ERROR: [site] This video is private. Log in to view it (HTTP Error 404)',
                     'ERROR: [site] Sign in to confirm your age',
                     'ERROR: [site] members-only content has been removed from your plan',
                     'ERROR: Unsupported URL: https://x/y',
                     'ERROR: [Piracy] This website is no longer supported',
                     'ERROR: [site] Unable to download webpage: HTTP Error 429: Too Many Requests',
                     'ERROR: [site] This video is not available in your country (HTTP Error 404)',
                     'ERROR: something nobody has seen before', ''):
            self.assertFalse(lp.classify_ytdlp_error(text).permanent, text)

    def test_status_is_extracted(self):
        v = lp.classify_ytdlp_error('ERROR: x: HTTP Error 404: Not Found')
        self.assertEqual((v.reason, v.http_status), ('ytdlp_http_404', 404))

    def test_yt_dlp_runs_without_the_users_login_so_a_gated_site_404_is_never_permanent(self):
        text = 'ERROR: [Iwara] abc: Unable to download JSON metadata: HTTP Error 404: Not Found'
        self.assertTrue(lp.classify_ytdlp_error(text, 'https://example.com/v').permanent)
        for url in ('https://www.iwara.tv/video/abc/x', 'https://rule34video.com/video/1/x', 'https://hanime.tv/videos/hentai/x'):
            v = lp.classify_ytdlp_error(text, url)
            self.assertEqual((v.reason, v.permanent), ('ytdlp_http_404', False), url)


class GatedHostTests(unittest.TestCase):
    def test_gated_hosts(self):
        for url in ('https://www.iwara.tv/video/x/y', 'https://hanime.tv/videos/hentai/x', 'https://joi.moe/x',
                    'https://the-joi-database.com/x', 'https://discuss.eroscripts.com/t/x/1', 'https://gofile.io/d/x',
                    'https://rule34video.com/video/1/x', 'https://rule34video.party/video/1/x'):
            self.assertTrue(lp.is_gated(url), url)
        for url in ('https://mega.nz/file/x', 'https://pixeldrain.com/u/x', 'https://example.com/'):
            self.assertFalse(lp.is_gated(url), url)

    def test_module_makes_no_network_requests(self):
        src = open(os.path.join(os.path.dirname(HERE), 'scripts', 'link_probe.py'), encoding='utf-8').read()
        for needle in ('urllib.request', 'urlopen', 'import requests', 'subprocess', 'socket', 'http.client'):
            self.assertNotIn(needle, src)


if __name__ == '__main__':
    unittest.main()

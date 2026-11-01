"""link_probe: which failures are proven dead (never retried) and which are merely blocked or unknown."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import link_probe  # noqa: E402

MEGA_FILE = 'https://mega.nz/file/mCxyjJKL#gQyu2ghRzmRgjiyQqNpLkobKQ7SPicS-cN5dH9h2pjw'
R34 = 'https://rule34video.com/video/3253844/jess-christmas-unwrapping/'


class MegaTests(unittest.TestCase):
    def verdict(self, answer, url=MEGA_FILE):
        with mock.patch.object(link_probe, '_mega_api', return_value=answer):
            return link_probe.probe(url)

    def test_removed_blocked_and_expired_are_permanent(self):
        for code, reason in ((-9, 'mega_not_found'), (-16, 'mega_blocked'), (-8, 'mega_expired')):
            v = self.verdict(code)
            self.assertEqual((v.reason, v.http_status, v.permanent), (reason, code, True))

    def test_rate_limit_and_tempunavail_are_not_permanent(self):
        for code in (-3, -4, -6, -18, -19):
            self.assertFalse(self.verdict(code).permanent)

    def test_object_means_alive_and_transport_failure_is_unknown(self):
        self.assertEqual(self.verdict({'s': 1234, 'at': 'x'}), ('mega_alive', None, False))
        self.assertEqual(self.verdict(None), ('mega_api_unreachable', None, False))

    def test_folder_and_legacy_links_use_the_right_command(self):
        with mock.patch.object(link_probe, '_mega_api', return_value=-9) as api:
            link_probe.probe('https://mega.nz/folder/AbCdEfGh#key')
            self.assertEqual(api.call_args.args[0]['a'], 'f')
            self.assertEqual(api.call_args.args[1], 'AbCdEfGh')
            link_probe.probe('https://mega.nz/#!AbCdEfGh!key')
            self.assertEqual(api.call_args.args[0], {'a': 'g', 'p': 'AbCdEfGh'})

    def test_unparseable_mega_link_is_not_probed(self):
        self.assertIsNone(link_probe.probe('https://mega.nz/'))


NOT_FOUND_BODY = '<html><head><title>404 Not Found</title></head><body><h1>Not Found</h1></body></html>'


class PageTests(unittest.TestCase):
    def verdict(self, status, body=''):
        with mock.patch.object(link_probe, '_http_status', return_value=(status, body)):
            return link_probe.probe(R34)

    def test_plain_404_and_410_pages_are_permanent(self):
        self.assertEqual(self.verdict(404, NOT_FOUND_BODY), ('http_404', 404, True))
        self.assertTrue(self.verdict(410, NOT_FOUND_BODY.replace('404', '410')).permanent)

    def test_404_that_is_not_the_plain_page_is_never_permanent(self):
        # a login-gated / private page that happens to carry a 404 status must not be mistaken for a removal
        for body in ('', '<title>Please log in to view this video</title>', '<title>Private video</title>'):
            v = self.verdict(404, body)
            self.assertEqual((v.reason, v.permanent), ('http_404_unrecognised', False))

    def test_blocked_rate_limited_and_server_errors_are_not(self):
        for status in (401, 403, 429, 500, 503, None):
            self.assertFalse(self.verdict(status).permanent, status)
        self.assertEqual(self.verdict(403).reason, 'blocked_403')

    def test_200_is_alive(self):
        self.assertEqual(self.verdict(200), ('page_alive', 200, False))

    def test_other_sites_are_not_probed_and_never_raise(self):
        self.assertIsNone(link_probe.probe('https://example.com/x'))
        with mock.patch.object(link_probe, '_http_status', side_effect=RuntimeError('boom')):
            self.assertIsNone(link_probe.probe(R34))


class ApiSiteTests(unittest.TestCase):
    def verdict(self, url, status, body):
        with mock.patch.object(link_probe, '_get_json', return_value=(status, body)):
            return link_probe.probe(url)

    def test_pixeldrain_only_not_found_is_permanent(self):
        nf = {'success': False, 'value': 'not_found'}
        self.assertEqual(self.verdict('https://pixeldrain.com/u/AbCd1234', 404, nf),
                         ('pixeldrain_not_found', 404, True))
        self.assertTrue(self.verdict('https://pixeldrain.com/l/AbCd1234', 404, nf).permanent)
        self.assertFalse(self.verdict('https://pixeldrain.com/u/AbCd1234', 200, {'success': True}).permanent)
        for status, body in ((429, {'value': 'too_many'}), (403, {'value': 'forbidden'}), (500, None), (None, None)):
            self.assertFalse(self.verdict('https://pixeldrain.com/u/AbCd1234', status, body).permanent)

    def test_pixeldrain_filesystem_links_are_not_probed(self):
        self.assertIsNone(link_probe.probe('https://pixeldrain.com/d/AbCd1234/some/path'))

    def test_yandex(self):
        url = 'https://disk.yandex.com/d/abc'
        self.assertEqual(self.verdict(url, 404, {'error': 'NotFoundError'}), ('yandex_not_found', 404, True))
        self.assertFalse(self.verdict(url, 429, {'error': 'TooManyRequestsError'}).permanent)
        self.assertFalse(self.verdict(url, 200, {'type': 'dir'}).permanent)


class YtdlpTierTests(unittest.TestCase):
    def test_explicit_removals_are_permanent(self):
        for text in ('ERROR: [Eporner] x: Unable to download webpage: HTTP Error 404: Not Found (caused by <HTTPError 404>)',
                     'ERROR: [site] This video has been removed by the uploader',
                     'ERROR: [site] 12: Video does not exist',
                     'ERROR: [generic] Got HTTP Error 410: Gone'):
            self.assertTrue(link_probe.classify_ytdlp_error(text).permanent, text)

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
            self.assertFalse(link_probe.classify_ytdlp_error(text).permanent, text)

    def test_status_is_extracted(self):
        v = link_probe.classify_ytdlp_error('ERROR: x: HTTP Error 404: Not Found')
        self.assertEqual((v.reason, v.http_status), ('ytdlp_http_404', 404))

    def test_used_for_unknown_and_ungated_domains_only_when_ytdlp_is_given(self):
        with mock.patch.object(link_probe, '_probe_ytdlp', return_value=link_probe.Verdict('r', 404, True)) as run:
            self.assertIsNone(link_probe.probe('https://example.com/v/1'))
            self.assertTrue(link_probe.probe('https://example.com/v/1', ['yt-dlp']).permanent)
            self.assertTrue(link_probe.probe('https://www.eporner.com/video-x/y/', ['yt-dlp']).permanent)
            self.assertEqual(run.call_count, 2)

    def test_gated_hosts_never_reach_any_probe(self):
        with mock.patch.object(link_probe, '_probe_ytdlp', side_effect=AssertionError), \
                mock.patch.object(link_probe, '_get_json', side_effect=AssertionError), \
                mock.patch.object(link_probe, '_http_status', side_effect=AssertionError):
            for url in ('https://www.iwara.tv/video/x/y', 'https://hanime.tv/videos/hentai/x', 'https://joi.moe/x',
                        'https://the-joi-database.com/x', 'https://discuss.eroscripts.com/t/x/1', 'https://gofile.io/d/x'):
                self.assertIsNone(link_probe.probe(url, ['yt-dlp']), url)


class GatedSitesTests(unittest.TestCase):
    def test_iwara_is_never_probed(self):
        # anonymous iwara answers 404 for login-only videos exactly as for deleted ones: no usable signal
        with mock.patch.object(link_probe, '_http_status', side_effect=AssertionError('must not request')):
            self.assertIsNone(link_probe.probe('https://www.iwara.tv/video/dJSZxsS1Cxvusg/mmd'))


class ClassifyEntryTests(unittest.TestCase):
    def test_adds_fields_only_when_probed(self):
        with mock.patch.object(link_probe, '_http_status', return_value=(404, NOT_FOUND_BODY)):
            e = link_probe.classify_entry({'link': R34})
        self.assertEqual((e['reason'], e['http_status'], e['permanent']), ('http_404', 404, True))
        self.assertEqual(link_probe.classify_entry({'link': 'https://example.com/x'}), {'link': 'https://example.com/x'})


if __name__ == '__main__':
    unittest.main()

"""iwara.tv and iwara.ai are one app and one backend; the site comes from the link, and the age gate is handled."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import downloadContent as dc  # noqa: E402
import link_probe  # noqa: E402
import link_probe_browser as lpb  # noqa: E402


class SiteTests(unittest.TestCase):
    def test_site_comes_from_the_links_host(self):
        self.assertEqual(dc._iwara_site('https://www.iwara.ai/video/abc/x'), 'www.iwara.ai')
        self.assertEqual(dc._iwara_site('https://iwara.ai/video/abc'), 'www.iwara.ai')
        self.assertEqual(dc._iwara_site('https://www.iwara.tv/video/abc/x'), 'www.iwara.tv')
        self.assertEqual(dc._iwara_site('https://ecchi.iwara.tv/video/abc'), 'www.iwara.tv')

    def test_iwara_tv_headers_are_unchanged_and_iwara_ai_adds_x_site(self):
        self.assertEqual(dc._iwara_site_headers('www.iwara.tv'),
                         {'Origin': 'https://www.iwara.tv', 'Referer': 'https://www.iwara.tv/'})
        self.assertEqual(dc._iwara_site_headers('www.iwara.ai'),
                         {'Origin': 'https://www.iwara.ai', 'Referer': 'https://www.iwara.ai/', 'X-Site': 'www.iwara.ai'})

    def test_iwara_ai_is_a_known_domain_with_the_iwara_handler(self):
        self.assertEqual(dc.check_domain('https://www.iwara.ai/video/abc/x'), 'iwara.ai')
        self.assertIs(dc.DOMAIN_HANDLERS['iwara.ai'], dc.DOMAIN_HANDLERS['iwara.tv'])

    def test_it_is_gated_like_iwara_tv(self):
        self.assertTrue(link_probe.is_gated('https://www.iwara.ai/video/abc/x'))


class OrchestratorTests(unittest.TestCase):
    def test_yt_dlp_is_tried_for_iwara_tv_only(self):
        with mock.patch.object(dc, '_download_iwara_ytdlp', return_value=False) as yt, \
                mock.patch.object(dc, '_download_iwara_api', return_value=True) as api:
            self.assertTrue(dc.download_iwara(None, 'https://www.iwara.ai/video/abc/x', '/tmp'))
            yt.assert_not_called()
            api.assert_called_once()
            self.assertTrue(dc.download_iwara(None, 'https://www.iwara.tv/video/abc/x', '/tmp'))
            yt.assert_called_once()

    def test_falls_through_to_the_browser_for_iwara_ai_too(self):
        with mock.patch.object(dc, '_download_iwara_api', return_value=False), \
                mock.patch.object(dc, '_download_iwara_browser', return_value=True) as browser:
            self.assertTrue(dc.download_iwara('drv', 'https://www.iwara.ai/video/abc/x', '/tmp'))
            browser.assert_called_once()


class ApiTierTests(unittest.TestCase):
    def setUp(self):
        dc._iwara_tokens.clear()
        self.addCleanup(dc._iwara_tokens.clear)

    def test_login_is_per_site_and_sends_the_site_headers(self):
        seen = []

        def fake_urlopen(req, *a, **k):
            seen.append(dict(req.header_items()))
            raise RuntimeError('stop here')
        with mock.patch.object(dc, '_get_secret', return_value='x'), \
                mock.patch.object(dc.urllib.request, 'urlopen', side_effect=fake_urlopen):
            dc._iwara_login('www.iwara.ai')
        headers = {k.lower(): v for k, v in seen[0].items()}
        self.assertEqual((headers['x-site'], headers['origin'], headers['referer']),
                         ('www.iwara.ai', 'https://www.iwara.ai', 'https://www.iwara.ai/'))

    def test_each_site_gets_its_own_token(self):
        logins = []
        with mock.patch.object(dc, '_iwara_login', side_effect=lambda site: logins.append(site) or f'tok-{site}'), \
                mock.patch.object(dc.urllib.request, 'urlopen', side_effect=RuntimeError('stop')):
            dc._download_iwara_api(None, 'https://www.iwara.ai/video/abc/x', '/tmp')
            dc._download_iwara_api(None, 'https://www.iwara.tv/video/abc/x', '/tmp')
            dc._download_iwara_api(None, 'https://www.iwara.ai/video/def/x', '/tmp')
        self.assertEqual(logins, ['www.iwara.ai', 'www.iwara.tv'])      # the third reuses the first
        self.assertEqual(set(dc._iwara_tokens), {'www.iwara.ai', 'www.iwara.tv'})


class BrowserClassifierTests(unittest.TestCase):
    def test_iwara_ai_pages_are_judged_like_iwara_tv(self):
        url = 'https://www.iwara.ai/video/abc123/x'
        state = {'url': url, 'nav_status': 200, 'title': 'Error', 'body': 'HTTP 404 - Not found',
                 'logged_in': True, 'ready': 'complete'}
        self.assertEqual(lpb.classify_page(url, state), ('iwara_not_found', 404, True))
        self.assertFalse(lpb.classify_page(url, dict(state, logged_in=False)).permanent)

    def test_a_page_from_the_other_iwara_site_is_not_confused_for_this_one(self):
        state = {'url': 'https://www.iwara.tv/video/abc123/x', 'nav_status': 200, 'title': 'Error',
                 'body': 'HTTP 404', 'logged_in': True, 'ready': 'complete'}
        self.assertIsNotNone(lpb.classify_page('https://www.iwara.ai/video/abc123/x', state))   # same id, same app
        self.assertIsNone(lpb.classify_page('https://www.iwara.ai/video/other/x', state))


class AdultGateTests(unittest.TestCase):
    """The gate's button is found by its own text inside the overlay, clicked through JS, and waited out."""
    def driver(self, present_after=0):
        d = mock.Mock()
        calls = {'n': 0}
        button = object()

        def find(by, xpath):
            calls['n'] += 1
            if calls['n'] <= present_after:
                return []
            return [button] if not d.clicked else []
        d.clicked = False
        d.find_elements.side_effect = find
        d.execute_script.side_effect = lambda *a: setattr(d, 'clicked', True)
        d.button = button
        return d

    def test_clicks_i_am_over_18_through_javascript_and_confirms_it_went_away(self):
        d = self.driver()
        self.assertTrue(dc._iwara_dismiss_adult_gate(d, timeout=1))
        d.execute_script.assert_called_once_with('arguments[0].click()', d.button)

    def test_waits_a_moment_for_the_overlay_to_be_drawn(self):
        d = self.driver(present_after=3)
        self.assertTrue(dc._iwara_dismiss_adult_gate(d, timeout=3))

    def test_no_gate_means_no_click_and_a_short_wait(self):
        d = mock.Mock()
        d.find_elements.return_value = []
        self.assertFalse(dc._iwara_dismiss_adult_gate(d, timeout=0.3))
        d.execute_script.assert_not_called()

    def test_a_gate_that_will_not_go_away_is_reported_not_looped_on(self):
        d = mock.Mock()
        d.find_elements.return_value = [object()]
        with mock.patch.object(dc.time, 'time', side_effect=iter([0, 0.1, 0.2, 100, 100, 100, 100, 100])), \
                mock.patch.object(dc.time, 'sleep'):
            self.assertFalse(dc._iwara_dismiss_adult_gate(d, timeout=1))

    def test_xpath_targets_only_the_over_18_button_inside_the_overlay(self):
        xp = dc._IWARA_ADULT_GATE_BUTTON
        self.assertIn('adultWarning', xp)
        self.assertIn('over 18', xp)
        self.assertNotIn('Go back', xp)


if __name__ == '__main__':
    unittest.main()

"""link_probe_browser.classify_page: what a signed-in (or signed-out) visitor sees -> verdict."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import link_probe_browser as lpb  # noqa: E402

IWARA = 'https://www.iwara.tv/video/dJSZxsS1Cxvusg/mmd'
R34 = 'https://rule34video.com/video/3253844/jess-christmas-unwrapping/'
HANIME = 'https://hanime.tv/videos/hentai/some-title-1'


def state(url=None, **kw):
    base = {'url': url or IWARA, 'nav_status': 200, 'title': 'x', 'body': '', 'logged_in': True, 'ready': 'complete'}
    base.update(kw)
    return base


class IwaraTests(unittest.TestCase):
    def v(self, title, body='', logged_in=True, url=IWARA):
        return lpb.classify_page(IWARA, state(url=url, title=title, body=body, logged_in=logged_in))

    def test_signed_in_not_found_page_is_permanent_and_a_titled_page_is_alive(self):
        self.assertEqual(self.v('Error', 'Iwara | 0 | Error | HTTP 404 - Not found'), ('iwara_not_found', 404, True))
        self.assertEqual(self.v('Some video title | Iwara'), ('iwara_alive', 200, False))

    def test_signed_out_not_found_is_never_permanent(self):
        # the exact false positive: a login-only video looks identical to a deleted one when signed out
        v = self.v('Error', 'HTTP 404 - Not found', logged_in=False)
        self.assertEqual((v.reason, v.permanent), ('iwara_not_found_logged_out', False))

    def test_other_error_pages_are_not_permanent(self):
        for code in (401, 403, 429, 500):
            self.assertFalse(self.v('Error', f'HTTP {code} - nope').permanent)
        self.assertFalse(self.v('Error', 'something else entirely').permanent)

    def test_a_page_for_a_different_video_is_never_judged(self):
        self.assertIsNone(self.v('Error', 'HTTP 404 - Not found', url='https://www.iwara.tv/video/otherId/x'))

    def test_non_video_urls_are_not_classified(self):
        self.assertIsNone(lpb.classify_page('https://www.iwara.tv/playlist/abc', state()))


class Rule34Tests(unittest.TestCase):
    def test_not_found_page_signed_in_is_permanent(self):
        v = lpb.classify_page(R34, state(url=R34, title='Page not Found', body='x | 404 / Page not found | y'))
        self.assertEqual((v.reason, v.permanent), ('rule34video_not_found', True))

    def test_not_found_page_signed_out_is_not(self):
        v = lpb.classify_page(R34, state(url=R34, title='Page not Found', logged_in=False))
        self.assertEqual((v.reason, v.permanent), ('rule34video_not_found_logged_out', False))

    def test_a_normal_page_is_alive(self):
        self.assertEqual(lpb.classify_page(R34, state(url=R34, title='[Artist] Some video')), ('page_alive', None, False))

    def test_login_wall_text_is_not_a_removal(self):
        v = lpb.classify_page(R34, state(url=R34, title='Login required', body='please log in to view this video'))
        self.assertFalse(v.permanent)


class HanimeTests(unittest.TestCase):
    def test_real_404_signed_in_only(self):
        self.assertTrue(lpb.classify_page(HANIME, state(url=HANIME, nav_status=404, title='Page not found')).permanent)
        self.assertFalse(lpb.classify_page(HANIME, state(url=HANIME, nav_status=404, title='Page not found',
                                                         logged_in=False)).permanent)

    def test_ordinary_page_is_unknown(self):
        self.assertIsNone(lpb.classify_page(HANIME, state(url=HANIME, nav_status=200, title='A title')))


class WiringTests(unittest.TestCase):
    def test_other_gated_sites_have_no_signature(self):
        for url in ('https://joi.moe/x', 'https://discuss.eroscripts.com/t/x/1', 'https://gofile.io/d/x'):
            self.assertIsNone(lpb.classify_page(url, state(url=url)))

    def test_reads_the_current_page_without_navigating(self):
        driver = mock.Mock()
        driver.execute_script.return_value = state(url=R34, title='Page not Found')
        v = lpb.verdict_from_current_page(driver, R34)
        self.assertTrue(v.permanent)
        driver.get.assert_not_called()
        driver.execute_cdp_cmd.assert_not_called()
        driver.switch_to.new_window.assert_not_called()

    def test_a_broken_browser_gives_no_verdict(self):
        driver = mock.Mock()
        driver.execute_script.side_effect = RuntimeError('browser gone')
        self.assertIsNone(lpb.verdict_from_current_page(driver, R34))

    def test_other_hosts_never_touch_the_browser(self):
        driver = mock.Mock()
        self.assertIsNone(lpb.verdict_from_current_page(driver, 'https://example.com/x'))
        driver.execute_script.assert_not_called()


if __name__ == '__main__':
    unittest.main()

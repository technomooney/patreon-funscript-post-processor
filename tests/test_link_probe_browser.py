"""link_probe_browser.classify_page: what a signed-in (or signed-out) visitor sees -> verdict."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import link_probe  # noqa: E402
import link_probe_browser as lpb  # noqa: E402

IWARA = 'https://www.iwara.tv/video/dJSZxsS1Cxvusg/mmd'
R34 = 'https://rule34video.com/video/3253844/jess-christmas-unwrapping/'
HANIME = 'https://hanime.tv/videos/hentai/some-title-1'


def state(**kw):
    base = {'nav_status': 200, 'title': 'x', 'body': '', 'calls': [], 'logged_in': True, 'ready': 'complete'}
    base.update(kw)
    return base


class IwaraTests(unittest.TestCase):
    def v(self, status, logged_in=True, vid='dJSZxsS1Cxvusg'):
        calls = [{'u': '//api.iwara.tv/user/token', 's': 200}]
        if status is not None:
            calls.append({'u': f'//api.iwara.tv/video/{vid}', 's': status})
        return lpb.classify_page(IWARA, state(calls=calls, logged_in=logged_in))

    def test_signed_in_404_is_permanent_and_200_is_alive(self):
        self.assertEqual(self.v(404), ('iwara_not_found', 404, True))
        self.assertEqual(self.v(200), ('iwara_alive', 200, False))

    def test_signed_out_404_is_never_permanent(self):
        # the exact false positive: a login-only video looks identical to a deleted one when signed out
        self.assertEqual(self.v(404, logged_in=False), ('iwara_not_found_logged_out', 404, False))

    def test_other_statuses_and_no_answer_are_not_permanent(self):
        for status in (401, 403, 429, 500):
            self.assertFalse(self.v(status).permanent)
        self.assertEqual(self.v(None), ('iwara_no_answer', None, False))

    def test_a_404_for_a_different_video_is_ignored(self):
        self.assertEqual(self.v(404, vid='someOtherId').reason, 'iwara_no_answer')

    def test_non_video_urls_are_not_classified(self):
        self.assertIsNone(lpb.classify_page('https://www.iwara.tv/playlist/abc', state()))


class Rule34Tests(unittest.TestCase):
    def test_not_found_page_signed_in_is_permanent(self):
        v = lpb.classify_page(R34, state(title='Page not Found', body='x | 404 / Page not found | y'))
        self.assertEqual((v.reason, v.permanent), ('rule34video_not_found', True))

    def test_not_found_page_signed_out_is_not(self):
        v = lpb.classify_page(R34, state(title='Page not Found', logged_in=False))
        self.assertEqual((v.reason, v.permanent), ('rule34video_not_found_logged_out', False))

    def test_a_normal_page_is_alive(self):
        self.assertEqual(lpb.classify_page(R34, state(title='[Artist] Some video')), ('page_alive', None, False))

    def test_login_wall_text_is_not_a_removal(self):
        v = lpb.classify_page(R34, state(title='Login required', body='please log in to view this video'))
        self.assertFalse(v.permanent)


class HanimeTests(unittest.TestCase):
    def test_real_404_signed_in_only(self):
        self.assertTrue(lpb.classify_page(HANIME, state(nav_status=404, title='Page not found')).permanent)
        self.assertFalse(lpb.classify_page(HANIME, state(nav_status=404, title='Page not found',
                                                         logged_in=False)).permanent)

    def test_ordinary_page_is_unknown(self):
        self.assertIsNone(lpb.classify_page(HANIME, state(nav_status=200, title='A title')))


class WiringTests(unittest.TestCase):
    def test_other_gated_sites_have_no_signature(self):
        for url in ('https://joi.moe/x', 'https://discuss.eroscripts.com/t/x/1', 'https://gofile.io/d/x'):
            self.assertIsNone(lpb.classify_page(url, state()))

    def test_link_probe_hands_gated_hosts_to_the_browser_callable_only(self):
        asked = []
        browser = lambda url: asked.append(url) or link_probe.Verdict('r', 404, True)   # noqa: E731
        self.assertTrue(link_probe.probe(IWARA, None, browser).permanent)
        self.assertIsNone(link_probe.probe(IWARA))                  # no browser: unknown
        with mock.patch.object(link_probe, '_mega_api', return_value=-9):
            link_probe.probe('https://mega.nz/file/AbCdEfGh#k', None, browser)
        self.assertEqual(asked, [IWARA])                            # an ungated site never touches the browser

    def test_probe_does_nothing_for_other_hosts(self):
        self.assertIsNone(lpb.probe(object(), 'https://example.com/x'))


if __name__ == '__main__':
    unittest.main()

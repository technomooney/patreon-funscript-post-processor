"""downloadContent records what a failed attempt learned, from the attempt itself, never by asking a second time."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import downloadContent as dc  # noqa: E402
import link_probe as lp  # noqa: E402


class ApplyVerdictTests(unittest.TestCase):
    def tearDown(self):
        dc._note_failure(None)
        dc._probe_browser = None

    def test_the_attempts_own_answer_goes_on_the_row(self):
        dc._note_failure(lp.mega_exit_verdict(9))
        e = {'link': 'https://mega.nz/file/AbCdEfGh#k'}
        dc._apply_failure_verdict(e)
        self.assertEqual((e['reason'], e['http_status'], e['permanent']), ('mega_not_found', -9, True))

    def test_no_answer_leaves_the_row_alone(self):
        e = {'link': 'https://example.com/x'}
        dc._apply_failure_verdict(e)
        self.assertEqual(e, {'link': 'https://example.com/x'})

    def test_gated_site_uses_the_logged_in_page_and_it_outranks_the_attempt(self):
        dc._note_failure(lp.Verdict('ytdlp_http_404', 404, True))
        dc._probe_browser = lambda url: lp.Verdict('iwara_alive', 200, False)
        e = {'link': 'https://www.iwara.tv/video/abc/x'}
        dc._apply_failure_verdict(e)
        self.assertEqual((e['reason'], e['permanent']), ('iwara_alive', False))

    def test_gated_site_never_trusts_an_attempt_made_without_the_login(self):
        dc._note_failure(lp.Verdict('ytdlp_http_404', 404, True))     # e.g. from a caller that forgot to check
        dc._probe_browser = lambda url: None                           # and the page says nothing
        e = {'link': 'https://www.iwara.tv/video/abc/x'}
        dc._apply_failure_verdict(e)
        self.assertEqual((e['reason'], e['permanent']), ('ytdlp_http_404', False))

    def test_ungated_site_never_asks_the_browser(self):
        dc._probe_browser = mock.Mock()
        dc._note_failure(lp.Verdict('pixeldrain_not_found', 404, True))
        e = {'link': 'https://pixeldrain.com/u/abc'}
        dc._apply_failure_verdict(e)
        dc._probe_browser.assert_not_called()
        self.assertTrue(e['permanent'])


class HttpErrorValueTests(unittest.TestCase):
    def error(self, body, consumed=False):
        e = urllib.error.HTTPError('https://x/api', 404, 'Not Found', {}, io.BytesIO(body))
        if consumed:
            e.read()                        # what fetch_diagnostics does before the handler sees the error
            e.body_bytes = body
        return e

    def test_reads_the_json_value(self):
        self.assertEqual(dc._http_error_value(self.error(b'{"value": "not_found"}'), 'value'), 'not_found')

    def test_uses_the_body_fetch_diagnostics_kept(self):
        self.assertEqual(dc._http_error_value(self.error(b'{"error": "NotFoundError"}', True), 'error'), 'NotFoundError')

    def test_garbage_or_missing_bodies_give_none(self):
        self.assertIsNone(dc._http_error_value(self.error(b'<html>nope</html>'), 'value'))
        self.assertIsNone(dc._http_error_value(self.error(b'{"value": 5}'), 'value'))
        self.assertIsNone(dc._http_error_value(self.error(b'[]'), 'value'))


class YtdlpRunTests(unittest.TestCase):
    def tearDown(self):
        dc._note_failure(None)

    def run_with(self, returncode, stderr, url='https://example.com/v'):
        done = subprocess.CompletedProcess(['yt-dlp'], returncode, None, stderr)
        with mock.patch.object(subprocess, 'run', return_value=done) as run, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            result = dc._run_ytdlp_download(['yt-dlp', url], url)
        return result, run, err.getvalue()

    def test_failure_is_classified_from_stderr_and_echoed(self):
        result, run, echoed = self.run_with(1, 'ERROR: [site] x: HTTP Error 404: Not Found\n')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(run.call_count, 1)                    # one run, no second request
        self.assertIn('HTTP Error 404', echoed)
        self.assertEqual(dc._last_failure.reason, 'ytdlp_http_404')
        self.assertTrue(dc._last_failure.permanent)

    def test_success_records_nothing(self):
        self.run_with(0, '')
        self.assertIsNone(dc._last_failure)

    def test_only_stderr_is_captured_so_progress_stays_live(self):
        _, run, _ = self.run_with(0, '')
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs['stderr'], subprocess.PIPE)
        self.assertNotIn('stdout', kwargs)
        self.assertNotIn('capture_output', kwargs)


if __name__ == '__main__':
    unittest.main()

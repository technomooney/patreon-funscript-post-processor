"""The relay's background fill is used by the pipeline: settings, priority ranges, and the hand-over to the download."""
import email.message
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import downloadContent as dc
import remote_cache_relay as rcr

MB = 1024 * 1024


class Origin:
    def __init__(self, size, fail_after=None):
        self.data = random.Random(5).randbytes(size)
        self.streams = []
        self.fail_after = fail_after

    def fetch(self, url, headers, start, end, timeout=60):
        return self.data[start:end + 1]

    def stream(self, url, headers, start, end, timeout=60):
        self.streams.append((start, end))
        pos, sent = start, 0
        while pos <= end:
            if self.fail_after is not None and sent >= self.fail_after:
                raise rcr.RemoteError('boom')
            stop = min(pos + rcr.BLOCK - 1, end)
            yield self.data[pos:stop + 1]
            pos, sent = stop + 1, sent + 1


def held_relay(origin, url='https://cdn.example/v.mkv', name='Real Title.mkv'):
    relay = rcr.CacheRelay(url, {}, len(origin.data), None, fetch=origin.fetch, stream=origin.stream)
    relay.start()
    relay.start_fill([(1 * MB, 1 * MB + 10)])
    headers = email.message.Message()
    headers['Content-Disposition'] = f'attachment; filename="{name}"'
    return {'url': url, 'relay': relay, 'head_headers': headers, 'size': len(origin.data)}


class SettingsTests(unittest.TestCase):
    def wanted(self, size, **env):
        with mock.patch.dict(os.environ, env, clear=False):
            for k in ('RELAY_PREFETCH', 'RELAY_PREFETCH_MAX_MB'):
                if k not in env:
                    os.environ.pop(k, None)
            return dc._relay_prefetch_wanted(size)

    def test_off_unless_asked_for(self):
        self.assertFalse(self.wanted(10 * MB))
        self.assertFalse(self.wanted(10 * MB, RELAY_PREFETCH='false'))
        self.assertTrue(self.wanted(10 * MB, RELAY_PREFETCH='true'))

    def test_the_size_cap_applies_and_zero_means_no_cap(self):
        self.assertFalse(self.wanted(11 * MB, RELAY_PREFETCH='true', RELAY_PREFETCH_MAX_MB='10'))
        self.assertTrue(self.wanted(10 * MB, RELAY_PREFETCH='true', RELAY_PREFETCH_MAX_MB='10'))
        self.assertTrue(self.wanted(50_000 * MB, RELAY_PREFETCH='true', RELAY_PREFETCH_MAX_MB='0'))


class PriorityRangeTests(unittest.TestCase):
    def test_ranges_start_at_the_file_start_and_stay_inside_the_file(self):
        size = 800 * MB
        with mock.patch.object(dc, '_remote_index_bytes', 0):
            ranges = dc._relay_priority_ranges(size, 1800.0)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(len(ranges), 1 + 2 * len(dc._audio_segments(1800.0)) + len(dc._visual_sample_positions(1800.0)))
        self.assertTrue(all(0 <= a <= b <= size - 1 for a, b in ranges))
        centres = [(a + b) // 2 for a, b in ranges[-len(dc._visual_sample_positions(1800.0)):]]
        self.assertEqual(centres, sorted(centres))             # the frame samples run through the file in order


class HandOverTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.part = os.path.join(self.dir, 'x.part')
        self.addCleanup(lambda: dc._close_held_relay())
        self.addCleanup(__import__('shutil').rmtree, self.dir, True)

    def test_the_download_is_written_from_the_fill_without_asking_again(self):
        origin = Origin(2 * MB + 12345)
        dc._held_relay = held_relay(origin)
        with mock.patch('builtins.print'):
            ext = dc._write_from_held_relay('https://cdn.example/v.mkv', self.part, None)
        self.assertEqual(ext, '.mkv')
        with open(self.part, 'rb') as f:
            self.assertEqual(f.read(), origin.data)
        self.assertIsNone(dc._held_relay)
        self.assertEqual(dc._last_fetch_original_name, 'Real Title.mkv')
        self.assertLessEqual(len(origin.streams), 3)           # the priority range, and the gaps around it

    def test_a_fill_that_fails_leaves_no_partial_file_and_signals_a_normal_download(self):
        origin = Origin(2 * MB, fail_after=3)
        dc._held_relay = held_relay(origin)
        with mock.patch('builtins.print'):
            self.assertIsNone(dc._write_from_held_relay('https://cdn.example/v.mkv', self.part, None))
        self.assertFalse(os.path.exists(self.part))

    def test_a_relay_for_another_url_is_dropped(self):
        origin = Origin(1 * MB)
        dc._held_relay = held_relay(origin, url='https://cdn.example/other.mkv')
        self.assertIsNone(dc._write_from_held_relay('https://cdn.example/v.mkv', self.part, None))
        self.assertIsNone(dc._held_relay)
        self.assertFalse(os.path.exists(self.part))

    def test_nothing_held_means_a_normal_download(self):
        dc._held_relay = None
        self.assertIsNone(dc._write_from_held_relay('https://cdn.example/v.mkv', self.part, None))


class FailedFillMemoryTests(unittest.TestCase):
    def setUp(self):
        dc._fill_refused_hosts.clear()
        self.addCleanup(dc._fill_refused_hosts.clear)

    def test_a_failed_fill_is_logged_and_the_host_is_not_filled_again(self):
        relay = mock.Mock(fill_error='the server ignored the Range header (HTTP 200)')
        with mock.patch.object(dc.fetch_diagnostics, 'log_fetch_failure') as log, mock.patch('builtins.print'):
            dc._note_fill_outcome('https://cdn.example/a.mp4', {}, relay)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][3], 'FILL')
        with mock.patch.dict(os.environ, {'RELAY_PREFETCH': 'true'}):
            self.assertFalse(dc._relay_prefetch_wanted(10 * MB, 'https://cdn.example/b.mp4'))
            self.assertTrue(dc._relay_prefetch_wanted(10 * MB, 'https://other.example/b.mp4'))

    def test_a_clean_fill_is_not_logged(self):
        with mock.patch.object(dc.fetch_diagnostics, 'log_fetch_failure') as log:
            dc._note_fill_outcome('https://cdn.example/a.mp4', {}, mock.Mock(fill_error=''))
        log.assert_not_called()
        self.assertEqual(dc._fill_refused_hosts, set())


class KeepOrCloseTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, self.dir, True)
        self.addCleanup(lambda: setattr(dc, '_held_relay', None))
        dc._held_relay = None
        self.relay = mock.Mock()
        self.relay.summary.return_value = ''
        self.relay.fill_error = ''
        open(os.path.join(self.dir, 'a.mp4'), 'wb').close()

    def run_av(self, verdict, prefetch=True, candidates=True):
        env = {'RELAY_PREFETCH': 'true' if prefetch else 'false'}
        with mock.patch.dict(os.environ, env), mock.patch.object(dc, '_open_relay', return_value=(self.relay, 'http://127.0.0.1:1/x/relay.mp4')), \
                mock.patch.object(dc, '_video_duration', return_value=100.0 if candidates else 5.0), \
                mock.patch.object(dc, '_precheck_av_stages', return_value=verdict), mock.patch('builtins.print'):
            return dc._precheck_av('https://cdn.example/v.mp4', {}, self.dir, 100.0, 5 * MB, None)

    def test_a_download_verdict_keeps_the_relay(self):
        self.assertIsNone(self.run_av(None))
        self.assertIs(dc._held_relay['relay'], self.relay)
        self.relay.close.assert_not_called()

    def test_a_skip_verdict_closes_it(self):
        self.assertEqual(self.run_av('duplicate'), 'duplicate')
        self.assertIsNone(dc._held_relay)
        self.relay.close.assert_called_once()

    def test_without_prefetch_it_is_always_closed(self):
        self.assertIsNone(self.run_av(None, prefetch=False))
        self.assertIsNone(dc._held_relay)
        self.relay.close.assert_called_once()

    def test_no_duration_match_with_prefetch_starts_the_download_early(self):
        self.assertIsNone(self.run_av(None, candidates=False))
        self.assertIs(dc._held_relay['relay'], self.relay)

    def test_an_error_in_the_checks_closes_it(self):
        with mock.patch.dict(os.environ, {'RELAY_PREFETCH': 'true'}), \
                mock.patch.object(dc, '_open_relay', return_value=(self.relay, 'http://127.0.0.1:1/x/relay.mp4')), \
                mock.patch.object(dc, '_video_duration', return_value=100.0), \
                mock.patch.object(dc, '_precheck_av_stages', side_effect=RuntimeError('x')), mock.patch('builtins.print'):
            with self.assertRaises(RuntimeError):
                dc._precheck_av('https://cdn.example/v.mp4', {}, self.dir, 100.0, 5 * MB, None)
        self.relay.close.assert_called_once()
        self.assertIsNone(dc._held_relay)


if __name__ == '__main__':
    unittest.main()

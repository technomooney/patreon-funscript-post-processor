"""A video whose size matches an existing file to the byte is settled by a few sampled reads, not a full download."""
import contextlib
import io
import os
import random
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import downloadContent as dc

SIZE = 51 * 1024 * 1024      # over the 50 MB line below which the code downloads and hashes instead
CHUNK = 64 * 1024


class FakeHead:
    def __init__(self, length):
        self.headers = {'Content-Length': str(length)}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _PrecheckBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.local = os.path.join(self.dir, "1_Hour_Of_Futa's_Cumming.mp4")
        with open(self.local, 'wb') as f:                  # sparse, with distinctive bytes where a sample can land
            f.truncate(SIZE)
            rnd = random.Random(1)
            for i in range(dc._VIDEO_SAMPLE_CHUNKS):
                f.seek(self._offset(i, dc._VIDEO_SAMPLE_CHUNKS))
                f.write(rnd.randbytes(CHUNK))
            f.seek(0)
            f.write(rnd.randbytes(CHUNK))
        self.remote_path = self.local
        self.range_calls = 0
        self.duration_calls = 0
        self.remote_length = SIZE

    @staticmethod
    def _offset(i, n):
        return round(i * (SIZE - CHUNK) / (n - 1))

    def tearDown(self):
        self._tmp.cleanup()

    def range_bytes(self, url, headers, start, length, timeout=30):
        self.range_calls += 1
        with open(self.remote_path, 'rb') as f:
            f.seek(start)
            return f.read(length)

    def duration(self, url, headers):
        self.duration_calls += 1
        return None            # the CDN refuses ffprobe, as in the real case

    def run_precheck(self, range_bytes=None):
        with mock.patch.object(dc.urllib.request, 'urlopen', side_effect=lambda *a, **k: FakeHead(self.remote_length)), \
                mock.patch.object(dc, '_name_from_response', return_value='1-hour-of-futa-s-cumming_1080p.mp4'), \
                mock.patch.object(dc, '_remote_range_bytes', range_bytes or self.range_bytes), \
                mock.patch.object(dc, '_remote_video_duration', self.duration), \
                contextlib.redirect_stdout(io.StringIO()):
            return dc._precheck_url('https://cdn.example/x.mp4', {}, self.dir)



class PrecheckSameSizeVideoTests(_PrecheckBase):
    def test_identical_file_with_a_different_name_is_skipped_without_the_duration_probe(self):
        reason = self.run_precheck()
        self.assertIn("identical to existing 1_Hour_Of_Futa's_Cumming.mp4", reason)
        self.assertIn('start, end and sampled blocks match', reason)
        self.assertEqual(self.range_calls, dc._VIDEO_SAMPLE_CHUNKS)      # a dozen small reads, not a hundred
        self.assertEqual(self.duration_calls, 0)

    def test_one_differing_sample_means_it_is_not_treated_as_a_duplicate(self):
        remote_dir = tempfile.TemporaryDirectory()                   # the remote copy must not sit in the library folder
        self.addCleanup(remote_dir.cleanup)
        other = os.path.join(remote_dir.name, 'remote.bin')
        shutil.copyfile(self.local, other)
        with open(other, 'r+b') as f:
            f.seek(self._offset(5, dc._VIDEO_SAMPLE_CHUNKS) + 100)    # damage just one of the sampled blocks
            f.write(b'\xff\xfe\xfd')
        self.remote_path = other
        self.assertIsNone(self.run_precheck())            # falls through to the old pipeline, which proceeds
        self.assertEqual(self.duration_calls, 1)

    def test_a_server_without_ranged_reads_falls_back_to_the_old_behaviour(self):
        self.assertIsNone(self.run_precheck(range_bytes=lambda *a, **k: None))
        self.assertEqual(self.duration_calls, 1)

    def test_a_different_size_never_asks_for_samples(self):
        self.remote_length = SIZE + 1
        self.assertIsNone(self.run_precheck())
        self.assertEqual(self.range_calls, 0)

    def test_the_sampling_helper_reads_each_remote_block_once_however_many_candidates(self):
        other = os.path.join(self.dir, 'other.mp4')
        with open(other, 'wb') as f:
            f.truncate(SIZE)
            f.write(random.Random(2).randbytes(CHUNK))
        cands = [('other.mp4', other), ("1_Hour_Of_Futa's_Cumming.mp4", self.local)]
        with mock.patch.object(dc, '_remote_range_bytes', self.range_bytes), contextlib.redirect_stdout(io.StringIO()):
            match = dc._sampled_identical('u', {}, SIZE, cands, 6)
        self.assertEqual(match[0], "1_Hour_Of_Futa's_Cumming.mp4")
        self.assertEqual(self.range_calls, 6)


class SlowCdnTests(_PrecheckBase):
    """The real case: the CDN answers the start and end of a big file at once but a read deep inside can hang."""
    def slow_middle(self, hang_from=2, only=None):
        calls = []

        def fake(url, headers, start, length, timeout=30):
            calls.append(start)
            index = round(start * (dc._VIDEO_SAMPLE_CHUNKS - 1) / (SIZE - CHUNK))
            if (only is None and 1 <= index <= dc._VIDEO_SAMPLE_CHUNKS - 2) or (only is not None and index in only):
                dc._range_note = 'TimeoutError: The read operation timed out'
                return None
            return self.range_bytes(url, headers, start, length, timeout)
        fake.calls = calls
        return fake

    def run_with(self, fake):
        out = io.StringIO()
        with mock.patch.object(dc.urllib.request, 'urlopen', side_effect=lambda *a, **k: FakeHead(SIZE)), \
                mock.patch.object(dc, '_name_from_response', return_value='x_1080p.mp4'), \
                mock.patch.object(dc, '_remote_range_bytes', fake), \
                mock.patch.object(dc, '_remote_video_duration', self.duration), \
                contextlib.redirect_stdout(out):
            return dc._precheck_url('https://cdn.example/x.mp4', {}, self.dir), out.getvalue()

    def test_some_slow_middle_blocks_are_skipped_and_the_rest_still_settle_it(self):
        result, text = self.run_with(self.slow_middle(only={3, 4, 7, 8}))
        self.assertIn('identical to existing', result)
        self.assertIn('4 slow block(s) skipped; comparing the 8 read', text)
        self.assertEqual(self.duration_calls, 0)

    def test_ends_are_read_first_so_a_slow_middle_cannot_hide_them(self):
        fake = self.slow_middle()
        result, _ = self.run_with(fake)
        self.assertIsNone(result)                    # only the 2 ends were readable: fewer than the 4 required
        self.assertEqual(fake.calls[:2], [0, SIZE - CHUNK])

    def test_exactly_the_minimum_is_enough(self):
        result, _ = self.run_with(self.slow_middle(only={2, 3, 4, 5, 6, 7, 8, 9}))     # ends + blocks 1 and 10 = 4
        self.assertIn('identical to existing', result)

    def test_an_unreadable_first_or_last_block_gives_up(self):
        for index in (0, dc._VIDEO_SAMPLE_CHUNKS - 1):
            result, text = self.run_with(self.slow_middle(only={index}))
            self.assertIsNone(result)
            self.assertIn('sampled reads not possible', text)

    def test_a_differing_block_among_those_read_still_means_not_identical(self):
        remote_dir = tempfile.TemporaryDirectory()
        self.addCleanup(remote_dir.cleanup)
        other = os.path.join(remote_dir.name, 'remote.bin')
        shutil.copyfile(self.local, other)
        with open(other, 'r+b') as f:
            f.seek(self._offset(5, dc._VIDEO_SAMPLE_CHUNKS) + 7)
            f.write(b'\x00\x01')
        self.remote_path = other
        result, text = self.run_with(self.slow_middle(only={2}))
        self.assertIsNone(result)
        self.assertIn('sampled block 6/12 differs', text)

    def test_sampling_stops_after_the_time_budget_and_judges_by_what_it_has(self):
        clock = iter([0, 0, 0, 0, 0, 0, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100])
        with mock.patch.object(dc.time, 'time', side_effect=lambda: next(clock, 100)):
            fake = self.slow_middle(only=set())
            result, text = self.run_with(fake)
        self.assertIn('identical to existing', result)
        self.assertLess(len(fake.calls), dc._VIDEO_SAMPLE_CHUNKS)


class PrecheckExplainsItselfTests(_PrecheckBase):
    def run_and_capture(self, **kw):
        out = io.StringIO()
        with mock.patch.object(dc.urllib.request, 'urlopen', side_effect=lambda *a, **k: FakeHead(self.remote_length)), \
                mock.patch.object(dc, '_name_from_response', return_value='1-hour-of-futa-s-cumming_1080p.mp4'), \
                mock.patch.object(dc, '_remote_range_bytes', kw.get('range_bytes') or self.range_bytes), \
                mock.patch.object(dc, '_remote_video_duration', self.duration), \
                contextlib.redirect_stdout(out):
            result = dc._precheck_url('https://cdn.example/x.mp4', {}, self.dir)
        return result, out.getvalue()

    def test_names_the_file_with_the_same_size(self):
        _, text = self.run_and_capture()
        self.assertIn("same size to the byte as: 1_Hour_Of_Futa's_Cumming.mp4", text)

    def test_says_when_the_sampled_reads_are_refused_and_why(self):
        def refuse(url, headers, start, length, timeout=30):
            dc._range_note = 'HTTP 403'
            return None
        result, text = self.run_and_capture(range_bytes=refuse)
        self.assertIsNone(result)
        self.assertIn('sampled reads not possible (HTTP 403)', text)

    def test_says_which_block_differed(self):
        remote_dir = tempfile.TemporaryDirectory()
        self.addCleanup(remote_dir.cleanup)
        other = os.path.join(remote_dir.name, 'remote.bin')
        shutil.copyfile(self.local, other)
        with open(other, 'r+b') as f:
            f.seek(self._offset(3, dc._VIDEO_SAMPLE_CHUNKS) + 7)
            f.write(b'\x00\x01\x02')
        self.remote_path = other
        _, text = self.run_and_capture()
        self.assertIn('sampled block 4/12 differs', text)

    def test_says_when_the_server_gave_no_size(self):
        self.remote_length = 0
        _, text = self.run_and_capture()
        self.assertIn('the server gave no size', text)


class RangeNoteTests(unittest.TestCase):
    def fake_response(self, status, body=b'x' * 10):
        resp = mock.MagicMock()
        resp.status = status
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        return resp

    def test_a_200_for_a_non_zero_offset_means_the_range_header_was_ignored(self):
        with mock.patch.object(dc.urllib.request, 'urlopen', return_value=self.fake_response(200)):
            self.assertIsNone(dc._remote_range_bytes('https://cdn.example/x.mp4', {}, 1000, 10))
        self.assertIn('ignored the Range header', dc._range_note)

    def test_a_200_at_offset_zero_is_fine(self):
        with mock.patch.object(dc.urllib.request, 'urlopen', return_value=self.fake_response(200)):
            self.assertEqual(dc._remote_range_bytes('https://cdn.example/x.mp4', {}, 0, 10), b'x' * 10)

    def test_206_is_fine_and_http_errors_are_named(self):
        with mock.patch.object(dc.urllib.request, 'urlopen', return_value=self.fake_response(206)):
            self.assertEqual(dc._remote_range_bytes('https://cdn.example/x.mp4', {}, 5000, 10), b'x' * 10)
        err = dc.urllib.error.HTTPError('https://cdn.example/x.mp4', 403, 'Forbidden', {}, io.BytesIO(b''))
        with mock.patch.object(dc.urllib.request, 'urlopen', side_effect=err):
            self.assertIsNone(dc._remote_range_bytes('https://cdn.example/x.mp4', {}, 5000, 10))
        self.assertEqual(dc._range_note, 'HTTP 403')


class DurationProbeNoteTests(unittest.TestCase):
    def probe(self, **run_kwargs):
        with mock.patch.object(dc.shutil, 'which', return_value='/usr/bin/ffprobe'), \
                mock.patch.object(dc, '_remote_mp4_duration', return_value=None), \
                mock.patch.object(dc.subprocess, 'run', **run_kwargs):
            return dc._remote_video_duration('https://cdn.example/x.mp4', {'Referer': 'r'})

    def test_success_returns_the_duration_and_no_note(self):
        done = mock.Mock(stdout='317.5\n', stderr='', returncode=0)
        self.assertEqual(self.probe(return_value=done), 317.5)
        self.assertEqual(dc._duration_probe_note, '')

    def test_a_timeout_is_named(self):
        self.assertIsNone(self.probe(side_effect=dc.subprocess.TimeoutExpired('ffprobe', dc._DURATION_PROBE_TIMEOUT)))
        self.assertEqual(dc._duration_probe_note, f'ffprobe timed out after {dc._DURATION_PROBE_TIMEOUT}s')

    def test_an_ffprobe_error_shows_its_last_line(self):
        done = mock.Mock(stdout='', stderr='noise\nServer returned 403 Forbidden (access denied)\n', returncode=1)
        self.assertIsNone(self.probe(return_value=done))
        self.assertEqual(dc._duration_probe_note, 'ffprobe error: Server returned 403 Forbidden (access denied)')

    def test_missing_ffprobe_is_named(self):
        with mock.patch.object(dc.shutil, 'which', return_value=None), \
                mock.patch.object(dc, '_remote_mp4_duration', return_value=None):
            self.assertIsNone(dc._remote_video_duration('u', {}))
        self.assertEqual(dc._duration_probe_note, 'ffprobe not installed')


def box(kind: bytes, payload: bytes = b'', large: bool = False) -> bytes:
    if large:
        return (1).to_bytes(4, 'big') + kind + (16 + len(payload)).to_bytes(8, 'big') + payload
    return (8 + len(payload)).to_bytes(4, 'big') + kind + payload


def mvhd(duration: int, timescale: int, version: int = 0) -> bytes:
    if version == 0:
        body = bytes([0, 0, 0, 0]) + (0).to_bytes(4, 'big') * 2 + timescale.to_bytes(4, 'big') + duration.to_bytes(4, 'big')
    else:
        body = bytes([1, 0, 0, 0]) + (0).to_bytes(8, 'big') * 2 + timescale.to_bytes(4, 'big') + duration.to_bytes(8, 'big')
    return box(b'mvhd', body + bytes(60))        # the rest of the real box (rate, volume, matrix, ...)


class RemoteMp4DurationTests(unittest.TestCase):
    """The duration comes from a few tiny ranged reads of the movie header, whatever the file size."""
    def serve(self, data, supports_ranges=True):
        self.reads = []

        def fake(url, headers, start, length, timeout=30):
            self.reads.append((start, length))
            if not supports_ranges:
                return None
            return data[start:start + length] or None
        return fake

    def duration(self, data, **kw):
        with mock.patch.object(dc, '_remote_range_bytes', self.serve(data, **kw)):
            return dc._remote_mp4_duration('https://cdn.example/x.mp4', {})

    FTYP = box(b'ftyp', b'isom' + bytes(4) + b'isomiso2')

    def test_index_first(self):
        data = self.FTYP + box(b'moov', mvhd(3600005, 1000) + bytes(500)) + box(b'mdat', bytes(1000))
        self.assertAlmostEqual(self.duration(data), 3600.005)
        self.assertEqual(len(self.reads), 3)                       # ftyp header, moov header, mvhd
        self.assertLessEqual(sum(n for _, n in self.reads), 16 + 16 + 160)

    def test_index_last_is_found_by_jumping_over_the_media_data(self):
        data = self.FTYP + box(b'free') + box(b'mdat', bytes(50_000)) + box(b'moov', mvhd(90, 30) + bytes(100))
        self.assertAlmostEqual(self.duration(data), 3.0)
        self.assertTrue(all(n <= 160 for _, n in self.reads))      # never reads the media data

    def test_64_bit_header_and_64_bit_box_sizes(self):
        data = self.FTYP + box(b'mdat', bytes(2000), large=True) + box(b'moov', mvhd(7_200_000, 1000, version=1) + bytes(40))
        self.assertAlmostEqual(self.duration(data), 7200.0)

    def test_not_an_mp4_or_no_range_support_or_damage_gives_none(self):
        self.assertIsNone(self.duration(b'RIFF' + bytes(100) + b'AVI '))
        self.assertIsNone(self.duration(self.FTYP + box(b'moov', mvhd(1000, 1000)), supports_ranges=False))
        self.assertIsNone(self.duration(self.FTYP + (0).to_bytes(4, 'big') + b'mdat' + bytes(20)))   # size 0 before a moov
        self.assertIsNone(self.duration(self.FTYP))                                                    # ends without a moov

    def test_a_fragmented_mp4_with_no_stored_duration_is_not_reported_as_zero_seconds(self):
        self.assertIsNone(self.duration(self.FTYP + box(b'moov', mvhd(0, 1000) + bytes(10))))

    def test_zero_timescale_and_unknown_version_give_none(self):
        self.assertIsNone(self.duration(self.FTYP + box(b'moov', mvhd(1000, 0) + bytes(10))))
        bad = bytearray(self.FTYP + box(b'moov', mvhd(1000, 1000) + bytes(10)))
        bad[len(self.FTYP) + 8 + 8] = 7                            # mvhd version byte
        self.assertIsNone(self.duration(bytes(bad)))

    def test_the_main_probe_uses_the_header_and_never_starts_ffprobe(self):
        data = self.FTYP + box(b'moov', mvhd(317_500, 1000) + bytes(100))
        with mock.patch.object(dc, '_remote_range_bytes', self.serve(data)), \
                mock.patch.object(dc.subprocess, 'run', side_effect=AssertionError('ffprobe must not run')):
            self.assertAlmostEqual(dc._remote_video_duration('https://cdn.example/x.mp4', {}), 317.5)

    def test_non_mp4_falls_back_to_ffprobe(self):
        done = mock.Mock(stdout='12.5\n', stderr='', returncode=0)
        with mock.patch.object(dc, '_remote_range_bytes', self.serve(b'\x1aE\xdf\xa3' + bytes(100))), \
                mock.patch.object(dc.shutil, 'which', return_value='/usr/bin/ffprobe'), \
                mock.patch.object(dc.subprocess, 'run', return_value=done) as run:
            self.assertEqual(dc._remote_video_duration('https://cdn.example/x.mkv', {}), 12.5)
        run.assert_called_once()


class LongVideoIndexTests(unittest.TestCase):
    """A 3 hour video has a ~15 MB index that every ffmpeg run must read at CDN speed: limits and frame counts follow."""
    MB = 1024 * 1024

    def setUp(self):
        self.addCleanup(setattr, dc, '_remote_index_bytes', dc._remote_index_bytes)

    def at(self, index_mb):
        dc._remote_index_bytes = int(index_mb * self.MB)

    def test_short_videos_keep_the_original_limits_and_the_full_frame_allowance(self):
        self.at(0.4)
        self.assertEqual((dc._scaled_timeout(60), dc._scaled_timeout(180), dc._scaled_timeout(30)), (60, 180, 30))
        self.assertEqual(dc._frame_samples_for_index(), 12)

    def test_an_hour_long_index_still_fits_the_old_frame_limit_barely_and_takes_six_frames(self):
        self.at(5.2)
        self.assertGreaterEqual(dc._scaled_timeout(60), 90)         # ~63 s to read at the slowest speed seen
        self.assertEqual(dc._frame_samples_for_index(), 6)

    def test_three_hour_index_gets_minutes_not_a_minute_and_five_frames(self):
        self.at(15.5)
        self.assertGreaterEqual(dc._scaled_timeout(60), 190)        # 15.5 MB / 85 KB/s = 187 s to read, plus margin
        self.assertGreaterEqual(dc._scaled_timeout(180), 270)
        self.assertEqual(dc._frame_samples_for_index(), 5)

    def test_the_limit_is_capped(self):
        self.at(200)
        self.assertEqual(dc._scaled_timeout(60), dc._MAX_SCALED_TIMEOUT)

    def test_the_mp4_walker_records_the_index_size_and_a_new_probe_resets_it(self):
        data = RemoteMp4DurationTests.FTYP + box(b'moov', mvhd(10_800_000, 1000) + bytes(3000)) + box(b'mdat', b'')
        with mock.patch.object(dc, '_remote_range_bytes', RemoteMp4DurationTests().serve(data)):
            self.assertAlmostEqual(dc._remote_video_duration('https://cdn.example/x.mp4', {}), 10800.0)
        self.assertEqual(dc._remote_index_bytes, 8 + len(mvhd(10_800_000, 1000)) + 3000)
        with mock.patch.object(dc, '_remote_range_bytes', lambda *a, **k: None), \
                mock.patch.object(dc.shutil, 'which', return_value=None):
            dc._remote_video_duration('https://cdn.example/y.mp4', {})
        self.assertEqual(dc._remote_index_bytes, 0)

    def test_the_frame_stage_uses_the_scaled_limit_and_the_reduced_count(self):
        self.at(15.5)
        seen = {}

        def fake_run(cmd, **kw):
            seen['timeout'] = kw.get('timeout')
            return mock.Mock(returncode=1, stdout=b'')
        with mock.patch.object(dc.shutil, 'which', return_value='/usr/bin/ffmpeg'), \
                mock.patch.object(dc.subprocess, 'run', fake_run):
            dc._stream_frame('https://cdn.example/x.mp4', {}, '00:09:00')
        self.assertGreaterEqual(seen['timeout'], 190)
        frames = []
        with mock.patch.object(dc, '_stream_frame', side_effect=lambda u, h, ts: frames.append(ts)), \
                mock.patch.object(dc, '_video_frame_hash', return_value=None), \
                mock.patch.object(dc.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            dc._remote_visually_similar('https://cdn.example/x.mp4', {}, 3 * 3600.0, '/nonexistent.mp4')
        self.assertEqual(len(set(frames)), 5)


class RemoteFrameSharingTests(unittest.TestCase):
    def test_two_candidates_share_one_extraction_of_each_remote_frame(self):
        calls = []
        shared = {}
        with mock.patch.object(dc, '_stream_frame', side_effect=lambda u, h, ts: calls.append(ts) or b'x' * 1024), \
                mock.patch.object(dc, '_video_frame_hash', return_value=b'x' * 1024), \
                contextlib.redirect_stdout(io.StringIO()):
            for path in ('/a.mp4', '/b.mp4'):
                dc._remote_visually_similar('https://cdn.example/x.mp4', {}, 600.0, path, shared)
        self.assertEqual(len(calls), len(set(calls)))


class FrameExtractRetryTests(unittest.TestCase):
    def test_a_frame_is_retried_three_times_before_it_counts_as_failed(self):
        calls = []
        with mock.patch.object(dc.time, 'sleep'):
            result = dc._extract_with_retries(lambda: calls.append(1))
        self.assertIsNone(result)
        self.assertEqual(len(calls), 4)          # first try + 3 retries

    def test_a_retry_that_succeeds_stops_retrying(self):
        results = iter([None, None, b'x' * 1024])
        with mock.patch.object(dc.time, 'sleep'):
            self.assertEqual(dc._extract_with_retries(lambda: next(results)), b'x' * 1024)


if __name__ == '__main__':
    unittest.main()

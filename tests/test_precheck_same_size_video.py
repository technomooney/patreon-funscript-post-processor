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

    def range_bytes(self, url, headers, start, length):
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
        self.assertIsNone(self.run_precheck(range_bytes=lambda *a: None))
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
        def refuse(url, headers, start, length):
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
                mock.patch.object(dc.subprocess, 'run', **run_kwargs):
            return dc._remote_video_duration('https://cdn.example/x.mp4', {'Referer': 'r'})

    def test_success_returns_the_duration_and_no_note(self):
        done = mock.Mock(stdout='317.5\n', stderr='', returncode=0)
        self.assertEqual(self.probe(return_value=done), 317.5)
        self.assertEqual(dc._duration_probe_note, '')

    def test_a_timeout_is_named(self):
        self.assertIsNone(self.probe(side_effect=dc.subprocess.TimeoutExpired('ffprobe', 30)))
        self.assertEqual(dc._duration_probe_note, 'ffprobe timed out after 30s')

    def test_an_ffprobe_error_shows_its_last_line(self):
        done = mock.Mock(stdout='', stderr='noise\nServer returned 403 Forbidden (access denied)\n', returncode=1)
        self.assertIsNone(self.probe(return_value=done))
        self.assertEqual(dc._duration_probe_note, 'ffprobe error: Server returned 403 Forbidden (access denied)')

    def test_missing_ffprobe_is_named(self):
        with mock.patch.object(dc.shutil, 'which', return_value=None):
            self.assertIsNone(dc._remote_video_duration('u', {}))
        self.assertEqual(dc._duration_probe_note, 'ffprobe not installed')


if __name__ == '__main__':
    unittest.main()

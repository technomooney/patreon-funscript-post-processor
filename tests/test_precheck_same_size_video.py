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


class PrecheckSameSizeVideoTests(unittest.TestCase):
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

if __name__ == '__main__':
    unittest.main()

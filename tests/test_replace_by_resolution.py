import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import downloadContent as dc  # noqa: E402


def q(w, h):
    return {'width': w, 'height': h}


class ReplacementByResolutionTests(unittest.TestCase):
    def check(self, remote, local, rsize=100, lsize=100, cap='1080'):
        with mock.patch.dict(os.environ, {'MAX_RESOLUTION': cap}):
            return dc._quality_is_replacement_candidate(remote, local, rsize, lsize)

    def test_higher_resolution_within_the_cap_is_an_upgrade_even_if_bigger(self):
        self.assertTrue(self.check(q(1920, 1080), q(1280, 720), rsize=500, lsize=100))

    def test_higher_resolution_above_the_cap_is_not_an_upgrade(self):
        self.assertFalse(self.check(q(3840, 2160), q(1280, 720)))

    def test_upgrade_stops_at_the_cap_not_beyond(self):
        self.assertTrue(self.check(q(1280, 720), q(854, 480), cap='720'))
        self.assertFalse(self.check(q(1920, 1080), q(854, 480), cap='720'))

    def test_local_above_the_cap_is_never_replaced_even_when_remote_is_smaller(self):
        self.assertFalse(self.check(q(1920, 1080), q(3840, 2160), rsize=10, lsize=100))
        self.assertFalse(self.check(q(3840, 2160), q(3840, 2160), rsize=10, lsize=100))

    def test_lower_remote_resolution_is_never_a_replacement(self):
        self.assertFalse(self.check(q(854, 480), q(1920, 1080), rsize=10, lsize=100))

    def test_same_resolution_still_replaced_only_when_smaller(self):
        self.assertTrue(self.check(q(1920, 1080), q(1920, 1080), rsize=50, lsize=100))
        self.assertFalse(self.check(q(1920, 1080), q(1920, 1080), rsize=150, lsize=100))

    def test_vertical_video_uses_the_short_side(self):
        self.assertFalse(self.check(q(1080, 1920), q(1080, 1920), rsize=150, lsize=100))
        self.assertTrue(self.check(q(1080, 1920), q(720, 1280)))


if __name__ == '__main__':
    unittest.main()

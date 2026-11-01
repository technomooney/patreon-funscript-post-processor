import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import mega_cooldown as mc


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cd = mc.Cooldown([60, 300, 900], self.clock)

    def test_free_to_start_until_the_first_hit(self):
        self.assertEqual(self.cd.remaining(), 0)

    def test_each_hit_waits_longer_and_the_last_interval_repeats(self):
        self.assertEqual([self.cd.hit() for _ in range(5)], [60, 300, 900, 900, 900])

    def test_remaining_counts_down_to_zero(self):
        self.cd.hit()
        self.clock.t += 45
        self.assertEqual(self.cd.remaining(), 15)
        self.clock.t += 20
        self.assertEqual(self.cd.remaining(), 0)

    def test_success_resets_both_the_wait_and_the_escalation(self):
        self.cd.hit()
        self.cd.hit()
        self.cd.ok()
        self.assertEqual(self.cd.remaining(), 0)
        self.assertEqual(self.cd.hit(), 60)

    def test_until_is_the_time_the_pause_ends(self):
        self.cd.hit()
        self.assertEqual(self.cd.until(), 1060.0)


class LevelsFromEnvTests(unittest.TestCase):
    def test_minutes_become_seconds(self):
        self.assertEqual(mc.levels_from_env('5,15, 30'), [300, 900, 1800])
        self.assertEqual(mc.levels_from_env('0.5'), [30])

    def test_bad_or_missing_values_fall_back_to_the_default(self):
        default = [m * 60 for m in mc.DEFAULT_MINUTES]
        for raw in (None, '', 'abc', '5,x', '0', '-3,5'):
            self.assertEqual(mc.levels_from_env(raw), default, raw)


if __name__ == '__main__':
    unittest.main()

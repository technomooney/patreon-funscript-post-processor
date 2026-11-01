import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import mega_cooldown as mc

LEVELS = [300, 900, 1800, 3600]


class Clock:
    def __init__(self):
        self.t = 100000.0

    def __call__(self):
        return self.t


class FakeStore:
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.events = []

    def load(self):
        return dict(self.state)

    def save(self, state):
        self.state = dict(state)

    def log(self, event, waited, next_wait, bad, good, note=''):
        self.events.append((event, waited, next_wait, bad, good))


class World:
    """MEGA with a rate limit that lasts `limit` seconds: an attempt made after waiting at least that long works."""
    def __init__(self, limit, store=None, levels=LEVELS):
        self.clock = Clock()
        self.store = store or FakeStore()
        self.said = []
        self.cd = mc.Cooldown(levels, self.clock, self.store, self.said.append)
        self.limit = limit
        self.tests = []          # every wait that was tested, in order

    def new_limit(self):
        """MEGA rate limits an ordinary attempt (no pause pending)."""
        return self.cd.hit()

    def test(self):
        """Wait out the current pause, then make the attempt. Returns True when the limit had cleared."""
        wait = self.cd.remaining()
        self.tests.append(int(wait))
        self.clock.t += wait
        self.cd.begin_attempt()
        if wait >= self.limit:
            self.cd.ok()
            return True
        self.cd.hit()
        return False

    def settle(self, max_tests=30):
        """Keep testing until a pause works."""
        for _ in range(max_tests):
            if self.test():
                return
        raise AssertionError('never cleared')


class CooldownMechanicsTests(unittest.TestCase):
    def test_free_to_start_until_the_first_hit(self):
        self.assertEqual(World(600).cd.remaining(), 0)

    def test_first_pause_is_the_first_level_and_counts_down(self):
        w = World(600)
        self.assertEqual(w.new_limit(), 300)
        w.clock.t += 200
        self.assertEqual(w.cd.remaining(), 100)
        w.clock.t += 150
        self.assertEqual(w.cd.remaining(), 0)

    def test_ok_clears_the_pause_at_once(self):
        w = World(600)
        w.new_limit()
        w.cd.ok()
        self.assertEqual(w.cd.remaining(), 0)

    def test_until_is_when_the_pause_ends(self):
        w = World(600)
        w.new_limit()
        self.assertEqual(w.cd.until(), w.clock.t + 300)


class HoningInTests(unittest.TestCase):
    def test_climbs_5_then_15_when_the_limit_lasts_ten_minutes(self):
        w = World(600)
        w.new_limit()
        w.settle()
        self.assertEqual(w.tests, [300, 900])
        self.assertEqual((w.cd.bad, w.cd.good), (300, 900))

    def test_next_limit_bisects_between_what_failed_and_what_worked(self):
        w = World(780)                      # the real limit is 13 minutes
        w.new_limit(); w.settle()           # 5 fails, 15 works
        w.tests.clear()
        w.new_limit(); w.settle()
        self.assertEqual(w.tests, [600, 780])           # 10 fails, 13 works
        w.tests.clear()
        w.new_limit(); w.settle()
        self.assertEqual(w.tests, [720, 780])           # 12 fails, 13 works: within a minute, so it is settled
        self.assertEqual((w.cd.bad, w.cd.good), (720, 780))
        self.assertTrue(w.cd._converged())

    def test_the_users_example_5_15_then_10_13_14(self):
        w = World(900)                      # needs the full 15 minutes
        w.new_limit(); w.settle()
        self.assertEqual(w.tests, [300, 900])
        w.tests.clear()
        w.new_limit(); w.settle()           # 5 already failed: do not repeat it
        w.new_limit(); w.settle()
        w.new_limit(); w.settle()
        self.assertEqual(w.tests[:3], [600, 780, 840])  # 10, 13, 14 minutes
        self.assertTrue(w.cd._converged())

    def test_once_settled_it_waits_exactly_that_long_without_testing_lower_again(self):
        w = World(900)
        for _ in range(6):
            w.new_limit(); w.settle()
        w.tests.clear()
        for _ in range(3):
            w.new_limit(); w.settle()
        self.assertEqual(w.tests, [900, 900, 900])

    def test_a_short_limit_is_found_and_then_honed_downwards(self):
        w = World(120)                      # clears in 2 minutes
        w.new_limit(); w.settle()           # 5 minutes works
        self.assertEqual(w.cd.good, 300)
        w.new_limit(); w.settle()
        self.assertLess(w.cd.good, 300)     # it tried less than 5
        for _ in range(8):
            w.new_limit(); w.settle()
        self.assertLessEqual(w.cd.good - w.cd.bad, 60 + 60)
        self.assertGreaterEqual(w.cd.good, 120)

    def test_when_the_settled_wait_stops_working_it_climbs_again(self):
        w = World(900)
        for _ in range(6):
            w.new_limit(); w.settle()
        self.assertEqual(w.cd.good, 900)
        w.limit = 1500                      # MEGA got stricter
        w.new_limit(); w.settle()
        self.assertEqual(w.cd.good, 1800)   # climbed past 900 to the next level that works
        self.assertEqual(w.cd.bad, 900)

    def test_never_waits_longer_than_the_last_level_when_nothing_works(self):
        w = World(10 ** 9)
        w.new_limit()
        for _ in range(7):
            w.test()
        self.assertEqual(w.tests[:4], [300, 900, 1800, 3600])
        self.assertTrue(all(t == 3600 for t in w.tests[3:]))

    def test_an_attempt_that_came_late_counts_for_the_time_it_really_waited(self):
        w = World(600)
        w.new_limit()                       # plans 5 minutes
        w.clock.t += 3000                   # but the run only got back to mega after 50 minutes
        w.cd.begin_attempt()
        w.cd.ok()
        self.assertEqual(w.cd.good, 3000)

    def test_a_late_attempt_that_is_still_limited_proves_the_long_wait_was_not_enough(self):
        w = World(10 ** 9)
        w.new_limit()
        w.clock.t += 3000
        w.cd.begin_attempt()
        w.cd.hit()
        self.assertEqual(w.cd.bad, 3000)

    def test_ok_without_a_pending_test_changes_nothing(self):
        w = World(600)
        w.cd.ok()
        self.assertEqual((w.cd.good, w.cd.bad, w.store.events), (None, 0, []))

    def test_a_failure_remembered_from_an_earlier_run_is_climbed_past_not_repeated(self):
        w = World(1500, store=FakeStore({'bad': 900}))
        self.assertEqual(w.new_limit(), 1800)            # 5 and 15 are known not to be enough
        w.settle()
        self.assertEqual((w.cd.bad, w.cd.good), (900, 1800))


class LoggingAndPersistenceTests(unittest.TestCase):
    def test_every_step_is_logged_and_said(self):
        w = World(600)
        w.new_limit(); w.settle()
        self.assertEqual([e[0] for e in w.store.events], ['hit', 'still-limited', 'cleared'])
        self.assertTrue(any('waiting 5m' in m for m in w.said))
        self.assertTrue(any('waiting 15m' in m for m in w.said))
        self.assertTrue(any('a 15m wait worked' in m for m in w.said))

    def test_state_is_saved_and_picked_up_by_the_next_run(self):
        first = World(780)
        first.new_limit(); first.settle()
        second = World(780, store=FakeStore(first.store.state))
        second.new_limit()
        self.assertEqual(second.cd.remaining(), 600)   # bisects between 5 (bad) and 15 (good) straight away

    def test_nonsense_saved_state_is_ignored(self):
        for state in ({}, {'good': 'x', 'bad': None}, {'good': 300, 'bad': 900}, {'good': -5}):
            w = World(600, store=FakeStore(state))
            self.assertGreater(w.new_limit(), 0, state)
            self.assertLessEqual(w.cd.bad, w.cd.good or 10 ** 9)

    def test_app_db_store_roundtrip_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = mc.AppDbStore(os.path.join(tmp, 'app.db'))
            self.assertEqual(store.load(), {})
            store.save({'good': 900, 'bad': 300})
            self.assertEqual(store.load(), {'good': 900, 'bad': 300})
            store.log('hit', None, 300, 0, None, 'new limit; waiting 5m')
            import sqlite3
            con = sqlite3.connect(os.path.join(tmp, 'app.db'))
            rows = con.execute('SELECT event, next_wait_s, note FROM mega_ratelimit_events').fetchall()
            con.close()
            self.assertEqual(rows, [('hit', 300, 'new limit; waiting 5m')])

    def test_a_broken_store_never_stops_a_download(self):
        store = mc.AppDbStore('/nonexistent-dir/definitely/app.db')
        self.assertEqual(store.load(), {})
        store.save({'good': 1})
        store.log('hit', None, 1, 0, None)


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

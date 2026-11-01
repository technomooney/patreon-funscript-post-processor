"""A shared "don't touch MEGA yet" timer that works out how long a MEGA rate limit really lasts.

mega-get exits 6 ("too many requests for this resource") when MEGA pushes back, and MEGA publishes no figure for how
long that lasts. So instead of guessing, every pause is an experiment and its outcome is remembered:

* No data yet: wait the first level (default 5 min), and if the next attempt is STILL limited, climb to the next
  (15, then 30, then 60 min) until one works. The wait that worked becomes `good`.
* Later limits, once a `good` is known: test between the longest wait that failed (`bad`) and `good` (so 5 failed and
  15 worked gives 10, then 13, then 14 min ...) until the two are within a minute. Waits already known to fail are
  never tested again. The wait it settles on is used from then on, and if even that fails the search climbs again.

Every pause applies to every mega-get (the foreground one and the background retry worker alike), and the very first
attempt after a pause is the test. The outcome counts only when the server really answered: exit 6 is "still
limited", a normal answer (a download, or a dead link) is "cleared", and anything else (timeouts, our own stall
kill) tells us nothing. The state survives between runs (the machine-wide app database) and each step is written as an
event row there and echoed to the console, so the resulting numbers can be inspected and tuned.

MEGA_RETRY_MINUTES gives the climbing steps (a comma-separated list of minutes).
"""
import math
import threading
import time

DEFAULT_MINUTES = (5, 15, 30, 60)
STEP = 60          # seconds: the search stops once good and bad are this close
_STATE_KEY = 'mega_calibration'


def levels_from_env(raw: str | None) -> list[int]:
    """Seconds per climbing step from a string like "5,15,30,60"; anything unusable falls back to the default."""
    try:
        minutes = [float(p) for p in (raw or '').replace(';', ',').split(',') if p.strip()]
    except ValueError:
        minutes = []
    if not minutes or any(m <= 0 for m in minutes):
        minutes = list(DEFAULT_MINUTES)
    return [int(m * 60) for m in minutes]


def _round_up_minute(seconds: float) -> int:
    return int(math.ceil(seconds / 60.0)) * 60


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return 'unknown'
    m, s = divmod(int(round(seconds)), 60)
    return f'{m}m{s:02d}s' if s else f'{m}m'


class AppDbStore:
    """Where the calibration lives: two small things in the machine-wide app database (app_db), because the limit
    follows the MEGA account and the IP address, not one library. Every call is best-effort: a locked or unwritable
    database must never stop a download."""

    def __init__(self, path: str | None = None):
        self._path = path

    def _connect(self):
        import app_db
        con = app_db.connect(self._path)
        con.execute('CREATE TABLE IF NOT EXISTS mega_ratelimit_events ('
                    'id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, event TEXT, waited_s INTEGER, '
                    'next_wait_s INTEGER, bad_s INTEGER, good_s INTEGER, note TEXT)')
        return con

    def load(self) -> dict:
        import json
        try:
            con = self._connect()
            try:
                import app_db
                raw = app_db.get_meta(con, _STATE_KEY)
            finally:
                con.close()
            state = json.loads(raw) if raw else {}
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def save(self, state: dict) -> None:
        import json
        try:
            con = self._connect()
            try:
                import app_db
                with con:
                    app_db.set_meta(con, _STATE_KEY, json.dumps(state))
            finally:
                con.close()
        except Exception:
            pass

    def log(self, event: str, waited, next_wait, bad, good, note: str = '') -> None:
        try:
            con = self._connect()
            try:
                with con:
                    con.execute('INSERT INTO mega_ratelimit_events(at, event, waited_s, next_wait_s, bad_s, good_s, '
                                'note) VALUES(?,?,?,?,?,?,?)',
                                (time.strftime('%Y-%m-%dT%H:%M:%S'), event, waited, next_wait, bad, good, note))
            finally:
                con.close()
        except Exception:
            pass


class Cooldown:
    def __init__(self, levels_seconds: list[int], clock=time.time, store=None, say=None):
        self.levels = list(levels_seconds)
        self._clock = clock
        self._store = store
        self._say = say or (lambda msg: None)
        self._lock = threading.RLock()
        self._loaded = store is None
        self.good: int | None = None     # shortest wait known to clear the limit
        self.bad = 0                     # longest wait known NOT to clear it (0 = none known)
        self._until = 0.0
        self._hit_at = 0.0
        self._testing: int | None = None     # the wait currently being tested, until an attempt answers it
        self._waited: int | None = None      # what that attempt actually waited, set when it begins

    # -- state -----------------------------------------------------------------------------------------------
    def _load_once(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        state = self._store.load()
        good, bad = state.get('good'), state.get('bad', 0)
        self.good = int(good) if isinstance(good, (int, float)) and good > 0 else None
        self.bad = int(bad) if isinstance(bad, (int, float)) and bad > 0 else 0
        if self.good is not None and self.bad >= self.good:
            self.bad = 0

    def _save(self) -> None:
        if self._store is not None:
            self._store.save({'good': self.good, 'bad': self.bad})

    def _event(self, event: str, waited, next_wait, note: str = '') -> None:
        self._say(f'rate limit {event}: ' + note)
        if self._store is not None:
            self._store.log(event, None if waited is None else int(waited),
                            None if next_wait is None else int(next_wait), self.bad or None, self.good, note)

    def _converged(self) -> bool:
        return self.good is not None and self.good - self.bad <= STEP

    def _next_wait(self) -> int:
        """The wait to test next, from what is known."""
        if self.good is None:                         # still climbing: the first step longer than anything that failed
            return next((lv for lv in self.levels if lv > self.bad), self.levels[-1])
        if self._converged():
            return self.good
        if self.bad == 0:                             # nothing failed yet: try the shortest step, or half of good
            first = self.levels[0]
            return first if first < self.good else max(STEP, min(self.good - STEP, _round_up_minute(self.good / 2)))
        mid = _round_up_minute(self.bad + (self.good - self.bad) / 2)
        return max(self.bad + STEP, min(mid, self.good - STEP))

    def _effective(self, planned: int, actual: int | None) -> int:
        """What a test really measured: the plan, unless the attempt came much later than the pause ended."""
        return planned if actual is None or actual - planned <= STEP else actual

    # -- the pause --------------------------------------------------------------------------------------------
    def remaining(self) -> float:
        """Seconds until a mega-get may start again (0 when it may start now)."""
        with self._lock:
            return max(0.0, self._until - self._clock())

    def until(self) -> float:
        with self._lock:
            return self._until

    def begin_attempt(self) -> None:
        """A mega-get is about to run: if it is the test of a pause, note how long it really waited."""
        with self._lock:
            if self._testing is not None and self._waited is None:
                self._waited = max(0, int(self._clock() - self._hit_at))

    def hit(self) -> int:
        """MEGA answered exit 6. Returns how many seconds every mega-get must now wait."""
        with self._lock:
            self._load_once()
            if self._testing is not None:
                waited = self._effective(self._testing, self._waited)
                self.bad = max(self.bad, waited)
                if self.good is not None and self.bad >= self.good:
                    self.good = None                  # the wait that used to work does not any more: climb again
                head = f'still limited after waiting {_fmt(waited)}'
            else:
                head = 'new limit'                    # keep what is known: never re-test a wait that already failed
            wait = self._next_wait()
            self._testing, self._waited = wait, None
            self._hit_at = self._clock()
            self._until = self._hit_at + wait
            self._save()
            self._event('hit' if head == 'new limit' else 'still-limited', None, wait,
                        f'{head}; waiting {_fmt(wait)} (longest failed {_fmt(self.bad) if self.bad else "none"}, '
                        f'shortest that worked {_fmt(self.good)})')
            return wait

    def ok(self) -> None:
        """MEGA answered normally: the limit has cleared."""
        with self._lock:
            self._load_once()
            if self._testing is not None:
                waited = self._effective(self._testing, self._waited)
                self.good = waited if self.good is None else min(self.good, waited)
                if self.bad >= self.good:
                    self.bad = 0
                self._save()
                state = 'settled' if self._converged() else 'narrowing'
                self._event('cleared', waited, None,
                            f'a {_fmt(waited)} wait worked ({state}: longest failed '
                            f'{_fmt(self.bad) if self.bad else "none"}, shortest that worked {_fmt(self.good)})')
            self._testing, self._waited, self._until = None, None, 0.0

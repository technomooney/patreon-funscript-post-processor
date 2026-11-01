"""A shared "don't touch MEGA yet" timer, so a rate limit is waited out instead of hammered.

mega-get exits 6 ("too many requests for this resource") when MEGA pushes back. MEGA publishes no figure for how
long that lasts, so the wait escalates: the first hit pauses every mega-get (the foreground one and the background
retry worker alike) for the first interval, each further hit for the next, and the last interval repeats. Any
success resets it. The intervals are the user's to tune (MEGA_RETRY_MINUTES, a comma-separated list of minutes).
"""
import threading
import time

DEFAULT_MINUTES = (5, 15, 30, 60)


def levels_from_env(raw: str | None) -> list[int]:
    """Seconds per escalation step from a string like "5,15,30,60"; anything unusable falls back to the default."""
    try:
        minutes = [float(p) for p in (raw or '').replace(';', ',').split(',') if p.strip()]
    except ValueError:
        minutes = []
    if not minutes or any(m <= 0 for m in minutes):
        minutes = list(DEFAULT_MINUTES)
    return [int(m * 60) for m in minutes]


class Cooldown:
    def __init__(self, levels_seconds: list[int], clock=time.time):
        self.levels = list(levels_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._until = 0.0
        self._level = 0

    def remaining(self) -> float:
        """Seconds until a mega-get may start again (0 when it may start now)."""
        with self._lock:
            return max(0.0, self._until - self._clock())

    def until(self) -> float:
        with self._lock:
            return self._until

    def hit(self) -> int:
        """Rate limited again: start the next, longer pause. Returns its length in seconds."""
        with self._lock:
            delay = self.levels[min(self._level, len(self.levels) - 1)]
            self._level += 1
            self._until = self._clock() + delay
            return delay

    def ok(self) -> None:
        """A download went through: the limit has cleared."""
        with self._lock:
            self._level = 0
            self._until = 0.0

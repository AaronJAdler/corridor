"""The application clock.

Business time is read here and passed into SQL as a parameter, never taken from the database
with ``now()``. That gives the code one time source, and lets a test move time forward
without sleeping.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that moves only when told to. For tests and the simulators."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("a clock needs a timezone-aware start")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0) -> datetime:
        delta = timedelta(seconds=seconds, minutes=minutes, hours=hours)
        if delta < timedelta(0):
            raise ValueError("a clock does not run backwards")
        self._now += delta
        return self._now


_clock: Clock = SystemClock()


def utcnow() -> datetime:
    """The current time, timezone-aware, in UTC."""
    return _clock.now()


@contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    """Replace the process clock for the duration of the block."""
    global _clock
    previous, _clock = _clock, clock
    try:
        yield clock
    finally:
        _clock = previous

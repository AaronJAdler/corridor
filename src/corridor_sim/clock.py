"""The simulator's clock, and the contract's timestamp format.

The simulator keeps its own time and shares no clock with Corridor. In manual mode time
moves only when a test says so, which makes every time-driven behaviour reproducible. In
realtime mode it follows the wall clock, which is what a running stack needs.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from corridor_sim.errors import ApiError

ClockMode = Literal["manual", "realtime"]

# Where a manual clock starts unless it is told otherwise.
DEFAULT_START: Final = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _wall_clock() -> datetime:
    return datetime.now(UTC)


class SimClock:
    def __init__(
        self,
        mode: ClockMode,
        start: datetime | None = None,
        *,
        wall_clock: Callable[[], datetime] = _wall_clock,
    ) -> None:
        self.mode: Final[ClockMode] = mode
        self._wall_clock = wall_clock
        if mode == "manual":
            self._now = _in_utc(start if start is not None else DEFAULT_START)
        else:
            self._now = _in_utc(wall_clock())

    def now(self) -> datetime:
        if self.mode == "realtime":
            # The wall clock can be stepped back under a running process. Everything here
            # assumes time only moves forward, so a backward step reads as time standing
            # still until the wall clock has caught up.
            self._now = max(self._now, _in_utc(self._wall_clock()))
        return self._now

    def advance(self, seconds: float) -> datetime:
        """Move a manual clock forward. A realtime clock is not the test's to move."""
        if self.mode == "realtime":
            raise ApiError(
                409, "clock_is_realtime", "The clock follows the wall clock and cannot be advanced."
            )
        delta = timedelta(seconds=seconds)
        if delta < timedelta(0):
            raise ValueError("a clock does not run backwards")
        self._now += delta
        return self._now


def format_time(moment: datetime) -> str:
    """ISO-8601 in UTC with a ``Z``, as the contract writes it.

    A whole second is written without a fraction. Anything finer keeps all six digits, so
    that a timestamp read back from a response names the same instant the books hold.
    """
    moment = _in_utc(moment)
    precision = "microseconds" if moment.microsecond else "seconds"
    return moment.replace(tzinfo=None).isoformat(timespec=precision) + "Z"


def parse_time(text: str) -> datetime:
    """Read an ISO-8601 timestamp that names an instant. Raises ``ValueError`` otherwise."""
    problem = "a time is ISO-8601 with a UTC offset, such as 2026-01-15T12:00:00Z"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(problem) from None
    if moment.tzinfo is None:
        raise ValueError(problem)
    return moment.astimezone(UTC)


def _in_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("a time needs a timezone")
    return moment.astimezone(UTC)

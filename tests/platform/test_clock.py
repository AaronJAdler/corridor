from datetime import UTC, datetime, timedelta

import pytest

from corridor.platform.clock import ManualClock, use_clock, utcnow


def test_the_system_clock_is_timezone_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is UTC
    assert abs(datetime.now(UTC) - now) < timedelta(seconds=5)


def test_a_manual_clock_replaces_the_system_clock_and_is_restored() -> None:
    start = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    with use_clock(ManualClock(start)) as clock:
        assert isinstance(clock, ManualClock)
        assert utcnow() == start
        clock.advance(seconds=31)
        assert utcnow() == start + timedelta(seconds=31)
    assert utcnow() > start + timedelta(days=1)


def test_a_manual_clock_refuses_naive_time_and_going_backwards() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(datetime(2026, 1, 15, 12, 0))  # noqa: DTZ001 - the point of the test
    clock = ManualClock(datetime(2026, 1, 15, 12, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(seconds=-1)

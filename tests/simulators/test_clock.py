"""The simulator's clock, and how it writes and reads the contract's timestamps."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from corridor_sim.clock import SimClock, format_time, parse_time
from corridor_sim.errors import ApiError

NOON = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def test_a_manual_clock_starts_at_a_fixed_instant_and_stays_there() -> None:
    clock = SimClock("manual")

    assert clock.mode == "manual"
    assert clock.now() == NOON
    assert clock.now() == NOON


def test_a_manual_clock_starts_where_it_is_told_to_in_utc() -> None:
    start = datetime(2027, 3, 1, 9, 30, tzinfo=timezone(timedelta(hours=-6)))

    clock = SimClock("manual", start)

    assert clock.now() == start
    assert clock.now().tzinfo is UTC


def test_a_manual_clock_moves_exactly_as_far_as_it_is_advanced() -> None:
    clock = SimClock("manual")

    assert clock.advance(30) == NOON + timedelta(seconds=30)
    assert clock.advance(0.25) == NOON + timedelta(seconds=30.25)
    assert clock.advance(0) == NOON + timedelta(seconds=30.25)
    assert clock.now() == NOON + timedelta(seconds=30.25)


def test_a_clock_does_not_run_backwards_or_start_without_a_timezone() -> None:
    with pytest.raises(ValueError, match="backwards"):
        SimClock("manual").advance(-1)
    with pytest.raises(ValueError, match="timezone"):
        SimClock("manual", datetime(2026, 1, 15, 12, 0))  # noqa: DTZ001 - the point of the test


def test_a_realtime_clock_follows_the_wall_clock() -> None:
    clock = SimClock("realtime")

    now = clock.now()

    assert clock.mode == "realtime"
    assert now.tzinfo is UTC
    assert abs(datetime.now(UTC) - now) < timedelta(seconds=5)


def test_a_realtime_clock_ignores_the_manual_start_time() -> None:
    clock = SimClock("realtime", NOON)

    assert abs(datetime.now(UTC) - clock.now()) < timedelta(seconds=5)


def test_a_realtime_clock_cannot_be_advanced() -> None:
    with pytest.raises(ApiError) as refusal:
        SimClock("realtime").advance(30)

    assert (refusal.value.status, refusal.value.code) == (409, "clock_is_realtime")


def test_a_realtime_clock_holds_still_when_the_wall_clock_steps_back() -> None:
    later = NOON + timedelta(seconds=5)
    readings = iter([NOON, later, later - timedelta(seconds=90), later + timedelta(seconds=1)])
    clock = SimClock("realtime", wall_clock=lambda: next(readings))

    assert [clock.now(), clock.now(), clock.now()] == [later, later, later + timedelta(seconds=1)]


@pytest.mark.parametrize(
    ("moment", "text"),
    [
        (NOON, "2026-01-15T12:00:00Z"),
        (NOON + timedelta(seconds=30), "2026-01-15T12:00:30Z"),
        (NOON + timedelta(milliseconds=250), "2026-01-15T12:00:00.250000Z"),
        (datetime(2026, 1, 15, 6, 0, tzinfo=timezone(timedelta(hours=-6))), "2026-01-15T12:00:00Z"),
    ],
)
def test_a_time_is_written_in_utc_with_a_z(moment: datetime, text: str) -> None:
    assert format_time(moment) == text
    assert parse_time(text) == moment


def test_a_time_without_a_timezone_is_never_written() -> None:
    with pytest.raises(ValueError, match="timezone"):
        format_time(datetime(2026, 1, 15, 12, 0))  # noqa: DTZ001 - the point of the test


@pytest.mark.parametrize(
    ("text", "moment"),
    [
        ("2026-01-15T12:00:00Z", NOON),
        ("2026-01-15T12:00:00+00:00", NOON),
        ("2026-01-15T14:00:00+02:00", NOON),
        ("2026-01-15T12:00:00.5Z", NOON + timedelta(milliseconds=500)),
    ],
)
def test_a_time_is_read_with_any_offset_and_kept_in_utc(text: str, moment: datetime) -> None:
    parsed = parse_time(text)

    assert parsed == moment
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize(
    "text", ["", "yesterday", "2026-01-15", "2026-01-15T12:00:00", "12:00:00Z", "1768478400"]
)
def test_a_time_that_names_no_instant_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="ISO-8601"):
        parse_time(text)

"""The retry policy: exponential backoff from two seconds, capped, with full jitter."""

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from corridor.outbox.retry import next_delay
from tests.outbox.helpers import Longest, Shortest

CAP_SECONDS = 300


@given(attempt=st.integers(1, 1_000), seed=st.integers(0, 2**32 - 1))
def test_a_delay_lies_between_zero_and_the_backoff_for_its_attempt(attempt: int, seed: int) -> None:
    delay = next_delay(attempt, rng=random.Random(seed))  # noqa: S311 - jitter, not a secret

    assert 0 <= delay <= min(CAP_SECONDS, 2 * 2 ** (attempt - 1))


@pytest.mark.parametrize(
    ("attempt", "longest"),
    [(1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (6, 64), (7, 128), (8, 256)],
)
def test_the_longest_delay_doubles_with_each_attempt(attempt: int, longest: int) -> None:
    assert next_delay(attempt, rng=Longest()) == longest
    assert next_delay(attempt, rng=Shortest()) == 0


@pytest.mark.parametrize("attempt", [9, 10, 50, 1_000])
def test_no_delay_is_longer_than_five_minutes(attempt: int) -> None:
    assert next_delay(attempt, rng=Longest()) == CAP_SECONDS


@pytest.mark.parametrize("attempt", [1, 4, 9])
def test_jitter_spreads_the_delays_for_one_attempt_over_the_whole_range(attempt: int) -> None:
    rng = random.Random(20260115)  # noqa: S311 - a reproducible sequence, not a secret
    longest = next_delay(attempt, rng=Longest())

    delays = [next_delay(attempt, rng=rng) for _ in range(200)]

    # Events that failed together must not come back together.
    assert len(set(delays)) == len(delays)
    assert min(delays) < longest * 0.1
    assert max(delays) > longest * 0.9


def test_the_same_seed_gives_the_same_delays_and_another_seed_gives_others() -> None:
    def sequence(seed: int) -> list[float]:
        rng = random.Random(seed)  # noqa: S311 - a reproducible sequence, not a secret
        return [next_delay(attempt, rng=rng) for attempt in range(1, 9)]

    assert sequence(7) == sequence(7)
    assert sequence(7) != sequence(8)


@pytest.mark.parametrize("attempt", [0, -1])
def test_an_attempt_is_counted_from_one(attempt: int) -> None:
    with pytest.raises(ValueError, match="attempt"):
        next_delay(attempt, rng=random.Random(1))  # noqa: S311 - jitter, not a secret

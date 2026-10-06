"""The token-bucket rate limiter, against a real Redis."""

import asyncio
import hashlib
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from prometheus_client import REGISTRY
from pydantic import SecretStr
from redis.asyncio import Redis

from corridor.platform.clock import ManualClock, use_clock
from corridor.platform.config import Settings
from corridor.platform.ids import new_id
from corridor.platform.ratelimit import Decision, Limit, RateLimiter
from corridor.platform.redis import RedisStore, create_redis

GROUP = "login"
SUBJECT = "203.0.113.9"
# Nothing listens on port 1, so a connection there is refused immediately.
DEAD_REDIS = SecretStr("redis://127.0.0.1:1/0")
# The bucket is kept to a millionth of a token and the clock to a millisecond.
MILLIONTH = Fraction(1, 1_000_000)
MILLISECOND = Fraction(1, 1_000)


@pytest.fixture
def limiter(redis: RedisStore, clock: ManualClock) -> RateLimiter:
    """A limiter on this test's own keys. The clock is frozen, so a bucket refills only when
    the test moves time."""
    return RateLimiter(redis)


@pytest.fixture
async def patient_redis(settings: Settings, redis: RedisStore) -> AsyncIterator[RedisStore]:
    """The test's Redis with a long timeout. A hundred callers at once on a busy machine can
    outlast the quarter second the application allows, and a timeout is an allowed request:
    it would be counted as one the limiter let through.

    It shares the test's key prefix, so the ``redis`` fixture removes what is written here."""
    patient = settings.model_copy(update={"redis_timeout_seconds": 10.0})
    store = RedisStore(create_redis(patient), settings.redis_key_prefix)
    try:
        yield store
    finally:
        await store.close()


def rejections(group: str) -> float:
    return (
        REGISTRY.get_sample_value("corridor_rate_limit_rejections_total", {"group": group}) or 0.0
    )


def redis_unavailable() -> float:
    return (
        REGISTRY.get_sample_value("corridor_redis_unavailable_total", {"use": "rate_limit"}) or 0.0
    )


async def keys_of(redis: RedisStore, settings: Settings) -> list[str]:
    return [key async for key in redis.client.scan_iter(match=f"{settings.redis_key_prefix}*")]


# --- the bucket ------------------------------------------------------------------------------


def test_a_limit_per_minute_allows_that_many_at_once_and_refills_them_over_a_minute() -> None:
    assert Limit.per_minute(600) == Limit(capacity=600, refill_per_second=10.0)


async def test_a_fresh_subject_gets_its_whole_capacity_and_then_is_refused(
    limiter: RateLimiter,
) -> None:
    limit = Limit(capacity=5, refill_per_second=0.5)

    decisions = [await limiter.check(GROUP, SUBJECT, limit) for _ in range(6)]

    assert [decision.allowed for decision in decisions] == [True, True, True, True, True, False]


async def test_remaining_counts_down_to_zero(limiter: RateLimiter) -> None:
    limit = Limit(capacity=5, refill_per_second=0.5)

    decisions = [await limiter.check(GROUP, SUBJECT, limit) for _ in range(6)]

    assert [decision.remaining for decision in decisions] == [4, 3, 2, 1, 0, 0]


async def test_an_allowed_request_has_nothing_to_wait_for(limiter: RateLimiter) -> None:
    decision = await limiter.check(GROUP, SUBJECT, Limit.per_minute(10))

    assert decision == Decision(allowed=True, remaining=9, retry_after_seconds=0)


@pytest.mark.parametrize(
    ("limit", "cost", "seconds"),
    [
        (Limit.per_minute(600), 1, 1),  # a tenth of a second, rounded up: never zero
        (Limit.per_minute(60), 1, 1),
        (Limit.per_minute(10), 1, 6),
        (Limit.per_minute(7), 1, 9),  # 8.57 seconds
        (Limit.per_minute(1), 1, 60),
        # Exactly a minute, although neither 11/60 nor a minute divided by it is exact in
        # binary floating point.
        (Limit.per_minute(11), 11, 60),
        (Limit(capacity=4, refill_per_second=0.5), 3, 6),
    ],
)
async def test_a_refusal_says_how_many_seconds_until_the_cost_can_be_met(
    limiter: RateLimiter, limit: Limit, cost: int, seconds: int
) -> None:
    await limiter.check(GROUP, SUBJECT, limit, cost=limit.capacity)

    refused = await limiter.check(GROUP, SUBJECT, limit, cost=cost)

    assert (refused.allowed, refused.retry_after_seconds) == (False, seconds)


async def test_the_wait_shrinks_as_time_passes(limiter: RateLimiter, clock: ManualClock) -> None:
    limit = Limit.per_minute(10)  # one token every six seconds
    await limiter.check(GROUP, SUBJECT, limit, cost=10)

    clock.advance(seconds=2.5)
    after_a_while = await limiter.check(GROUP, SUBJECT, limit)
    clock.advance(seconds=3.4)
    nearly_there = await limiter.check(GROUP, SUBJECT, limit)

    assert (after_a_while.allowed, after_a_while.retry_after_seconds) == (False, 4)
    assert (nearly_there.allowed, nearly_there.retry_after_seconds) == (False, 1)


async def test_waiting_as_long_as_a_refusal_says_is_enough_however_the_refill_rounds(
    limiter: RateLimiter, clock: ManualClock
) -> None:
    # A rate and a moment picked for their rounding. The refill so far rounds up; the refill
    # over the whole wait rounds down. A wait worked out from the first, eight seconds, ends
    # with the bucket a millionth of a token short.
    limit = Limit(capacity=1, refill_per_second=0.1048547)
    await limiter.check(GROUP, SUBJECT, limit)

    clock.advance(seconds=1.537)
    refused = await limiter.check(GROUP, SUBJECT, limit)
    clock.advance(seconds=refused.retry_after_seconds - 1)
    a_second_early = await limiter.check(GROUP, SUBJECT, limit)
    clock.advance(seconds=1)
    on_time = await limiter.check(GROUP, SUBJECT, limit)

    assert (refused.allowed, refused.retry_after_seconds) == (False, 9)
    assert a_second_early.allowed is False
    assert on_time.allowed is True


async def test_a_wait_is_not_cut_short_where_float_division_and_multiplication_disagree(
    limiter: RateLimiter, clock: ManualClock
) -> None:
    # A rate picked for its rounding. Dividing the missing token by it gives 10013
    # milliseconds exactly, yet 10013 milliseconds multiplied by it come to a hair under the
    # token, so the token is really due at 10014.
    limit = Limit(capacity=1, refill_per_second=0.09987011884550084)
    await limiter.check(GROUP, SUBJECT, limit)

    clock.advance(seconds=0.013)
    ten_seconds_short = await limiter.check(GROUP, SUBJECT, limit)
    clock.advance(seconds=10)
    a_millisecond_short = await limiter.check(GROUP, SUBJECT, limit)
    clock.advance(seconds=0.001)
    due = await limiter.check(GROUP, SUBJECT, limit)

    # 10.001 seconds to go is 11 when rounded up, and one millisecond to go is not zero.
    assert (ten_seconds_short.allowed, ten_seconds_short.retry_after_seconds) == (False, 11)
    assert (a_millisecond_short.allowed, a_millisecond_short.retry_after_seconds) == (False, 1)
    assert due.allowed is True


# --- refill ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "cost", "milliseconds"),
    [
        (Limit.per_minute(10), 1, 6_000),
        (Limit.per_minute(600), 1, 100),
        (Limit.per_minute(7), 1, 8_572),  # 8571.43, and the clock counts whole milliseconds
        # In floating point a minute at 11 a minute comes to a hair under eleven tokens.
        (Limit.per_minute(11), 11, 60_000),
    ],
)
async def test_tokens_return_exactly_when_they_are_due(
    limiter: RateLimiter, clock: ManualClock, limit: Limit, cost: int, milliseconds: int
) -> None:
    await limiter.check(GROUP, SUBJECT, limit, cost=limit.capacity)

    clock.advance(seconds=(milliseconds - 1) / 1000)
    a_millisecond_early = await limiter.check(GROUP, SUBJECT, limit, cost=cost)
    clock.advance(seconds=0.001)
    on_time = await limiter.check(GROUP, SUBJECT, limit, cost=cost)
    straight_after = await limiter.check(GROUP, SUBJECT, limit, cost=cost)

    assert a_millisecond_early.allowed is False
    assert (on_time.allowed, on_time.remaining) == (True, 0)
    assert straight_after.allowed is False


async def test_an_idle_bucket_refills_to_its_capacity_and_no_further(
    limiter: RateLimiter, clock: ManualClock
) -> None:
    limit = Limit.per_minute(10)
    await limiter.check(GROUP, SUBJECT, limit, cost=4)

    clock.advance(hours=1)
    decisions = [await limiter.check(GROUP, SUBJECT, limit) for _ in range(11)]

    assert [decision.allowed for decision in decisions] == [True] * 10 + [False]
    assert decisions[0].remaining == 9


async def test_a_refusal_leaves_the_bucket_exactly_as_it_was(
    limiter: RateLimiter, redis: RedisStore, settings: Settings, clock: ManualClock
) -> None:
    limit = Limit.per_minute(10)
    await limiter.check(GROUP, SUBJECT, limit, cost=10)
    (key,) = await keys_of(redis, settings)
    # A lifetime the limiter would never set, to show that a refusal does not renew it.
    await redis.client.pexpire(key, 45_000)
    before = await redis.client.hgetall(key)

    clock.advance(seconds=3)
    refused = await limiter.check(GROUP, SUBJECT, limit)

    assert refused.allowed is False
    assert await redis.client.hgetall(key) == before
    assert await redis.client.pttl(key) <= 45_000


# --- one bucket per subject per group --------------------------------------------------------


async def test_each_subject_has_a_bucket_of_its_own(limiter: RateLimiter) -> None:
    limit = Limit.per_minute(2)
    await limiter.check(GROUP, SUBJECT, limit, cost=2)

    same = await limiter.check(GROUP, SUBJECT, limit)
    other = await limiter.check(GROUP, "203.0.113.10", limit)

    assert same.allowed is False
    assert (other.allowed, other.remaining) == (True, 1)


async def test_each_group_has_a_bucket_of_its_own(limiter: RateLimiter) -> None:
    limit = Limit.per_minute(2)
    await limiter.check(GROUP, SUBJECT, limit, cost=2)

    same = await limiter.check(GROUP, SUBJECT, limit)
    other = await limiter.check("global", SUBJECT, limit)

    assert same.allowed is False
    assert (other.allowed, other.remaining) == (True, 1)


# --- concurrency -----------------------------------------------------------------------------


async def test_100_concurrent_checks_against_a_capacity_of_10_allow_exactly_10(
    patient_redis: RedisStore, clock: ManualClock
) -> None:
    limiter = RateLimiter(patient_redis)
    limit = Limit.per_minute(10)
    unavailable_before = redis_unavailable()

    decisions = await asyncio.gather(*(limiter.check(GROUP, SUBJECT, limit) for _ in range(100)))

    allowed = [decision for decision in decisions if decision.allowed]
    assert len(allowed) == 10
    # Each of the ten took a different token: no two callers saw the same bucket.
    assert sorted(decision.remaining for decision in allowed) == list(range(10))
    # Every answer came from Redis; none is the allowance made when it cannot be reached.
    assert redis_unavailable() == unavailable_before


async def naive_take(client: Redis, keys: list[str], args: list[float]) -> list[int]:
    """The bucket as two round trips, a read and then a write, instead of one script.

    Right for one caller at a time. It takes the script's arguments and gives the script's
    reply, so it can stand in for it.
    """
    capacity, _rate, now, cost, ttl = (int(value) for value in args)
    stored = await client.hget(keys[0], "tokens")
    tokens = int(stored) if stored is not None else capacity
    if tokens < cost:
        return [0, tokens, 1000]
    await client.hset(keys[0], mapping={"tokens": tokens - cost, "ts": now})
    await client.pexpire(keys[0], ttl)
    return [1, tokens - cost, 0]


async def test_the_race_that_one_script_prevents_is_real(patient_redis: RedisStore) -> None:
    """A control: read the bucket and write it back as two steps, and callers that arrive
    together all read the same tokens and all spend them. This is what running the bucket
    as one script avoids."""
    key = patient_redis.key("rl", "control", "naive")
    args = [10, 10 / 60, 0, 1, 120_000]

    in_turn = [(await naive_take(patient_redis.client, [key], args))[0] for _ in range(11)]
    await patient_redis.client.delete(key)
    at_once = await asyncio.gather(
        *(naive_take(patient_redis.client, [key], args) for _ in range(100))
    )

    assert in_turn == [1] * 10 + [0]
    assert sum(reply[0] for reply in at_once) > 10


# --- cost ------------------------------------------------------------------------------------


async def test_a_request_takes_its_cost_in_tokens(limiter: RateLimiter) -> None:
    limit = Limit.per_minute(10)

    first = await limiter.check(GROUP, SUBJECT, limit, cost=3)
    too_much = await limiter.check(GROUP, SUBJECT, limit, cost=8)
    the_rest = await limiter.check(GROUP, SUBJECT, limit, cost=7)

    assert (first.allowed, first.remaining) == (True, 7)
    # Refused for want of one token, which is six seconds away. A refusal takes nothing.
    assert too_much == Decision(allowed=False, remaining=7, retry_after_seconds=6)
    assert (the_rest.allowed, the_rest.remaining) == (True, 0)


async def test_a_cost_above_the_capacity_is_an_error_not_a_refusal(
    limiter: RateLimiter, redis: RedisStore, settings: Settings
) -> None:
    before = rejections(GROUP)

    with pytest.raises(ValueError, match="cost of 6 can never be met by a capacity of 5"):
        await limiter.check(GROUP, SUBJECT, Limit.per_minute(5), cost=6)

    assert rejections(GROUP) == before
    assert await keys_of(redis, settings) == []


@pytest.mark.parametrize("cost", [0, -1])
async def test_a_request_costs_at_least_one_token(limiter: RateLimiter, cost: int) -> None:
    limit = Limit.per_minute(5)
    await limiter.check(GROUP, SUBJECT, limit, cost=5)

    with pytest.raises(ValueError, match="at least one token"):
        await limiter.check(GROUP, SUBJECT, limit, cost=cost)

    # In particular, a negative cost did not put tokens back.
    assert (await limiter.check(GROUP, SUBJECT, limit)).allowed is False


@pytest.mark.parametrize("rate", [0.0, -1.0, float("inf"), float("nan")])
async def test_a_limit_must_refill_at_a_positive_finite_rate(
    limiter: RateLimiter, rate: float
) -> None:
    with pytest.raises(ValueError, match="positive, finite rate"):
        await limiter.check(GROUP, SUBJECT, Limit(capacity=5, refill_per_second=rate))


# --- clocks that disagree --------------------------------------------------------------------


async def test_a_caller_whose_clock_is_behind_sees_no_time_pass(
    limiter: RateLimiter, clock: ManualClock
) -> None:
    limit = Limit.per_minute(10)
    await limiter.check(GROUP, SUBJECT, limit)

    # Another host, half a minute behind the one that wrote the bucket. Counting those
    # thirty seconds as negative time would take five tokens away.
    with use_clock(ManualClock(clock.now() - timedelta(seconds=30))):
        behind = await limiter.check(GROUP, SUBJECT, limit)

    assert (behind.allowed, behind.remaining) == (True, 8)


async def test_time_going_backwards_and_forwards_again_mints_no_tokens(
    limiter: RateLimiter, clock: ManualClock
) -> None:
    limit = Limit.per_minute(10)
    await limiter.check(GROUP, SUBJECT, limit, cost=5)

    with use_clock(ManualClock(clock.now() - timedelta(seconds=60))):
        await limiter.check(GROUP, SUBJECT, limit)
    # Back on the first host, where no time has passed. Had the slow host stamped the bucket
    # with its own time, this would look like a minute's refill.
    back = await limiter.check(GROUP, SUBJECT, limit)

    assert (back.allowed, back.remaining) == (True, 3)


# --- keys ------------------------------------------------------------------------------------


async def test_a_bucket_is_a_hash_under_the_stores_prefix(
    limiter: RateLimiter, redis: RedisStore, settings: Settings
) -> None:
    await limiter.check(GROUP, SUBJECT, Limit.per_minute(10))

    digest = hashlib.sha256(SUBJECT.encode()).hexdigest()
    key = f"{settings.redis_key_prefix}rl:{GROUP}:{digest}"
    assert await keys_of(redis, settings) == [key]
    assert set(await redis.client.hgetall(key)) == {"tokens", "ts"}


@pytest.mark.parametrize(
    ("limit", "milliseconds"),
    [
        (Limit.per_minute(600), 120_000),
        (Limit(capacity=5, refill_per_second=0.5), 20_000),
    ],
)
async def test_an_idle_bucket_expires_after_twice_the_time_it_takes_to_refill(
    limiter: RateLimiter, redis: RedisStore, settings: Settings, limit: Limit, milliseconds: int
) -> None:
    await limiter.check(GROUP, SUBJECT, limit)

    (key,) = await keys_of(redis, settings)
    assert milliseconds - 5_000 < await redis.client.pttl(key) <= milliseconds


@pytest.mark.parametrize(
    "subject",
    [
        "two words",
        "line\nbreak\r\n",
        "a:b:c",
        "*",
        "",
        "x" * 10_000,
        "顧客@example.com",
        "\ud800",
    ],
    ids=[
        "spaces",
        "newlines",
        "colons",
        "glob",
        "empty",
        "very-long",
        "not-ascii",
        "lone-surrogate",
    ],
)
async def test_a_hostile_subject_is_limited_like_any_other_and_never_reaches_a_key(
    limiter: RateLimiter, redis: RedisStore, settings: Settings, subject: str
) -> None:
    limit = Limit.per_minute(2)

    decisions = [await limiter.check(GROUP, subject, limit) for _ in range(3)]

    assert [decision.allowed for decision in decisions] == [True, True, False]
    # The whole key is the prefix, the group and a digest: nothing of the subject is in it.
    (key,) = await keys_of(redis, settings)
    assert re.fullmatch(re.escape(f"{settings.redis_key_prefix}rl:{GROUP}:") + "[0-9a-f]{64}", key)


# --- Redis unavailable -----------------------------------------------------------------------


async def test_an_unreachable_redis_allows_the_request_and_is_counted(settings: Settings) -> None:
    dead = settings.model_copy(update={"redis_url": DEAD_REDIS})
    store = RedisStore(create_redis(dead), "unused:")
    unavailable_before, rejections_before = redis_unavailable(), rejections(GROUP)
    try:
        decision = await RateLimiter(store).check(GROUP, SUBJECT, Limit.per_minute(3))
    finally:
        await store.close()

    assert decision == Decision(allowed=True, remaining=3, retry_after_seconds=0)
    assert redis_unavailable() == unavailable_before + 1
    assert rejections(GROUP) == rejections_before


# --- metrics ---------------------------------------------------------------------------------


async def test_a_refusal_is_counted_under_its_group(limiter: RateLimiter) -> None:
    limit = Limit.per_minute(1)
    before = rejections(GROUP), rejections("global")

    await limiter.check(GROUP, SUBJECT, limit)
    after_an_allowed_request = rejections(GROUP)
    await limiter.check(GROUP, SUBJECT, limit)
    await limiter.check(GROUP, SUBJECT, limit)

    assert after_an_allowed_request == before[0]
    assert (rejections(GROUP), rejections("global")) == (before[0] + 2, before[1])


# --- generated limits and timings ------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    limit: Limit
    spent: int
    elapsed_ms: int
    cost: int


@st.composite
def cases(draw: st.DrawFn) -> Case:
    limit = draw(
        st.one_of(
            st.integers(1, 2_000).map(Limit.per_minute),
            # Any rate, provided a full refill takes at least five seconds: a bucket's
            # lifetime in Redis runs in real time and has to outlast the example.
            st.builds(
                lambda capacity, seconds: Limit(capacity, capacity / seconds),
                st.integers(1, 200),
                st.floats(5, 600),
            ),
        )
    )
    return Case(
        limit=limit,
        spent=draw(st.integers(1, limit.capacity)),
        elapsed_ms=draw(st.integers(0, 150_000)),
        cost=draw(st.integers(1, limit.capacity)),
    )


async def spend_wait_and_ask_again(app_settings: Settings, case: Case) -> None:
    limit, cost = case.limit, case.cost
    rate = Fraction(limit.refill_per_second)
    store = RedisStore(create_redis(app_settings), app_settings.redis_key_prefix)
    limiter = RateLimiter(store)
    subject = str(new_id())
    try:
        with use_clock(ManualClock(datetime(2026, 1, 15, 12, 0, tzinfo=UTC))) as clock:
            assert isinstance(clock, ManualClock)
            first = await limiter.check(GROUP, subject, limit, cost=case.spent)
            clock.advance(seconds=case.elapsed_ms / 1000)
            second = await limiter.check(GROUP, subject, limit, cost=cost)

            # The same bucket in exact arithmetic.
            level = min(
                Fraction(limit.capacity),
                limit.capacity - case.spent + rate * case.elapsed_ms / 1000,
            )
            assert (first.allowed, first.remaining) == (True, limit.capacity - case.spent)
            if level >= cost + MILLIONTH:
                assert second.allowed is True
            if level <= cost - MILLIONTH:
                assert second.allowed is False

            if second.allowed:
                left = level - cost
                assert second.retry_after_seconds == 0
                assert math.floor(left - MILLIONTH) <= second.remaining
                assert second.remaining <= math.floor(left + MILLIONTH)
                return

            wait = (cost - level) / rate
            assert second.retry_after_seconds >= 1
            assert math.ceil(wait - MILLISECOND) <= second.retry_after_seconds
            assert second.retry_after_seconds <= math.ceil(wait + MILLISECOND)
            # And the wait is exact on the limiter's own terms: enough, and not a second
            # more than enough.
            clock.advance(seconds=second.retry_after_seconds - 1)
            a_second_early = await limiter.check(GROUP, subject, limit, cost=cost)
            clock.advance(seconds=1)
            on_time = await limiter.check(GROUP, subject, limit, cost=cost)
            assert (a_second_early.allowed, on_time.allowed) == (False, True)
    finally:
        await store.client.delete(
            store.key("rl", GROUP, hashlib.sha256(subject.encode()).hexdigest())
        )
        await store.close()


@hypothesis_settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(case=cases())
def test_the_bucket_agrees_with_exact_arithmetic_and_its_waits_are_exact(
    settings: Settings, case: Case
) -> None:
    asyncio.run(spend_wait_and_ask_again(settings, case))

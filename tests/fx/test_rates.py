"""The mid rate: cached for a few seconds, and never used once it is old.

The cache is Redis when there is a key to authenticate its entries with, which is what the
``settings`` of this module have, and the process itself when there is not.
"""

import gc
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any, NoReturn

import pytest
from pydantic import SecretStr

from corridor import fx
from corridor.fx import RateUnavailable, rates
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.config import Settings
from corridor.platform.redis import RedisStore, create_redis
from corridor.providers import ProviderMisconfigured, ProviderOutcomeUnknown, ProviderRejected
from tests.fx.support import StubRates

pytestmark = pytest.mark.usefixtures("clock")

# Not a secret: it authenticates cache entries in a database that lives for one test.
MAC_KEY = "fx-cache-mac-key-for-tests-0000001"  # pragma: allowlist secret
OTHER_MAC_KEY = "fx-cache-mac-key-for-tests-0000002"  # pragma: allowlist secret


@pytest.fixture
def unkeyed(settings: Settings) -> Settings:
    """Settings with no key, as the suite's are everywhere else: rates are cached in the
    process."""
    return settings.model_copy(update={"fx_cache_mac_key": None})


@pytest.fixture(name="settings")
def keyed(settings: Settings) -> Settings:
    """The settings with a key for the rate cache, so that rates are cached in Redis."""
    return settings.model_copy(update={"fx_cache_mac_key": SecretStr(MAC_KEY)})


def cache_key(redis: RedisStore) -> str:
    return redis.key("fx", "rate", "USD", "MXN")


async def test_the_rate_comes_from_the_source(redis: RedisStore, settings: Settings) -> None:
    source = StubRates(mid=Decimal("17.251234"))

    rate = await fx.get_mid(redis, source, "USD", "MXN", settings=settings)

    assert (rate.base, rate.quote, rate.as_of) == ("USD", "MXN", utcnow())
    assert rate.mid == Decimal("17.251234")
    assert type(rate.mid) is Decimal
    assert source.asked == [("USD", "MXN")]


async def test_a_second_call_is_answered_from_the_cache(
    redis: RedisStore, settings: Settings
) -> None:
    source = StubRates(mid=Decimal("17.251234"))

    first = await fx.get_mid(redis, source, "USD", "MXN", settings=settings)
    second = await fx.get_mid(redis, source, "USD", "MXN", settings=settings)

    assert second == first
    assert type(second.mid) is Decimal
    assert len(source.asked) == 1


async def test_the_cache_is_per_pair_and_direction(redis: RedisStore, settings: Settings) -> None:
    source = StubRates()

    await fx.get_mid(redis, source, "USD", "MXN", settings=settings)
    await fx.get_mid(redis, source, "MXN", "USD", settings=settings)
    await fx.get_mid(redis, source, "USD", "BRL", settings=settings)

    assert source.asked == [("USD", "MXN"), ("MXN", "USD"), ("USD", "BRL")]


async def test_the_cached_rate_expires_after_the_configured_seconds(
    redis: RedisStore, settings: Settings
) -> None:
    await fx.get_mid(redis, StubRates(), "USD", "MXN", settings=settings)

    assert 0 < await redis.client.ttl(cache_key(redis)) <= settings.fx_rate_cache_seconds


async def test_with_redis_unreachable_the_rate_is_fetched_every_time(settings: Settings) -> None:
    # Port 1 on localhost: nothing listens there, so the connection is refused at once.
    dead = settings.model_copy(update={"redis_url": SecretStr("redis://127.0.0.1:1/0")})
    store = RedisStore(create_redis(dead), "unused:")
    source = StubRates()
    try:
        first = await fx.get_mid(store, source, "USD", "MXN", settings=settings)
        second = await fx.get_mid(store, source, "USD", "MXN", settings=settings)
    finally:
        await store.close()

    assert first == second
    assert first.mid == source.mid
    assert len(source.asked) == 2


async def test_a_stale_rate_is_refused_and_not_cached(
    redis: RedisStore, settings: Settings
) -> None:
    source = StubRates(as_of=utcnow() - timedelta(seconds=16))

    with pytest.raises(RateUnavailable) as refusal:
        await fx.get_mid(redis, source, "USD", "MXN", settings=settings)

    assert (refusal.value.status, refusal.value.code) == (503, "rate_unavailable")
    assert await redis.client.get(cache_key(redis)) is None
    # The source has caught up: it is asked again, and nothing stale answers for it.
    source.as_of = utcnow()
    assert (await fx.get_mid(redis, source, "USD", "MXN", settings=settings)).as_of == utcnow()
    assert len(source.asked) == 2


async def test_a_rate_is_good_until_it_is_older_than_the_limit(
    redis: RedisStore, settings: Settings, clock: ManualClock
) -> None:
    updated = utcnow()
    source = StubRates(as_of=updated)
    clock.advance(seconds=settings.fx_rate_max_age_seconds)

    assert (await fx.get_mid(redis, source, "USD", "MXN", settings=settings)).as_of == updated

    await redis.client.delete(cache_key(redis))
    clock.advance(seconds=1)
    with pytest.raises(RateUnavailable):
        await fx.get_mid(redis, source, "USD", "MXN", settings=settings)


async def test_a_cached_rate_that_has_grown_old_is_not_served(
    redis: RedisStore, settings: Settings, clock: ManualClock
) -> None:
    source = StubRates(mid=Decimal("17.25"), as_of=utcnow())
    await fx.get_mid(redis, source, "USD", "MXN", settings=settings)
    clock.advance(seconds=16)
    source.mid, source.as_of = Decimal("18.5"), utcnow()

    rate = await fx.get_mid(redis, source, "USD", "MXN", settings=settings)

    assert rate.mid == Decimal("18.5")
    assert len(source.asked) == 2


@pytest.mark.parametrize("cached", ["not json", "{}", '{"mid": "1e5", "as_of": 3}', "[1]"])
async def test_a_cache_entry_that_cannot_be_read_is_a_miss(
    redis: RedisStore, settings: Settings, cached: str
) -> None:
    await redis.client.set(cache_key(redis), cached)
    source = StubRates()

    rate = await fx.get_mid(redis, source, "USD", "MXN", settings=settings)

    assert rate.mid == source.mid
    assert len(source.asked) == 1


async def test_the_cache_holds_the_mid_as_a_decimal_string(
    redis: RedisStore, settings: Settings
) -> None:
    await fx.get_mid(redis, StubRates(mid=Decimal("17.250000")), "USD", "MXN", settings=settings)

    cached = json.loads(await redis.client.get(cache_key(redis)))

    assert (cached["mid"], cached["as_of"]) == ("17.250000", utcnow().isoformat())
    assert set(cached) == {"mid", "as_of", "mac"}
    assert MAC_KEY not in json.dumps(cached)


@pytest.mark.parametrize(
    "failure",
    [
        ProviderOutcomeUnknown("timed out", provider="stubfx", operation="get_rate"),
        ProviderMisconfigured("no key", provider="stubfx", operation="get_rate"),
        ProviderRejected("unknown_pair", "no", 404, provider="stubfx", operation="get_rate"),
    ],
)
async def test_a_source_that_cannot_answer_is_a_rate_unavailable(
    redis: RedisStore, settings: Settings, failure: Exception
) -> None:
    with pytest.raises(RateUnavailable) as refusal:
        await fx.get_mid(redis, StubRates(failure=failure), "USD", "MXN", settings=settings)

    assert (refusal.value.status, refusal.value.code) == (503, "rate_unavailable")
    assert await redis.client.get(cache_key(redis)) is None


# --- a rate dated ahead of the clock ---------------------------------------------------------


async def test_a_rate_dated_a_little_ahead_of_the_clock_is_used(
    redis: RedisStore, settings: Settings
) -> None:
    source = StubRates(as_of=utcnow() + timedelta(seconds=5))

    rate = await fx.get_mid(redis, source, "USD", "MXN", settings=settings)

    assert rate.as_of == utcnow() + timedelta(seconds=5)


async def test_a_rate_dated_further_ahead_is_refused_and_not_cached(
    redis: RedisStore, settings: Settings
) -> None:
    source = StubRates(as_of=utcnow() + timedelta(seconds=5, milliseconds=1))

    with pytest.raises(RateUnavailable):
        await fx.get_mid(redis, source, "USD", "MXN", settings=settings)

    assert await redis.client.get(cache_key(redis)) is None


async def test_a_rate_from_far_in_the_future_cannot_outlive_its_welcome(
    redis: RedisStore, unkeyed: Settings, clock: ManualClock
) -> None:
    # Dated a day ahead, it would still pass for fresh a day from now.
    source = StubRates(as_of=utcnow() + timedelta(days=1))

    with pytest.raises(RateUnavailable):
        await fx.get_mid(redis, source, "USD", "MXN", settings=unkeyed)
    source.as_of = None
    clock.advance(seconds=1)

    assert (await fx.get_mid(redis, source, "USD", "MXN", settings=unkeyed)).as_of == utcnow()
    assert len(source.asked) == 2


# --- what is in Redis is not believed on sight ----------------------------------------------


async def cached_entry(redis: RedisStore, settings: Settings) -> dict[str, str]:
    """Cache a real rate, and return the entry as it sits in Redis."""
    await fx.get_mid(redis, StubRates(mid=Decimal("17.25")), "USD", "MXN", settings=settings)
    entry: dict[str, str] = json.loads(await redis.client.get(cache_key(redis)))
    return entry


@pytest.mark.parametrize(
    "tamper",
    [
        lambda entry: {**entry, "mid": "0.01"},
        lambda entry: {**entry, "mid": "17.250"},
        lambda entry: {**entry, "as_of": (utcnow() + timedelta(seconds=1)).isoformat()},
        lambda entry: {**entry, "mac": "0" * 64},
        lambda entry: {**entry, "mac": ""},
        lambda entry: {**entry, "mac": 7},
        lambda entry: {**entry, "mac": "\u00e9" * 64},
        lambda entry: {name: value for name, value in entry.items() if name != "mac"},
    ],
    ids=[
        "mid",
        "mid-padded",
        "as-of",
        "wrong-mac",
        "empty-mac",
        "mac-not-text",
        "mac-not-ascii",
        "no-mac",
    ],
)
async def test_a_cached_rate_without_the_right_mac_is_ignored_and_fetched_again(
    redis: RedisStore, settings: Settings, tamper: Any
) -> None:
    entry = await cached_entry(redis, settings)
    await redis.client.set(cache_key(redis), json.dumps(tamper(entry)))
    source = StubRates(mid=Decimal("18.5"))

    rate = await fx.get_mid(redis, source, "USD", "MXN", settings=settings)

    assert rate.mid == Decimal("18.5")
    assert len(source.asked) == 1
    # What replaced it is an entry that does verify.
    assert await fx.get_mid(redis, source, "USD", "MXN", settings=settings) == rate
    assert len(source.asked) == 1


async def test_an_entry_copied_from_another_pair_is_ignored(
    redis: RedisStore, settings: Settings
) -> None:
    await cached_entry(redis, settings)
    cheap = await redis.client.get(cache_key(redis))
    await redis.client.set(redis.key("fx", "rate", "USD", "BRL"), cheap)
    source = StubRates(mid=Decimal("5.4"))

    rate = await fx.get_mid(redis, source, "USD", "BRL", settings=settings)

    assert rate.mid == Decimal("5.4")
    assert source.asked == [("USD", "BRL")]


async def test_an_entry_written_under_another_key_is_ignored(
    redis: RedisStore, settings: Settings
) -> None:
    await cached_entry(redis, settings)
    rekeyed = settings.model_copy(update={"fx_cache_mac_key": SecretStr(OTHER_MAC_KEY)})
    source = StubRates(mid=Decimal("18.5"))

    rate = await fx.get_mid(redis, source, "USD", "MXN", settings=rekeyed)

    assert rate.mid == Decimal("18.5")


# --- without a key: no Redis, and a cache in the process ------------------------------------


async def test_without_a_key_nothing_is_written_to_redis(
    redis: RedisStore, unkeyed: Settings
) -> None:
    await fx.get_mid(redis, StubRates(), "USD", "MXN", settings=unkeyed)

    assert await redis.client.get(cache_key(redis)) is None


async def test_without_a_key_nothing_in_redis_is_read(
    redis: RedisStore, settings: Settings, unkeyed: Settings
) -> None:
    # A well-formed entry, with a MAC that would verify if there were a key.
    await cached_entry(redis, settings)
    source = StubRates(mid=Decimal("18.5"))

    rate = await fx.get_mid(redis, source, "USD", "MXN", settings=unkeyed)

    assert rate.mid == Decimal("18.5")


class Untouchable:
    """A Redis client that must not be used: whatever is asked of it fails the test."""

    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"Redis was asked to {name} with no key to authenticate rates")


async def test_without_a_key_redis_is_not_asked_at_all(unkeyed: Settings) -> None:
    untouchable: Any = Untouchable()
    store = RedisStore(untouchable, "unused:")
    source = StubRates()

    first = await fx.get_mid(store, source, "USD", "MXN", settings=unkeyed)
    second = await fx.get_mid(store, source, "USD", "MXN", settings=unkeyed)

    assert first == second
    assert len(source.asked) == 1


async def test_the_process_cache_of_a_source_goes_when_the_source_does(
    redis: RedisStore, unkeyed: Settings
) -> None:
    source = StubRates()
    await fx.get_mid(redis, source, "USD", "MXN", settings=unkeyed)
    # An id is handed to another object once its own is gone. Rates left under it would
    # be served for a source that never gave them.
    identity = id(source)
    assert identity in rates._local

    del source
    gc.collect()

    assert identity not in rates._local


async def test_without_a_key_a_second_call_is_answered_from_the_process(
    redis: RedisStore, unkeyed: Settings, clock: ManualClock
) -> None:
    source = StubRates(mid=Decimal("17.25"))

    first = await fx.get_mid(redis, source, "USD", "MXN", settings=unkeyed)
    clock.advance(seconds=unkeyed.fx_rate_cache_seconds - 1)
    second = await fx.get_mid(redis, source, "USD", "MXN", settings=unkeyed)
    await fx.get_mid(redis, source, "MXN", "USD", settings=unkeyed)

    assert second == first
    assert source.asked == [("USD", "MXN"), ("MXN", "USD")]


async def test_the_process_keeps_a_rate_only_for_the_configured_seconds(
    redis: RedisStore, unkeyed: Settings, clock: ManualClock
) -> None:
    source = StubRates(mid=Decimal("17.25"), as_of=utcnow())
    await fx.get_mid(redis, source, "USD", "MXN", settings=unkeyed)
    clock.advance(seconds=unkeyed.fx_rate_cache_seconds)
    source.mid, source.as_of = Decimal("18.5"), utcnow()

    rate = await fx.get_mid(redis, source, "USD", "MXN", settings=unkeyed)

    assert rate.mid == Decimal("18.5")
    assert len(source.asked) == 2


async def test_the_process_cache_belongs_to_the_source_it_came_from(
    redis: RedisStore, unkeyed: Settings
) -> None:
    await fx.get_mid(redis, StubRates(mid=Decimal("17.25")), "USD", "MXN", settings=unkeyed)
    other = StubRates(mid=Decimal("18.5"))

    rate = await fx.get_mid(redis, other, "USD", "MXN", settings=unkeyed)

    assert rate.mid == Decimal("18.5")
    assert len(other.asked) == 1

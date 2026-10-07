"""The mid rate: cached in Redis for a few seconds, and never used once it is old."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from corridor import fx
from corridor.fx import RateUnavailable
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.config import Settings
from corridor.platform.redis import RedisStore, create_redis
from corridor.providers import ProviderMisconfigured, ProviderOutcomeUnknown, ProviderRejected
from tests.fx.support import StubRates

pytestmark = pytest.mark.usefixtures("clock")


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

    assert cached == {"mid": "17.250000", "as_of": utcnow().isoformat()}


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

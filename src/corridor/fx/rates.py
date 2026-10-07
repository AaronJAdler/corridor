"""The mid-market rate: from the source, through a short cache in Redis.

Redis only saves a call. With Redis down the source is asked every time, and a rate that
is too old is refused wherever it came from.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final

from corridor.fx.errors import RateUnavailable
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.logging import get_logger
from corridor.platform.redis import RedisStore
from corridor.providers import ProviderError, Rate, RateSource

log = get_logger(__name__)

_USE: Final = "fx_rate"


async def get_mid(
    redis: RedisStore, source: RateSource, base: str, quote: str, *, settings: Settings
) -> Rate:
    """The mid rate for the pair, no older than ``fx_rate_max_age_seconds``.

    This can call the rate source over the network: never call it inside a transaction.
    """
    max_age = timedelta(seconds=settings.fx_rate_max_age_seconds)
    key = redis.key("fx", "rate", base, quote)

    cached = _decode(await redis.attempt(_read(redis, key), default=None, use=_USE), base, quote)
    # The cache outlives nothing by much, but a rate can be cached near the end of its
    # life: it is checked again on the way out.
    if cached is not None and not _is_stale(cached, max_age):
        return cached

    try:
        rate = await source.get_rate(base, quote)
    except ProviderError as error:
        log.warning("fx.rate_fetch_failed", provider=source.name, reason=type(error).__name__)
        raise RateUnavailable from error

    if _is_stale(rate, max_age):
        log.warning("fx.rate_stale", provider=source.name, as_of=rate.as_of.isoformat())
        raise RateUnavailable

    await redis.attempt(
        _write(redis, key, _encode(rate), settings.fx_rate_cache_seconds), default=None, use=_USE
    )
    return rate


def _is_stale(rate: Rate, max_age: timedelta) -> bool:
    return utcnow() - rate.as_of > max_age


async def _read(redis: RedisStore, key: str) -> str | None:
    value = await redis.client.get(key)
    return value if isinstance(value, str) else None


async def _write(redis: RedisStore, key: str, value: str, seconds: int) -> None:
    await redis.client.set(key, value, ex=seconds)


def _encode(rate: Rate) -> str:
    # The mid as a string: a JSON number would come back as a float.
    return json.dumps({"mid": format(rate.mid, "f"), "as_of": rate.as_of.isoformat()})


def _decode(cached: str | None, base: str, quote: str) -> Rate | None:
    """The cached rate, or None for anything that is not one: the cache is a convenience,
    and an entry that cannot be read is the same as no entry."""
    if cached is None:
        return None
    try:
        document = json.loads(cached)
        mid = Decimal(document["mid"])
        as_of = datetime.fromisoformat(document["as_of"])
    except ValueError, TypeError, KeyError, InvalidOperation:
        return None
    if not mid.is_finite() or mid <= 0 or as_of.tzinfo is None:
        return None
    return Rate(base=base, quote=quote, mid=mid, as_of=as_of)

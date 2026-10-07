"""The mid-market rate: from the source, through a short cache.

The cache only saves a call. It is in Redis when there is a key to authenticate what is
put there, and in this process when there is not: a rate sets the price of a conversion,
and Redis is not trusted to say what one is. With Redis down the source is asked every
time, and a rate that is too old is refused wherever it came from.
"""

import hashlib
import hmac
import json
import weakref
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

# How far ahead of this clock a rate's time may be. Clocks differ a little; a rate dated
# further ahead than that would pass for fresh long after it had stopped being so.
MAX_FUTURE_SKEW: Final = timedelta(seconds=5)

# The MAC key is derived from the configured one under this label, so that the configured
# key is never itself used on data and the same key in another role makes other MACs.
_MAC_LABEL: Final = b"corridor/fx-rate-cache/v1"

# The rates cached in this process: for each rate source alive, by its ``id``, the rates
# it gave, each with the time it stops being served. Kept per source and dropped with it,
# so a source that is replaced takes its rates along, and its ``id`` is free of them
# before another object can be given it.
type _Held = tuple[Rate, datetime]
_local: Final[dict[int, tuple[weakref.ref[RateSource], dict[tuple[str, str], _Held]]]] = {}


async def get_mid(
    redis: RedisStore, source: RateSource, base: str, quote: str, *, settings: Settings
) -> Rate:
    """The mid rate for the pair, no older than ``fx_rate_max_age_seconds``.

    This can call the rate source over the network: never call it inside a transaction.
    """
    max_age = timedelta(seconds=settings.fx_rate_max_age_seconds)
    key = redis.key("fx", "rate", base, quote)
    mac_key = _mac_key(settings)

    if mac_key is not None:
        cached = _decode(
            await redis.attempt(_read(redis, key), default=None, use=_USE), base, quote, mac_key
        )
    else:
        cached = _recall(source, base, quote)
    # The cache outlives nothing by much, but a rate can be cached near the end of its
    # life: it is checked again on the way out.
    if cached is not None and not _is_stale(cached, max_age):
        return cached

    try:
        rate = await source.get_rate(base, quote)
    except ProviderError as error:
        log.warning("fx.rate_fetch_failed", provider=source.name, reason=type(error).__name__)
        raise RateUnavailable from error

    if _is_from_the_future(rate):
        log.warning("fx.rate_from_the_future", provider=source.name, as_of=rate.as_of.isoformat())
        raise RateUnavailable
    if _is_stale(rate, max_age):
        log.warning("fx.rate_stale", provider=source.name, as_of=rate.as_of.isoformat())
        raise RateUnavailable

    if mac_key is not None:
        await redis.attempt(
            _write(redis, key, _encode(rate, mac_key), settings.fx_rate_cache_seconds),
            default=None,
            use=_USE,
        )
    else:
        _remember(source, rate, settings.fx_rate_cache_seconds)
    return rate


def _is_stale(rate: Rate, max_age: timedelta) -> bool:
    return utcnow() - rate.as_of > max_age


def _is_from_the_future(rate: Rate) -> bool:
    return rate.as_of - utcnow() > MAX_FUTURE_SKEW


def _recall(source: RateSource, base: str, quote: str) -> Rate | None:
    held = _local.get(id(source))
    if held is None:
        return None
    found = held[1].get((base, quote))
    if found is None or found[1] <= utcnow():
        return None
    return found[0]


def _remember(source: RateSource, rate: Rate, seconds: int) -> None:
    identity = id(source)
    held = _local.get(identity)
    if held is None:
        # The reference is kept only for what it does when the source goes: it takes the
        # source's rates with it.
        held = (weakref.ref(source, lambda _gone: _local.pop(identity, None)), {})
        _local[identity] = held
    now = utcnow()
    rates = held[1]
    # Whatever has lapsed goes as something new comes in, so the map stays the size of
    # the pairs in use.
    for pair in [pair for pair, (_, until) in rates.items() if until <= now]:
        del rates[pair]
    rates[rate.base, rate.quote] = (rate, now + timedelta(seconds=seconds))


def _mac_key(settings: Settings) -> bytes | None:
    if settings.fx_cache_mac_key is None:
        return None
    configured = settings.fx_cache_mac_key.get_secret_value().encode()
    return hmac.new(configured, _MAC_LABEL, hashlib.sha256).digest()


def _mac(mac_key: bytes, base: str, quote: str, mid: str, as_of: str) -> str:
    # The pair is in the MAC, so an entry copied to another pair's key does not verify.
    # JSON, so that no choice of values makes two different entries read the same.
    message = json.dumps([base, quote, mid, as_of]).encode()
    return hmac.new(mac_key, message, hashlib.sha256).hexdigest()


async def _read(redis: RedisStore, key: str) -> str | None:
    value = await redis.client.get(key)
    return value if isinstance(value, str) else None


async def _write(redis: RedisStore, key: str, value: str, seconds: int) -> None:
    await redis.client.set(key, value, ex=seconds)


def _encode(rate: Rate, mac_key: bytes) -> str:
    # The mid as a string: a JSON number would come back as a float.
    mid, as_of = format(rate.mid, "f"), rate.as_of.isoformat()
    return json.dumps(
        {"mid": mid, "as_of": as_of, "mac": _mac(mac_key, rate.base, rate.quote, mid, as_of)}
    )


def _decode(cached: str | None, base: str, quote: str, mac_key: bytes) -> Rate | None:
    """The cached rate, or None for anything that is not one this deployment wrote: the
    cache is a convenience, and an entry that cannot be read or does not carry the right
    MAC is the same as no entry."""
    if cached is None:
        return None
    try:
        document = json.loads(cached)
        text_mid, text_as_of, mac = document["mid"], document["as_of"], document["mac"]
        if not (isinstance(text_mid, str) and isinstance(text_as_of, str) and isinstance(mac, str)):
            return None
        # Before anything in the entry is used. Compared as bytes and in constant time.
        expected = _mac(mac_key, base, quote, text_mid, text_as_of)
        if not hmac.compare_digest(mac.encode(), expected.encode()):
            log.warning("fx.rate_cache_rejected", base=base, quote=quote)
            return None
        mid = Decimal(text_mid)
        as_of = datetime.fromisoformat(text_as_of)
    except ValueError, TypeError, KeyError, InvalidOperation:
        return None
    if not mid.is_finite() or mid <= 0 or as_of.tzinfo is None:
        return None
    return Rate(base=base, quote=quote, mid=mid, as_of=as_of)

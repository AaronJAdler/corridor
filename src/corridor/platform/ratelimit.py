"""Rate limiting: a token bucket per subject, kept in Redis.

A bucket holds up to ``capacity`` tokens and gains ``refill_per_second`` of them every
second. A request takes its cost from the bucket, or is refused and told how long to wait.
One Lua script reads the bucket, refills it, decides and writes it back, so callers that
arrive together cannot spend the same token.

Nothing here is needed for correctness, so the limiter fails open: when Redis cannot be
reached the request is allowed and the failure is counted.
"""

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Self

from redis.commands.core import AsyncScript

from corridor.platform.clock import utcnow
from corridor.platform.metrics import RATE_LIMIT_REJECTIONS
from corridor.platform.redis import RedisStore

# KEYS[1]  the bucket: a hash with the fields tokens and ts
# ARGV[1]  capacity, in tokens
# ARGV[2]  refill rate, in tokens per second
# ARGV[3]  the caller's clock, in milliseconds
# ARGV[4]  what this request costs, in tokens
# ARGV[5]  how long an untouched bucket is kept, in milliseconds
#
# Replies {allowed, whole tokens left, milliseconds until the cost can be met}.
_BUCKET_SCRIPT: Final = """
-- A Lua number is a double. The level is held as a whole number of millionths of a token,
-- so that comparing it with the cost and taking the cost from it are exact.
local unit = 1000000
local capacity = tonumber(ARGV[1]) * unit
local per_ms = tonumber(ARGV[2]) * unit / 1000
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4]) * unit

local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local stored = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now

-- A caller whose clock is behind the one that last wrote the bucket sees no time pass.
local elapsed = math.max(0, now - ts)
-- The refill is the one inexact quantity. It is rounded to the nearest millionth.
local level = math.min(capacity, stored + math.floor(elapsed * per_ms + 0.5))

if level < cost then
  -- A refusal writes nothing: the stored bucket still describes it, and a flood of refused
  -- requests costs no writes. A later call therefore refills from the same stored level,
  -- and the wait is worked out from that level too: it ends at the first millisecond at
  -- which the refill, rounded as above, covers what the stored level is missing.
  local missing = cost - stored
  local due = math.ceil((missing - 0.5) / per_ms)
  -- The division can round down onto a millisecond that the multiplication above falls
  -- just short of. The token is then due a millisecond later.
  if math.floor(due * per_ms + 0.5) < missing then
    due = due + 1
  end
  return {0, math.floor(level / unit), due - elapsed}
end

level = level - cost
-- The bucket's time never moves backwards. If a slow clock could set it back, the next
-- caller with a correct clock would be credited for time that had already been counted.
redis.call('HSET', KEYS[1], 'tokens', level, 'ts', math.max(ts, now))
redis.call('PEXPIRE', KEYS[1], ARGV[5])
return {1, math.floor(level / unit), 0}
"""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Limit:
    """How fast one subject may go: a burst, and the rate at which it is earned back."""

    # The burst: how many requests an idle caller may make at once.
    capacity: int
    # The sustained rate.
    refill_per_second: float

    @classmethod
    def per_minute(cls, n: int) -> Self:
        """``n`` requests at once, and ``n`` more over every minute after that."""
        return cls(capacity=n, refill_per_second=n / 60)


@dataclass(frozen=True, slots=True)
class Decision:
    """What the limiter decided about one request."""

    allowed: bool
    # Whole tokens left after this request.
    remaining: int
    # 0 when allowed; otherwise at least 1.
    retry_after_seconds: int


class RateLimiter:
    """Token buckets in one Redis store.

    One instance per store is enough, and is what keeps the script from being prepared
    again for every request.
    """

    def __init__(self, store: RedisStore) -> None:
        self._store = store
        self._script: AsyncScript = store.client.register_script(_BUCKET_SCRIPT)

    async def check(self, group: str, subject: str, limit: Limit, *, cost: int = 1) -> Decision:
        """Take ``cost`` tokens from the subject's bucket in ``group``, or refuse.

        A group is one limit ("global", "auth"); each has its own bucket for every subject.
        A cost that no bucket under this limit could ever meet is a mistake in the caller,
        so it raises ``ValueError`` instead of being refused and counted.
        """
        _check_request(limit, cost)
        key = self._store.key("rl", group, _digest(subject))
        decision = await self._store.attempt(
            self._take(key, limit, cost),
            default=Decision(allowed=True, remaining=limit.capacity, retry_after_seconds=0),
            use="rate_limit",
        )
        if not decision.allowed:
            RATE_LIMIT_REJECTIONS.labels(group=group).inc()
        return decision

    async def _take(self, key: str, limit: Limit, cost: int) -> Decision:
        reply: list[int] = await self._script(
            keys=[key],
            args=[limit.capacity, limit.refill_per_second, _now_ms(), cost, _idle_ttl_ms(limit)],
        )
        allowed, remaining, wait_ms = reply
        return Decision(
            allowed=allowed == 1,
            remaining=remaining,
            retry_after_seconds=math.ceil(wait_ms / 1000),
        )


def _check_request(limit: Limit, cost: int) -> None:
    if not 0 < limit.refill_per_second < math.inf:
        raise ValueError("a limit refills at a positive, finite rate")
    if cost < 1:
        raise ValueError("a request costs at least one token")
    if cost > limit.capacity:
        raise ValueError(f"a cost of {cost} can never be met by a capacity of {limit.capacity}")


def _digest(subject: str) -> str:
    # A subject is whatever a client sent as an address or an email, and it becomes part of
    # a Redis key. Its digest is a fixed-length key part whatever characters it holds.
    # "surrogatepass" lets a string that is not valid Unicode be hashed instead of failing.
    return hashlib.sha256(subject.encode("utf-8", "surrogatepass")).hexdigest()


def _now_ms() -> int:
    # The application clock, never Redis TIME: the system has one clock, and tests move it.
    # Integer arithmetic, so no float rounding sits between the clock and the bucket.
    return (utcnow() - _EPOCH) // timedelta(milliseconds=1)


def _idle_ttl_ms(limit: Limit) -> int:
    # Twice the time a full refill takes. A bucket left alone that long is full again, so
    # forgetting it loses nothing. Rounded up: PEXPIRE takes whole milliseconds, and zero
    # would delete the key.
    return math.ceil(2 * 1000 * limit.capacity / limit.refill_per_second)

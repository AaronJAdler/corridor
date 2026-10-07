"""Redis access.

Redis holds only what can be rebuilt: rate-limit buckets, a rate cache and revocation hints.
Every caller states what happens when Redis is unavailable by passing a default to
``attempt``. For all but one that default lets the request go on. The exception is the
rate limit on requests that move money, which would rather refuse than go uncounted.
"""

from collections.abc import Awaitable

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from corridor.platform.config import Settings
from corridor.platform.metrics import REDIS_UNAVAILABLE


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
        # No client-side retry: a caller with a fallback wants the failure now, not after
        # several timeouts.
        retry=Retry(NoBackoff(), 0),
    )


class RedisStore:
    """A Redis client plus the key prefix that namespaces this deployment's keys."""

    def __init__(self, client: Redis, prefix: str) -> None:
        self.client = client
        self._prefix = prefix

    def key(self, *parts: str) -> str:
        return self._prefix + ":".join(parts)

    async def attempt[T](self, operation: Awaitable[T], *, default: T, use: str) -> T:
        """Await a Redis operation, returning ``default`` if Redis cannot be reached."""
        try:
            return await operation
        except RedisError, OSError:
            REDIS_UNAVAILABLE.labels(use=use).inc()
            return default

    async def ping(self) -> bool:
        return await self.attempt(self._ping(), default=False, use="ping")

    async def _ping(self) -> bool:
        return bool(await self.client.ping())

    async def close(self) -> None:
        await self.client.aclose()

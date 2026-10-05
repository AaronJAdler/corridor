"""Test Redis.

Tests share one Redis server, so each test works under its own key prefix and deletes only
its own keys when it ends.
"""

import os
import secrets

from redis.asyncio import Redis

REDIS_URL_ENV = "CORRIDOR_TEST_REDIS_URL"


def redis_url() -> str:
    url = os.environ.get(REDIS_URL_ENV)
    if not url:
        raise RuntimeError(
            f"{REDIS_URL_ENV} is not set. Tests need a real Redis, for example redis://127.0.0.1:6379/0"
        )
    return url


def unique_prefix() -> str:
    return f"corridor-test:{secrets.token_hex(6)}:"


async def delete_prefix(client: Redis, prefix: str) -> None:
    async for key in client.scan_iter(match=f"{prefix}*", count=500):
        await client.delete(key)

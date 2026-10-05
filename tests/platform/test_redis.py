from prometheus_client import REGISTRY
from pydantic import SecretStr

from corridor.platform.config import Settings
from corridor.platform.redis import RedisStore, create_redis


def _unavailable(use: str) -> float:
    return REGISTRY.get_sample_value("corridor_redis_unavailable_total", {"use": use}) or 0.0


async def test_keys_are_namespaced_by_the_configured_prefix(
    redis: RedisStore, settings: Settings
) -> None:
    key = redis.key("rl", "login", "203.0.113.9")
    assert key == f"{settings.redis_key_prefix}rl:login:203.0.113.9"

    await redis.client.set(key, "1", ex=30)
    assert await redis.client.get(key) == "1"


async def test_a_working_redis_answers(redis: RedisStore) -> None:
    assert await redis.ping() is True
    assert (
        await redis.attempt(redis.client.get(redis.key("missing")), default="fallback", use="t")
        is None
    )


async def test_an_unreachable_redis_yields_the_default_and_is_counted(settings: Settings) -> None:
    # Port 1 on localhost: nothing listens there, so the connection is refused at once.
    dead = settings.model_copy(update={"redis_url": SecretStr("redis://127.0.0.1:1/0")})
    store = RedisStore(create_redis(dead), "unused:")
    before = _unavailable("test-read")
    try:
        value = await store.attempt(
            store.client.get("anything"), default="fallback", use="test-read"
        )
        assert value == "fallback"
        assert await store.ping() is False
    finally:
        await store.close()

    assert _unavailable("test-read") == before + 1

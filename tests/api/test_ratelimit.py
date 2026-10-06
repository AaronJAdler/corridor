"""Rate limiting as the API applies it: every request is counted against its client address,
and a route can carry a tighter limit of its own."""

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI
from prometheus_client import REGISTRY
from pydantic import SecretStr
from starlette.types import Message, Receive, Scope, Send

from corridor.api.app import create_app
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.api.ratelimit import RateLimitMiddleware, rate_limit
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.ratelimit import Limit, RateLimiter
from corridor.platform.redis import RedisStore

ADDRESS = "203.0.113.7"
SERVICE_ENDPOINTS = ["/healthz", "/readyz", "/metrics"]
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
# Nothing listens on port 1, so a connection there is refused immediately.
DEAD_REDIS = SecretStr("redis://127.0.0.1:1/0")


@pytest.fixture(autouse=True)
def _frozen_clock_and_own_keys(clock: ManualClock, redis: RedisStore) -> None:
    """Every test here runs with the clock frozen, so a bucket refills only when the test
    moves time, and has its Redis keys removed when it ends."""


async def probe() -> dict[str, str]:
    return {"probe": "served"}


@asynccontextmanager
async def running(settings: Settings) -> AsyncIterator[FastAPI]:
    """The real app, started, with two routes added: one under the global limit alone and
    one that also carries a limit of its own."""
    application = create_app(settings)
    application.add_api_route("/probe", probe)
    application.add_api_route(
        "/probe/own-limit",
        probe,
        dependencies=[
            Depends(rate_limit("probe", per_minute=lambda s: s.rate_limit_auth_per_minute))
        ],
    )
    async with application.router.lifespan_context(application):
        yield application


def client_at(app: FastAPI, address: str | None = ADDRESS) -> httpx.AsyncClient:
    """An HTTP client whose requests reach the app from ``address``."""
    peer = (address, 50_000) if address is not None else None
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=peer)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="http://corridor.test")


def with_limits(settings: Settings, *, everywhere: int, own: int = 1_000) -> Settings:
    return settings.model_copy(
        update={"rate_limit_per_minute": everywhere, "rate_limit_auth_per_minute": own}
    )


async def statuses(http: httpx.AsyncClient, path: str, times: int) -> list[int]:
    return [(await http.get(path)).status_code for _ in range(times)]


def rejections(group: str) -> float:
    return (
        REGISTRY.get_sample_value("corridor_rate_limit_rejections_total", {"group": group}) or 0.0
    )


def redis_unavailable() -> float:
    return (
        REGISTRY.get_sample_value("corridor_redis_unavailable_total", {"use": "rate_limit"}) or 0.0
    )


def refusal_document(response: httpx.Response) -> dict[str, object]:
    return {
        "type": "https://corridor.example/problems/rate-limited",
        "title": "Too many requests",
        "status": 429,
        "code": "rate_limited",
        "request_id": response.headers["x-request-id"],
    }


# --- every request, by client address --------------------------------------------------------


async def test_a_client_is_served_up_to_the_limit_and_then_refused(settings: Settings) -> None:
    async with running(with_limits(settings, everywhere=3)) as app, client_at(app) as http:
        served = await statuses(http, "/probe", 4)

    assert served == [200, 200, 200, 429]


async def test_a_refusal_is_a_problem_document_with_retry_after_and_the_request_id(
    settings: Settings,
) -> None:
    async with running(with_limits(settings, everywhere=3)) as app, client_at(app) as http:
        await statuses(http, "/probe", 3)
        refused = await http.get("/probe")

    assert refused.status_code == 429
    assert refused.headers["content-type"] == PROBLEM_CONTENT_TYPE
    # Three a minute is a token every twenty seconds.
    assert refused.headers["retry-after"] == "20"
    assert UUID.fullmatch(refused.headers["x-request-id"])
    assert refused.json() == refusal_document(refused)


async def test_a_refused_request_is_still_logged_with_its_request_id(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    async with running(with_limits(settings, everywhere=1)) as app, client_at(app) as http:
        await http.get("/probe")
        capsys.readouterr()
        refused = await http.get("/probe")
        logged = capsys.readouterr().out

    lines = [json.loads(line) for line in logged.splitlines()]
    access = [line for line in lines if line["event"] == "http.request"]
    assert [(line["status"], line["request_id"]) for line in access] == [
        (429, refused.headers["x-request-id"])
    ]


async def test_waiting_as_long_as_retry_after_says_is_enough_and_a_second_less_is_not(
    settings: Settings, clock: ManualClock
) -> None:
    async with running(with_limits(settings, everywhere=3)) as app, client_at(app) as http:
        await statuses(http, "/probe", 3)
        wait = int((await http.get("/probe")).headers["retry-after"])
        clock.advance(seconds=wait - 1)
        too_soon = await http.get("/probe")
        clock.advance(seconds=1)
        on_time = await http.get("/probe")

    assert (wait, too_soon.status_code, on_time.status_code) == (20, 429, 200)


async def test_each_client_address_has_a_bucket_of_its_own(settings: Settings) -> None:
    async with (
        running(with_limits(settings, everywhere=1)) as app,
        client_at(app, "203.0.113.7") as one,
        client_at(app, "203.0.113.8") as another,
    ):
        first = await statuses(one, "/probe", 2)
        second = await statuses(another, "/probe", 2)

    assert first == [200, 429]
    assert second == [200, 429]


async def test_a_forwarded_for_header_does_not_change_whose_bucket_is_used(
    settings: Settings,
) -> None:
    # Any client can send the header. Only the server's trusted-proxy handling may read it,
    # and by the time a request reaches the app it has.
    async with running(with_limits(settings, everywhere=1)) as app, client_at(app) as http:
        first = await http.get("/probe")
        spoofed = await http.get("/probe", headers={"X-Forwarded-For": "198.51.100.1"})

    assert (first.status_code, spoofed.status_code) == (200, 429)


async def test_requests_with_no_client_address_share_a_bucket_of_their_own(
    settings: Settings, redis: RedisStore
) -> None:
    async with running(with_limits(settings, everywhere=2)) as app, client_at(app, None) as http:
        served = await statuses(http, "/probe", 3)

    assert served == [200, 200, 429]
    # They were counted under a subject of their own, which is now spent.
    spent = await RateLimiter(redis).check("global", "unknown", Limit.per_minute(2))
    assert spent.allowed is False


async def test_the_app_as_wired_counts_a_request_against_the_configured_global_limit(
    client: httpx.AsyncClient, redis: RedisStore, settings: Settings
) -> None:
    await client.get("/v1/no-such-thing")

    # The shared client connects from 127.0.0.1. Its request took one of the 600 tokens a
    # minute that the settings allow, and this check takes the next.
    limit = Limit.per_minute(settings.rate_limit_per_minute)
    following = await RateLimiter(redis).check("global", "127.0.0.1", limit)
    assert (limit.capacity, following.remaining) == (600, 598)


# --- what is never limited -------------------------------------------------------------------


@pytest.mark.parametrize("path", SERVICE_ENDPOINTS)
async def test_a_service_endpoint_is_served_when_the_clients_bucket_is_empty(
    settings: Settings, path: str
) -> None:
    async with running(with_limits(settings, everywhere=1)) as app, client_at(app) as http:
        spent = await statuses(http, "/probe", 2)
        service = await statuses(http, path, 3)

    assert spent == [200, 429]
    assert service == [200, 200, 200]


@pytest.mark.parametrize("path", SERVICE_ENDPOINTS)
async def test_a_service_endpoint_takes_nothing_from_the_clients_bucket(
    settings: Settings, path: str
) -> None:
    async with running(with_limits(settings, everywhere=2)) as app, client_at(app) as http:
        await statuses(http, path, 5)
        afterwards = await statuses(http, "/probe", 3)

    assert afterwards == [200, 200, 429]


@pytest.mark.parametrize("path", ["/probe", "/probe/own-limit"])
async def test_rate_limiting_can_be_switched_off(
    settings: Settings, redis: RedisStore, path: str
) -> None:
    off = with_limits(settings, everywhere=1, own=1).model_copy(
        update={"rate_limit_enabled": False}
    )
    async with running(off) as app, client_at(app) as http:
        served = await statuses(http, path, 3)

    assert served == [200, 200, 200]
    # Redis was not even asked.
    keys = [key async for key in redis.client.scan_iter(match=f"{settings.redis_key_prefix}*")]
    assert keys == []


async def test_a_scope_that_is_not_http_passes_straight_through() -> None:
    passed: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        passed.append(scope["type"])

    async def receive() -> Message:
        raise AssertionError("the middleware has no business reading this connection")

    async def send(message: Message) -> None:
        raise AssertionError("the middleware has no business answering this connection")

    middleware = RateLimitMiddleware(inner)
    # Neither scope names an app, so trying to limit either would fail looking for one.
    await middleware({"type": "lifespan"}, receive, send)
    await middleware({"type": "websocket", "path": "/probe"}, receive, send)

    assert passed == ["lifespan", "websocket"]


# --- a route with a limit of its own ---------------------------------------------------------


async def test_a_route_with_its_own_limit_is_refused_once_that_limit_is_spent(
    settings: Settings,
) -> None:
    async with (
        running(with_limits(settings, everywhere=100, own=2)) as app,
        client_at(app) as http,
    ):
        served = await statuses(http, "/probe/own-limit", 2)
        refused = await http.get("/probe/own-limit")
        elsewhere = await http.get("/probe")

    assert served == [200, 200]
    # The same refusal as the global limit gives. Two a minute is a token every thirty seconds.
    assert refused.status_code == 429
    assert refused.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert refused.headers["retry-after"] == "30"
    assert refused.json() == refusal_document(refused)
    # The limit belongs to that route: the same client is still served elsewhere.
    assert elsewhere.status_code == 200


async def test_a_route_with_its_own_limit_takes_one_global_token_for_each_request(
    settings: Settings,
) -> None:
    before = rejections("probe"), rejections("global")
    async with running(with_limits(settings, everywhere=5, own=2)) as app, client_at(app) as http:
        own = await statuses(http, "/probe/own-limit", 3)
        elsewhere = await statuses(http, "/probe", 3)

    # Three requests to the route, the third refused by the route's own limit, took three of
    # the five global tokens: no more and no fewer. Two were left for the other route.
    assert own == [200, 200, 429]
    assert elsewhere == [200, 200, 429]
    assert (rejections("probe"), rejections("global")) == (before[0] + 1, before[1] + 1)


# --- Redis unavailable -----------------------------------------------------------------------


async def test_with_redis_unreachable_every_request_is_served(settings: Settings) -> None:
    down = with_limits(settings, everywhere=1, own=1).model_copy(update={"redis_url": DEAD_REDIS})
    before = redis_unavailable()
    async with running(down) as app, client_at(app) as http:
        served = await statuses(http, "/probe", 3) + await statuses(http, "/probe/own-limit", 3)

    assert served == [200, 200, 200, 200, 200, 200]
    # Each request asked once for the global limit; the last three asked again for their own.
    assert redis_unavailable() == before + 9

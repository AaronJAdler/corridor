"""Every response carries the security headers: served, refused, missing or broken."""

import dataclasses

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse

from corridor.api.headers import CONTENT_SECURITY_POLICY, SECURITY_HEADERS
from corridor.platform.clock import ManualClock
from corridor.platform.redis import RedisStore
from tests.support.auth import register_user


async def _boom() -> None:
    raise RuntimeError("something unexpected")


def assert_secured(response: httpx.Response, *, cache: str = "no-store") -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, (name, response.status_code)
    assert response.headers.get("content-security-policy") == CONTENT_SECURITY_POLICY
    assert response.headers.get("cache-control") == cache


def test_the_headers_are_the_ones_meant() -> None:
    # Written out: a header dropped from the list is a decision that shows up here.
    assert SECURITY_HEADERS == {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    }
    assert CONTENT_SECURITY_POLICY == "default-src 'none'; frame-ancestors 'none'"


async def test_a_served_request_carries_them(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert_secured(response)


async def test_an_authenticated_answer_carries_them(client: httpx.AsyncClient) -> None:
    maria = await register_user(client)

    response = await client.get("/v1/wallets", headers=maria.headers)

    assert response.status_code == 200
    assert_secured(response)


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/v1/no-such-thing", 404),
        ("DELETE", "/healthz", 405),
        ("GET", "/v1/me", 401),
        ("POST", "/v1/auth/login", 422),
    ],
)
async def test_a_refused_request_carries_them(
    client: httpx.AsyncClient, method: str, path: str, status: int
) -> None:
    response = await client.request(method, path)

    assert response.status_code == status
    assert_secured(response)


async def test_an_answer_with_no_body_carries_them(client: httpx.AsyncClient) -> None:
    maria = await register_user(client)

    response = await client.post("/v1/auth/logout", headers=maria.headers)

    assert response.status_code == 204
    assert_secured(response)


async def test_an_unexpected_error_carries_them(app: FastAPI, client: httpx.AsyncClient) -> None:
    # The handler for this one runs outside every middleware the app adds.
    app.add_api_route("/boom", _boom)

    response = await client.get("/boom")

    assert response.status_code == 500
    assert_secured(response)


async def test_a_request_refused_by_the_rate_limiter_carries_them(
    app: FastAPI, client: httpx.AsyncClient, clock: ManualClock, redis: RedisStore
) -> None:
    container = app.state.container
    app.state.container = dataclasses.replace(
        container, settings=container.settings.model_copy(update={"rate_limit_per_minute": 1})
    )
    await client.get("/v1/me")

    response = await client.get("/v1/me")

    assert response.status_code == 429
    assert_secured(response)


async def test_a_request_refused_for_its_size_carries_them(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/auth/login", content=b"x" * (64 * 1024 + 1))

    assert response.status_code == 413
    assert_secured(response)


async def test_a_handler_that_allows_caching_is_left_to(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    async def cached() -> JSONResponse:
        return JSONResponse({}, headers={"Cache-Control": "public, max-age=60"})

    app.add_api_route("/cached", cached)

    response = await client.get("/cached")

    assert_secured(response, cache="public, max-age=60")


async def test_the_documentation_page_is_not_given_a_policy_that_would_blank_it(
    client: httpx.AsyncClient,
) -> None:
    # It loads a script and a stylesheet, which the policy for everything else forbids.
    response = await client.get("/docs")

    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value

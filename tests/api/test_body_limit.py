"""A request body longer than the limit is refused before anything parses it."""

import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.platform.config import Settings

LIMIT = 64 * 1024


async def _echo(request: Request) -> dict[str, int]:
    return {"read": len(await request.body())}


async def _echo_json(body: dict[str, Any]) -> dict[str, int]:
    return {"fields": len(body)}


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    """The app with two routes that read a body: one as bytes and one as a parsed document."""
    app.add_api_route("/echo", _echo, methods=["POST"])
    app.add_api_route("/echo-json", _echo_json, methods=["POST"])
    return app


async def _chunks(total: int, size: int = 4096) -> AsyncIterator[bytes]:
    """A body of ``total`` bytes sent in pieces, with no length declared."""
    sent = 0
    while sent < total:
        piece = min(size, total - sent)
        yield b"x" * piece
        sent += piece


def assert_too_large(response: httpx.Response) -> None:
    assert response.status_code == 413, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = response.json()
    assert (body["code"], body["status"]) == ("payload_too_large", 413)
    assert body["request_id"] == response.headers["x-request-id"]


def test_the_limit_is_64_kib_unless_configured(settings: Settings) -> None:
    assert settings.max_request_body_bytes == LIMIT


async def test_a_body_of_exactly_the_limit_is_served(client: httpx.AsyncClient) -> None:
    response = await client.post("/echo", content=b"x" * LIMIT)

    assert (response.status_code, response.json()) == (200, {"read": LIMIT})


async def test_a_body_one_byte_over_the_limit_is_refused_with_413(
    client: httpx.AsyncClient,
) -> None:
    assert_too_large(await client.post("/echo", content=b"x" * (LIMIT + 1)))


async def test_a_declared_length_over_the_limit_is_refused_without_the_body_being_read(
    app: FastAPI,
) -> None:
    received: list[str] = []

    async def receive() -> dict[str, Any]:
        received.append("asked")
        return {"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/echo",
        "raw_path": b"/echo",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-length", str(LIMIT + 1).encode())],
        "client": ("203.0.113.9", 50_000),
        "server": ("corridor.test", 80),
    }
    await app(scope, receive, send)

    assert sent[0]["status"] == 413
    assert received == []


async def test_a_body_with_no_declared_length_is_refused_once_it_passes_the_limit(
    client: httpx.AsyncClient,
) -> None:
    assert_too_large(await client.post("/echo", content=_chunks(LIMIT + 1)))


async def test_a_body_with_no_declared_length_within_the_limit_is_served(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/echo", content=_chunks(LIMIT))

    assert (response.status_code, response.json()) == (200, {"read": LIMIT})


async def test_a_body_that_understates_its_length_is_refused_by_what_arrives(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/echo", content=b"x" * (LIMIT + 1), headers={"Content-Length": "10"}
    )

    assert_too_large(response)


async def test_an_oversized_document_is_refused_as_too_large_and_not_as_malformed(
    client: httpx.AsyncClient,
) -> None:
    # The parser is what was reading when the limit was passed. Its own account of the
    # failure, a 400 that says the body could not be parsed, is not what is answered.
    document = json.dumps({"padding": "x" * LIMIT}).encode()

    response = await client.post(
        "/echo-json", content=_chunks(len(document)), headers={"Content-Type": "application/json"}
    )

    assert_too_large(response)


async def test_a_real_route_refuses_an_oversized_body_before_it_asks_who_is_calling(
    client: httpx.AsyncClient,
) -> None:
    # No credential is sent. Had the request reached the route it would be a 401.
    response = await client.post("/v1/transfers", content=b"{" + b" " * LIMIT + b"}")

    assert_too_large(response)


async def test_the_limit_is_a_setting(app: FastAPI, client: httpx.AsyncClient) -> None:
    container = app.state.container
    app.state.container = dataclasses.replace(
        container, settings=container.settings.model_copy(update={"max_request_body_bytes": 2048})
    )

    served = await client.post("/echo", content=b"x" * 2048)
    refused = await client.post("/echo", content=b"x" * 2049)

    assert served.status_code == 200
    assert_too_large(refused)
    assert "2048" in refused.json()["detail"]


async def test_a_request_without_a_body_is_not_affected(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).status_code == 200

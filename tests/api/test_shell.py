"""The app shell: service endpoints, request ids and error rendering."""

import json
import re
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, SecretStr

from corridor.api.app import create_app
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.platform.config import Settings
from corridor.platform.errors import DomainError, RateLimited

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
# Nothing listens on port 1, so a connection there is refused immediately.
DEAD_POSTGRES = SecretStr("postgresql+asyncpg://nobody@127.0.0.1:1/nothing")
DEAD_REDIS = SecretStr("redis://127.0.0.1:1/0")


class Teapot(DomainError):
    status = 418
    code = "teapot"
    title = "I'm a teapot"


class Echo(BaseModel):
    name: str
    password: str


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    """The real app plus a few routes that fail in the ways the handlers must render."""

    async def refuse() -> None:
        raise Teapot("Short and stout.", capacity_ml=250)

    async def limited() -> None:
        raise RateLimited(7)

    async def crash() -> None:
        raise RuntimeError("secret internal detail: /var/lib/keys")

    async def echo(body: Echo) -> dict[str, str]:
        return {"name": body.name}

    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    app.add_api_route("/probe/refuse", refuse)
    app.add_api_route("/probe/limited", limited)
    app.add_api_route("/probe/crash", crash)
    app.add_api_route("/probe/echo", echo, methods=["POST"])
    app.add_api_route("/probe/items/{item_id}", item)
    return app


async def _client_for(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://corridor.test") as http:
            yield http


def without_request_id(response: httpx.Response) -> dict[str, object]:
    body: dict[str, object] = response.json()
    assert body.pop("request_id") == response.headers["x-request-id"]
    return body


# --- service endpoints ---------------------------------------------------------------------


async def test_healthz_says_the_process_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


async def test_readyz_is_ok_when_both_stores_answer(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}}


async def test_readyz_reports_redis_down_as_degraded_not_failed(settings: Settings) -> None:
    async for client in _client_for(settings.model_copy(update={"redis_url": DEAD_REDIS})):
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "checks": {"postgres": "ok", "redis": "down"}}


async def test_readyz_fails_when_postgres_is_down(settings: Settings) -> None:
    async for client in _client_for(settings.model_copy(update={"database_url": DEAD_POSTGRES})):
        response = await client.get("/readyz")
        health = await client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "down", "redis": "ok"},
    }
    # Liveness does not depend on the database: a restart would not fix an outage there.
    assert health.status_code == 200


async def test_metrics_are_not_served_on_the_api_port_unless_asked_for(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    response = await client.get("/metrics")
    missing = await client.get("/no-such-path")

    assert settings.metrics_public is False
    assert response.status_code == 404
    assert "corridor_" not in response.text
    # Answered exactly as a path that was never there.
    assert without_request_id(response) == without_request_id(missing)


async def test_metrics_are_exposed_in_prometheus_format_where_a_deployment_asks(
    settings: Settings,
) -> None:
    async for client in _client_for(settings.model_copy(update={"metrics_public": True})):
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE corridor_db_transaction_retries_total counter" in response.text


async def test_interactive_docs_are_off_in_production(settings: Settings) -> None:
    async for client in _client_for(settings.model_copy(update={"environment": "production"})):
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
    async for client in _client_for(settings):
        assert (await client.get("/openapi.json")).status_code == 200


# --- request ids ---------------------------------------------------------------------------


async def test_every_response_carries_a_generated_request_id(client: httpx.AsyncClient) -> None:
    first = (await client.get("/healthz")).headers["x-request-id"]
    second = (await client.get("/healthz")).headers["x-request-id"]

    assert UUID.fullmatch(first)
    assert UUID.fullmatch(second)
    assert first != second


async def test_a_well_formed_client_request_id_is_kept(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "mobile-7f3a9c21.retry-2"})
    assert response.headers["x-request-id"] == "mobile-7f3a9c21.retry-2"


@pytest.mark.parametrize(
    "supplied", ["short", "x" * 65, "has space in it", "<script>alert(1)</script>", "a,b;c=d{}"]
)
async def test_a_malformed_client_request_id_is_replaced(
    client: httpx.AsyncClient, supplied: str
) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": supplied})
    assert UUID.fullmatch(response.headers["x-request-id"])


# --- errors --------------------------------------------------------------------------------


async def test_a_domain_error_becomes_a_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.get("/probe/refuse")

    assert response.status_code == 418
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json() == {
        "type": "https://corridor.example/problems/teapot",
        "title": "I'm a teapot",
        "status": 418,
        "code": "teapot",
        "detail": "Short and stout.",
        "request_id": response.headers["x-request-id"],
        "capacity_ml": 250,
    }


async def test_a_domain_error_can_set_response_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/probe/limited")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json()["code"] == "rate_limited"


async def test_an_extension_member_cannot_replace_a_standard_one(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    async def impostor() -> None:
        raise Teapot("Honest detail.", status=200, code="ok", request_id="forged")

    app.add_api_route("/probe/impostor", impostor)
    body = (await client.get("/probe/impostor")).json()

    assert (body["status"], body["code"]) == (418, "teapot")
    assert UUID.fullmatch(body["request_id"])


async def test_an_unknown_route_is_a_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/no-such-thing")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = response.json()
    assert (body["code"], body["title"], body["status"]) == ("not_found", "Not Found", 404)
    assert body["request_id"] == response.headers["x-request-id"]


async def test_a_wrong_method_is_a_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.post("/healthz")

    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"
    assert response.headers["allow"] == "GET"


async def test_a_schema_violation_names_the_field_and_never_echoes_the_input(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/probe/echo", json={"name": 42, "password": ["hunter2-in-a-list"]}
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = response.json()
    assert body["code"] == "invalid_request"
    assert {error["field"] for error in body["errors"]} == {"body.name", "body.password"}
    assert all(set(error) == {"field", "message", "type"} for error in body["errors"])
    assert "hunter2" not in response.text
    assert "42" not in json.dumps(body["errors"])


async def test_a_bad_path_parameter_is_a_schema_violation(client: httpx.AsyncClient) -> None:
    response = await client.get("/probe/items/not-a-number")
    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "path.item_id"


async def test_an_unexpected_error_is_a_500_that_reveals_nothing(client: httpx.AsyncClient) -> None:
    response = await client.get("/probe/crash")

    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = response.json()
    assert body["code"] == "internal_error"
    assert "secret internal detail" not in response.text
    assert "RuntimeError" not in response.text
    # The id in the body and the header is what ties the report to the logged traceback.
    assert body["request_id"] == response.headers["x-request-id"]
    assert UUID.fullmatch(body["request_id"])


# --- access log ----------------------------------------------------------------------------


async def test_the_access_log_records_the_route_template_not_the_path(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    response = await client.get("/probe/items/31337")
    await client.get("/v1/no-such-thing/31337")

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    access = [line for line in lines if line["event"] == "http.request"]
    assert [(line["method"], line["route"], line["status"]) for line in access] == [
        ("GET", "/probe/items/{item_id}", 200),
        ("GET", "unmatched", 404),
    ]
    assert access[0]["request_id"] == response.headers["x-request-id"]
    assert "31337" not in json.dumps(access)
    assert access[0]["duration_ms"] >= 0


async def test_an_unexpected_error_is_logged_with_its_traceback_and_request_id(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    response = await client.get("/probe/crash")

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    (error,) = [line for line in lines if line["event"] == "request.unhandled_error"]
    assert error["request_id"] == response.headers["x-request-id"]
    assert error["level"] == "error"
    assert "RuntimeError" in error["exception"]

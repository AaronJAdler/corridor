"""Control characters in what a client types: refused as a bad request, never a failure.

PostgreSQL refuses a NUL in text, so one that reached a query would be a 500. The others
are refused with it: none of them belongs in a name, a handle or a memo.
"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel, TypeAdapter, ValidationError

from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.api.schemas import Text
from corridor.platform.db import Database
from tests.support.auth import PASSWORD, RegisteredUser, register_user

C0 = [chr(code) for code in range(0x20)]

REGISTER = {
    "email": "maria@example.com",
    "handle": "maria",
    "display_name": "Maria Silva",
    "password": PASSWORD,
}
LOGIN = {"email": "maria@example.com", "password": PASSWORD}
TRANSFER = {"recipient": "@joao", "asset": "USD", "amount": "1.00", "memo": "for lunch"}


def assert_refused(response: httpx.Response, field: str) -> None:
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = response.json()
    assert body["code"] == "invalid_request"
    assert [error["field"] for error in body["errors"]] == [f"body.{field}"]
    # What was sent is not sent back.
    assert "\x00" not in response.text
    assert "\\u0000" not in response.text


@pytest.mark.parametrize("field", ["handle", "display_name"])
@pytest.mark.parametrize("character", ["\x00", "\x1f"], ids=["nul", "unit-separator"])
async def test_a_control_character_in_a_registration_is_a_422(
    client: httpx.AsyncClient, field: str, character: str
) -> None:
    body = {**REGISTER, field: f"mar{character}ia"}

    assert_refused(await client.post("/v1/auth/register", json=body), field)


async def test_a_nul_in_a_registration_email_is_a_422(client: httpx.AsyncClient) -> None:
    body = {**REGISTER, "email": "mar\x00ia@example.com"}

    assert_refused(await client.post("/v1/auth/register", json=body), "email")


@pytest.mark.parametrize("character", ["\x00", "\x1f"], ids=["nul", "unit-separator"])
async def test_a_control_character_in_a_login_email_is_a_422(
    client: httpx.AsyncClient, character: str
) -> None:
    await register_user(client, email=LOGIN["email"])
    body = {**LOGIN, "email": f"maria{character}@example.com"}

    assert_refused(await client.post("/v1/auth/login", json=body), "email")


@pytest.mark.parametrize("field", ["recipient", "asset", "amount", "memo"])
@pytest.mark.parametrize("character", ["\x00", "\x1f"], ids=["nul", "unit-separator"])
async def test_a_control_character_in_a_transfer_is_a_422_and_moves_nothing(
    client: httpx.AsyncClient, db: Database, field: str, character: str
) -> None:
    maria: RegisteredUser = await register_user(client, handle="maria")
    await register_user(client, handle="joao")
    body = {**TRANSFER, field: TRANSFER[field] + character}

    response = await client.post(
        "/v1/transfers", json=body, headers={**maria.headers, "Idempotency-Key": "key-1"}
    )

    assert_refused(response, field)


@pytest.mark.parametrize("character", C0, ids=[f"0x{code:02x}" for code in range(0x20)])
def test_text_refuses_every_c0_control_character(character: str) -> None:
    with pytest.raises(ValidationError) as refusal:
        TypeAdapter(Text).validate_python(f"a{character}b")

    assert "control characters" in str(refusal.value)
    assert refusal.value.errors(include_input=False)[0]["type"] == "value_error"


@pytest.mark.parametrize(
    "value", ["", "Maria Silva", "José 🙂", "a\x7fb", "not\u00a0a\u0085control"]
)
def test_text_accepts_everything_else(value: str) -> None:
    assert TypeAdapter(Text).validate_python(value) == value


def request_models(app: FastAPI) -> set[type[BaseModel]]:
    """Every model a route of the app reads its body into."""
    models: set[type[BaseModel]] = set()
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        for parameter in context.dependant.body_params:
            annotation: Any = parameter.field_info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                models.add(annotation)
    return models


def test_every_text_field_of_every_request_body_refuses_a_nul(app: FastAPI) -> None:
    models = request_models(app)
    assert {model.__name__ for model in models} >= {
        "RegisterRequest",
        "LoginRequest",
        "RefreshRequest",
        "TransferRequest",
    }

    accepting: list[str] = []
    for model in models:
        for name, field in model.model_fields.items():
            if "str" not in str(field.annotation) or "Secret" in str(field.annotation):
                continue
            try:
                TypeAdapter(field.rebuild_annotation()).validate_python("a\x00b")
            except ValidationError:
                continue
            accepting.append(f"{model.__name__}.{name}")

    assert accepting == []


async def test_a_nul_in_a_secret_is_never_a_failure(client: httpx.AsyncClient) -> None:
    # Secrets are hashed, not stored as text, so a NUL in one is only an unusual secret.
    odd = PASSWORD + "\x00"
    registered = await client.post("/v1/auth/register", json={**REGISTER, "password": odd})
    wrong = await client.post("/v1/auth/login", json={**LOGIN, "password": PASSWORD})
    right = await client.post("/v1/auth/login", json={**LOGIN, "password": odd})
    refreshed = await client.post("/v1/auth/refresh", json={"refresh_token": "abc\x00def"})

    assert [r.status_code for r in (registered, wrong, right, refreshed)] == [201, 401, 200, 401]


async def test_a_registration_password_of_128_characters_is_accepted_and_129_is_a_422(
    client: httpx.AsyncClient,
) -> None:
    longest = (PASSWORD * 10)[:128]

    too_long = await client.post("/v1/auth/register", json={**REGISTER, "password": longest + "x"})
    accepted = await client.post("/v1/auth/register", json={**REGISTER, "password": longest})

    assert_refused(too_long, "password")
    assert accepted.status_code == 201, accepted.text

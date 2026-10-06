"""A registered, logged-in user in one line, for any test that needs to call the API as one."""

import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

# The password of every test user. It protects nothing.
PASSWORD: Final = "correct horse battery staple"  # pragma: allowlist secret


@dataclass(frozen=True, slots=True)
class RegisteredUser:
    """What a client knows after registering and logging in."""

    user: dict[str, Any]
    password: str = field(repr=False)
    tokens: dict[str, Any] = field(repr=False)

    @property
    def id(self) -> str:
        return str(self.user["id"])

    @property
    def email(self) -> str:
        return str(self.user["email"])

    @property
    def access_token(self) -> str:
        return str(self.tokens["access_token"])

    @property
    def refresh_token(self) -> str:
        return str(self.tokens["refresh_token"])

    @property
    def headers(self) -> dict[str, str]:
        """What authenticates a request as this user."""
        return bearer(self.access_token)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def register_user(client: httpx.AsyncClient, **overrides: str) -> RegisteredUser:
    """Register a user over HTTP and log them in.

    Every field has a default, and the defaults differ from call to call, so a test can
    register several users without naming them. Pass ``email``, ``handle``, ``display_name``
    or ``password`` to choose one.
    """
    name = f"user_{secrets.token_hex(5)}"
    body = {
        "email": f"{name}@example.com",
        "handle": name,
        "display_name": name.replace("_", " ").title(),
        "password": PASSWORD,
        **overrides,
    }
    registered = await client.post("/v1/auth/register", json=body)
    assert registered.status_code == 201, registered.text
    tokens = await login(client, body["email"], body["password"])
    return RegisteredUser(user=registered.json(), password=body["password"], tokens=tokens)


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> dict[str, Any]:
    """Log in and return the token response."""
    response = await client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    tokens: dict[str, Any] = response.json()
    return tokens


@dataclass(frozen=True, slots=True)
class ServedRoute:
    """One path and method the app answers, with every callable its dependencies run."""

    path: str
    method: str
    calls: frozenset[object]


def served_routes(app: FastAPI) -> Iterator[ServedRoute]:
    """Every route the app serves, including those of the routers it includes.

    ``app.routes`` is not that list: an included router is one opaque entry in it. FastAPI
    resolves the entries into the routes they stand for, each with the dependencies it has
    in effect, and that is what its own OpenAPI document is built from.
    """
    for context in iter_route_contexts(app.routes):
        methods = context.methods
        if context.path is None or not methods:
            # A mount or a websocket. Reported under a method nothing is listed with, so
            # that it cannot pass for a known route.
            yield ServedRoute(str(context.path), "*", frozenset())
            continue
        # A route that FastAPI did not build has no dependencies at all.
        built = isinstance(context.original_route, APIRoute)
        calls = frozenset(_calls(context.dependant)) if built else frozenset()
        for method in sorted(methods):
            # Starlette answers HEAD wherever it answers GET, with the same handler.
            if method == "HEAD" and "GET" in methods:
                continue
            yield ServedRoute(context.path, method, calls)


def _calls(dependant: Dependant) -> Iterator[object]:
    yield dependant.call
    for dependency in dependant.dependencies:
        yield from _calls(dependency)

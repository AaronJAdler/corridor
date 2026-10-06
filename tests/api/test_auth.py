"""Authentication over HTTP: registering, logging in, refreshing, logging out, and what a
request has to carry to be let in."""

import dataclasses
import json
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from corridor import audit, identity
from corridor.api.app import create_app
from corridor.api.deps import AdminPrincipal, get_principal, require
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.api.routers import auth as auth_routes
from corridor.cli import app as cli
from corridor.identity import Principal, Scope
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.redis import RedisStore, create_redis
from tests.support.auth import PASSWORD, bearer, login, register_user, served_routes

# Derived from the one marked constant, so that no second credential is written down.
WRONG_PASSWORD = PASSWORD + " but wrong"
WEAK_PASSWORD = PASSWORD[:11]

# Nothing listens on port 1, so a connection there is refused immediately.
DEAD_REDIS = SecretStr("redis://127.0.0.1:1/0")

MARIA = {
    "email": "maria@example.com",
    "handle": "maria",
    "display_name": "Maria Silva",
    "password": PASSWORD,
}


@pytest.fixture(autouse=True)
def _own_redis_keys(redis: RedisStore) -> None:
    """Every test here has the Redis keys it made removed when it ends."""


@asynccontextmanager
async def serving(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """A client for an app of its own, started with other settings than the fixture's."""
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://corridor.test") as http:
            yield http


@asynccontextmanager
async def redis_unreachable(app: FastAPI, settings: Settings) -> AsyncIterator[None]:
    """Cut the running app off from Redis for the length of the block."""
    container = app.state.container
    dead = RedisStore(
        create_redis(settings.model_copy(update={"redis_url": DEAD_REDIS})),
        settings.redis_key_prefix,
    )
    app.state.container = dataclasses.replace(container, redis=dead)
    try:
        yield
    finally:
        app.state.container = container
        await dead.close()


async def events(db: Database, action: str) -> list[audit.AuditEvent]:
    async with db.transaction() as session:
        return await audit.list_events(session, action=action)


def without_request_id(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    assert body.pop("request_id") == response.headers["x-request-id"]
    return body


def session_id_of(access_token: str, settings: Settings) -> Any:
    claims = identity.verify_access_token(
        access_token, keys=identity.load_keyset(settings), settings=settings
    )
    return claims.session_id


async def me(client: httpx.AsyncClient, access_token: str) -> httpx.Response:
    return await client.get("/v1/me", headers=bearer(access_token))


async def refresh(client: httpx.AsyncClient, refresh_token: str) -> httpx.Response:
    return await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})


# --- start-up --------------------------------------------------------------------------------


async def test_the_app_does_not_start_without_a_signing_key(settings: Settings) -> None:
    application = create_app(settings.model_copy(update={"jwt_signing_key": None}))

    with pytest.raises(identity.ConfigurationError, match="CORRIDOR_JWT_SIGNING_KEY"):
        async with application.router.lifespan_context(application):
            pass


# --- register --------------------------------------------------------------------------------


async def test_registering_creates_a_user_and_returns_it(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/auth/register", json={**MARIA, "email": "Maria@Example.com"})

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "email",
        "handle",
        "display_name",
        "role",
        "kyc_tier",
        "status",
        "created_at",
    }
    assert {key: body[key] for key in ("email", "handle", "display_name")} == {
        "email": "maria@example.com",
        "handle": "maria",
        "display_name": "Maria Silva",
    }
    assert (body["role"], body["kyc_tier"], body["status"]) == ("user", 0, "active")
    assert PASSWORD not in response.text


async def test_a_registered_user_can_log_in(client: httpx.AsyncClient) -> None:
    await client.post("/v1/auth/register", json=MARIA)

    response = await client.post(
        "/v1/auth/login", json={"email": MARIA["email"], "password": PASSWORD}
    )

    assert response.status_code == 200


async def test_registering_is_audited(client: httpx.AsyncClient, db: Database) -> None:
    response = await client.post("/v1/auth/register", json=MARIA)
    user_id = response.json()["id"]

    (event,) = await events(db, "auth.registered")
    assert (event.actor_type, event.actor_id, str(event.principal_id)) == ("user", user_id, user_id)
    assert (event.resource_type, event.resource_id, event.outcome) == ("user", user_id, "success")
    assert event.request_id == response.headers["x-request-id"]


async def test_a_taken_email_is_refused_with_409(client: httpx.AsyncClient) -> None:
    await client.post("/v1/auth/register", json=MARIA)

    response = await client.post(
        "/v1/auth/register", json={**MARIA, "email": "MARIA@example.com", "handle": "maria2"}
    )

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == "email_taken"


async def test_a_taken_handle_is_refused_with_409(client: httpx.AsyncClient) -> None:
    await client.post("/v1/auth/register", json=MARIA)

    response = await client.post(
        "/v1/auth/register", json={**MARIA, "email": "other@example.com", "handle": "Maria"}
    )

    assert (response.status_code, response.json()["code"]) == (409, "handle_taken")


async def test_a_refused_registration_is_not_audited(
    client: httpx.AsyncClient, db: Database
) -> None:
    await client.post("/v1/auth/register", json=MARIA)
    await client.post("/v1/auth/register", json=MARIA)

    assert len(await events(db, "auth.registered")) == 1


async def test_a_weak_password_is_refused_and_never_echoed(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/auth/register", json={**MARIA, "password": WEAK_PASSWORD})

    assert (response.status_code, response.json()["code"]) == (422, "weak_password")
    assert WEAK_PASSWORD not in response.text
    # Nothing was created: the address is still free.
    assert (await client.post("/v1/auth/register", json=MARIA)).status_code == 201


async def test_a_password_of_the_wrong_type_is_refused_and_never_echoed(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/v1/auth/register", json={**MARIA, "password": [WEAK_PASSWORD]})

    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    assert WEAK_PASSWORD not in response.text


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("email", "not-an-email", "invalid_request"),
        ("handle", "no spaces allowed", "invalid_handle"),
        ("display_name", "   ", "invalid_request"),
        ("display_name", "x" * 101, "invalid_request"),
    ],
)
async def test_a_malformed_registration_is_refused(
    client: httpx.AsyncClient, field: str, value: str, code: str
) -> None:
    response = await client.post("/v1/auth/register", json={**MARIA, field: value})

    assert (response.status_code, response.json()["code"]) == (422, code)


async def test_a_registration_hook_sees_the_new_user_in_the_same_transaction(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []

    async def hook(session: AsyncSession, user: identity.User) -> None:
        # The row is not committed yet, so only the transaction that wrote it can read it.
        handle = await session.execute(
            text("SELECT handle FROM users WHERE id = :id"), {"id": user.id}
        )
        seen.append((str(user.id), handle.scalar_one()))

    monkeypatch.setattr(auth_routes, "on_user_registered", [hook])
    response = await client.post("/v1/auth/register", json=MARIA)

    assert seen == [(response.json()["id"], "maria")]


async def test_a_registration_hook_that_fails_undoes_the_registration(
    client: httpx.AsyncClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hook(session: AsyncSession, user: identity.User) -> None:
        raise RuntimeError("no wallet today")

    monkeypatch.setattr(auth_routes, "on_user_registered", [hook])
    failed = await client.post("/v1/auth/register", json=MARIA)
    monkeypatch.setattr(auth_routes, "on_user_registered", [])
    again = await client.post("/v1/auth/register", json=MARIA)

    assert (failed.status_code, again.status_code) == (500, 201)
    assert len(await events(db, "auth.registered")) == 1


# --- login -----------------------------------------------------------------------------------


async def test_logging_in_returns_a_token_pair(client: httpx.AsyncClient) -> None:
    await client.post("/v1/auth/register", json=MARIA)

    response = await client.post(
        "/v1/auth/login", json={"email": "Maria@Example.com", "password": PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert (body["token_type"], body["expires_in"]) == ("Bearer", 900)
    # A token response is for its recipient alone: no cache may keep it.
    assert response.headers["cache-control"] == "no-store"
    assert (await me(client, body["access_token"])).status_code == 200


async def test_logging_in_is_audited(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    maria = await register_user(client)

    (event,) = await events(db, "auth.logged_in")
    assert (event.actor_type, event.actor_id, str(event.principal_id)) == (
        "user",
        maria.id,
        maria.id,
    )
    assert (event.resource_type, event.outcome) == ("session", "success")
    assert event.resource_id == str(session_id_of(maria.access_token, settings))


async def test_every_failed_login_gets_the_same_401(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    maria = await register_user(client)
    locked = await register_user(client)
    for _ in range(settings.login_lockout_threshold):
        await client.post(
            "/v1/auth/login", json={"email": locked.email, "password": WRONG_PASSWORD}
        )

    wrong_password = await client.post(
        "/v1/auth/login", json={"email": maria.email, "password": WRONG_PASSWORD}
    )
    unknown_email = await client.post(
        "/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    # The right password, and still refused: the account is locked.
    locked_out = await client.post(
        "/v1/auth/login", json={"email": locked.email, "password": PASSWORD}
    )
    not_an_address = await client.post(
        "/v1/auth/login", json={"email": "not an address", "password": PASSWORD}
    )

    responses = [wrong_password, unknown_email, locked_out, not_an_address]
    assert [response.status_code for response in responses] == [401] * 4
    assert [response.headers["content-type"] for response in responses] == [
        PROBLEM_CONTENT_TYPE
    ] * 4
    assert [response.headers["www-authenticate"] for response in responses] == ["Bearer"] * 4
    bodies = [without_request_id(response) for response in responses]
    assert bodies[0]["code"] == "invalid_credentials"
    assert bodies == [bodies[0]] * 4


async def test_a_failed_login_is_audited_with_its_reason_though_the_request_is_refused(
    client: httpx.AsyncClient, db: Database
) -> None:
    maria = await register_user(client)

    refused = await client.post(
        "/v1/auth/login", json={"email": maria.email, "password": WRONG_PASSWORD}
    )
    await client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})

    unknown, wrong = await events(db, "auth.login_failed")
    assert (wrong.outcome, wrong.details, wrong.actor_id, str(wrong.principal_id)) == (
        "denied",
        {"reason": "bad_password"},
        maria.id,
        maria.id,
    )
    assert wrong.request_id == refused.headers["x-request-id"]
    # Nobody is known to have made this attempt, and what they typed is not kept.
    assert (unknown.outcome, unknown.details, unknown.actor_id, unknown.principal_id) == (
        "denied",
        {"reason": "unknown_email"},
        None,
        None,
    )


async def test_repeated_failures_lock_the_account_until_the_lock_expires(
    client: httpx.AsyncClient, db: Database, settings: Settings, clock: ManualClock
) -> None:
    maria = await register_user(client)
    for _ in range(settings.login_lockout_threshold):
        await client.post("/v1/auth/login", json={"email": maria.email, "password": WRONG_PASSWORD})

    during = await client.post("/v1/auth/login", json={"email": maria.email, "password": PASSWORD})
    clock.advance(seconds=settings.login_lockout_base_seconds - 1)
    just_before = await client.post(
        "/v1/auth/login", json={"email": maria.email, "password": PASSWORD}
    )
    clock.advance(seconds=1)
    after = await client.post("/v1/auth/login", json={"email": maria.email, "password": PASSWORD})

    assert (during.status_code, just_before.status_code, after.status_code) == (401, 401, 200)
    reasons = [event.details["reason"] for event in await events(db, "auth.login_failed")]
    assert reasons == ["locked", "locked"] + ["bad_password"] * settings.login_lockout_threshold


async def test_one_failure_short_of_the_threshold_does_not_lock(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    maria = await register_user(client)
    for _ in range(settings.login_lockout_threshold - 1):
        await client.post("/v1/auth/login", json={"email": maria.email, "password": WRONG_PASSWORD})

    response = await client.post(
        "/v1/auth/login", json={"email": maria.email, "password": PASSWORD}
    )

    assert response.status_code == 200


async def test_a_login_writes_neither_the_password_nor_a_token_to_the_log(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await client.post("/v1/auth/register", json=MARIA)
    capsys.readouterr()

    await client.post("/v1/auth/login", json={"email": MARIA["email"], "password": WRONG_PASSWORD})
    tokens = await login(client, MARIA["email"])
    await me(client, tokens["access_token"])
    rotated = (await refresh(client, tokens["refresh_token"])).json()
    await refresh(client, tokens["refresh_token"])
    logged = capsys.readouterr().out

    lines = [json.loads(line) for line in logged.splitlines()]
    assert {"auth.login_failed", "auth.logged_in", "auth.refresh_reuse_detected"} <= {
        line["event"] for line in lines
    }
    for secret in (
        PASSWORD,
        WRONG_PASSWORD,
        tokens["access_token"],
        tokens["refresh_token"],
        rotated["access_token"],
        rotated["refresh_token"],
    ):
        assert secret not in logged
    # Not even the signature alone, which is what makes a token worth stealing.
    assert tokens["access_token"].rsplit(".", 1)[1] not in logged


# --- the bearer credential -------------------------------------------------------------------


async def test_me_returns_the_caller(client: httpx.AsyncClient) -> None:
    maria = await register_user(client, handle="maria")

    response = await client.get("/v1/me", headers=maria.headers)

    assert response.status_code == 200
    assert response.json() == maria.user


async def test_a_request_without_a_credential_is_unauthenticated(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/v1/me")

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer garbage",
        "Bearer a.b.c",
        "Bearer",
        "Bearer ",
        "Basic bWFyaWE6aHVudGVy",
        "garbage",
        "Bearer ck_live_0123456789abcdef0123",
    ],
)
async def test_a_request_with_a_bad_credential_is_unauthenticated(
    client: httpx.AsyncClient, header: str
) -> None:
    response = await client.get("/v1/me", headers={"Authorization": header})

    assert response.status_code == 401
    assert response.json()["code"] in {"unauthenticated", "invalid_token"}
    assert "garbage" not in response.text


async def test_the_scheme_name_is_matched_without_regard_to_case(
    client: httpx.AsyncClient,
) -> None:
    maria = await register_user(client)

    response = await client.get("/v1/me", headers={"Authorization": f"bearer {maria.access_token}"})

    assert response.status_code == 200


async def test_an_access_token_is_good_until_the_second_it_expires(
    client: httpx.AsyncClient, settings: Settings, clock: ManualClock
) -> None:
    maria = await register_user(client)

    clock.advance(seconds=settings.access_token_ttl_seconds - 1)
    last_second = await me(client, maria.access_token)
    clock.advance(seconds=1)
    expired = await me(client, maria.access_token)

    assert (last_second.status_code, expired.status_code) == (200, 401)
    assert expired.json()["code"] == "invalid_token"


async def test_a_token_for_another_audience_or_from_another_key_is_refused(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    maria = await register_user(client)
    keys = identity.load_keyset(settings)
    other_key = settings.model_copy(
        update={"jwt_signing_key": SecretStr(identity.generate_private_key_pem())}
    )

    def mint(keys: identity.KeySet, settings: Settings) -> str:
        token, _ = identity.mint_access_token(
            user_id=maria.user["id"], session_id=new_id(), role="user", keys=keys, settings=settings
        )
        return token

    ours = mint(keys, settings)
    wrong_audience = mint(keys, settings.model_copy(update={"jwt_audience": "another-api"}))
    wrong_issuer = mint(keys, settings.model_copy(update={"jwt_issuer": "someone-else"}))
    wrong_key = mint(identity.load_keyset(other_key), settings)

    # The control: minted the same way with nothing changed, the token is accepted.
    assert (await me(client, ours)).status_code == 200
    for token in (wrong_audience, wrong_issuer, wrong_key):
        response = await me(client, token)
        assert (response.status_code, response.json()["code"]) == (401, "invalid_token")


async def test_an_agent_key_is_not_accepted_yet(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/v1/me", headers={"Authorization": "Bearer ck_test_0123456789abcdef0123"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


async def test_the_principal_is_bound_to_the_log_context_of_the_request(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    maria = await register_user(client)
    capsys.readouterr()

    await client.get("/v1/me", headers=maria.headers)
    await client.get("/healthz")

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    authenticated, anonymous = [line for line in lines if line["event"] == "http.request"]
    assert (authenticated["principal_id"], authenticated["actor_id"]) == (maria.id, maria.id)
    assert authenticated["actor_type"] == "user"
    # And it does not leak into the next request.
    assert "principal_id" not in anonymous


# --- scope and role dependencies -------------------------------------------------------------


async def scoped(principal: Principal = Depends(require(Scope.WALLET_READ))) -> dict[str, str]:  # noqa: B008
    return {"user": str(principal.user_id)}


async def admin_only(principal: AdminPrincipal) -> dict[str, str]:
    return {"user": str(principal.user_id)}


def agent_with(*scopes: str) -> Principal:
    return Principal(
        user_id=new_id(),
        actor_type="agent",
        actor_id=new_id(),
        role="user",
        scopes=frozenset(scopes),
        session_id=None,
    )


async def test_a_user_session_passes_any_scope_check(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.add_api_route("/probe/scoped", scoped)
    maria = await register_user(client)

    response = await client.get("/probe/scoped", headers=maria.headers)

    assert (response.status_code, response.json()) == (200, {"user": maria.id})


async def test_a_scope_check_needs_a_credential_first(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.add_api_route("/probe/scoped", scoped)

    assert (await client.get("/probe/scoped")).status_code == 401


async def test_a_credential_without_the_scope_is_refused_with_403(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.add_api_route("/probe/scoped", scoped)
    app.dependency_overrides[get_principal] = lambda: agent_with(Scope.TRANSFERS_READ)

    response = await client.get("/probe/scoped")

    assert (response.status_code, response.json()["code"]) == (403, "insufficient_scope")


async def test_a_credential_with_only_that_scope_is_let_through(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.add_api_route("/probe/scoped", scoped)
    app.dependency_overrides[get_principal] = lambda: agent_with(Scope.WALLET_READ)

    assert (await client.get("/probe/scoped")).status_code == 200


async def test_an_admin_route_refuses_an_ordinary_user(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.add_api_route("/probe/admin", admin_only)
    maria = await register_user(client)

    response = await client.get("/probe/admin", headers=maria.headers)

    assert (response.status_code, response.json()["code"]) == (403, "permission_denied")
    assert (await client.get("/probe/admin")).status_code == 401


async def test_an_admin_route_admits_an_admin(
    app: FastAPI, client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    app.add_api_route("/probe/admin", admin_only)
    password_hash = await identity.PasswordHasher(settings).hash(PASSWORD)
    async with db.transaction() as session:
        root = await identity.register(
            session,
            email="root@example.com",
            handle="root",
            display_name="Root",
            password_hash=password_hash,
            role="admin",
        )
    tokens = await login(client, "root@example.com")

    response = await client.get("/probe/admin", headers=bearer(tokens["access_token"]))

    assert (response.status_code, response.json()) == (200, {"user": str(root.id)})


# --- refresh ---------------------------------------------------------------------------------


async def test_refreshing_exchanges_the_pair_for_a_new_one(client: httpx.AsyncClient) -> None:
    maria = await register_user(client)

    response = await refresh(client, maria.refresh_token)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert (body["token_type"], body["expires_in"]) == ("Bearer", 900)
    assert response.headers["cache-control"] == "no-store"
    assert body["refresh_token"] != maria.refresh_token
    assert body["access_token"] != maria.access_token
    assert (await me(client, body["access_token"])).json() == maria.user
    # The new refresh token is good for the next exchange.
    assert (await refresh(client, body["refresh_token"])).status_code == 200


async def test_refreshing_keeps_the_session_and_is_audited(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    maria = await register_user(client)

    response = await refresh(client, maria.refresh_token)

    session_id = session_id_of(maria.access_token, settings)
    assert session_id_of(response.json()["access_token"], settings) == session_id
    (event,) = await events(db, "auth.token_refreshed")
    assert (event.actor_id, event.resource_type, event.resource_id, event.outcome) == (
        maria.id,
        "session",
        str(session_id),
        "success",
    )


async def test_an_unknown_refresh_token_is_refused(client: httpx.AsyncClient) -> None:
    response = await refresh(client, "never-issued-by-anyone")

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == "invalid_token"
    assert "never-issued" not in response.text


async def test_an_access_token_is_not_a_refresh_token(client: httpx.AsyncClient) -> None:
    maria = await register_user(client)

    assert (await refresh(client, maria.access_token)).status_code == 401


async def test_a_reused_refresh_token_ends_the_session(client: httpx.AsyncClient) -> None:
    maria = await register_user(client)
    rotated = (await refresh(client, maria.refresh_token)).json()

    reused = await refresh(client, maria.refresh_token)
    unknown = await refresh(client, "never-issued-by-anyone")
    # The token the thief or the owner got from the first exchange is dead too.
    successor = await refresh(client, rotated["refresh_token"])

    assert (reused.status_code, successor.status_code) == (401, 401)
    # Nothing tells the presenter that the reuse was noticed.
    assert without_request_id(reused) == without_request_id(unknown)
    assert without_request_id(successor) == without_request_id(unknown)


async def test_a_detected_reuse_is_audited_though_the_request_is_refused(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    maria = await register_user(client)
    await refresh(client, maria.refresh_token)

    reused = await refresh(client, maria.refresh_token)

    (event,) = await events(db, "auth.refresh_reuse_detected")
    assert (event.outcome, event.actor_id, str(event.principal_id)) == (
        "denied",
        maria.id,
        maria.id,
    )
    assert (event.resource_type, event.resource_id) == (
        "session",
        str(session_id_of(maria.access_token, settings)),
    )
    assert event.request_id == reused.headers["x-request-id"]


async def test_a_detected_reuse_refuses_the_sessions_access_tokens_at_once(
    client: httpx.AsyncClient,
) -> None:
    maria = await register_user(client)
    bystander = await register_user(client)
    rotated = (await refresh(client, maria.refresh_token)).json()
    assert (await me(client, maria.access_token)).status_code == 200

    await refresh(client, maria.refresh_token)

    for token in (maria.access_token, rotated["access_token"]):
        response = await me(client, token)
        assert (response.status_code, response.json()["code"]) == (401, "invalid_token")
    # Another user's session is untouched.
    assert (await me(client, bystander.access_token)).status_code == 200


async def test_without_redis_a_revoked_sessions_access_token_lasts_only_until_it_expires(
    app: FastAPI, client: httpx.AsyncClient, settings: Settings, clock: ManualClock
) -> None:
    maria = await register_user(client)
    await refresh(client, maria.refresh_token)
    await refresh(client, maria.refresh_token)
    assert (await me(client, maria.access_token)).status_code == 401

    async with redis_unreachable(app, settings):
        # The hint cannot be read, so the token is taken at its word: the window the
        # design accepts. PostgreSQL still refuses the session's refresh tokens.
        while_down = await me(client, maria.access_token)
        still_revoked = await refresh(client, maria.refresh_token)
        clock.advance(seconds=15 * 60 - 1)
        last_second = await me(client, maria.access_token)
        clock.advance(seconds=1)
        expired = await me(client, maria.access_token)

    assert settings.access_token_ttl_seconds == 15 * 60
    assert (while_down.status_code, last_second.status_code) == (200, 200)
    assert (still_revoked.status_code, expired.status_code) == (401, 401)


async def test_a_reuse_detected_while_redis_is_down_still_ends_the_session(
    app: FastAPI, client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    maria = await register_user(client)
    rotated = (await refresh(client, maria.refresh_token)).json()

    async with redis_unreachable(app, settings):
        reused = await refresh(client, maria.refresh_token)

    assert reused.status_code == 401
    assert (await refresh(client, rotated["refresh_token"])).status_code == 401
    assert len(await events(db, "auth.refresh_reuse_detected")) == 1


# --- logout ----------------------------------------------------------------------------------


async def test_logging_out_ends_the_session(client: httpx.AsyncClient) -> None:
    maria = await register_user(client)
    elsewhere = await login(client, maria.email)

    response = await client.post("/v1/auth/logout", headers=maria.headers)

    assert (response.status_code, response.content) == (204, b"")
    assert (await me(client, maria.access_token)).status_code == 401
    assert (await refresh(client, maria.refresh_token)).status_code == 401
    # Only this session: the same user's other login goes on.
    assert (await me(client, elsewhere["access_token"])).status_code == 200
    assert (await refresh(client, elsewhere["refresh_token"])).status_code == 200


async def test_logging_out_is_audited(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    maria = await register_user(client)

    await client.post("/v1/auth/logout", headers=maria.headers)

    (event,) = await events(db, "auth.logged_out")
    assert (event.actor_id, str(event.principal_id), event.outcome) == (
        maria.id,
        maria.id,
        "success",
    )
    assert (event.resource_type, event.resource_id) == (
        "session",
        str(session_id_of(maria.access_token, settings)),
    )


async def test_logging_out_needs_a_credential(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/auth/logout")

    assert (response.status_code, response.json()["code"]) == (401, "unauthenticated")


async def test_the_revocation_mark_lasts_as_long_as_an_access_token(
    client: httpx.AsyncClient, redis: RedisStore, settings: Settings
) -> None:
    maria = await register_user(client)
    session_id = session_id_of(maria.access_token, settings)

    await client.post("/v1/auth/logout", headers=maria.headers)

    remaining = await redis.client.ttl(redis.key("revoked", "sid", str(session_id)))
    assert settings.access_token_ttl_seconds - 5 <= remaining <= settings.access_token_ttl_seconds


async def test_logging_out_without_redis_still_revokes_the_refresh_token(
    app: FastAPI, client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    maria = await register_user(client)

    async with redis_unreachable(app, settings):
        response = await client.post("/v1/auth/logout", headers=maria.headers)

    assert response.status_code == 204
    assert (await refresh(client, maria.refresh_token)).status_code == 401
    assert len(await events(db, "auth.logged_out")) == 1


# --- rate limit ------------------------------------------------------------------------------


async def test_logins_beyond_the_auth_limit_are_refused_with_429(
    settings: Settings, clock: ManualClock
) -> None:
    limited = settings.model_copy(update={"rate_limit_auth_per_minute": 3})
    attempt = {"email": "nobody@example.com", "password": PASSWORD}

    async with serving(limited) as http:
        served = [(await http.post("/v1/auth/login", json=attempt)).status_code for _ in range(3)]
        refused = await http.post("/v1/auth/login", json=attempt)
        # One bucket for the whole group: registering is refused as well.
        register = await http.post("/v1/auth/register", json=MARIA)
        # Outside the group, only the far larger global limit applies.
        elsewhere = await http.get("/.well-known/jwks.json")

    assert served == [401, 401, 401]
    assert (refused.status_code, refused.json()["code"]) == (429, "rate_limited")
    assert refused.headers["retry-after"] == "20"
    assert (register.status_code, elsewhere.status_code) == (429, 200)


async def test_a_login_refused_by_the_rate_limit_is_not_counted_against_the_account(
    settings: Settings, db: Database, clock: ManualClock
) -> None:
    limited = settings.model_copy(update={"rate_limit_auth_per_minute": 1})

    async with serving(limited) as http:
        await http.post("/v1/auth/register", json=MARIA)
        await http.post(
            "/v1/auth/login", json={"email": MARIA["email"], "password": WRONG_PASSWORD}
        )

    # The refused request never reached the handler: nothing was checked and nothing recorded.
    assert await events(db, "auth.login_failed") == []


def test_every_route_under_v1_auth_carries_the_auth_rate_limit(app: FastAPI) -> None:
    routes = [route for route in served_routes(app) if route.path.startswith("/v1/auth/")]

    assert sorted(route.path for route in routes) == [
        "/v1/auth/login",
        "/v1/auth/logout",
        "/v1/auth/refresh",
        "/v1/auth/register",
    ]
    assert [r.path for r in routes if auth_routes.auth_rate_limit not in r.calls] == []


# --- keys ------------------------------------------------------------------------------------


async def test_the_jwks_is_public_and_holds_no_private_parameter(
    client: httpx.AsyncClient,
) -> None:
    maria = await register_user(client)

    response = await client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    (key,) = response.json()["keys"]
    assert set(key) == {"kty", "crv", "x", "y", "kid", "use", "alg"}
    assert "d" not in key
    assert (key["kty"], key["crv"], key["alg"], key["use"]) == ("EC", "P-256", "ES256", "sig")
    assert key["kid"] == jwt.get_unverified_header(maria.access_token)["kid"]
    assert "PRIVATE" not in response.text


async def test_a_token_verifies_against_the_published_key(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    maria = await register_user(client)
    (key,) = (await client.get("/.well-known/jwks.json")).json()["keys"]

    claims = jwt.decode(
        maria.access_token,
        jwt.PyJWK(key).key,
        algorithms=["ES256"],
        audience=settings.jwt_audience,
        options={"verify_exp": False},
    )

    assert claims["sub"] == maria.id


def test_keys_generate_writes_a_key_and_prints_the_settings_that_use_it(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "keys"

    result = CliRunner().invoke(cli, ["keys", "generate", "--out", str(directory)])

    assert result.exit_code == 0, result.output
    assert "PRIVATE KEY" not in result.output
    lines = result.output.splitlines()
    assert [line.split("=", 1)[0] for line in lines] == [
        "CORRIDOR_JWT_SIGNING_KEY_FILE",
        "CORRIDOR_JWT_ADDITIONAL_PUBLIC_KEYS",
    ]
    # Each line is what a shell or an env file takes as it stands.
    for line in lines:
        name, value = line.split("=", 1)
        monkeypatch.setenv(name, shlex.split(value)[0])
    configured = Settings(
        _env_file=None,
        database_url=settings.database_url,
        redis_url=settings.redis_url,
    )
    keys = identity.load_keyset(configured)
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        [f"{keys.signing_kid}.pem", f"{keys.signing_kid}.pub.pem"]
    )
    assert configured.jwt_signing_key_file == directory / f"{keys.signing_kid}.pem"
    assert list(keys.public_keys) == [keys.signing_kid]
    # The public line alone lets another instance verify this key's tokens.
    verifier = identity.load_keyset(
        settings.model_copy(
            update={"jwt_additional_public_keys": configured.jwt_additional_public_keys}
        )
    )
    assert keys.signing_kid in verifier.public_keys

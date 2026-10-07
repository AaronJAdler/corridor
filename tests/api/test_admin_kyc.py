"""The admin endpoint that changes a user's KYC tier."""

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from corridor import identity, risk
from corridor.api.deps import get_principal
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.identity import Principal
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from tests.support.auth import PASSWORD, RegisteredUser, bearer, login, register_user


def url(user_id: object) -> str:
    return f"/v1/admin/users/{user_id}/kyc-tier"


def assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == code


@pytest.fixture
async def maria(client: httpx.AsyncClient) -> RegisteredUser:
    return await register_user(client, handle="maria")


@pytest.fixture
async def root(client: httpx.AsyncClient, db: Database, settings: Settings) -> dict[str, str]:
    """The headers of a logged-in administrator."""
    password_hash = await identity.PasswordHasher(settings).hash(PASSWORD)
    async with db.transaction() as session:
        await identity.register(
            session,
            email="root@example.com",
            handle="root",
            display_name="Root",
            password_hash=password_hash,
            role="admin",
        )
    tokens = await login(client, "root@example.com")
    return bearer(tokens["access_token"])


async def tier_of(db: Database, user_id: str) -> int:
    async with db.transaction() as session:
        found = await session.execute(
            text("SELECT kyc_tier FROM users WHERE id = :id"), {"id": user_id}
        )
        return int(found.scalar_one())


async def audited(db: Database) -> list[dict[str, Any]]:
    async with db.transaction() as session:
        found = await session.execute(
            text("SELECT * FROM audit_events WHERE action = 'user.kyc_tier_changed' ORDER BY id")
        )
        return [dict(row) for row in found.mappings()]


async def test_an_admin_changes_a_users_tier(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, root: dict[str, str]
) -> None:
    response = await client.put(url(maria.id), json={"kyc_tier": 2}, headers=root)

    assert response.status_code == 200, response.text
    assert (response.json()["id"], response.json()["kyc_tier"]) == (maria.id, 2)
    assert await tier_of(db, maria.id) == 2


async def test_the_change_is_audited_with_the_old_tier_and_the_new(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, root: dict[str, str]
) -> None:
    await client.put(url(maria.id), json={"kyc_tier": 1}, headers=root)
    await client.put(url(maria.id), json={"kyc_tier": 2}, headers=root)

    first, second = await audited(db)
    admin_id = (await client.get("/v1/me", headers=root)).json()["id"]
    assert (first["actor_type"], first["actor_id"]) == ("admin", admin_id)
    assert (str(first["principal_id"]), first["outcome"]) == (maria.id, "success")
    assert (first["resource_type"], first["resource_id"]) == ("user", maria.id)
    assert first["details"] == {"old_tier": 0, "new_tier": 1}
    assert second["details"] == {"old_tier": 1, "new_tier": 2}
    assert first["request_id"] is not None


async def test_the_users_limits_follow_the_new_tier(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, root: dict[str, str]
) -> None:
    owner = Principal.for_user(uuid.UUID(maria.id), "user", new_id())

    def asked() -> risk.MoneyMovement:
        # More than tier 0 may move at once, and within what tier 1 may.
        return risk.MoneyMovement(
            kind="transfer", user_id=owner.user_id, principal=owner, asset="USD", amount=5_000_00
        )

    with pytest.raises(risk.LimitExceeded):
        async with db.transaction() as session:
            await risk.authorize(session, asked())

    await client.put(url(maria.id), json={"kyc_tier": 1}, headers=root)

    async with db.transaction() as session:
        assert (await risk.authorize(session, asked())).outcome == "allow"


async def test_a_user_who_is_not_an_admin_is_refused_and_nothing_changes(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    other = await register_user(client)

    for target in (maria, other):
        response = await client.put(url(target.id), json={"kyc_tier": 2}, headers=maria.headers)
        assert_problem(response, 403, "permission_denied")

    assert await tier_of(db, maria.id) == await tier_of(db, other.id) == 0
    assert await audited(db) == []


async def test_a_request_with_no_credential_is_refused(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    response = await client.put(url(maria.id), json={"kyc_tier": 2})

    assert_problem(response, 401, "unauthenticated")
    assert await tier_of(db, maria.id) == 0


async def test_an_agent_of_an_admin_is_refused(
    app: FastAPI, client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=new_id(),
        actor_type="agent",
        actor_id=new_id(),
        role="admin",
        scopes=frozenset({"*"}),
        session_id=None,
    )

    response = await client.put(url(maria.id), json={"kyc_tier": 2})

    assert_problem(response, 403, "permission_denied")
    assert await tier_of(db, maria.id) == 0


async def test_a_user_who_does_not_exist_is_not_found_and_nothing_is_audited(
    client: httpx.AsyncClient, db: Database, root: dict[str, str]
) -> None:
    response = await client.put(url(new_id()), json={"kyc_tier": 1}, headers=root)

    assert_problem(response, 404, "user_not_found")
    assert await audited(db) == []


@pytest.mark.parametrize(
    "body",
    [
        {"kyc_tier": 3},
        {"kyc_tier": -1},
        {"kyc_tier": "1"},
        {"kyc_tier": True},
        {"kyc_tier": 1.0},
        {"kyc_tier": None},
        {},
        {"kyc_tier": 1, "status": "closed"},
    ],
)
async def test_a_tier_that_is_not_one_of_the_tiers_is_refused_and_nothing_changes(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    root: dict[str, str],
    body: dict[str, Any],
) -> None:
    response = await client.put(url(maria.id), json=body, headers=root)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert await tier_of(db, maria.id) == 0
    assert await audited(db) == []


async def test_an_id_that_is_not_an_id_is_refused(
    client: httpx.AsyncClient, root: dict[str, str]
) -> None:
    response = await client.put(url("maria"), json={"kyc_tier": 1}, headers=root)

    assert response.status_code == 422, response.text


async def test_setting_the_tier_a_user_already_has_is_still_audited(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, root: dict[str, str]
) -> None:
    response = await client.put(url(maria.id), json={"kyc_tier": 0}, headers=root)

    assert response.status_code == 200, response.text
    (event,) = await audited(db)
    assert event["details"] == {"old_tier": 0, "new_tier": 0}

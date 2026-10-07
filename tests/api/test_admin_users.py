"""The admin endpoints that change a user's role and close an account."""

import uuid

import httpx
import pytest

from corridor import ledger, wallets
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from tests.ops.support import admin, audited
from tests.payments.support import rows
from tests.support.auth import RegisteredUser, login, register_user
from tests.support.ledger import fund


def role_url(user_id: object) -> str:
    return f"/v1/admin/users/{user_id}/role"


def close_url(user_id: object) -> str:
    return f"/v1/admin/users/{user_id}/close"


async def stored(db: Database, user_id: str) -> tuple[str, str]:
    (row,) = await rows(db, "SELECT role, status FROM users WHERE id = :id", id=uuid.UUID(user_id))
    return str(row["role"]), str(row["status"])


async def give(db: Database, user: RegisteredUser, amount: int) -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), "USD")
        await fund(session, wallet.available_account_id, amount)


# --- who may ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["role", "close"])
async def test_changing_a_users_standing_needs_an_administrator(
    client: httpx.AsyncClient, db: Database, path: str
) -> None:
    maria, joao = await register_user(client), await register_user(client)
    url = f"/v1/admin/users/{joao.id}/{path}"
    body = {"role": "admin"} if path == "role" else None

    anonymous = await client.post(url, json=body)
    as_user = await client.post(url, json=body, headers=maria.headers)
    on_herself = await client.post(
        f"/v1/admin/users/{maria.id}/{path}", json=body, headers=maria.headers
    )

    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "unauthenticated")
    assert (as_user.status_code, as_user.json()["code"]) == (403, "permission_denied")
    assert (on_herself.status_code, on_herself.json()["code"]) == (403, "permission_denied")
    assert await stored(db, joao.id) == ("user", "active")
    assert await stored(db, maria.id) == ("user", "active")


# --- roles -----------------------------------------------------------------------------------


async def test_an_admin_makes_a_user_an_administrator_and_it_is_audited(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)

    response = await client.post(role_url(maria.id), json={"role": "admin"}, headers=root.headers)

    assert response.status_code == 200, response.text
    assert (response.json()["id"], response.json()["role"]) == (maria.id, "admin")
    assert await stored(db, maria.id) == ("admin", "active")
    (event,) = await audited(db, "user.role_changed")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert (event["resource_type"], event["resource_id"]) == ("user", maria.id)
    assert event["details"] == {"old_role": "user", "new_role": "admin"}


async def test_a_new_administrator_acts_as_one_only_with_a_new_token(
    client: httpx.AsyncClient, db: Database, settings: Settings, clock: ManualClock
) -> None:
    root = await admin(client, db, settings)
    maria, joao = await register_user(client), await register_user(client)
    await client.post(role_url(maria.id), json={"role": "admin"}, headers=root.headers)

    # The token she held says "user", and was ended with the change.
    stale = await client.post(role_url(joao.id), json={"role": "admin"}, headers=maria.headers)
    # Tokens are ended to the end of the second the change was made in.
    clock.advance(seconds=2)
    fresh = {"Authorization": f"Bearer {(await login(client, maria.email))['access_token']}"}
    promoted = await client.post(role_url(joao.id), json={"role": "admin"}, headers=fresh)

    assert stale.status_code == 401
    assert promoted.status_code == 200, promoted.text
    assert await stored(db, joao.id) == ("admin", "active")


async def test_an_admin_takes_the_role_away_from_another_administrator(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root, other = await admin(client, db, settings), await admin(client, db, settings)

    response = await client.post(role_url(other.id), json={"role": "user"}, headers=root.headers)

    assert (response.status_code, response.json()["role"]) == (200, "user")
    assert await stored(db, other.id) == ("user", "active")
    # The token the other held as an administrator no longer works.
    after = await client.post(role_url(root.id), json={"role": "user"}, headers=other.headers)
    assert after.status_code == 401
    assert await stored(db, root.id) == ("admin", "active")


@pytest.mark.parametrize("role", ["user", "admin"])
async def test_an_admin_cannot_change_their_own_role(
    client: httpx.AsyncClient, db: Database, settings: Settings, role: str
) -> None:
    root = await admin(client, db, settings)

    response = await client.post(role_url(root.id), json={"role": role}, headers=root.headers)

    assert (response.status_code, response.json()["code"]) == (409, "own_account")
    assert await stored(db, root.id) == ("admin", "active")
    assert await audited(db, "user.role_changed") == []
    # And the session that asked still works: nothing was ended.
    assert (await client.get("/v1/me", headers=root.headers)).status_code == 200


@pytest.mark.parametrize(
    "body", [{"role": "owner"}, {"role": None}, {}, {"role": "admin", "kyc_tier": 2}]
)
async def test_a_role_that_is_not_one_is_refused(
    client: httpx.AsyncClient,
    db: Database,
    settings: Settings,
    body: dict[str, object],
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)

    response = await client.post(role_url(maria.id), json=body, headers=root.headers)

    assert response.status_code == 422, response.text
    assert await stored(db, maria.id) == ("user", "active")


@pytest.mark.parametrize("url", [role_url, close_url])
async def test_a_user_who_does_not_exist_is_not_found(
    client: httpx.AsyncClient, db: Database, settings: Settings, url: object
) -> None:
    root = await admin(client, db, settings)
    assert callable(url)

    response = await client.post(url(uuid.uuid4()), json={"role": "admin"}, headers=root.headers)

    assert (response.status_code, response.json()["code"]) == (404, "user_not_found")


# --- closing ---------------------------------------------------------------------------------


async def test_an_admin_closes_an_empty_account_and_it_is_audited(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)

    response = await client.post(close_url(maria.id), headers=root.headers)

    assert response.status_code == 200, response.text
    assert (response.json()["id"], response.json()["status"]) == (maria.id, "closed")
    assert await stored(db, maria.id) == ("user", "closed")
    (event,) = await audited(db, "user.closed")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert (event["resource_type"], event["resource_id"]) == ("user", maria.id)
    assert event["details"] == {"old_status": "active"}
    # Nothing she holds works any more.
    assert (await client.get("/v1/me", headers=maria.headers)).status_code == 401


async def test_an_account_with_money_in_it_is_not_closed(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)
    await give(db, maria, 1)

    response = await client.post(close_url(maria.id), headers=root.headers)

    assert (response.status_code, response.json()["code"]) == (409, "account_holds_funds")
    assert await stored(db, maria.id) == ("user", "active")
    assert await audited(db, "user.closed") == []
    assert (await client.get("/v1/me", headers=maria.headers)).status_code == 200


async def test_an_account_with_funds_on_hold_and_nothing_available_is_not_closed(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)
    await give(db, maria, 40_00)
    async with db.transaction() as session:
        # All of it reserved, as a withdrawal that is on its way reserves it.
        wallet = await wallets.get_wallet(session, uuid.UUID(maria.id), "USD")
        await ledger.post_entry(
            session,
            ledger.EntryDraft(
                "withdrawal_hold",
                "test_withdrawal",
                "wd_1",
                (
                    ledger.debit(wallet.available_account_id, 40_00),
                    ledger.credit(wallet.held_account_id, 40_00),
                ),
            ),
        )

    response = await client.post(close_url(maria.id), headers=root.headers)

    assert (response.status_code, response.json()["code"]) == (409, "account_holds_funds")
    assert await stored(db, maria.id) == ("user", "active")


async def test_an_admin_cannot_close_their_own_account(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)

    response = await client.post(close_url(root.id), headers=root.headers)

    assert (response.status_code, response.json()["code"]) == (409, "own_account")
    assert await stored(db, root.id) == ("admin", "active")
    assert await audited(db, "user.closed") == []


async def test_closing_a_closed_account_changes_nothing(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)
    first = await client.post(close_url(maria.id), headers=root.headers)

    again = await client.post(close_url(maria.id), headers=root.headers)

    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json()["status"] == "closed"
    assert await stored(db, maria.id) == ("user", "closed")

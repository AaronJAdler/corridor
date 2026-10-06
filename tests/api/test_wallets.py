"""Wallets over HTTP: balances and statements, for the user the credential acts for only."""

import uuid

import httpx
import pytest
from fastapi import FastAPI

from corridor import ledger, wallets
from corridor.api.deps import get_principal
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.identity import Principal, Scope
from corridor.ledger import EntryDraft, credit, debit
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.money import ASSETS
from tests.support.auth import RegisteredUser, register_user
from tests.support.ledger import fund


async def deposit(db: Database, user: RegisteredUser, amount: int, asset: str = "USD") -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), asset)
        await fund(session, wallet.available_account_id, amount, asset)


async def hold(db: Database, user: RegisteredUser, amount: int, asset: str = "USD") -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), asset)
        await ledger.post_entry(
            session,
            EntryDraft(
                kind="withdrawal_hold",
                source_type="test_withdrawal",
                source_id=str(new_id()),
                postings=(
                    debit(wallet.available_account_id, amount),
                    credit(wallet.held_account_id, amount),
                ),
            ),
        )


async def balances(client: httpx.AsyncClient, user: RegisteredUser) -> dict[str, dict[str, str]]:
    response = await client.get("/v1/wallets", headers=user.headers)
    assert response.status_code == 200, response.text
    return {wallet["asset"]: wallet for wallet in response.json()["wallets"]}


def assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == code


async def test_a_new_user_sees_an_empty_wallet_for_every_asset(client: httpx.AsyncClient) -> None:
    user = await register_user(client)

    response = await client.get("/v1/wallets", headers=user.headers)

    assert response.status_code == 200
    assert response.json() == {
        "wallets": [
            {"asset": "BRL", "available": "0.00", "held": "0.00", "total": "0.00"},
            {"asset": "MXN", "available": "0.00", "held": "0.00", "total": "0.00"},
            {"asset": "USD", "available": "0.00", "held": "0.00", "total": "0.00"},
            {"asset": "USDC", "available": "0.000000", "held": "0.000000", "total": "0.000000"},
        ]
    }
    assert {wallet["asset"] for wallet in response.json()["wallets"]} == set(ASSETS)


async def test_balances_are_decimal_strings_with_the_assets_decimal_places(
    client: httpx.AsyncClient, db: Database
) -> None:
    user = await register_user(client)
    await deposit(db, user, 1_234_50)
    await deposit(db, user, 2_500_001, "USDC")
    await hold(db, user, 34_05)

    found = await balances(client, user)

    assert found["USD"] == {
        "asset": "USD",
        "available": "1200.45",
        "held": "34.05",
        "total": "1234.50",
    }
    assert found["USDC"] == {
        "asset": "USDC",
        "available": "2.500001",
        "held": "0.000000",
        "total": "2.500001",
    }


async def test_an_amount_too_large_for_a_json_number_is_carried_exactly(
    client: httpx.AsyncClient, db: Database
) -> None:
    user = await register_user(client)
    await deposit(db, user, 2**70 + 1)

    assert (await balances(client, user))["USD"]["available"] == "11805916207174113034.25"


async def test_another_users_money_is_never_in_my_wallet(
    client: httpx.AsyncClient, db: Database
) -> None:
    me = await register_user(client)
    them = await register_user(client)
    await deposit(db, them, 500_00)

    assert (await balances(client, me))["USD"]["total"] == "0.00"
    assert (await balances(client, them))["USD"]["total"] == "500.00"


async def test_another_users_entries_are_never_in_my_statement(
    client: httpx.AsyncClient, db: Database
) -> None:
    me = await register_user(client)
    them = await register_user(client)
    await deposit(db, them, 500_00)

    response = await client.get("/v1/wallets/USD/entries", headers=me.headers)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("path", ["/v1/wallets", "/v1/wallets/USD/entries"])
async def test_a_request_without_a_token_is_refused(client: httpx.AsyncClient, path: str) -> None:
    assert_problem(await client.get(path), 401, "unauthenticated")


@pytest.mark.parametrize("path", ["/v1/wallets", "/v1/wallets/USD/entries"])
async def test_a_credential_without_the_wallet_read_scope_is_refused(
    app: FastAPI, client: httpx.AsyncClient, path: str
) -> None:
    user = await register_user(client)
    narrow = Principal(
        user_id=uuid.UUID(user.id),
        actor_type="agent",
        actor_id=new_id(),
        role="user",
        scopes=frozenset({Scope.TRANSFERS_READ}),
        session_id=None,
    )
    app.dependency_overrides[get_principal] = lambda: narrow

    assert_problem(await client.get(path), 403, "insufficient_scope")


@pytest.mark.parametrize("path", ["/v1/wallets", "/v1/wallets/USD/entries"])
async def test_a_credential_with_only_the_wallet_read_scope_reads_its_owners_wallet(
    app: FastAPI, client: httpx.AsyncClient, db: Database, path: str
) -> None:
    user = await register_user(client)
    await deposit(db, user, 5_00)
    agent = Principal(
        user_id=uuid.UUID(user.id),
        actor_type="agent",
        actor_id=new_id(),
        role="user",
        scopes=frozenset({Scope.WALLET_READ}),
        session_id=None,
    )
    app.dependency_overrides[get_principal] = lambda: agent

    response = await client.get(path)

    assert response.status_code == 200
    assert "5.00" in response.text


async def test_a_statement_entry_says_what_happened_in_decimal_strings(
    client: httpx.AsyncClient, db: Database
) -> None:
    user = await register_user(client)
    await deposit(db, user, 10_00)
    await hold(db, user, 2_50)

    response = await client.get("/v1/wallets/USD/entries", headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert [
        (e["kind"], e["direction"], e["amount"], e["balance_after"], e["asset"])
        for e in body["items"]
    ] == [
        ("withdrawal_hold", "debit", "2.50", "7.50", "USD"),
        ("deposit", "credit", "10.00", "10.00", "USD"),
    ]
    for entry in body["items"]:
        assert set(entry) == {
            "id",
            "kind",
            "direction",
            "asset",
            "amount",
            "balance_after",
            "posted_at",
        }
        uuid.UUID(entry["id"])


async def test_a_statement_pages_over_http_without_repeating_or_skipping(
    client: httpx.AsyncClient, db: Database
) -> None:
    user = await register_user(client)
    for cents in range(1, 6):
        await deposit(db, user, cents)

    amounts: list[str] = []
    params: dict[str, str | int] = {"limit": 2}
    for _ in range(3):
        response = await client.get("/v1/wallets/USD/entries", headers=user.headers, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        amounts += [entry["amount"] for entry in body["items"]]
        if body["next_cursor"] is None:
            break
        params = {"limit": 2, "cursor": body["next_cursor"]}
        await deposit(db, user, 99_00)

    assert amounts == ["0.05", "0.04", "0.03", "0.02", "0.01"]
    assert body["next_cursor"] is None


@pytest.mark.parametrize("cursor", ["garbage", "", "e30", "!!!"])
async def test_a_bad_cursor_is_a_422(client: httpx.AsyncClient, cursor: str) -> None:
    user = await register_user(client)

    response = await client.get(
        "/v1/wallets/USD/entries", headers=user.headers, params={"cursor": cursor}
    )

    assert_problem(response, 422, "invalid_cursor")


async def test_a_cursor_from_another_users_statement_is_a_422(
    client: httpx.AsyncClient, db: Database
) -> None:
    me = await register_user(client)
    them = await register_user(client)
    for user in (me, them):
        await deposit(db, user, 1_00)
        await deposit(db, user, 2_00)
    theirs = (
        await client.get("/v1/wallets/USD/entries", headers=them.headers, params={"limit": 1})
    ).json()["next_cursor"]
    assert theirs is not None

    response = await client.get(
        "/v1/wallets/USD/entries", headers=me.headers, params={"cursor": theirs}
    )

    assert_problem(response, 422, "invalid_cursor")


async def test_a_cursor_from_another_asset_is_a_422(
    client: httpx.AsyncClient, db: Database
) -> None:
    user = await register_user(client)
    await deposit(db, user, 1_00)
    await deposit(db, user, 2_00)
    usd = (
        await client.get("/v1/wallets/USD/entries", headers=user.headers, params={"limit": 1})
    ).json()["next_cursor"]

    response = await client.get(
        "/v1/wallets/MXN/entries", headers=user.headers, params={"cursor": usd}
    )

    assert_problem(response, 422, "invalid_cursor")


@pytest.mark.parametrize("limit", ["0", "-3", "many", "1.5"])
async def test_a_limit_that_is_not_a_positive_whole_number_is_a_422(
    client: httpx.AsyncClient, limit: str
) -> None:
    user = await register_user(client)

    response = await client.get(
        "/v1/wallets/USD/entries", headers=user.headers, params={"limit": limit}
    )

    assert_problem(response, 422, "invalid_request")


async def test_a_limit_above_the_cap_is_served_at_the_cap(
    client: httpx.AsyncClient, db: Database
) -> None:
    user = await register_user(client)
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), "USD")
        for _ in range(201):
            await fund(session, wallet.available_account_id, 1_00)

    response = await client.get(
        "/v1/wallets/USD/entries", headers=user.headers, params={"limit": 5000}
    )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 200
    assert response.json()["next_cursor"] is not None


async def test_the_statement_of_an_unsupported_asset_is_a_422(client: httpx.AsyncClient) -> None:
    user = await register_user(client)

    response = await client.get("/v1/wallets/EUR/entries", headers=user.headers)

    assert_problem(response, 422, "unknown_asset")


async def test_registration_and_its_wallets_are_one_transaction(
    client: httpx.AsyncClient, db: Database
) -> None:
    user = await register_user(client)

    async with db.transaction() as session:
        found = await wallets.get_wallets(session, uuid.UUID(user.id))
        accounts = await ledger.list_accounts(session, owner_id=uuid.UUID(user.id))

    assert [wallet.asset for wallet in found] == sorted(ASSETS)
    assert len(accounts) == 2 * len(ASSETS)

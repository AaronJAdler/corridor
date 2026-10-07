"""Deposit instructions and deposits over HTTP."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from corridor import payments
from corridor.api.deps import get_principal
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.identity import Principal, Scope
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import SimBank, SimCustody, is_valid_address
from tests.support.auth import RegisteredUser, register_user
from tests.support.providers import (  # noqa: F401
    Sim,
    bank,
    custody,
    provider_settings,
    sim,
    wire,
)

INSTRUCTIONS = "/v1/deposit-instructions"
DEPOSITS = "/v1/deposits"


def assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == code


@pytest.fixture
async def api(
    app: FastAPI,
    client: httpx.AsyncClient,
    bank: SimBank,  # noqa: F811
    custody: SimCustody,  # noqa: F811
) -> httpx.AsyncClient:
    """The API, with its provider clients pointed at the simulator."""
    wire(app, bank, custody)
    return client


@pytest.fixture
async def maria(api: httpx.AsyncClient) -> RegisteredUser:
    return await register_user(api, handle="maria")


@pytest.fixture
async def joao(api: httpx.AsyncClient) -> RegisteredUser:
    return await register_user(api, handle="joao")


@pytest.fixture
async def as_agent(app: FastAPI) -> AsyncIterator[list[Principal]]:
    """Requests are made by whichever principal the test puts in the list."""
    acting: list[Principal] = []
    app.dependency_overrides[get_principal] = lambda: acting[0]
    yield acting
    app.dependency_overrides.pop(get_principal)


def agent_for(user: RegisteredUser, *scopes: str) -> Principal:
    return Principal(
        user_id=uuid.UUID(user.id),
        actor_type="agent",
        actor_id=new_id(),
        role="user",
        scopes=frozenset(scopes),
        session_id=None,
    )


async def test_bank_instructions_show_the_account_to_pay_into(
    api: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    response = await api.get(INSTRUCTIONS, params={"asset": "USD"}, headers=maria.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"asset", "kind", "details"}
    assert (body["asset"], body["kind"]) == ("USD", "bank")
    assert set(body["details"]) == {"rail", "bank_name", "account_number", "routing_number"}


async def test_stablecoin_instructions_show_an_address(
    api: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    response = await api.get(INSTRUCTIONS, params={"asset": "USDC"}, headers=maria.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["asset"], body["kind"]) == ("USDC", "chain")
    assert body["details"]["network"] == "simchain"
    assert is_valid_address(body["details"]["address"])


async def test_instructions_are_the_same_every_time_and_differ_between_users(
    api: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    first = await api.get(INSTRUCTIONS, params={"asset": "USD"}, headers=maria.headers)
    again = await api.get(INSTRUCTIONS, params={"asset": "USD"}, headers=maria.headers)
    other = await api.get(INSTRUCTIONS, params={"asset": "USD"}, headers=joao.headers)

    assert first.json() == again.json()
    assert other.json()["details"]["account_number"] != first.json()["details"]["account_number"]


async def test_instructions_need_a_credential_and_an_asset(
    api: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    assert_problem(await api.get(INSTRUCTIONS, params={"asset": "USD"}), 401, "unauthenticated")
    assert_problem(await api.get(INSTRUCTIONS, headers=maria.headers), 422, "invalid_request")
    assert_problem(
        await api.get(INSTRUCTIONS, params={"asset": "EUR"}, headers=maria.headers),
        422,
        "unknown_asset",
    )


async def test_instructions_need_the_deposits_scope(
    api: httpx.AsyncClient, maria: RegisteredUser, as_agent: list[Principal]
) -> None:
    as_agent.append(agent_for(maria, Scope.WALLET_READ))
    assert_problem(await api.get(INSTRUCTIONS, params={"asset": "USD"}), 403, "insufficient_scope")

    as_agent[0] = agent_for(maria, Scope.DEPOSITS_READ)
    assert (await api.get(INSTRUCTIONS, params={"asset": "USD"})).status_code == 200


async def test_a_provider_that_is_down_is_a_503_and_a_retry_succeeds(
    api: httpx.AsyncClient,
    sim: Sim,  # noqa: F811
    maria: RegisteredUser,
) -> None:
    await sim.inject("bank.create_virtual_account", "error")

    refused = await api.get(INSTRUCTIONS, params={"asset": "USD"}, headers=maria.headers)

    assert_problem(refused, 503, "provider_unavailable")
    retried = await api.get(INSTRUCTIONS, params={"asset": "USD"}, headers=maria.headers)
    assert retried.status_code == 200


async def test_an_api_without_providers_configured_answers_503(
    client: httpx.AsyncClient, db: Database
) -> None:
    user = await register_user(client)

    response = await client.get(INSTRUCTIONS, params={"asset": "USD"}, headers=user.headers)

    assert_problem(response, 503, "provider_unavailable")


async def deposit(
    api: httpx.AsyncClient,
    db: Database,
    sim: Sim,  # noqa: F811
    user: RegisteredUser,
    amount: str = "250.00",
) -> str:
    """A bank deposit to the user's own account, credited. Returns its id as the API shows it."""
    instructions = await api.get(INSTRUCTIONS, params={"asset": "USD"}, headers=user.headers)
    account = sim.app.state.sim.bank._virtual_account_of[user.id, "USD"]
    assert instructions.status_code == 200, instructions.text
    await payments.apply_bank_deposit_received(db, await sim.bank_deposit(account, amount))
    listed = await api.get(DEPOSITS, params={"limit": 1}, headers=user.headers)
    return str(listed.json()["items"][0]["id"])


async def test_a_credited_deposit_is_listed_and_is_in_the_wallet(
    api: httpx.AsyncClient,
    db: Database,
    sim: Sim,  # noqa: F811
    maria: RegisteredUser,
) -> None:
    deposit_id = await deposit(api, db, sim, maria)

    response = await api.get(DEPOSITS, headers=maria.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    (item,) = body["items"]
    assert set(item) == {
        "id",
        "asset",
        "amount",
        "kind",
        "status",
        "tx_hash",
        "created_at",
        "updated_at",
    }
    assert item["id"] == deposit_id
    assert (item["asset"], item["amount"], item["kind"]) == ("USD", "250.00", "bank")
    assert (item["status"], item["tx_hash"]) == ("completed", None)
    wallets = (await api.get("/v1/wallets", headers=maria.headers)).json()["wallets"]
    assert next(w["available"] for w in wallets if w["asset"] == "USD") == "250.00"


async def test_a_pending_chain_deposit_is_shown_with_its_hash_and_is_not_in_the_wallet(
    api: httpx.AsyncClient,
    db: Database,
    sim: Sim,  # noqa: F811
    maria: RegisteredUser,
) -> None:
    instructions = await api.get(INSTRUCTIONS, params={"asset": "USDC"}, headers=maria.headers)
    seen = await sim.chain_deposit(instructions.json()["details"]["address"], "1.5")
    await payments.apply_chain_deposit_detected(db, seen)

    (item,) = (await api.get(DEPOSITS, headers=maria.headers)).json()["items"]

    assert (item["asset"], item["amount"], item["kind"]) == ("USDC", "1.500000", "chain")
    assert (item["status"], item["tx_hash"]) == ("pending", seen["tx_hash"])
    wallets = (await api.get("/v1/wallets", headers=maria.headers)).json()["wallets"]
    assert next(w["available"] for w in wallets if w["asset"] == "USDC") == "0.000000"


async def test_one_deposit_is_read_by_its_owner_and_is_a_404_for_anyone_else(
    api: httpx.AsyncClient,
    db: Database,
    sim: Sim,  # noqa: F811
    maria: RegisteredUser,
    joao: RegisteredUser,
) -> None:
    deposit_id = await deposit(api, db, sim, maria)

    own = await api.get(f"{DEPOSITS}/{deposit_id}", headers=maria.headers)
    other = await api.get(f"{DEPOSITS}/{deposit_id}", headers=joao.headers)
    missing = await api.get(f"{DEPOSITS}/{new_id()}", headers=maria.headers)

    assert own.status_code == 200, own.text
    assert (own.json()["id"], own.json()["amount"]) == (deposit_id, "250.00")
    assert_problem(other, 404, "deposit_not_found")
    assert_problem(missing, 404, "deposit_not_found")
    assert other.json()["detail"] == missing.json()["detail"]
    assert (await api.get(DEPOSITS, headers=joao.headers)).json()["items"] == []


async def test_deposits_are_paged_newest_first(
    api: httpx.AsyncClient,
    db: Database,
    sim: Sim,  # noqa: F811
    maria: RegisteredUser,
) -> None:
    for amount in ("1.00", "2.00", "3.00"):
        await deposit(api, db, sim, maria, amount)

    first = (await api.get(DEPOSITS, params={"limit": 2}, headers=maria.headers)).json()
    second = (
        await api.get(
            DEPOSITS, params={"limit": 2, "cursor": first["next_cursor"]}, headers=maria.headers
        )
    ).json()

    assert [item["amount"] for item in first["items"]] == ["3.00", "2.00"]
    assert [item["amount"] for item in second["items"]] == ["1.00"]
    assert second["next_cursor"] is None
    assert_problem(
        await api.get(DEPOSITS, params={"cursor": "nonsense"}, headers=maria.headers),
        422,
        "invalid_cursor",
    )


async def test_reading_deposits_needs_a_credential_with_the_deposits_scope(
    api: httpx.AsyncClient, maria: RegisteredUser, as_agent: list[Principal]
) -> None:
    as_agent.append(agent_for(maria, Scope.WALLET_READ))
    assert_problem(await api.get(DEPOSITS), 403, "insufficient_scope")
    assert_problem(await api.get(f"{DEPOSITS}/{new_id()}"), 403, "insufficient_scope")

    as_agent[0] = agent_for(maria, Scope.DEPOSITS_READ)
    assert (await api.get(DEPOSITS)).status_code == 200

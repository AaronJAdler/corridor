"""Withdrawals over HTTP: a request reserves the funds and is answered 202."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from corridor import payments, wallets
from corridor.api.app import create_app
from corridor.api.deps import get_principal
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.identity import Principal, Scope
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import ProviderMisconfigured, SimBank, SimCustody
from tests.support.auth import RegisteredUser, register_user
from tests.support.ledger import fund
from tests.support.providers import (  # noqa: F401
    ACCOUNT_NUMBER,
    EXTERNAL_ADDRESS,
    ROUTING_NUMBER,
    Sim,
    bank,
    custody,
    provider_settings,
    sim,
    wire,
    with_providers,
)

URL = "/v1/withdrawals"


def assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == code


async def deposit(db: Database, user: RegisteredUser, amount: int, asset: str = "USD") -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), asset)
        await fund(session, wallet.available_account_id, amount, asset)


async def balances(
    api: httpx.AsyncClient, user: RegisteredUser, asset: str = "USD"
) -> tuple[str, str]:
    response = await api.get("/v1/wallets", headers=user.headers)
    wallet = next(w for w in response.json()["wallets"] if w["asset"] == asset)
    return wallet["available"], wallet["held"]


async def count(db: Database, table: str) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


async def beneficiary_of(api: httpx.AsyncClient, user: RegisteredUser) -> str:
    response = await api.post(
        "/v1/beneficiaries",
        json={
            "asset": "USD",
            "holder_name": "Maria Silva",
            "account_number": ACCOUNT_NUMBER,
            "routing_number": ROUTING_NUMBER,
        },
        headers={**user.headers, "Idempotency-Key": f"ben-{new_id()}"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def post(
    api: httpx.AsyncClient,
    user: RegisteredUser,
    body: dict[str, Any],
    *,
    key: str | None = "key-1",
) -> httpx.Response:
    headers = dict(user.headers)
    if key is not None:
        headers["Idempotency-Key"] = key
    return await api.post(URL, json=body, headers=headers)


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
async def maria(api: httpx.AsyncClient, db: Database) -> RegisteredUser:
    """A user with 500.00 USD."""
    user = await register_user(api, handle="maria")
    await deposit(db, user, 500_00)
    return user


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


async def test_a_bank_withdrawal_is_accepted_and_its_funds_are_held(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    beneficiary = await beneficiary_of(api, maria)

    response = await post(
        api, maria, {"asset": "USD", "amount": "100.00", "beneficiary_id": beneficiary}
    )

    assert response.status_code == 202, response.text
    assert "idempotent-replayed" not in response.headers
    body = response.json()
    assert set(body) == {
        "id",
        "status",
        "asset",
        "amount",
        "fee",
        "kind",
        "beneficiary_id",
        "to_address",
        "failure_reason",
        "created_at",
        "updated_at",
    }
    uuid.UUID(body["id"])
    assert (body["status"], body["kind"]) == ("held", "bank")
    assert (body["asset"], body["amount"], body["fee"]) == ("USD", "100.00", "0.00")
    assert (body["beneficiary_id"], body["to_address"]) == (beneficiary, None)
    assert await balances(api, maria) == ("400.00", "100.00")
    assert await count(db, "outbox_events") == 1


async def test_an_on_chain_withdrawal_is_accepted_for_a_valid_address(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    await deposit(db, maria, 50_000_000, "USDC")

    response = await post(
        api, maria, {"asset": "USDC", "amount": "25", "to_address": EXTERNAL_ADDRESS}
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert (body["kind"], body["amount"], body["to_address"]) == (
        "chain",
        "25.000000",
        EXTERNAL_ADDRESS,
    )
    assert await balances(api, maria, "USDC") == ("25.000000", "25.000000")


async def test_the_fee_is_shown_and_held_with_the_amount(
    settings: Settings,
    db: Database,
    bank: SimBank,  # noqa: F811
    custody: SimCustody,  # noqa: F811
) -> None:
    charging = create_app(settings.model_copy(update={"withdrawal_fee_bps": 150}))
    async with charging.router.lifespan_context(charging):
        wire(charging, bank, custody)
        transport = httpx.ASGITransport(app=charging, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://fee.test") as http:
            user = await register_user(http)
            await deposit(db, user, 500_00)
            beneficiary = await beneficiary_of(http, user)

            response = await post(
                http, user, {"asset": "USD", "amount": "100.00", "beneficiary_id": beneficiary}
            )

            assert response.status_code == 202, response.text
            assert (response.json()["amount"], response.json()["fee"]) == ("100.00", "1.50")
            assert await balances(http, user) == ("398.50", "101.50")


async def test_a_retry_with_the_same_key_replays_the_answer_and_holds_once(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    beneficiary = await beneficiary_of(api, maria)
    body = {"asset": "USD", "amount": "100.00", "beneficiary_id": beneficiary}

    first = await post(api, maria, body)
    again = await post(api, maria, body)
    several = await asyncio.gather(*(post(api, maria, body) for _ in range(10)))

    assert (first.status_code, again.status_code) == (202, 202)
    assert again.json() == first.json()
    assert again.headers["idempotent-replayed"] == "true"
    assert {response.json()["id"] for response in several} == {first.json()["id"]}
    assert await balances(api, maria) == ("400.00", "100.00")
    assert await count(db, "withdrawals") == 1
    assert await count(db, "outbox_events") == 1


async def test_a_key_is_required_and_a_key_reused_for_another_request_is_refused(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    beneficiary = await beneficiary_of(api, maria)
    body = {"asset": "USD", "amount": "100.00", "beneficiary_id": beneficiary}

    assert_problem(await post(api, maria, body, key=None), 400, "idempotency_key_required")
    await post(api, maria, body)
    assert_problem(
        await post(api, maria, {**body, "amount": "1.00"}), 422, "idempotency_key_reused"
    )

    assert await balances(api, maria) == ("400.00", "100.00")


async def test_requests_that_cannot_be_performed_hold_nothing(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    hers = await beneficiary_of(api, maria)
    his = await beneficiary_of(api, joao)
    await deposit(db, maria, 50_000_000, "USDC")
    refusals = [
        ({"asset": "USD", "amount": "500.01", "beneficiary_id": hers}, 402, "insufficient_funds"),
        ({"asset": "USD", "amount": "1.00", "beneficiary_id": his}, 404, "beneficiary_not_found"),
        (
            {"asset": "USD", "amount": "1.00", "beneficiary_id": str(new_id())},
            404,
            "beneficiary_not_found",
        ),
        (
            {"asset": "MXN", "amount": "1.00", "beneficiary_id": hers},
            422,
            "beneficiary_asset_mismatch",
        ),
        ({"asset": "USDC", "amount": "1", "to_address": "sim1nonsense"}, 422, "invalid_address"),
        (
            {"asset": "USDC", "amount": "1", "beneficiary_id": hers},
            422,
            "invalid_withdrawal_target",
        ),
        (
            {"asset": "USD", "amount": "1.00", "to_address": EXTERNAL_ADDRESS},
            422,
            "invalid_withdrawal_target",
        ),
        ({"asset": "USD", "amount": "1.00"}, 422, "invalid_withdrawal_target"),
        ({"asset": "USD", "amount": "1.005", "beneficiary_id": hers}, 422, "invalid_amount"),
        ({"asset": "USD", "amount": "0.00", "beneficiary_id": hers}, 422, "invalid_amount"),
        ({"asset": "USD", "amount": 1, "beneficiary_id": hers}, 422, "invalid_request"),
        ({"asset": "EUR", "amount": "1.00", "beneficiary_id": hers}, 422, "unknown_asset"),
        ({"asset": "USD", "amount": "1.00", "beneficiary_id": "nonsense"}, 422, "invalid_request"),
        (
            {"asset": "USD", "amount": "1.00", "beneficiary_id": hers, "memo": "x"},
            422,
            "invalid_request",
        ),
    ]

    for number, (body, status, code) in enumerate(refusals):
        assert_problem(await post(api, maria, body, key=f"key-{number}"), status, code)

    assert await balances(api, maria) == ("500.00", "0.00")
    assert await balances(api, maria, "USDC") == ("50.000000", "0.000000")
    assert await count(db, "withdrawals") == 0
    assert await count(db, "outbox_events") == 0


async def test_a_withdrawal_is_canceled_once_and_then_it_is_a_conflict(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    beneficiary = await beneficiary_of(api, maria)
    made = await post(
        api, maria, {"asset": "USD", "amount": "100.00", "beneficiary_id": beneficiary}
    )
    cancel_url = f"{URL}/{made.json()['id']}/cancel"

    canceled = await api.post(cancel_url, headers=maria.headers)
    again = await api.post(cancel_url, headers=maria.headers)

    assert canceled.status_code == 200, canceled.text
    assert (canceled.json()["id"], canceled.json()["status"]) == (made.json()["id"], "canceled")
    assert_problem(again, 409, "withdrawal_not_cancelable")
    assert await balances(api, maria) == ("500.00", "0.00")


async def test_a_withdrawal_is_read_listed_and_canceled_by_its_owner_only(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    beneficiary = await beneficiary_of(api, maria)
    made = [
        (
            await post(
                api,
                maria,
                {"asset": "USD", "amount": amount, "beneficiary_id": beneficiary},
                key=f"key-{amount}",
            )
        ).json()
        for amount in ("1.00", "2.00", "3.00")
    ]
    one = f"{URL}/{made[0]['id']}"

    own = await api.get(one, headers=maria.headers)
    first = (await api.get(URL, params={"limit": 2}, headers=maria.headers)).json()
    rest = (
        await api.get(
            URL, params={"limit": 2, "cursor": first["next_cursor"]}, headers=maria.headers
        )
    ).json()

    assert own.json() == made[0]
    assert [item["amount"] for item in first["items"]] == ["3.00", "2.00"]
    assert [item["amount"] for item in rest["items"]] == ["1.00"]
    assert rest["next_cursor"] is None
    assert_problem(await api.get(one, headers=joao.headers), 404, "withdrawal_not_found")
    assert_problem(
        await api.post(f"{one}/cancel", headers=joao.headers), 404, "withdrawal_not_found"
    )
    assert_problem(
        await api.post(f"{URL}/{new_id()}/cancel", headers=maria.headers),
        404,
        "withdrawal_not_found",
    )
    assert (await api.get(URL, headers=joao.headers)).json()["items"] == []
    assert await balances(api, maria) == ("494.00", "6.00")


async def test_withdrawals_need_a_credential_with_their_scopes(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser, as_agent: list[Principal]
) -> None:
    body = {"asset": "USDC", "amount": "1", "to_address": EXTERNAL_ADDRESS}
    await deposit(db, maria, 5_000_000, "USDC")

    as_agent.append(agent_for(maria, Scope.WITHDRAWALS_READ))
    assert_problem(
        await api.post(URL, json=body, headers={"Idempotency-Key": "k"}), 403, "insufficient_scope"
    )
    assert_problem(await api.post(f"{URL}/{new_id()}/cancel"), 403, "insufficient_scope")
    assert (await api.get(URL)).status_code == 200

    as_agent[0] = agent_for(maria, Scope.WITHDRAWALS_CREATE)
    assert_problem(await api.get(URL), 403, "insufficient_scope")
    assert_problem(await api.get(f"{URL}/{new_id()}"), 403, "insufficient_scope")
    made = await api.post(URL, json=body, headers={"Idempotency-Key": "k"})
    assert made.status_code == 202, made.text
    assert (await api.post(f"{URL}/{made.json()['id']}/cancel")).status_code == 200


async def test_without_a_credential_a_withdrawal_is_refused(api: httpx.AsyncClient) -> None:
    body = {"asset": "USDC", "amount": "1", "to_address": EXTERNAL_ADDRESS}

    assert_problem(
        await api.post(URL, json=body, headers={"Idempotency-Key": "k"}), 401, "unauthenticated"
    )
    assert_problem(await api.get(URL), 401, "unauthenticated")
    assert_problem(await api.post(f"{URL}/{new_id()}/cancel"), 401, "unauthenticated")


async def test_a_withdrawal_is_seen_through_to_completion(
    api: httpx.AsyncClient,
    db: Database,
    sim: Sim,  # noqa: F811
    bank: SimBank,  # noqa: F811
    custody: SimCustody,  # noqa: F811
    maria: RegisteredUser,
) -> None:
    beneficiary = await beneficiary_of(api, maria)
    made = await post(
        api, maria, {"asset": "USD", "amount": "100.00", "beneficiary_id": beneficiary}
    )
    one = f"{URL}/{made.json()['id']}"

    await payments.submit_withdrawal(db, bank, custody, uuid.UUID(made.json()["id"]))
    assert (await api.get(one, headers=maria.headers)).json()["status"] == "submitted"
    assert_problem(
        await api.post(f"{one}/cancel", headers=maria.headers), 409, "withdrawal_not_cancelable"
    )

    await sim.advance(30)
    await payments.apply_payout_completed(db, await sim.last_event("payout.completed"))

    done = (await api.get(one, headers=maria.headers)).json()
    assert (done["status"], done["amount"], done["failure_reason"]) == ("completed", "100.00", None)
    assert await balances(api, maria) == ("400.00", "0.00")


async def test_the_api_builds_its_provider_clients_from_its_settings(
    settings: Settings,
    app: FastAPI,
) -> None:
    assert (app.state.container.bank, app.state.container.custody) == (None, None)

    configured = create_app(with_providers(settings))
    async with configured.router.lifespan_context(configured):
        container = configured.state.container
        assert isinstance(container.bank, SimBank)
        assert isinstance(container.custody, SimCustody)
        assert (container.bank.name, container.custody.name) == ("simbank", "simcustody")


async def test_an_api_given_a_provider_address_and_no_key_does_not_start(
    settings: Settings,
) -> None:
    half = create_app(settings.model_copy(update={"bank_rail_url": "http://sim.test"}))

    with pytest.raises(ProviderMisconfigured):
        async with half.router.lifespan_context(half):
            pass

"""Beneficiaries over HTTP, and the one thing about them that must never be seen again."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from corridor.api.deps import get_principal
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.identity import Principal, Scope
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.logging import configure_logging
from corridor.providers import SimBank, SimCustody
from tests.support.auth import RegisteredUser, register_user
from tests.support.providers import (  # noqa: F401
    ACCOUNT_NUMBER,
    ROUTING_NUMBER,
    Sim,
    bank,
    custody,
    provider_settings,
    sim,
    wire,
)

URL = "/v1/beneficiaries"


def assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == code


def account(**changes: Any) -> dict[str, Any]:
    return {
        "asset": "USD",
        "holder_name": "Maria Silva",
        "account_number": ACCOUNT_NUMBER,
        "routing_number": ROUTING_NUMBER,
        **changes,
    }


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


async def test_a_beneficiary_is_created_and_shown_as_a_mask(
    api: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    response = await post(api, maria, account())

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "asset", "holder_name", "account_mask", "created_at"}
    uuid.UUID(body["id"])
    assert (body["asset"], body["holder_name"], body["account_mask"]) == (
        "USD",
        "Maria Silva",
        "••••6789",
    )


async def test_a_repeated_request_returns_the_same_beneficiary(
    api: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    first = await post(api, maria, account())
    again = await post(api, maria, account())
    other = await post(api, maria, account(), key="key-2")

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert other.json()["id"] != first.json()["id"]
    listed = (await api.get(URL, headers=maria.headers)).json()
    assert [item["id"] for item in listed["items"]] == [other.json()["id"], first.json()["id"]]
    assert listed["next_cursor"] is None


async def test_a_key_is_required_and_cannot_be_reused_for_another_account(
    api: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    assert_problem(await post(api, maria, account(), key=None), 400, "idempotency_key_required")
    await post(api, maria, account())

    reused = await post(api, maria, account(account_number="000987654321"))

    assert_problem(reused, 422, "idempotency_key_reused")


async def test_beneficiaries_are_listed_to_their_owner_only(
    api: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    created = await post(api, maria, account())

    hers = (await api.get(URL, headers=maria.headers)).json()
    his = (await api.get(URL, headers=joao.headers)).json()

    assert hers["items"] == [created.json()]
    assert his == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (account(account_number="12"), "invalid_account"),
        (account(routing_number=None), "invalid_account"),
        (account(asset="USDC"), "unsupported_asset"),
        (account(asset="EUR"), "unknown_asset"),
        (account(holder_name=""), "invalid_request"),
        (account(account_number=""), "invalid_request"),
        (account(account_number=123456789), "invalid_request"),
        (account(iban="DE00"), "invalid_request"),
        ({"asset": "USD", "holder_name": "Maria Silva"}, "invalid_request"),
    ],
)
async def test_a_request_that_cannot_be_a_beneficiary_is_refused_and_stores_nothing(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser, body: dict[str, Any], code: str
) -> None:
    response = await post(api, maria, body)

    assert_problem(response, 422, code)
    assert (await api.get(URL, headers=maria.headers)).json()["items"] == []


async def test_a_bank_that_is_down_is_a_503_and_the_retry_creates_one_beneficiary(
    api: httpx.AsyncClient,
    sim: Sim,  # noqa: F811
    maria: RegisteredUser,
) -> None:
    await sim.inject("bank.create_beneficiary", "error_after_effect")

    assert_problem(await post(api, maria, account()), 503, "provider_unavailable")
    retried = await post(api, maria, account())

    assert retried.status_code == 201, retried.text
    assert len(sim.app.state.sim.bank._beneficiaries) == 1


async def test_beneficiaries_need_a_credential_with_their_scopes(
    api: httpx.AsyncClient, maria: RegisteredUser, as_agent: list[Principal]
) -> None:
    as_agent.append(agent_for(maria, Scope.BENEFICIARIES_READ))
    assert_problem(
        await api.post(URL, json=account(), headers={"Idempotency-Key": "k"}),
        403,
        "insufficient_scope",
    )
    assert (await api.get(URL)).status_code == 200

    as_agent[0] = agent_for(maria, Scope.BENEFICIARIES_WRITE)
    assert_problem(await api.get(URL), 403, "insufficient_scope")
    created = await api.post(URL, json=account(), headers={"Idempotency-Key": "k"})
    assert created.status_code == 201, created.text


async def test_without_a_credential_nothing_is_sent_to_the_bank(
    api: httpx.AsyncClient,
    sim: Sim,  # noqa: F811
) -> None:
    response = await api.post(URL, json=account(), headers={"Idempotency-Key": "k"})

    assert_problem(response, 401, "unauthenticated")
    assert sim.recorder.requests == []


async def test_the_account_number_is_in_no_table_no_log_line_and_no_response(
    api: httpx.AsyncClient,
    db: Database,
    owner_db: Database,
    sim: Sim,  # noqa: F811
    maria: RegisteredUser,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("DEBUG", "json")
    capsys.readouterr()
    responses = [
        await post(api, maria, account()),
        await post(api, maria, account()),
        # Refused by the schema, by the bank, for a reused key, and by a bank that is down.
        await post(api, maria, account(holder_name=""), key="key-2"),
        await post(api, maria, account(routing_number="1"), key="key-3"),
        await post(api, maria, account(holder_name="Someone Else")),
        await api.get(URL, headers=maria.headers),
    ]
    await sim.inject("bank.create_beneficiary", "error")
    responses.append(await post(api, maria, account(), key="key-4"))
    created = responses[0].json()
    withdrawn = await api.post(
        "/v1/withdrawals",
        json={"asset": "USD", "amount": "1.00", "beneficiary_id": created["id"]},
        headers={**maria.headers, "Idempotency-Key": "w-1"},
    )
    responses.append(withdrawn)
    logged = capsys.readouterr()

    # The request did reach the bank with the number in it: the search below is not
    # passing because the number never existed.
    assert ACCOUNT_NUMBER in sim.recorder.sent("POST", "/bank/v1/beneficiaries")[0].content.decode()
    assert [response.status_code for response in responses] == [
        201,
        201,
        422,
        422,
        422,
        200,
        503,
        402,
    ]
    for response in responses:
        assert ACCOUNT_NUMBER not in response.text
        assert ACCOUNT_NUMBER not in json.dumps(dict(response.headers))
    assert "provider.call" in logged.out
    assert ACCOUNT_NUMBER not in logged.out
    assert ACCOUNT_NUMBER not in logged.err

    # Every column of every table, as text: a row cast to text is all of its values.
    async with owner_db.transaction() as session:
        tables = (
            await session.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
            )
        ).scalars()
        searched = 0
        for table in tables.all():
            found = await session.execute(
                text(f'SELECT count(*) FROM "{table}" AS t WHERE t::text LIKE :needle'),  # noqa: S608
                {"needle": f"%{ACCOUNT_NUMBER}%"},
            )
            assert found.scalar_one() == 0, f"the account number is stored in {table}"
            searched += 1
        mask = await session.execute(
            text("SELECT count(*) FROM beneficiaries AS t WHERE t::text LIKE '%6789%'")
        )
    assert searched > 10
    assert mask.scalar_one() == 1

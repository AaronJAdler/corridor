"""Approval requests: a payment above an agent's threshold moves nothing until its owner
says so, and then moves exactly once.
"""

import asyncio
import dataclasses
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from corridor import agents
from corridor.identity import InsufficientScope, Principal, Scope
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import SimBank, SimCustody
from tests.agents.support import (
    APPROVALS,
    Acting,
    act,
    an_agent,
    assert_problem,
    available,
    count,
    events,
    fund,
    rows,
    send,
    set_policy,
    user_recipient,
    withdraw,
)
from tests.support.auth import RegisteredUser, login, register_user
from tests.support.providers import (  # noqa: F401
    EXTERNAL_ADDRESS,
    bank,
    custody,
    provider_settings,
    sim,
    wire,
)

SENDS = Scope.TRANSFERS_CREATE
DAY = 24 * 3600


@pytest.fixture
async def maria(client: httpx.AsyncClient, db: Database) -> RegisteredUser:
    """A user with 1,000.00 USD."""
    user = await register_user(client, handle="maria")
    await fund(db, user, 1_000_00)
    return user


@pytest.fixture
async def agent(client: httpx.AsyncClient, maria: RegisteredUser) -> Acting:
    """An agent of Maria's that may pay anyone, and up to 20.00 without asking her."""
    return await an_agent(client, maria, SENDS, approval_threshold_usd="20.00", any_recipient=True)


async def asked(client: httpx.AsyncClient, agent: Acting, to: RegisteredUser, amount: str) -> str:
    """Have the agent ask for a transfer above its threshold. The id of the request."""
    response = await send(client, agent.headers, to, amount)
    assert response.status_code == 202, response.text
    return str(response.json()["approval_request"]["id"])


async def decide(
    client: httpx.AsyncClient, user: RegisteredUser, approval_id: str, verb: str = "approve"
) -> httpx.Response:
    return await client.post(f"{APPROVALS}/{approval_id}/{verb}", headers=user.headers)


async def listed(client: httpx.AsyncClient, user: RegisteredUser) -> list[dict[str, Any]]:
    response = await client.get(APPROVALS, headers=user.headers)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def logged_in_again(client: httpx.AsyncClient, user: RegisteredUser) -> RegisteredUser:
    return dataclasses.replace(user, tokens=await login(client, user.email))


async def stored(db: Database) -> list[dict[str, Any]]:
    return await rows(
        db,
        "SELECT id::text, status, movement_id::text, failure_code, decided_at"
        " FROM agent_approval_requests ORDER BY id",
    )


# --- asking ----------------------------------------------------------------------------------


async def test_a_payment_above_the_threshold_moves_nothing_and_asks_the_owner(
    client: httpx.AsyncClient,
    db: Database,
    clock: ManualClock,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    response = await client.post(
        "/v1/transfers",
        json={"recipient": "@joao", "asset": "USD", "amount": "20.01", "memo": "Rent"},
        headers={**agent.headers, "Idempotency-Key": "ask-1"},
    )

    assert response.status_code == 202, response.text
    request = response.json()["approval_request"]
    assert request == {
        "id": request["id"],
        "agent_id": agent.id,
        "kind": "transfer",
        "status": "pending",
        "request": {
            "asset": "USD",
            "amount": "20.01",
            "recipient_id": joao.id,
            "memo": "Rent",
            "beneficiary_id": None,
            "to_address": None,
        },
        "movement_id": None,
        "failure_code": None,
        "expires_at": "2026-01-16T12:00:00Z",
        "decided_at": None,
        "created_at": "2026-01-15T12:00:00Z",
    }
    assert (await available(client, maria), await available(client, joao)) == ("1000.00", "0.00")
    assert await count(db, "transfers") == 0
    assert await count(db, "journal_entries") == 1  # Maria's funding, and nothing since.
    assert await listed(client, maria) == [request]


async def test_a_payment_at_the_threshold_is_made_without_asking(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    response = await send(client, agent.headers, joao, "20.00")

    assert response.status_code == 201, response.text
    assert await available(client, joao) == "20.00"
    assert await count(db, "agent_approval_requests") == 0


async def test_an_agent_with_no_threshold_never_asks(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    free = await an_agent(client, maria, SENDS, any_recipient=True)

    assert (await send(client, free.headers, joao, "900.00")).status_code == 201
    assert await count(db, "agent_approval_requests") == 0


async def test_the_owner_is_never_asked_to_approve_their_own_payment(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    assert (await send(client, maria.headers, joao, "500.00")).status_code == 201
    assert await count(db, "agent_approval_requests") == 0


async def test_the_same_idempotency_key_gets_the_same_request_and_makes_no_second(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    first = await send(client, agent.headers, joao, "50.00", key="ask-1")
    again = await send(client, agent.headers, joao, "50.00", key="ask-1")

    assert (first.status_code, again.status_code) == (202, 202)
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert await count(db, "agent_approval_requests") == 1


async def test_a_request_above_the_agents_cap_is_refused_and_not_put_to_the_owner(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    capped = await an_agent(
        client,
        maria,
        SENDS,
        per_tx_usd="100.00",
        approval_threshold_usd="20.00",
        any_recipient=True,
    )

    response = await send(client, capped.headers, joao, "100.01")

    assert_problem(response, 422, "limit_exceeded")
    assert await count(db, "agent_approval_requests") == 0


async def test_a_request_to_pay_someone_off_the_list_is_refused_and_not_put_to_the_owner(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    carla = await register_user(client, handle="carla")
    listing = await an_agent(
        client,
        maria,
        SENDS,
        approval_threshold_usd="20.00",
        allowed_recipients=[user_recipient(joao)],
    )

    response = await send(client, listing.headers, carla, "50.00")

    assert_problem(response, 403, "recipient_not_allowed")
    assert await count(db, "agent_approval_requests") == 0


async def test_a_request_to_pay_nobody_is_refused_as_a_transfer_to_nobody_is(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, agent: Acting
) -> None:
    response = await send(client, agent.headers, "@nobody_here", "50.00")

    assert_problem(response, 404, "recipient_not_found")
    assert await count(db, "agent_approval_requests") == 0


async def test_the_recipient_is_fixed_when_the_agent_asks(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    listing = await an_agent(
        client,
        maria,
        SENDS,
        approval_threshold_usd="20.00",
        allowed_recipients=[user_recipient(joao)],
    )

    response = await send(client, listing.headers, "@joao", "50.00")

    assert response.status_code == 202, response.text
    (row,) = await rows(db, "SELECT request FROM agent_approval_requests")
    assert row["request"] == {
        "recipient": joao.id,
        "asset": "USD",
        "amount": "5000",
        "memo": None,
    }


async def test_only_an_agent_with_the_movements_scope_can_ask_however_it_is_reached(
    db: Database, settings: Settings, maria: RegisteredUser, joao: RegisteredUser, agent: Acting
) -> None:
    intent = agents.TransferIntent(recipient=joao.id, asset="USD", amount=50_00)
    reader = Principal.for_agent(
        uuid.UUID(maria.id), uuid.UUID(agent.id), {Scope.TRANSFERS_READ, Scope.WITHDRAWALS_CREATE}
    )
    owner = Principal.for_user(uuid.UUID(maria.id), "user", new_id())

    async with db.transaction() as session:
        with pytest.raises(InsufficientScope):
            await agents.request_approval(session, reader, intent, settings=settings)
        with pytest.raises(ValueError, match="only an agent"):
            await agents.request_approval(session, owner, intent, settings=settings)

    assert await count(db, "agent_approval_requests") == 0


# --- approving -------------------------------------------------------------------------------


async def test_the_owners_approval_makes_the_payment(
    client: httpx.AsyncClient,
    db: Database,
    clock: ManualClock,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")
    clock.advance(seconds=60)

    response = await decide(client, maria, approval_id)

    assert response.status_code == 200, response.text
    approved = response.json()
    assert (approved["status"], approved["decided_at"]) == ("executed", "2026-01-15T12:01:00Z")
    assert (await available(client, maria), await available(client, joao)) == ("950.00", "50.00")
    # The transfer has the id the request gave it, and is the agent's doing.
    (transfer,) = await rows(
        db, "SELECT id::text, initiated_by_type, initiated_by_id::text, amount FROM transfers"
    )
    assert transfer == {
        "id": approved["movement_id"],
        "initiated_by_type": "agent",
        "initiated_by_id": agent.id,
        "amount": 50_00,
    }
    (row,) = await stored(db)
    assert (row["status"], row["movement_id"]) == ("executed", approved["movement_id"])
    assert await listed(client, maria) == [approved]


async def test_approving_twice_makes_one_payment(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")

    first = await decide(client, maria, approval_id)
    second = await decide(client, maria, approval_id)

    assert first.status_code == 200, first.text
    assert_problem(second, 409, "approval_already_decided")
    assert await count(db, "transfers") == 1
    assert await available(client, joao) == "50.00"


async def test_20_approvals_at_once_make_exactly_one_payment(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")

    answers = await asyncio.gather(*(decide(client, maria, approval_id) for _ in range(20)))

    assert sorted(answer.status_code for answer in answers) == [200] + [409] * 19
    assert await count(db, "transfers") == 1
    assert (await available(client, maria), await available(client, joao)) == ("950.00", "50.00")
    assert [row["status"] for row in await stored(db)] == ["executed"]
    assert len(await events(db, "agent.approval_approved")) == 1


async def test_approving_and_rejecting_at_once_ends_one_way_only(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")

    answers = await asyncio.gather(
        *(decide(client, maria, approval_id, verb) for verb in ["approve", "reject"] * 10)
    )

    assert sorted(answer.status_code for answer in answers) == [200] + [409] * 19
    (row,) = await stored(db)
    moved = {"executed": 1, "rejected": 0}[row["status"]]
    assert await count(db, "transfers") == moved
    assert await available(client, joao) == ("50.00" if moved else "0.00")


async def test_the_movements_id_alone_keeps_a_request_from_being_carried_out_twice(
    client: httpx.AsyncClient,
    db: Database,
    superuser_db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")
    assert (await decide(client, maria, approval_id)).status_code == 200
    # What no request can do: the record of the decision is wiped, as if it had been lost.
    async with superuser_db.transaction() as session:
        await session.execute(
            text("UPDATE agent_approval_requests SET status = 'pending', decided_at = NULL")
        )

    again = await decide(client, maria, approval_id)

    assert again.status_code == 500, again.text
    assert await count(db, "transfers") == 1
    assert (await available(client, maria), await available(client, joao)) == ("950.00", "50.00")


async def test_an_agents_key_cannot_approve_reject_or_list(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
) -> None:
    agent = await an_agent(
        client,
        maria,
        *sorted(agents.AGENT_SCOPES),
        approval_threshold_usd="20.00",
        any_recipient=True,
    )
    approval_id = await asked(client, agent, joao, "50.00")

    approved = await client.post(f"{APPROVALS}/{approval_id}/approve", headers=agent.headers)
    rejected = await client.post(f"{APPROVALS}/{approval_id}/reject", headers=agent.headers)
    seen = await client.get(APPROVALS, headers=agent.headers)

    for response in (approved, rejected, seen):
        assert_problem(response, 403, "insufficient_scope")
    assert [row["status"] for row in await stored(db)] == ["pending"]
    assert await available(client, joao) == "0.00"


async def test_only_the_owners_own_session_decides_however_the_function_is_reached(
    client: httpx.AsyncClient,
    db: Database,
    settings: Settings,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = uuid.UUID(await asked(client, agent, joao, "50.00"))
    principal = Principal.for_agent(
        uuid.UUID(maria.id), uuid.UUID(agent.id), sorted(agents.AGENT_SCOPES)
    )

    async with db.transaction() as session:
        with pytest.raises(InsufficientScope):
            await agents.approve(session, principal, approval_id, settings=settings)
        with pytest.raises(InsufficientScope):
            await agents.reject(session, principal, approval_id)
        with pytest.raises(InsufficientScope):
            await agents.list_approvals(session, principal)

    assert [row["status"] for row in await stored(db)] == ["pending"]


async def test_another_users_request_is_not_found_and_not_listed(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")

    approved = await decide(client, joao, approval_id)
    rejected = await decide(client, joao, approval_id, "reject")
    nothing = await decide(client, joao, str(new_id()))

    assert_problem(approved, 404, "approval_not_found")
    assert_problem(rejected, 404, "approval_not_found")
    assert approved.json()["detail"] == nothing.json()["detail"]
    assert await listed(client, joao) == []
    assert [row["status"] for row in await stored(db)] == ["pending"]
    assert await available(client, joao) == "0.00"


# --- rejecting -------------------------------------------------------------------------------


async def test_a_rejected_request_moves_nothing_and_cannot_be_approved_after(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")

    rejected = await decide(client, maria, approval_id, "reject")
    approved = await decide(client, maria, approval_id)
    again = await decide(client, maria, approval_id, "reject")

    assert rejected.status_code == 200, rejected.text
    assert (rejected.json()["status"], rejected.json()["movement_id"]) == ("rejected", None)
    assert_problem(approved, 409, "approval_already_decided")
    assert_problem(again, 409, "approval_already_decided")
    assert await count(db, "transfers") == 0
    assert (await available(client, maria), await available(client, joao)) == ("1000.00", "0.00")


async def test_a_payment_that_was_made_cannot_be_rejected_after(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")
    await decide(client, maria, approval_id)

    assert_problem(
        await decide(client, maria, approval_id, "reject"), 409, "approval_already_decided"
    )
    assert [row["status"] for row in await stored(db)] == ["executed"]


# --- expiry ----------------------------------------------------------------------------------


async def test_a_request_can_be_approved_until_24_hours_have_passed(
    client: httpx.AsyncClient,
    clock: ManualClock,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")
    clock.advance(seconds=DAY - 1)
    # A day is longer than an access token lasts.
    maria, joao = await logged_in_again(client, maria), await logged_in_again(client, joao)

    assert (await decide(client, maria, approval_id)).status_code == 200
    assert await available(client, joao) == "50.00"


async def test_a_request_that_has_expired_cannot_be_approved_and_is_recorded_as_expired(
    client: httpx.AsyncClient,
    db: Database,
    clock: ManualClock,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")
    clock.advance(seconds=DAY)
    # A day is longer than an access token lasts.
    maria, joao = await logged_in_again(client, maria), await logged_in_again(client, joao)
    # Expired for whoever looks, before anything has recorded it.
    assert [item["status"] for item in await listed(client, maria)] == ["expired"]
    assert [row["status"] for row in await stored(db)] == ["pending"]

    response = await decide(client, maria, approval_id)

    assert_problem(response, 409, "approval_expired")
    (row,) = await stored(db)
    assert (row["status"], row["decided_at"]) == ("expired", clock.now())
    assert await count(db, "transfers") == 0
    assert await available(client, joao) == "0.00"
    assert_problem(await decide(client, maria, approval_id), 409, "approval_already_decided")
    (event,) = await events(db, "agent.approval_expired")
    assert (event["outcome"], event["details"]["agent_id"]) == ("denied", agent.id)


# --- an approval that cannot be carried out --------------------------------------------------


async def test_an_approval_the_wallet_cannot_cover_fails_and_moves_nothing(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "600.00")
    # Maria spends what the request would have needed.
    carla = await register_user(client, handle="carla")
    assert (await send(client, maria.headers, carla, "500.00")).status_code == 201

    response = await decide(client, maria, approval_id)

    assert_problem(response, 402, "insufficient_funds")
    (row,) = await stored(db)
    assert (row["status"], row["failure_code"]) == ("failed", "insufficient_funds")
    assert row["decided_at"] is not None
    assert await count(db, "transfers") == 1
    assert (await available(client, maria), await available(client, joao)) == ("500.00", "0.00")
    # Nothing the refused movement wrote before it was refused is left behind.
    assert await rows(db, "SELECT id FROM risk_usage WHERE agent_id = :agent", agent=agent.id) == []
    (item,) = await listed(client, maria)
    assert (item["status"], item["failure_code"], item["movement_id"]) == (
        "failed",
        "insufficient_funds",
        None,
    )
    # It failed for good: money arriving later does not bring it back.
    await fund(db, maria, 1_000_00)
    assert_problem(await decide(client, maria, approval_id), 409, "approval_already_decided")
    assert await available(client, joao) == "0.00"


async def test_the_cap_at_once_still_applies_to_an_approved_request(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")
    await set_policy(
        client,
        maria,
        agent.id,
        per_tx_usd="40.00",
        approval_threshold_usd="20.00",
        any_recipient=True,
    )

    response = await decide(client, maria, approval_id)

    assert_problem(response, 422, "limit_exceeded")
    assert (response.json()["limit"], response.json()["scope"]) == ("per_transaction", "agent")
    assert [(r["status"], r["failure_code"]) for r in await stored(db)] == [
        ("failed", "limit_exceeded")
    ]
    assert await available(client, joao) == "0.00"


async def test_the_daily_cap_still_applies_to_an_approved_request(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
) -> None:
    agent = await an_agent(
        client,
        maria,
        SENDS,
        daily_usd="60.00",
        approval_threshold_usd="20.00",
        any_recipient=True,
    )
    approval_id = await asked(client, agent, joao, "50.00")
    # What the agent has spent by the time Maria answers leaves no room for it.
    assert (await send(client, agent.headers, joao, "15.00")).status_code == 201

    response = await decide(client, maria, approval_id)

    assert_problem(response, 422, "limit_exceeded")
    assert (response.json()["limit"], response.json()["scope"]) == ("daily", "agent")
    assert [(r["status"], r["failure_code"]) for r in await stored(db)] == [
        ("failed", "limit_exceeded")
    ]
    assert await available(client, joao) == "15.00"


async def test_an_approved_payment_counts_against_the_agents_daily_cap(
    client: httpx.AsyncClient,
    maria: RegisteredUser,
    joao: RegisteredUser,
) -> None:
    agent = await an_agent(
        client,
        maria,
        SENDS,
        daily_usd="60.00",
        approval_threshold_usd="20.00",
        any_recipient=True,
    )
    assert (await decide(client, maria, await asked(client, agent, joao, "50.00"))).status_code == (
        200
    )

    assert_problem(await send(client, agent.headers, joao, "10.01"), 422, "limit_exceeded")
    assert (await send(client, agent.headers, joao, "10.00")).status_code == 201


async def test_the_list_of_recipients_still_applies_to_an_approved_request(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
) -> None:
    agent = await an_agent(
        client,
        maria,
        SENDS,
        approval_threshold_usd="20.00",
        allowed_recipients=[user_recipient(joao)],
    )
    approval_id = await asked(client, agent, joao, "50.00")
    await set_policy(client, maria, agent.id, approval_threshold_usd="20.00")

    response = await decide(client, maria, approval_id)

    assert_problem(response, 403, "recipient_not_allowed")
    assert [(r["status"], r["failure_code"]) for r in await stored(db)] == [
        ("failed", "recipient_not_allowed")
    ]
    assert await available(client, joao) == "0.00"


@pytest.mark.parametrize("verb", ["pause", "revoke"])
async def test_what_a_stopped_agent_asked_for_is_not_carried_out(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
    verb: str,
) -> None:
    approval_id = await asked(client, agent, joao, "50.00")
    await act(client, maria, agent.id, verb)

    response = await decide(client, maria, approval_id)

    assert_problem(response, 409, "agent_not_active")
    assert [(r["status"], r["failure_code"]) for r in await stored(db)] == [
        ("failed", "agent_not_active")
    ]
    assert await count(db, "transfers") == 0
    assert await available(client, joao) == "0.00"


# --- withdrawals -----------------------------------------------------------------------------


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


async def test_a_withdrawal_above_the_threshold_reserves_nothing_until_it_is_approved(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    await fund(db, maria, 100_000_000, "USDC")
    agent = await an_agent(
        api, maria, Scope.WITHDRAWALS_CREATE, approval_threshold_usd="20.00", any_recipient=True
    )

    response = await withdraw(
        api, agent.headers, {"asset": "USDC", "amount": "25", "to_address": EXTERNAL_ADDRESS}
    )

    assert response.status_code == 202, response.text
    request = response.json()["approval_request"]
    assert (request["kind"], request["status"]) == ("withdrawal", "pending")
    assert request["request"] == {
        "asset": "USDC",
        "amount": "25.000000",
        "recipient_id": None,
        "memo": None,
        "beneficiary_id": None,
        "to_address": EXTERNAL_ADDRESS,
    }
    assert await count(db, "withdrawals") == 0
    assert await available(api, maria, "USDC") == "100.000000"

    approved = await decide(api, maria, request["id"])

    assert approved.status_code == 200, approved.text
    (withdrawal,) = await rows(db, "SELECT id::text, status, amount, to_address FROM withdrawals")
    assert withdrawal == {
        "id": approved.json()["movement_id"],
        "status": "held",
        "amount": 25_000_000,
        "to_address": EXTERNAL_ADDRESS,
    }
    (event,) = await events(db, "withdrawal.requested")
    assert (event["actor_type"], event["actor_id"]) == ("agent", agent.id)
    assert str(event["principal_id"]) == maria.id


# --- reading ---------------------------------------------------------------------------------


async def test_the_owners_requests_are_listed_newest_first_a_page_at_a_time(
    client: httpx.AsyncClient,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    ids = [await asked(client, agent, joao, f"{amount}.00") for amount in (30, 40, 50)]

    first = await client.get(APPROVALS, params={"limit": 2}, headers=maria.headers)
    rest = await client.get(
        APPROVALS,
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
        headers=maria.headers,
    )

    assert [item["id"] for item in first.json()["items"]] == [ids[2], ids[1]]
    assert [item["id"] for item in rest.json()["items"]] == [ids[0]]
    assert rest.json()["next_cursor"] is None


async def test_a_cursor_from_another_users_list_is_refused(
    client: httpx.AsyncClient,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    for amount in ("30.00", "40.00"):
        await asked(client, agent, joao, amount)
    cursor = (await client.get(APPROVALS, params={"limit": 1}, headers=maria.headers)).json()[
        "next_cursor"
    ]

    response = await client.get(APPROVALS, params={"cursor": cursor}, headers=joao.headers)

    assert_problem(response, 422, "invalid_cursor")


# --- the audit log ---------------------------------------------------------------------------


async def test_asking_approving_and_rejecting_are_audited_with_the_agent_and_its_owner(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approved_id = await asked(client, agent, joao, "50.00")
    rejected_id = await asked(client, agent, joao, "60.00")
    await decide(client, maria, approved_id)
    await decide(client, maria, rejected_id, "reject")

    asked_for = await events(db, "agent.approval_requested")
    (approved,) = await events(db, "agent.approval_approved")
    (rejected,) = await events(db, "agent.approval_rejected")
    (moved,) = await events(db, "transfer.created")

    # The agent asked, for Maria.
    assert [(e["actor_type"], e["actor_id"], str(e["principal_id"])) for e in asked_for] == [
        ("agent", agent.id, maria.id)
    ] * 2
    assert [e["resource_id"] for e in asked_for] == [approved_id, rejected_id]
    # Maria decided, and each decision names the agent whose request it was.
    for event, approval_id, amount in (
        (approved, approved_id, "5000"),
        (rejected, rejected_id, "6000"),
    ):
        assert (event["actor_type"], event["actor_id"]) == ("user", maria.id)
        assert str(event["principal_id"]) == maria.id
        assert (event["resource_type"], event["resource_id"]) == ("approval_request", approval_id)
        assert event["outcome"] == "success"
        assert event["details"]["agent_id"] == agent.id
        assert event["details"]["owner_user_id"] == maria.id
        assert (event["details"]["asset"], event["details"]["amount"]) == ("USD", amount)
    # The payment itself is the agent's, for Maria, under the id the request named.
    assert (moved["actor_type"], moved["actor_id"]) == ("agent", agent.id)
    assert str(moved["principal_id"]) == maria.id
    assert moved["resource_id"] == approved["details"]["movement_id"]


async def test_an_approval_that_failed_is_audited_as_failed(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    agent: Acting,
) -> None:
    approval_id = await asked(client, agent, joao, "600.00")
    assert (await send(client, maria.headers, joao, "500.00")).status_code == 201

    await decide(client, maria, approval_id)

    (event,) = await events(db, "agent.approval_approved")
    assert event["outcome"] == "failed"
    assert event["details"]["failure_code"] == "insufficient_funds"
    assert event["details"]["agent_id"] == agent.id
    assert len(await events(db, "transfer.created")) == 1  # Maria's own, and no other.

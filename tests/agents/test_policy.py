"""Spend policy: what an agent may move and whom it may pay, enforced on the server.

Every test here drives the API with a real key of a real agent, so what is shown is what a
client holding that key can and cannot do, whatever it was told to do.
"""

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from corridor import agents
from corridor.identity import InsufficientScope, Principal, Scope
from corridor.platform.clock import ManualClock
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import SimBank, SimCustody
from tests.agents.support import (
    AGENTS,
    NO_LIMITS,
    Acting,
    act,
    an_agent,
    assert_problem,
    available,
    count,
    create_agent,
    events,
    fund,
    give_open_policy,
    put_policy,
    rows,
    send,
    set_policy,
    user_recipient,
    withdraw,
)
from tests.support.auth import RegisteredUser, register_user
from tests.support.providers import (  # noqa: F401
    ACCOUNT_NUMBER,
    EXTERNAL_ADDRESS,
    ROUTING_NUMBER,
    bank,
    custody,
    provider_settings,
    sim,
    wire,
)

SENDS = Scope.TRANSFERS_CREATE


@pytest.fixture
async def maria(client: httpx.AsyncClient, db: Database) -> RegisteredUser:
    """A user with 1,000.00 USD."""
    user = await register_user(client, handle="maria")
    await fund(db, user, 1_000_00)
    return user


@pytest.fixture
async def carla(client: httpx.AsyncClient) -> RegisteredUser:
    return await register_user(client, handle="carla")


async def a_beneficiary(client: httpx.AsyncClient, user: RegisteredUser) -> str:
    response = await client.post(
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


# --- whom an agent may pay -------------------------------------------------------------------


async def test_an_agent_whose_owner_set_no_policy_can_pay_nobody(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS)

    response = await send(client, agent.headers, joao, "5.00")

    assert_problem(response, 403, "recipient_not_allowed")
    assert await available(client, maria) == "1000.00"
    assert await count(db, "transfers") == 0


async def test_an_agent_pays_a_recipient_on_its_list(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS, allowed_recipients=[user_recipient(joao)])

    response = await send(client, agent.headers, joao, "5.00")

    assert response.status_code == 201, response.text
    assert (await available(client, maria), await available(client, joao)) == ("995.00", "5.00")


@pytest.mark.parametrize("named_by", ["id", "handle", "email"])
async def test_a_listed_recipient_is_the_same_person_however_the_agent_names_them(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser, named_by: str
) -> None:
    agent = await an_agent(client, maria, SENDS, allowed_recipients=[user_recipient(joao)])
    name = {"id": joao.id, "handle": "@joao", "email": joao.email}[named_by]

    assert (await send(client, agent.headers, name, "5.00")).status_code == 201


async def test_an_agent_cannot_pay_a_recipient_who_is_not_on_its_list(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    carla: RegisteredUser,
) -> None:
    agent = await an_agent(client, maria, SENDS, allowed_recipients=[user_recipient(joao)])

    response = await send(client, agent.headers, carla, "5.00")

    assert_problem(response, 403, "recipient_not_allowed")
    assert (await available(client, maria), await available(client, carla)) == ("1000.00", "0.00")
    assert await count(db, "transfers") == 0


async def test_a_recipient_who_does_not_exist_is_answered_as_one_who_is_not_listed(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS, allowed_recipients=[user_recipient(joao)])

    nobody = await send(client, agent.headers, "@nobody_here", "5.00")
    not_listed = await send(client, agent.headers, (await register_user(client)).id, "5.00")

    assert_problem(nobody, 403, "recipient_not_allowed")
    assert nobody.json()["detail"] == not_listed.json()["detail"]


async def test_a_policy_that_allows_any_recipient_lets_the_agent_pay_anyone(
    client: httpx.AsyncClient, maria: RegisteredUser, carla: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS, any_recipient=True)

    assert (await send(client, agent.headers, carla, "5.00")).status_code == 201
    assert await available(client, carla) == "5.00"


async def test_one_agents_list_does_not_let_another_agent_pay(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    await an_agent(client, maria, SENDS, allowed_recipients=[user_recipient(joao)])
    other = await an_agent(client, maria, SENDS)

    assert_problem(await send(client, other.headers, joao, "5.00"), 403, "recipient_not_allowed")


async def test_the_owner_is_not_bound_by_an_agents_policy(
    client: httpx.AsyncClient, maria: RegisteredUser, carla: RegisteredUser
) -> None:
    await an_agent(client, maria, SENDS, per_tx_usd="1.00")

    assert (await send(client, maria.headers, carla, "500.00")).status_code == 201


# --- how much an agent may move --------------------------------------------------------------


async def test_an_agent_cannot_send_more_at_once_than_its_cap(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS, per_tx_usd="50.00", any_recipient=True)

    over = await send(client, agent.headers, joao, "50.01")
    at = await send(client, agent.headers, joao, "50.00")

    assert_problem(over, 422, "limit_exceeded")
    assert (over.json()["limit"], over.json()["scope"]) == ("per_transaction", "agent")
    assert at.status_code == 201, at.text
    assert await available(client, maria) == "950.00"
    assert await count(db, "transfers") == 1


async def test_the_cap_is_given_to_risk_which_enforces_it_where_the_money_moves(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS, per_tx_usd="50.00", daily_usd="120.00")

    limits = await rows(
        db,
        "SELECT scope, kind, per_tx_usd, daily_usd FROM risk_limits WHERE agent_id = :agent",
        agent=agent.id,
    )

    assert limits == [{"scope": "agent", "kind": None, "per_tx_usd": 50_00, "daily_usd": 120_00}]


async def test_an_agent_cannot_send_more_in_24_hours_than_its_daily_cap(
    client: httpx.AsyncClient,
    db: Database,
    clock: ManualClock,
    maria: RegisteredUser,
    joao: RegisteredUser,
) -> None:
    agent = await an_agent(client, maria, SENDS, daily_usd="50.00", any_recipient=True)
    assert (await send(client, agent.headers, joao, "30.00")).status_code == 201

    over = await send(client, agent.headers, joao, "20.01")
    within = await send(client, agent.headers, joao, "20.00")
    full = await send(client, agent.headers, joao, "0.01")

    assert_problem(over, 422, "limit_exceeded")
    assert (over.json()["limit"], over.json()["scope"]) == ("daily", "agent")
    assert within.status_code == 201, within.text
    assert_problem(full, 422, "limit_exceeded")
    assert await available(client, maria) == "950.00"

    clock.advance(seconds=24 * 3600)
    assert (await send(client, agent.headers, joao, "50.00")).status_code == 201


async def test_what_the_owner_spends_does_not_count_against_the_agents_daily_cap(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS, daily_usd="50.00", any_recipient=True)
    assert (await send(client, maria.headers, joao, "400.00")).status_code == 201

    assert (await send(client, agent.headers, joao, "50.00")).status_code == 201


async def test_a_cap_is_in_us_dollars_whatever_asset_moves(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    await fund(db, maria, 100_000_00, "MXN")
    agent = await an_agent(client, maria, SENDS, per_tx_usd="10.00", any_recipient=True)

    # 10,000 pesos are worth far more than ten dollars at any rate the peso has had.
    over = await send(client, agent.headers, joao, "10000.00", asset="MXN")

    assert_problem(over, 422, "limit_exceeded")
    assert await available(client, maria, "MXN") == "100000.00"


# --- a policy changes at once ----------------------------------------------------------------


async def test_raising_a_cap_takes_effect_with_the_next_request(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS, per_tx_usd="10.00", any_recipient=True)
    assert_problem(await send(client, agent.headers, joao, "40.00"), 422, "limit_exceeded")

    await set_policy(client, maria, agent.id, per_tx_usd="40.00", any_recipient=True)

    assert (await send(client, agent.headers, joao, "40.00")).status_code == 201


async def test_lowering_a_cap_takes_effect_with_the_next_request(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS, per_tx_usd="40.00", any_recipient=True)
    assert (await send(client, agent.headers, joao, "40.00")).status_code == 201

    await set_policy(client, maria, agent.id, per_tx_usd="10.00", any_recipient=True)

    assert_problem(await send(client, agent.headers, joao, "40.00"), 422, "limit_exceeded")


async def test_removing_a_cap_removes_it_from_risk_as_well(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await an_agent(
        client, maria, SENDS, per_tx_usd="10.00", daily_usd="10.00", any_recipient=True
    )

    await set_policy(client, maria, agent.id, any_recipient=True)

    assert (await send(client, agent.headers, joao, "40.00")).status_code == 201


async def test_taking_a_recipient_off_the_list_takes_effect_with_the_next_request(
    client: httpx.AsyncClient,
    maria: RegisteredUser,
    joao: RegisteredUser,
    carla: RegisteredUser,
) -> None:
    both = [user_recipient(joao), user_recipient(carla)]
    agent = await an_agent(client, maria, SENDS, allowed_recipients=both)
    assert (await send(client, agent.headers, joao, "5.00")).status_code == 201

    await set_policy(client, maria, agent.id, allowed_recipients=[user_recipient(carla)])

    assert_problem(await send(client, agent.headers, joao, "5.00"), 403, "recipient_not_allowed")
    assert (await send(client, agent.headers, carla, "5.00")).status_code == 201


async def test_a_policy_is_replaced_whole_so_allowing_anyone_can_be_taken_back(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS, any_recipient=True)

    await set_policy(client, maria, agent.id)

    assert_problem(await send(client, agent.headers, joao, "5.00"), 403, "recipient_not_allowed")


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


async def test_an_agent_withdraws_only_to_a_beneficiary_on_its_list(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    listed, unlisted = await a_beneficiary(api, maria), await a_beneficiary(api, maria)
    agent = await an_agent(
        api,
        maria,
        Scope.WITHDRAWALS_CREATE,
        allowed_recipients=[{"kind": "beneficiary", "id": listed}],
    )

    refused = await withdraw(
        api, agent.headers, {"asset": "USD", "amount": "20.00", "beneficiary_id": unlisted}
    )
    made = await withdraw(
        api, agent.headers, {"asset": "USD", "amount": "20.00", "beneficiary_id": listed}
    )

    assert_problem(refused, 403, "recipient_not_allowed")
    assert made.status_code == 202, made.text
    assert made.json()["beneficiary_id"] == listed
    assert await count(db, "withdrawals") == 1


async def test_a_user_on_the_list_is_not_a_beneficiary_with_the_same_id(
    api: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    beneficiary = await a_beneficiary(api, maria)
    agent = await an_agent(
        api,
        maria,
        Scope.WITHDRAWALS_CREATE,
        allowed_recipients=[{"kind": "user", "id": beneficiary}],
    )

    refused = await withdraw(
        api, agent.headers, {"asset": "USD", "amount": "20.00", "beneficiary_id": beneficiary}
    )

    assert_problem(refused, 403, "recipient_not_allowed")


async def test_an_agent_withdraws_to_an_address_only_if_it_may_pay_anyone(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    await fund(db, maria, 100_000_000, "USDC")
    body = {"asset": "USDC", "amount": "25", "to_address": EXTERNAL_ADDRESS}
    agent = await an_agent(
        api, maria, Scope.WITHDRAWALS_CREATE, allowed_recipients=[user_recipient(joao)]
    )

    refused = await withdraw(api, agent.headers, body)
    await set_policy(api, maria, agent.id, any_recipient=True)
    made = await withdraw(api, agent.headers, body)

    assert_problem(refused, 403, "recipient_not_allowed")
    assert made.status_code == 202, made.text
    assert made.json()["status"] == "held"
    assert await count(db, "withdrawals") == 1


async def test_a_withdrawal_above_the_agents_cap_reserves_nothing(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    await fund(db, maria, 100_000_000, "USDC")
    agent = await an_agent(
        api, maria, Scope.WITHDRAWALS_CREATE, per_tx_usd="20.00", any_recipient=True
    )

    over = await withdraw(
        api, agent.headers, {"asset": "USDC", "amount": "25", "to_address": EXTERNAL_ADDRESS}
    )

    assert_problem(over, 422, "limit_exceeded")
    assert await available(api, maria, "USDC") == "100.000000"
    assert await count(db, "withdrawals") == 0


# --- what the policy is asked directly -------------------------------------------------------


def acting_for(user: RegisteredUser, agent: Acting, *scopes: str) -> Principal:
    return Principal.for_agent(uuid.UUID(user.id), uuid.UUID(agent.id), scopes)


async def test_a_conversion_needs_no_recipient(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, Scope.FX_CONVERT)
    principal = acting_for(maria, agent, Scope.FX_CONVERT)

    async with db.transaction() as session:
        conversion = await agents.check_policy(session, principal, "conversion", "USD", 5_00)
        with pytest.raises(agents.RecipientNotAllowed):
            await agents.check_policy(session, principal, "transfer", "USD", 5_00)

    assert conversion.outcome == "allow"


async def test_a_user_acting_for_themselves_has_no_policy_to_ask(
    db: Database, maria: RegisteredUser
) -> None:
    owner = Principal.for_user(uuid.UUID(maria.id), "user", new_id())

    async with db.transaction() as session:
        decision = await agents.check_policy(session, owner, "transfer", "USD", 10**12)

    assert decision.outcome == "allow"


async def test_a_made_up_agent_given_an_open_policy_may_pay_anyone(
    db: Database, maria: RegisteredUser
) -> None:
    stand_in = Principal.for_agent(uuid.UUID(maria.id), new_id(), {SENDS})
    to = agents.Recipient("user", str(new_id()))
    async with db.transaction() as session:
        with pytest.raises(agents.RecipientNotAllowed):
            await agents.check_policy(session, stand_in, "transfer", "USD", 5_00, to)

    await give_open_policy(db, stand_in)

    async with db.transaction() as session:
        decision = await agents.check_policy(session, stand_in, "transfer", "USD", 5_00, to)
    assert decision.outcome == "allow"


# --- setting and reading a policy ------------------------------------------------------------


async def test_a_policy_is_read_back_as_it_was_set(
    client: httpx.AsyncClient,
    clock: ManualClock,
    maria: RegisteredUser,
    joao: RegisteredUser,
) -> None:
    agent = await create_agent(client, maria)
    beneficiary = str(new_id())
    recipients = [{"kind": "beneficiary", "id": beneficiary}, user_recipient(joao)]

    stored = await set_policy(
        client,
        maria,
        agent["id"],
        per_tx_usd="50",
        daily_usd="200.5",
        approval_threshold_usd="20.00",
        # Named twice: it is on the list once.
        allowed_recipients=[*recipients, user_recipient(joao)],
    )
    read = await client.get(f"{AGENTS}/{agent['id']}/policy", headers=maria.headers)

    assert stored == {
        "agent_id": agent["id"],
        "per_tx_usd": "50.00",
        "daily_usd": "200.50",
        "approval_threshold_usd": "20.00",
        "any_recipient": False,
        "allowed_recipients": recipients,
        "updated_at": "2026-01-15T12:00:00Z",
    }
    assert read.status_code == 200, read.text
    assert read.json() == stored


async def test_an_agent_with_no_policy_reads_as_allowing_nothing(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)

    read = await client.get(f"{AGENTS}/{agent['id']}/policy", headers=maria.headers)

    assert read.json() == {
        "agent_id": agent["id"],
        **NO_LIMITS,
        "any_recipient": False,
        "allowed_recipients": [],
        "updated_at": None,
    }


async def test_another_users_agent_has_no_policy_to_set_or_read(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)

    written = await put_policy(client, joao, agent["id"], any_recipient=True)
    read = await client.get(f"{AGENTS}/{agent['id']}/policy", headers=joao.headers)

    assert_problem(written, 404, "agent_not_found")
    assert_problem(read, 404, "agent_not_found")
    assert await count(db, "agent_policies") == 0
    assert await rows(db, "SELECT id FROM risk_limits WHERE scope = 'agent'") == []


async def test_an_agents_own_key_cannot_set_or_read_its_policy(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, *sorted(agents.AGENT_SCOPES))
    url = f"{AGENTS}/{agent.id}/policy"

    written = await client.put(
        url, json={**NO_LIMITS, "any_recipient": True}, headers=agent.headers
    )
    read = await client.get(url, headers=agent.headers)

    assert_problem(written, 403, "insufficient_scope")
    assert_problem(read, 403, "insufficient_scope")
    assert await count(db, "agent_policies") == 0


async def test_only_the_owners_own_session_sets_a_policy_however_the_function_is_reached(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS)
    principal = acting_for(maria, agent, *sorted(agents.AGENT_SCOPES))

    async with db.transaction() as session:
        with pytest.raises(InsufficientScope):
            await agents.set_policy(
                session,
                principal,
                uuid.UUID(agent.id),
                per_tx_usd=None,
                daily_usd=None,
                approval_threshold_usd=None,
                any_recipient=True,
            )
        with pytest.raises(InsufficientScope):
            await agents.get_policy(session, principal, uuid.UUID(agent.id))


async def test_a_revoked_agent_cannot_be_given_a_policy(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)
    await act(client, maria, agent["id"], "revoke")

    assert_problem(
        await put_policy(client, maria, agent["id"], any_recipient=True), 409, "agent_revoked"
    )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"per_tx_usd": "-1.00"}, "invalid_amount"),
        ({"daily_usd": "1.005"}, "invalid_amount"),
        ({"approval_threshold_usd": "ten"}, "invalid_amount"),
        ({"per_tx_usd": 50}, "invalid_request"),
        (
            {"allowed_recipients": [{"kind": "address", "id": str(uuid.uuid4())}]},
            "invalid_request",
        ),
        ({"allowed_recipients": [{"kind": "user", "id": "@joao"}]}, "invalid_request"),
        ({"any_recipient": "yes please"}, "invalid_request"),
        ({"spend_whatever": True}, "invalid_request"),
    ],
)
async def test_a_policy_that_does_not_say_what_it_means_is_refused(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    change: dict[str, Any],
    code: str,
) -> None:
    agent = await create_agent(client, maria)

    response = await put_policy(client, maria, agent["id"], **change)

    assert_problem(response, 422, code)
    assert await count(db, "agent_policies") == 0


@pytest.mark.parametrize("left_out", sorted(NO_LIMITS))
async def test_each_amount_of_a_policy_has_to_be_stated_even_if_it_is_no_limit(
    client: httpx.AsyncClient, maria: RegisteredUser, left_out: str
) -> None:
    agent = await create_agent(client, maria)
    body = {name: None for name in NO_LIMITS if name != left_out}

    response = await client.put(f"{AGENTS}/{agent['id']}/policy", json=body, headers=maria.headers)

    assert_problem(response, 422, "invalid_request")


async def test_a_policy_names_at_most_a_hundred_recipients(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)
    principal = Principal.for_user(uuid.UUID(maria.id), "user", new_id())
    many = [agents.AllowedRecipient("user", new_id()) for _ in range(101)]

    over = await put_policy(
        client,
        maria,
        agent["id"],
        allowed_recipients=[{"kind": r.kind, "id": str(r.target_id)} for r in many],
    )
    async with db.transaction() as session:
        with pytest.raises(agents.InvalidPolicy):
            await agents.set_policy(
                session,
                principal,
                uuid.UUID(agent["id"]),
                per_tx_usd=None,
                daily_usd=None,
                approval_threshold_usd=None,
                allowed_recipients=many,
            )
        at = await agents.set_policy(
            session,
            principal,
            uuid.UUID(agent["id"]),
            per_tx_usd=None,
            daily_usd=None,
            approval_threshold_usd=None,
            allowed_recipients=many[:100],
        )

    assert_problem(over, 422, "invalid_request")
    assert len(at.allowed_recipients) == 100


@pytest.mark.parametrize("amount", [-1, 10**38, 12.5, True])
async def test_an_amount_of_a_policy_is_a_whole_number_of_cents_that_can_be_stored(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, amount: Any
) -> None:
    agent = await create_agent(client, maria)
    principal = Principal.for_user(uuid.UUID(maria.id), "user", new_id())

    async with db.transaction() as session:
        with pytest.raises(agents.InvalidPolicy):
            await agents.set_policy(
                session,
                principal,
                uuid.UUID(agent["id"]),
                per_tx_usd=None,
                daily_usd=None,
                approval_threshold_usd=amount,
            )

    assert await count(db, "agent_policies") == 0


# --- the audit log ---------------------------------------------------------------------------


async def test_a_change_of_policy_is_audited_with_the_agent_and_its_owner(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)

    await set_policy(
        client,
        maria,
        agent["id"],
        per_tx_usd="50.00",
        approval_threshold_usd="20.00",
        allowed_recipients=[user_recipient(joao)],
    )

    (event,) = await events(db, "agent.policy_changed")
    assert (event["actor_type"], event["actor_id"]) == ("user", maria.id)
    assert str(event["principal_id"]) == maria.id
    assert (event["resource_type"], event["resource_id"]) == ("agent", agent["id"])
    assert event["details"] == {
        "agent_id": agent["id"],
        "per_tx_usd": "50.00",
        "daily_usd": None,
        "approval_threshold_usd": "20.00",
        "any_recipient": False,
        "allowed_recipients": [user_recipient(joao)],
    }


async def test_a_policy_that_was_refused_is_not_audited_as_changed(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)

    await put_policy(client, joao, agent["id"], any_recipient=True)

    assert await events(db, "agent.policy_changed") == []


async def test_an_agents_transfer_is_audited_with_the_agent_and_its_owner(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    agent = await an_agent(client, maria, SENDS, any_recipient=True)

    sent = await send(client, agent.headers, joao, "5.00")

    (event,) = await events(db, "transfer.created")
    assert (event["actor_type"], event["actor_id"]) == ("agent", agent.id)
    assert str(event["principal_id"]) == maria.id
    assert event["resource_id"] == sent.json()["id"]


async def test_an_agents_withdrawal_is_audited_with_the_agent_and_its_owner(
    api: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    await fund(db, maria, 100_000_000, "USDC")
    agent = await an_agent(api, maria, Scope.WITHDRAWALS_CREATE, any_recipient=True)

    made = await withdraw(
        api, agent.headers, {"asset": "USDC", "amount": "25", "to_address": EXTERNAL_ADDRESS}
    )

    (event,) = await events(db, "withdrawal.requested")
    assert (event["actor_type"], event["actor_id"]) == ("agent", agent.id)
    assert str(event["principal_id"]) == maria.id
    assert event["resource_id"] == made.json()["id"]

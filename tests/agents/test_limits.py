"""How many agents a user may have, keys an agent may have, and requests an agent may
leave waiting for its owner: each refused past its limit, and each limit held when the
requests arrive together."""

import asyncio
import dataclasses
from datetime import timedelta
from typing import Any

import httpx
import pytest

from corridor.identity import Scope
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from tests.agents.support import (
    AGENTS,
    APPROVALS,
    Acting,
    act,
    an_agent,
    assert_problem,
    count,
    create_agent,
    fund,
    issue_key,
    send,
)
from tests.support.auth import RegisteredUser, login

MAX_AGENTS = 3
MAX_KEYS = 2
MAX_PENDING = 2
DAY = 24 * 3600


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "max_agents_per_user": MAX_AGENTS,
            "max_keys_per_agent": MAX_KEYS,
            "max_pending_approvals_per_agent": MAX_PENDING,
        }
    )


def test_the_limits_where_nothing_is_configured() -> None:
    fields = Settings.model_fields

    assert fields["max_agents_per_user"].default == 20
    assert fields["max_keys_per_agent"].default == 10
    assert fields["max_pending_approvals_per_agent"].default == 20


def limit_of(response: httpx.Response) -> Any:
    return response.json()["limit"]


# --- agents ------------------------------------------------------------------------------------


async def new_agent(client: httpx.AsyncClient, user: RegisteredUser) -> httpx.Response:
    return await client.post(AGENTS, json={"name": "Bill payer"}, headers=user.headers)


async def test_a_user_with_as_many_agents_as_allowed_is_refused_another(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    for _ in range(MAX_AGENTS):
        await create_agent(client, maria)

    refused = await new_agent(client, maria)

    assert_problem(refused, 409, "agent_limit_reached")
    assert limit_of(refused) == MAX_AGENTS
    assert await count(db, "agents") == MAX_AGENTS


async def test_the_limit_on_agents_is_each_users_own(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    for _ in range(MAX_AGENTS):
        await create_agent(client, maria)

    assert (await new_agent(client, joao)).status_code == 201


async def test_a_revoked_agent_takes_no_place_and_a_paused_one_does(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    made = [await create_agent(client, maria) for _ in range(MAX_AGENTS)]

    await act(client, maria, made[0]["id"], "pause")
    while_paused = await new_agent(client, maria)
    await act(client, maria, made[0]["id"], "revoke")
    after_revoking = await new_agent(client, maria)

    assert_problem(while_paused, 409, "agent_limit_reached")
    assert after_revoking.status_code == 201


async def test_agents_asked_for_together_cannot_take_more_places_than_there_are(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    answers = await asyncio.gather(*(new_agent(client, maria) for _ in range(MAX_AGENTS + 5)))

    statuses = sorted(answer.status_code for answer in answers)
    assert statuses == [201] * MAX_AGENTS + [409] * 5
    assert await count(db, "agents") == MAX_AGENTS


# --- keys --------------------------------------------------------------------------------------


async def new_key(
    client: httpx.AsyncClient, user: RegisteredUser, agent_id: str, **more: Any
) -> httpx.Response:
    return await client.post(
        f"{AGENTS}/{agent_id}/keys",
        json={"scopes": [Scope.WALLET_READ], **more},
        headers=user.headers,
    )


async def test_an_agent_with_as_many_keys_as_allowed_is_refused_another(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)
    for _ in range(MAX_KEYS):
        await issue_key(client, maria, agent["id"], Scope.WALLET_READ)

    refused = await new_key(client, maria, agent["id"])

    assert_problem(refused, 409, "agent_key_limit_reached")
    assert limit_of(refused) == MAX_KEYS
    assert "key" not in refused.json()
    assert await count(db, "agent_keys") == MAX_KEYS


async def test_the_limit_on_keys_is_each_agents_own(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    full, other = await create_agent(client, maria), await create_agent(client, maria)
    for _ in range(MAX_KEYS):
        await issue_key(client, maria, full["id"], Scope.WALLET_READ)

    assert (await new_key(client, maria, other["id"])).status_code == 201


async def test_a_revoked_key_takes_no_place(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)
    issued = [
        await issue_key(client, maria, agent["id"], Scope.WALLET_READ) for _ in range(MAX_KEYS)
    ]

    revoked = await client.delete(
        f"{AGENTS}/{agent['id']}/keys/{issued[0]['id']}", headers=maria.headers
    )

    assert revoked.status_code == 204
    assert (await new_key(client, maria, agent["id"])).status_code == 201


async def test_a_key_that_has_expired_takes_no_place(
    clock: ManualClock, client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)
    in_an_hour = clock.now() + timedelta(hours=1)
    for _ in range(MAX_KEYS):
        await issue_key(client, maria, agent["id"], Scope.WALLET_READ, expires_at=in_an_hour)

    before = await new_key(client, maria, agent["id"])
    clock.advance(seconds=3600)
    # Her own access token did not last the hour either.
    maria = dataclasses.replace(maria, tokens=await login(client, maria.email))
    after = await new_key(client, maria, agent["id"])

    assert_problem(before, 409, "agent_key_limit_reached")
    assert after.status_code == 201


async def test_keys_asked_for_together_cannot_take_more_places_than_there_are(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)

    answers = await asyncio.gather(
        *(new_key(client, maria, agent["id"]) for _ in range(MAX_KEYS + 5))
    )

    statuses = sorted(answer.status_code for answer in answers)
    assert statuses == [201] * MAX_KEYS + [409] * 5
    assert await count(db, "agent_keys") == MAX_KEYS


# --- requests waiting for approval ---------------------------------------------------------------


@pytest.fixture
async def asking(client: httpx.AsyncClient, db: Database, maria: RegisteredUser) -> Acting:
    """An agent of Maria's that has to ask before it pays anything at all."""
    await fund(db, maria, 1_000_00)
    return await an_agent(
        client, maria, Scope.TRANSFERS_CREATE, approval_threshold_usd="0", any_recipient=True
    )


async def test_an_agent_with_as_many_waiting_requests_as_allowed_is_refused_another(
    client: httpx.AsyncClient, db: Database, joao: RegisteredUser, asking: Acting
) -> None:
    asked = [await send(client, asking.headers, joao, "5.00") for _ in range(MAX_PENDING)]

    refused = await send(client, asking.headers, joao, "5.00")

    assert [answer.status_code for answer in asked] == [202] * MAX_PENDING
    assert_problem(refused, 409, "approval_limit_reached")
    assert limit_of(refused) == MAX_PENDING
    assert await count(db, "agent_approval_requests") == MAX_PENDING
    assert await count(db, "transfers") == 0


async def test_deciding_a_request_makes_room_for_another(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser, asking: Acting
) -> None:
    asked = [await send(client, asking.headers, joao, "5.00") for _ in range(MAX_PENDING)]
    first = asked[0].json()["approval_request"]["id"]

    rejected = await client.post(f"{APPROVALS}/{first}/reject", headers=maria.headers)

    assert rejected.status_code == 200
    assert (await send(client, asking.headers, joao, "5.00")).status_code == 202


async def test_a_request_that_has_expired_is_not_waiting_for_anybody(
    clock: ManualClock, client: httpx.AsyncClient, joao: RegisteredUser, asking: Acting
) -> None:
    for _ in range(MAX_PENDING):
        await send(client, asking.headers, joao, "5.00")

    clock.advance(seconds=DAY - 1)
    before = await send(client, asking.headers, joao, "5.00")
    clock.advance(seconds=1)
    after = await send(client, asking.headers, joao, "5.00")

    assert_problem(before, 409, "approval_limit_reached")
    assert after.status_code == 202


async def test_the_limit_on_waiting_requests_is_each_agents_own(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser, asking: Acting
) -> None:
    other = await an_agent(
        client, maria, Scope.TRANSFERS_CREATE, approval_threshold_usd="0", any_recipient=True
    )
    for _ in range(MAX_PENDING):
        await send(client, asking.headers, joao, "5.00")

    assert (await send(client, other.headers, joao, "5.00")).status_code == 202


async def test_requests_made_together_cannot_take_more_places_than_there_are(
    client: httpx.AsyncClient, db: Database, joao: RegisteredUser, asking: Acting
) -> None:
    answers = await asyncio.gather(
        *(send(client, asking.headers, joao, "5.00") for _ in range(MAX_PENDING + 5))
    )

    statuses = sorted(answer.status_code for answer in answers)
    assert statuses == [202] * MAX_PENDING + [409] * 5
    assert await count(db, "agent_approval_requests") == MAX_PENDING

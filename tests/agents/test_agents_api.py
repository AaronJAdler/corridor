"""Agents over HTTP: a user creates them, gives them keys, and stops them."""

import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.identity import Scope
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from tests.agents.support import (
    AGENTS,
    act,
    agent_key,
    create_agent,
    events,
    issue_key,
    key_headers,
    rows,
)
from tests.support.auth import RegisteredUser

EVERY_SCOPE = [str(scope) for scope in Scope]


def assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == code


async def listed(client: httpx.AsyncClient, user: RegisteredUser, **params: Any) -> dict[str, Any]:
    response = await client.get(AGENTS, params=params, headers=user.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def management_requests(agent_id: str, key_id: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    """One request to each route under /v1/agents."""
    return [
        ("POST", AGENTS, {"name": "Another"}),
        ("GET", AGENTS, None),
        ("POST", f"{AGENTS}/{agent_id}/pause", None),
        ("POST", f"{AGENTS}/{agent_id}/resume", None),
        ("POST", f"{AGENTS}/{agent_id}/revoke", None),
        ("POST", f"{AGENTS}/{agent_id}/keys", {"scopes": [Scope.WALLET_READ]}),
        ("DELETE", f"{AGENTS}/{agent_id}/keys/{key_id}", None),
    ]


# --- creating and listing --------------------------------------------------------------------


async def test_a_new_agent_is_active_and_has_no_key(
    clock: ManualClock, client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    response = await client.post(AGENTS, json={"name": "  Bill payer "}, headers=maria.headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "name", "status", "created_at", "keys"}
    assert (body["name"], body["status"], body["keys"]) == ("Bill payer", "active", [])
    assert body["created_at"] == "2026-01-15T12:00:00Z"


async def test_creating_an_agent_is_audited_with_the_agent_and_its_owner(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(client, maria)

    assert await events(db, "agent.created") == [
        {
            "actor_type": "user",
            "actor_id": maria.id,
            "principal_id": uuid.UUID(maria.id),
            "resource_type": "agent",
            "resource_id": agent["id"],
            "outcome": "success",
            "details": {},
        }
    ]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 101},
        {"name": "two\nlines"},
        {"name": "ok", "status": "revoked"},
        {"name": "ok", "owner_user_id": "01990000-0000-7000-8000-000000000000"},
    ],
)
async def test_an_agent_needs_a_name_and_takes_nothing_else(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database, body: dict[str, Any]
) -> None:
    response = await client.post(AGENTS, json=body, headers=maria.headers)

    assert response.status_code == 422, response.text
    assert await rows(db, "SELECT id FROM agents") == []


async def test_a_user_sees_their_own_agents_newest_first_and_nobody_elses(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    first = await create_agent(client, maria, "First")
    await create_agent(client, joao, "Not Maria's")
    second = await create_agent(client, maria, "Second")

    page = await listed(client, maria)

    assert [agent["id"] for agent in page["items"]] == [second["id"], first["id"]]
    assert page["next_cursor"] is None


async def test_the_list_shows_each_agents_keys_and_never_a_key_or_its_hash(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(client, maria)
    issued = await issue_key(client, maria, agent["id"], Scope.WALLET_READ, Scope.FX_READ)
    (stored,) = await rows(db, "SELECT key_hash FROM agent_keys")

    response = await client.get(AGENTS, headers=maria.headers)

    (shown,) = response.json()["items"][0]["keys"]
    assert shown == {
        "id": issued["id"],
        "agent_id": agent["id"],
        "prefix": issued["prefix"],
        "scopes": ["fx:read", "wallet:read"],
        "expires_at": None,
        "revoked_at": None,
        "last_used_at": None,
        "created_at": issued["created_at"],
    }
    assert issued["key"] not in response.text
    assert issued["key"].split("_", 3)[3] not in response.text
    assert stored["key_hash"] not in response.text


async def test_the_list_is_paged_by_a_cursor_that_is_good_for_its_own_user_only(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    created = [(await create_agent(client, maria, f"Agent {n}"))["id"] for n in range(3)]

    first = await listed(client, maria, limit=2)
    second = await listed(client, maria, limit=2, cursor=first["next_cursor"])
    stolen = await client.get(AGENTS, params={"cursor": first["next_cursor"]}, headers=joao.headers)
    nonsense = await client.get(AGENTS, params={"limit": 0}, headers=maria.headers)

    assert [a["id"] for a in first["items"]] == [created[2], created[1]]
    assert [a["id"] for a in second["items"]] == [created[0]]
    assert second["next_cursor"] is None
    assert_problem(stolen, 422, "invalid_cursor")
    assert nonsense.status_code == 422


# --- pausing, resuming and revoking ----------------------------------------------------------


async def status_after(
    client: httpx.AsyncClient, user: RegisteredUser, agent_id: str, verb: str
) -> httpx.Response:
    return await client.post(f"{AGENTS}/{agent_id}/{verb}", headers=user.headers)


async def test_an_agent_is_paused_and_resumed_and_each_is_audited(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(client, maria)

    paused = await status_after(client, maria, agent["id"], "pause")
    resumed = await status_after(client, maria, agent["id"], "resume")

    assert (paused.status_code, paused.json()["status"]) == (200, "paused")
    assert (resumed.status_code, resumed.json()["status"]) == (200, "active")
    assert paused.json()["id"] == agent["id"]
    for action in ("agent.paused", "agent.resumed"):
        (event,) = await events(db, action)
        assert (event["actor_id"], event["resource_id"]) == (maria.id, agent["id"])
        assert str(event["principal_id"]) == maria.id


@pytest.mark.parametrize(
    ("verbs", "status", "action"),
    [
        (["pause", "pause"], "paused", "agent.paused"),
        (["resume"], "active", "agent.resumed"),
        (["revoke", "revoke"], "revoked", "agent.revoked"),
    ],
)
async def test_asking_for_the_state_an_agent_is_in_changes_nothing_and_is_not_audited_again(
    client: httpx.AsyncClient,
    maria: RegisteredUser,
    db: Database,
    verbs: list[str],
    status: str,
    action: str,
) -> None:
    agent = await create_agent(client, maria)

    responses = [await status_after(client, maria, agent["id"], verb) for verb in verbs]

    assert [(r.status_code, r.json()["status"]) for r in responses] == [(200, status)] * len(verbs)
    assert len(await events(db, action)) == len(verbs) - 1


@pytest.mark.parametrize("verb", ["pause", "resume"])
async def test_a_revoked_agent_stays_revoked(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database, verb: str
) -> None:
    agent = await create_agent(client, maria)
    await act(client, maria, agent["id"], "revoke")

    response = await status_after(client, maria, agent["id"], verb)

    assert_problem(response, 409, "agent_revoked")
    assert await rows(db, "SELECT status FROM agents") == [{"status": "revoked"}]


async def test_a_paused_agent_can_be_revoked(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)
    await act(client, maria, agent["id"], "pause")

    response = await status_after(client, maria, agent["id"], "revoke")

    assert (response.status_code, response.json()["status"]) == (200, "revoked")


async def test_an_agents_keys_are_shown_with_its_status(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)
    issued = await issue_key(client, maria, agent["id"], Scope.WALLET_READ)

    response = await status_after(client, maria, agent["id"], "pause")

    assert [key["id"] for key in response.json()["keys"]] == [issued["id"]]


# --- keys ------------------------------------------------------------------------------------


async def test_a_key_is_shown_once_in_a_response_that_is_not_to_be_stored(
    clock: ManualClock, client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)

    response = await client.post(
        f"{AGENTS}/{agent['id']}/keys",
        json={"scopes": [Scope.TRANSFERS_CREATE, Scope.WALLET_READ, Scope.WALLET_READ]},
        headers=maria.headers,
    )

    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "id",
        "agent_id",
        "prefix",
        "scopes",
        "expires_at",
        "revoked_at",
        "last_used_at",
        "created_at",
        "key",
    }
    assert body["key"].startswith(f"ck_test_{body['prefix']}_")
    assert body["scopes"] == ["transfers:create", "wallet:read"]
    assert (body["agent_id"], body["expires_at"], body["revoked_at"]) == (agent["id"], None, None)


async def test_the_key_is_stored_nowhere_and_logged_nowhere(
    client: httpx.AsyncClient,
    maria: RegisteredUser,
    owner_db: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = await create_agent(client, maria)
    capsys.readouterr()
    issued = await issue_key(client, maria, agent["id"], *EVERY_SCOPE)
    key = issued["key"]
    secret = key.split("_", 3)[3]
    # Used, revoked and used again, so that every path a key takes has been taken.
    await client.get("/v1/wallets", headers=key_headers(key))
    await client.delete(f"{AGENTS}/{agent['id']}/keys/{issued['id']}", headers=maria.headers)
    await client.get("/v1/wallets", headers=key_headers(key))

    logged = capsys.readouterr()
    assert "agent.key_created" in logged.out
    assert secret not in logged.out
    assert secret not in logged.err
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
                {"needle": f"%{secret}%"},
            )
            assert found.scalar_one() == 0, f"the key is stored in {table}"
            searched += 1
        prefix = await session.execute(
            text("SELECT count(*) FROM agent_keys AS t WHERE t::text LIKE :needle"),
            {"needle": f"%{issued['prefix']}%"},
        )
    assert searched > 10
    # The control: the search does find what is stored.
    assert prefix.scalar_one() == 1


async def test_issuing_a_key_is_audited_with_its_id_and_scopes_and_not_the_key(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(client, maria)
    issued = await issue_key(client, maria, agent["id"], Scope.WALLET_READ)

    (event,) = await events(db, "agent.key_created")

    assert (event["actor_type"], event["actor_id"]) == ("user", maria.id)
    assert (str(event["principal_id"]), event["resource_type"], event["resource_id"]) == (
        maria.id,
        "agent",
        agent["id"],
    )
    assert event["details"] == {
        "key_id": issued["id"],
        "scopes": ["wallet:read"],
        "expires_at": None,
    }
    assert issued["key"].split("_", 3)[3] not in json.dumps(event, default=str)


@pytest.mark.parametrize(
    "scopes",
    [
        [],
        ["*"],
        ["wallet:read", "*"],
        ["agents:manage"],
        ["admin"],
        ["wallet:write"],
        ["WALLET:READ"],
        [" wallet:read"],
        [""],
    ],
)
async def test_a_key_is_given_at_least_one_scope_and_only_scopes_an_agent_may_hold(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database, scopes: list[str]
) -> None:
    agent = await create_agent(client, maria)

    response = await client.post(
        f"{AGENTS}/{agent['id']}/keys", json={"scopes": scopes}, headers=maria.headers
    )

    assert_problem(response, 422, "invalid_scopes")
    assert await rows(db, "SELECT id FROM agent_keys") == []
    assert await events(db, "agent.key_created") == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"scopes": "wallet:read"},
        {"scopes": ["wallet:read"] * 33},
        {"scopes": ["x" * 65]},
        {"scopes": ["wallet:read"], "agent_id": "01990000-0000-7000-8000-000000000000"},
        {"scopes": ["wallet:read"], "expires_at": "2027-01-01T00:00:00"},
        {"scopes": ["wallet:read"], "expires_at": "tomorrow"},
    ],
)
async def test_a_malformed_request_for_a_key_is_refused(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database, body: dict[str, Any]
) -> None:
    agent = await create_agent(client, maria)

    response = await client.post(f"{AGENTS}/{agent['id']}/keys", json=body, headers=maria.headers)

    assert response.status_code == 422, response.text
    assert await rows(db, "SELECT id FROM agent_keys") == []


async def test_a_key_may_expire_but_not_in_the_past_or_at_this_instant(
    clock: ManualClock, client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(client, maria)
    url = f"{AGENTS}/{agent['id']}/keys"

    async def ask(expires_at: str) -> httpx.Response:
        body = {"scopes": [Scope.WALLET_READ], "expires_at": expires_at}
        return await client.post(url, json=body, headers=maria.headers)

    now = utcnow()
    past = await ask((now - timedelta(seconds=1)).isoformat())
    at_once = await ask(now.isoformat())
    future = await ask("2026-01-15T09:00:01-03:00")

    assert_problem(past, 422, "invalid_expiry")
    assert_problem(at_once, 422, "invalid_expiry")
    assert future.status_code == 201, future.text
    assert future.json()["expires_at"] == "2026-01-15T12:00:01Z"


async def test_a_paused_agent_can_be_given_a_key_and_a_revoked_one_cannot(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(client, maria)
    url = f"{AGENTS}/{agent['id']}/keys"
    await act(client, maria, agent["id"], "pause")

    while_paused = await client.post(url, json={"scopes": ["wallet:read"]}, headers=maria.headers)
    await act(client, maria, agent["id"], "revoke")
    once_revoked = await client.post(url, json={"scopes": ["wallet:read"]}, headers=maria.headers)

    assert while_paused.status_code == 201, while_paused.text
    assert_problem(once_revoked, 409, "agent_revoked")
    assert len(await rows(db, "SELECT id FROM agent_keys")) == 1


async def test_revoking_a_key_keeps_its_row_and_is_audited(
    clock: ManualClock, client: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(client, maria)
    issued = await issue_key(client, maria, agent["id"], Scope.WALLET_READ)
    clock.advance(minutes=5)

    response = await client.delete(
        f"{AGENTS}/{agent['id']}/keys/{issued['id']}", headers=maria.headers
    )

    assert (response.status_code, response.content) == (204, b"")
    assert await rows(db, "SELECT id::text, revoked_at FROM agent_keys") == [
        {"id": issued["id"], "revoked_at": utcnow()}
    ]
    (event,) = await events(db, "agent.key_revoked")
    assert (event["actor_id"], event["resource_id"]) == (maria.id, agent["id"])
    assert event["details"] == {"key_id": issued["id"]}
    (shown,) = (await listed(client, maria))["items"][0]["keys"]
    assert shown["revoked_at"] == "2026-01-15T12:05:00Z"


async def test_revoking_a_key_again_changes_nothing(
    clock: ManualClock, client: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(client, maria)
    issued = await issue_key(client, maria, agent["id"], Scope.WALLET_READ)
    url = f"{AGENTS}/{agent['id']}/keys/{issued['id']}"
    await client.delete(url, headers=maria.headers)
    first = utcnow()
    clock.advance(minutes=5)

    again = await client.delete(url, headers=maria.headers)

    assert again.status_code == 204
    assert await rows(db, "SELECT revoked_at FROM agent_keys") == [{"revoked_at": first}]
    assert len(await events(db, "agent.key_revoked")) == 1


async def test_a_key_is_revoked_only_under_the_agent_it_belongs_to(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(client, maria)
    other = await create_agent(client, maria, "Other")
    issued = await issue_key(client, maria, agent["id"], Scope.WALLET_READ)

    wrong_agent = await client.delete(
        f"{AGENTS}/{other['id']}/keys/{issued['id']}", headers=maria.headers
    )
    no_such_key = await client.delete(
        f"{AGENTS}/{agent['id']}/keys/{new_id()}", headers=maria.headers
    )

    assert_problem(wrong_agent, 404, "agent_key_not_found")
    assert_problem(no_such_key, 404, "agent_key_not_found")
    assert await rows(db, "SELECT revoked_at FROM agent_keys") == [{"revoked_at": None}]


# --- whose agents they are -------------------------------------------------------------------


async def test_another_users_agent_is_not_found_on_any_route_and_is_left_as_it_was(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(client, maria)
    issued = await issue_key(client, maria, agent["id"], Scope.WALLET_READ)
    by_id = [r for r in management_requests(agent["id"], issued["id"]) if agent["id"] in r[1]]
    assert len(by_id) == 5

    responses = [
        await client.request(method, url, json=body, headers=joao.headers)
        for method, url, body in by_id
    ]
    unknown = [
        await client.request(method, url, json=body, headers=maria.headers)
        for method, url, body in management_requests(str(new_id()), issued["id"])
        if AGENTS + "/" in url
    ]

    for response in responses + unknown:
        assert_problem(response, 404, "agent_not_found")
    assert await rows(db, "SELECT status FROM agents") == [{"status": "active"}]
    assert await rows(db, "SELECT revoked_at FROM agent_keys") == [{"revoked_at": None}]
    assert (await listed(client, joao))["items"] == []


async def test_an_agent_id_that_is_not_an_id_is_a_422(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    response = await client.post(f"{AGENTS}/not-an-id/pause", headers=maria.headers)

    assert response.status_code == 422


async def test_every_agent_route_needs_a_credential(client: httpx.AsyncClient) -> None:
    for method, url, body in management_requests(str(new_id()), str(new_id())):
        response = await client.request(method, url, json=body)

        assert_problem(response, 401, "unauthenticated")


async def test_an_agents_key_is_refused_on_every_agent_route_whatever_its_scopes(
    client: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(client, maria)
    issued = await issue_key(client, maria, agent["id"], *EVERY_SCOPE)
    # The control: the key is a good one.
    assert (await client.get("/v1/wallets", headers=key_headers(issued["key"]))).status_code == 200

    for method, url, body in management_requests(agent["id"], issued["id"]):
        response = await client.request(method, url, json=body, headers=key_headers(issued["key"]))

        assert_problem(response, 403, "insufficient_scope")
    assert await rows(db, "SELECT status FROM agents") == [{"status": "active"}]
    assert len(await rows(db, "SELECT id FROM agent_keys WHERE revoked_at IS NULL")) == 1


async def test_a_key_cannot_read_who_its_owner_is_or_end_a_session(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    # Neither route names a scope, so neither is open to an agent.
    key = await agent_key(client, maria, *EVERY_SCOPE)

    me = await client.get("/v1/me", headers=key_headers(key))
    logout = await client.post("/v1/auth/logout", headers=key_headers(key))

    assert_problem(me, 403, "insufficient_scope")
    assert_problem(logout, 403, "insufficient_scope")
    assert (await client.get("/v1/me", headers=maria.headers)).status_code == 200

"""Builders for the agent tests: an agent and a key in a line each, over HTTP."""

from datetime import datetime
from typing import Any, Final

import httpx
from sqlalchemy import text

from corridor.platform.db import Database
from tests.support.auth import RegisteredUser, bearer

# The key that agent keys are hashed under in these tests. It protects nothing.
HASH_KEY: Final = "agents-test-hash-key-0123456789abcdef"  # pragma: allowlist secret
OTHER_HASH_KEY: Final = "agents-test-other-key-0123456789abcdef"  # pragma: allowlist secret

AGENTS: Final = "/v1/agents"


async def create_agent(
    client: httpx.AsyncClient, user: RegisteredUser, name: str = "Bill payer"
) -> dict[str, Any]:
    response = await client.post(AGENTS, json={"name": name}, headers=user.headers)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def issue_key(
    client: httpx.AsyncClient,
    user: RegisteredUser,
    agent_id: str,
    *scopes: str,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    """Issue a key over HTTP. The answer holds the key itself under ``key``."""
    body: dict[str, Any] = {"scopes": list(scopes)}
    if expires_at is not None:
        body["expires_at"] = expires_at.isoformat()
    response = await client.post(f"{AGENTS}/{agent_id}/keys", json=body, headers=user.headers)
    assert response.status_code == 201, response.text
    issued: dict[str, Any] = response.json()
    return issued


async def agent_key(client: httpx.AsyncClient, user: RegisteredUser, *scopes: str) -> str:
    """A working key of a new agent of ``user`` with exactly these scopes."""
    agent = await create_agent(client, user)
    return str((await issue_key(client, user, agent["id"], *scopes))["key"])


async def act(client: httpx.AsyncClient, user: RegisteredUser, agent_id: str, verb: str) -> None:
    response = await client.post(f"{AGENTS}/{agent_id}/{verb}", headers=user.headers)
    assert response.status_code == 200, response.text


def key_headers(key: str) -> dict[str, str]:
    return bearer(key)


async def rows(db: Database, query: str, **parameters: object) -> list[dict[str, Any]]:
    async with db.transaction() as session:
        found = await session.execute(text(query), parameters)
        return [dict(row) for row in found.mappings()]


async def events(db: Database, action: str) -> list[dict[str, Any]]:
    """The audit events of one action, oldest first."""
    return await rows(
        db,
        "SELECT actor_type, actor_id, principal_id, resource_type, resource_id, outcome, details"
        " FROM audit_events WHERE action = :action ORDER BY id",
        action=action,
    )

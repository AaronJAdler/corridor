"""Builders for the agent tests: an agent, a key, a policy and a payment in a line each,
over HTTP."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import httpx
from sqlalchemy import text

from corridor import wallets
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.identity import Principal
from corridor.platform.clock import utcnow
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from tests.support import ledger as ledger_support
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


# --- policies and money ----------------------------------------------------------------------

TRANSFERS: Final = "/v1/transfers"
WITHDRAWALS: Final = "/v1/withdrawals"
APPROVALS: Final = "/v1/approvals"

# A policy that sets nothing: no cap, no threshold, and nobody the agent may pay.
NO_LIMITS: Final[dict[str, Any]] = {
    "per_tx_usd": None,
    "daily_usd": None,
    "approval_threshold_usd": None,
}


def user_recipient(user: RegisteredUser) -> dict[str, str]:
    return {"kind": "user", "id": user.id}


async def put_policy(
    client: httpx.AsyncClient, user: RegisteredUser, agent_id: str, **policy: Any
) -> httpx.Response:
    return await client.put(
        f"{AGENTS}/{agent_id}/policy", json={**NO_LIMITS, **policy}, headers=user.headers
    )


async def set_policy(
    client: httpx.AsyncClient, user: RegisteredUser, agent_id: str, **policy: Any
) -> dict[str, Any]:
    """Replace an agent's policy over HTTP. What is not named is not limited, and nobody
    is allowed who is not named."""
    response = await put_policy(client, user, agent_id, **policy)
    assert response.status_code == 200, response.text
    stored: dict[str, Any] = response.json()
    return stored


@dataclass(frozen=True)
class Acting:
    """An agent and the headers of one of its keys."""

    id: str
    headers: dict[str, str]


async def an_agent(
    client: httpx.AsyncClient, user: RegisteredUser, *scopes: str, **policy: Any
) -> Acting:
    """A new agent of ``user`` with a key of these scopes and, if any of it is given, a
    policy. With none, the agent is as its owner first finds it: able to pay nobody."""
    agent = await create_agent(client, user)
    issued = await issue_key(client, user, agent["id"], *scopes)
    if policy:
        await set_policy(client, user, agent["id"], **policy)
    return Acting(id=agent["id"], headers=key_headers(issued["key"]))


async def give_open_policy(db: Database, agent: Principal) -> None:
    """Let a made-up agent pay anyone, for a test that puts a principal in place of a key.

    Such an agent has no row, and so could have no policy and pay nobody. This writes the
    row and a policy with no limits, which is what a test about something else wants.
    """
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO agents (id, owner_user_id, name, status, created_at)"
                " VALUES (:id, :owner, 'Stand-in', 'active', :now)"
            ),
            {"id": agent.actor_id, "owner": agent.user_id, "now": utcnow()},
        )
        await session.execute(
            text(
                "INSERT INTO agent_policies (agent_id, per_tx_usd, daily_usd,"
                " approval_threshold_usd, any_recipient, updated_at)"
                " VALUES (:id, NULL, NULL, NULL, true, :now)"
            ),
            {"id": agent.actor_id, "now": utcnow()},
        )


async def fund(db: Database, user: RegisteredUser, amount: int, asset: str = "USD") -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), asset)
        await ledger_support.fund(session, wallet.available_account_id, amount, asset)


async def available(client: httpx.AsyncClient, user: RegisteredUser, asset: str = "USD") -> str:
    response = await client.get("/v1/wallets", headers=user.headers)
    assert response.status_code == 200, response.text
    return next(str(w["available"]) for w in response.json()["wallets"] if w["asset"] == asset)


async def send(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    recipient: RegisteredUser | str,
    amount: str,
    *,
    key: str | None = None,
    asset: str = "USD",
) -> httpx.Response:
    """Ask for a transfer with these credentials, under a key of its own unless one is given."""
    to = recipient if isinstance(recipient, str) else recipient.id
    return await client.post(
        TRANSFERS,
        json={"recipient": to, "asset": asset, "amount": amount},
        headers={**headers, "Idempotency-Key": key or f"send-{new_id()}"},
    )


async def withdraw(
    client: httpx.AsyncClient, headers: dict[str, str], body: dict[str, Any]
) -> httpx.Response:
    return await client.post(
        WITHDRAWALS, json=body, headers={**headers, "Idempotency-Key": f"out-{new_id()}"}
    )


async def count(db: Database, table: str) -> int:
    return int((await rows(db, f"SELECT count(*) AS n FROM {table}"))[0]["n"])  # noqa: S608


def assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == code

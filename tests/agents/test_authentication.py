"""An agent's key as a credential: whom it authenticates, and every way it is refused."""

import hmac
import json
import uuid
from datetime import timedelta
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from pydantic import SecretStr
from sqlalchemy import text

from corridor import agents, identity
from corridor.agents import service
from corridor.api.app import create_app
from corridor.api.deps import require
from corridor.identity import Principal, Scope
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from tests.agents.support import (
    AGENTS,
    OTHER_HASH_KEY,
    act,
    agent_key,
    create_agent,
    issue_key,
    key_headers,
    rows,
)
from tests.support.auth import RegisteredUser

PROBE = "/probe/whoami"


async def whoami(
    principal: Annotated[Principal, Depends(require(Scope.WALLET_READ))],
) -> dict[str, Any]:
    return {
        "user_id": str(principal.user_id),
        "actor_type": principal.actor_type,
        "actor_id": str(principal.actor_id),
        "agent_id": None if principal.agent_id is None else str(principal.agent_id),
        "role": principal.role,
        "scopes": sorted(principal.scopes),
        "session_id": principal.session_id,
        "is_admin": principal.is_admin,
    }


@pytest.fixture
async def probe(app: FastAPI, client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The API with one more route, which answers with the principal it was given."""
    app.add_api_route(PROBE, whoami)
    return client


async def present(client: httpx.AsyncClient, key: str) -> httpx.Response:
    return await client.get(PROBE, headers=key_headers(key))


def refusal(response: httpx.Response) -> dict[str, Any]:
    """A problem document less the one member that differs between any two requests."""
    body: dict[str, Any] = response.json()
    body.pop("request_id")
    return body


NOT_ACCEPTED = {
    "type": "https://corridor.example/problems/unauthenticated",
    "title": "Authentication required",
    "status": 401,
    "code": "unauthenticated",
    "detail": "This credential is not accepted.",
}


def assert_not_accepted(response: httpx.Response) -> None:
    assert response.status_code == 401, response.text
    assert refusal(response) == NOT_ACCEPTED
    assert response.headers["www-authenticate"] == "Bearer"


def with_another_secret(key: str) -> str:
    mark, environment, prefix, secret = key.split("_", 3)
    return "_".join((mark, environment, prefix, secret[::-1]))


def with_another_prefix(key: str) -> str:
    mark, environment, prefix, secret = key.split("_", 3)
    return "_".join((mark, environment, prefix[::-1], secret))


async def test_a_key_authenticates_its_agent_acting_for_the_owner_with_the_keys_scopes(
    probe: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(probe, maria)
    issued = await issue_key(probe, maria, agent["id"], Scope.WALLET_READ, Scope.FX_READ)

    response = await present(probe, issued["key"])

    assert response.status_code == 200, response.text
    assert response.json() == {
        "user_id": maria.id,
        "actor_type": "agent",
        "actor_id": agent["id"],
        "agent_id": agent["id"],
        "role": "user",
        "scopes": ["fx:read", "wallet:read"],
        "session_id": None,
        "is_admin": False,
    }


async def test_the_agent_is_bound_to_the_log_context_of_the_request(
    probe: httpx.AsyncClient, maria: RegisteredUser, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = await create_agent(probe, maria)
    issued = await issue_key(probe, maria, agent["id"], Scope.WALLET_READ)
    capsys.readouterr()

    await present(probe, issued["key"])

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    (request,) = [line for line in lines if line["event"] == "http.request"]
    assert (request["principal_id"], request["actor_type"], request["actor_id"]) == (
        maria.id,
        "agent",
        agent["id"],
    )


async def test_a_wrong_secret_and_an_unknown_prefix_get_one_and_the_same_refusal(
    probe: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    key = await agent_key(probe, maria, Scope.WALLET_READ)

    wrong_secret = await present(probe, with_another_secret(key))
    unknown_prefix = await present(probe, with_another_prefix(key))
    malformed = await present(probe, "ck_test_tooshort")

    # The control: the key these were made from is accepted.
    assert (await present(probe, key)).status_code == 200
    for response in (wrong_secret, unknown_prefix, malformed):
        assert_not_accepted(response)
    assert dict(wrong_secret.headers).keys() == dict(unknown_prefix.headers).keys()


async def test_an_unknown_prefix_is_still_compared_once_with_a_digest_that_matches_nothing(
    probe: httpx.AsyncClient,
    maria: RegisteredUser,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = await agent_key(probe, maria, Scope.WALLET_READ)
    compared: list[tuple[str, str]] = []

    def recording(stored: str, presented: str) -> bool:
        compared.append((stored, presented))
        return hmac.compare_digest(stored, presented)

    monkeypatch.setattr(service, "compare_digest", recording)

    unknown = with_another_prefix(key)
    await present(probe, unknown)
    await present(probe, key)

    presented = agents.read_key(unknown, settings)
    assert presented is not None
    # Both requests did the same work: one comparison of a 64-character digest each.
    (for_unknown, for_known) = compared
    assert for_unknown == ("0" * 64, presented.digest)
    assert for_known[0] == for_known[1] != "0" * 64


async def test_a_key_that_matches_the_digest_of_no_key_is_never_accepted(
    probe: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whatever the comparison says, a prefix that names no key authenticates nobody.
    monkeypatch.setattr(service, "compare_digest", lambda stored, presented: True)

    assert_not_accepted(await present(probe, "ck_test_abcdefghijkl_" + "S" * 43))


async def test_a_revoked_key_is_refused_and_its_agents_other_key_still_works(
    probe: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(probe, maria)
    revoked = await issue_key(probe, maria, agent["id"], Scope.WALLET_READ)
    kept = await issue_key(probe, maria, agent["id"], Scope.WALLET_READ)
    assert (await present(probe, revoked["key"])).status_code == 200

    deleted = await probe.delete(
        f"{AGENTS}/{agent['id']}/keys/{revoked['id']}", headers=maria.headers
    )

    assert deleted.status_code == 204, deleted.text
    assert_not_accepted(await present(probe, revoked["key"]))
    assert (await present(probe, kept["key"])).status_code == 200


async def test_a_paused_agents_key_is_refused_until_the_agent_is_resumed(
    probe: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(probe, maria)
    key = (await issue_key(probe, maria, agent["id"], Scope.WALLET_READ))["key"]

    await act(probe, maria, agent["id"], "pause")
    while_paused = await present(probe, key)
    await act(probe, maria, agent["id"], "resume")
    resumed = await present(probe, key)

    assert_not_accepted(while_paused)
    assert resumed.status_code == 200


async def test_a_revoked_agents_key_is_refused(
    probe: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(probe, maria)
    key = (await issue_key(probe, maria, agent["id"], Scope.WALLET_READ))["key"]
    assert (await present(probe, key)).status_code == 200

    await act(probe, maria, agent["id"], "revoke")

    assert_not_accepted(await present(probe, key))


async def test_a_key_works_until_the_instant_it_expires_and_not_at_it(
    clock: ManualClock, probe: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    agent = await create_agent(probe, maria)
    expires_at = utcnow() + timedelta(hours=1)
    key = (await issue_key(probe, maria, agent["id"], Scope.WALLET_READ, expires_at=expires_at))[
        "key"
    ]

    clock.advance(seconds=3599)
    just_before = await present(probe, key)
    clock.advance(seconds=1)
    at_expiry = await present(probe, key)
    clock.advance(hours=24)
    long_after = await present(probe, key)

    assert just_before.status_code == 200
    assert_not_accepted(at_expiry)
    assert_not_accepted(long_after)


async def test_a_key_without_an_expiry_goes_on_working(
    clock: ManualClock, probe: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    key = await agent_key(probe, maria, Scope.WALLET_READ)

    clock.advance(hours=24 * 365)

    assert (await present(probe, key)).status_code == 200


async def test_the_key_of_a_restricted_owner_is_refused_until_the_restriction_is_lifted(
    probe: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    key = await agent_key(probe, maria, Scope.WALLET_READ)

    async with db.transaction() as session:
        await identity.restrict_user(session, uuid.UUID(maria.id), "under review")
    restricted = await present(probe, key)
    async with db.transaction() as session:
        await identity.lift_restriction(session, uuid.UUID(maria.id))
    lifted = await present(probe, key)

    assert_not_accepted(restricted)
    assert lifted.status_code == 200


async def test_the_key_of_a_closed_owner_is_refused(
    probe: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    key = await agent_key(probe, maria, Scope.WALLET_READ)

    async with db.transaction() as session:
        await session.execute(
            text("UPDATE users SET status = 'closed' WHERE id = :id"), {"id": maria.id}
        )

    assert_not_accepted(await present(probe, key))


async def test_every_refusal_of_a_genuine_key_reads_like_the_refusal_of_a_forged_one(
    clock: ManualClock, probe: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    paused_agent = await create_agent(probe, maria)
    paused = (await issue_key(probe, maria, paused_agent["id"], Scope.WALLET_READ))["key"]
    await act(probe, maria, paused_agent["id"], "pause")
    revoked_agent = await create_agent(probe, joao)
    revoked = (await issue_key(probe, joao, revoked_agent["id"], Scope.WALLET_READ))["key"]
    await act(probe, joao, revoked_agent["id"], "revoke")
    expiring_agent = await create_agent(probe, maria)
    expired = (
        await issue_key(
            probe,
            maria,
            expiring_agent["id"],
            Scope.WALLET_READ,
            expires_at=utcnow() + timedelta(seconds=30),
        )
    )["key"]
    clock.advance(seconds=31)

    refusals = [
        refusal(await present(probe, key))
        for key in (paused, revoked, expired, with_another_secret(paused))
    ]

    assert refusals == [NOT_ACCEPTED] * 4


async def test_why_a_key_was_refused_is_logged_without_the_key(
    probe: httpx.AsyncClient, maria: RegisteredUser, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = await create_agent(probe, maria)
    key = (await issue_key(probe, maria, agent["id"], Scope.WALLET_READ))["key"]
    await act(probe, maria, agent["id"], "pause")
    capsys.readouterr()

    await present(probe, key)
    await present(probe, with_another_secret(key))

    logged = capsys.readouterr().out
    refused = [
        line
        for line in map(json.loads, logged.splitlines())
        if line["event"] == "auth.api_key_refused"
    ]
    assert [(line["reason"], line["agent_id"]) for line in refused] == [
        ("agent_paused", agent["id"]),
        ("unknown_key", None),
    ]
    assert key.split("_", 3)[3] not in logged
    assert key.split("_", 3)[3][::-1] not in logged


# --- when a key was last used ----------------------------------------------------------------


async def last_used(db: Database, key_id: str) -> Any:
    (row,) = await rows(db, "SELECT last_used_at FROM agent_keys WHERE id = :id", id=key_id)
    return row["last_used_at"]


async def test_using_a_key_records_when_and_at_most_once_a_minute(
    clock: ManualClock, probe: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(probe, maria)
    issued = await issue_key(probe, maria, agent["id"], Scope.WALLET_READ)
    start = utcnow()
    assert await last_used(db, issued["id"]) is None

    await present(probe, issued["key"])
    first = await last_used(db, issued["id"])
    clock.advance(seconds=59)
    await present(probe, issued["key"])
    within_the_minute = await last_used(db, issued["id"])
    clock.advance(seconds=1)
    await present(probe, issued["key"])
    a_minute_on = await last_used(db, issued["id"])

    assert (first, within_the_minute) == (start, start)
    assert a_minute_on == start + timedelta(seconds=60)


async def test_a_refused_key_is_not_recorded_as_used(
    probe: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    agent = await create_agent(probe, maria)
    issued = await issue_key(probe, maria, agent["id"], Scope.WALLET_READ)
    await act(probe, maria, agent["id"], "pause")

    await present(probe, issued["key"])
    await present(probe, with_another_secret(issued["key"]))

    assert await last_used(db, issued["id"]) is None


async def test_the_use_of_a_key_is_recorded_even_if_the_request_it_made_is_refused(
    clock: ManualClock, probe: httpx.AsyncClient, maria: RegisteredUser, db: Database
) -> None:
    # It is committed before the handler runs, on its own, so it does not share the fate
    # of the request's transaction or hold a lock while the request takes its own.
    agent = await create_agent(probe, maria)
    issued = await issue_key(probe, maria, agent["id"], Scope.FX_READ)

    response = await present(probe, issued["key"])

    assert response.status_code == 403
    assert await last_used(db, issued["id"]) == utcnow()


# --- what a key can never carry --------------------------------------------------------------


async def test_a_scope_no_agent_may_hold_is_dropped_even_if_it_is_in_the_keys_row(
    probe: httpx.AsyncClient, maria: RegisteredUser, superuser_db: Database
) -> None:
    agent = await create_agent(probe, maria)
    issued = await issue_key(probe, maria, agent["id"], Scope.WALLET_READ)
    # Nothing in the application can write this. It is written here the way a mistake in
    # a later migration, or someone with the owner's password, might.
    async with superuser_db.transaction() as session:
        await session.execute(
            text("UPDATE agent_keys SET scopes = :scopes WHERE id = :id"),
            {"scopes": ["wallet:read", "agents:manage", "admin"], "id": issued["id"]},
        )

    response = await present(probe, issued["key"])

    assert response.json()["scopes"] == ["wallet:read"]


def test_the_scopes_an_agent_may_hold_are_exactly_these() -> None:
    # Written out in full: opening a scope to agents is a decision that shows up here.
    assert {
        "wallet:read",
        "transfers:read",
        "transfers:create",
        "deposits:read",
        "withdrawals:read",
        "withdrawals:create",
        "beneficiaries:read",
        "beneficiaries:write",
        "fx:read",
        "fx:convert",
    } == agents.AGENT_SCOPES
    assert "*" not in agents.AGENT_SCOPES


# --- a deployment without a hash key ---------------------------------------------------------


async def test_without_a_hash_key_no_key_is_accepted_and_none_can_be_issued(
    probe: httpx.AsyncClient, maria: RegisteredUser, settings: Settings
) -> None:
    agent = await create_agent(probe, maria)
    key = (await issue_key(probe, maria, agent["id"], Scope.WALLET_READ))["key"]
    unconfigured = create_app(settings.model_copy(update={"api_key_hash_key": None}))
    unconfigured.add_api_route(PROBE, whoami)

    async with unconfigured.router.lifespan_context(unconfigured):
        transport = httpx.ASGITransport(app=unconfigured, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://corridor.test") as http:
            presented = await present(http, key)
            issuing = await http.post(
                f"{AGENTS}/{agent['id']}/keys",
                json={"scopes": [Scope.WALLET_READ]},
                headers=maria.headers,
            )

    assert_not_accepted(presented)
    assert (issuing.status_code, issuing.json()["code"]) == (503, "agent_keys_unavailable")


async def test_a_key_hashed_under_another_server_key_is_refused(
    probe: httpx.AsyncClient, maria: RegisteredUser, settings: Settings
) -> None:
    key = await agent_key(probe, maria, Scope.WALLET_READ)
    rekeyed = create_app(
        settings.model_copy(update={"api_key_hash_key": SecretStr(OTHER_HASH_KEY)})
    )
    rekeyed.add_api_route(PROBE, whoami)

    async with rekeyed.router.lifespan_context(rekeyed):
        transport = httpx.ASGITransport(app=rekeyed, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://corridor.test") as http:
            assert_not_accepted(await present(http, key))

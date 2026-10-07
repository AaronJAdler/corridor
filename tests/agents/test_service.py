"""The agents service, called directly: what it checks for itself, whoever calls it."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import agents, identity
from corridor.agents import keys, service
from corridor.identity import InsufficientScope, Principal, Scope
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from tests.agents.support import events, rows

# Not the hash of a password: these users never log in.
NO_PASSWORD = "not-a-hash"  # pragma: allowlist secret


async def add_user(db: Database, name: str) -> identity.User:
    async with db.transaction() as session:
        return await identity.register(
            session,
            email=f"{name}@example.com",
            handle=name,
            display_name=name.title(),
            password_hash=NO_PASSWORD,
        )


def acting_as(user: identity.User) -> Principal:
    return Principal.for_user(user.id, user.role, new_id())


@pytest.fixture
async def maria(db: Database) -> identity.User:
    return await add_user(db, "maria")


@pytest.fixture
async def agent(db: Database, maria: identity.User) -> agents.Agent:
    async with db.transaction() as session:
        return await agents.create_agent(session, acting_as(maria), name="Bill payer")


async def issue(
    db: Database,
    settings: Settings,
    owner: identity.User,
    agent_id: uuid.UUID,
    *scopes: str,
    expires_at: datetime | None = None,
) -> agents.IssuedKey:
    async with db.transaction() as session:
        return await agents.issue_key(
            session,
            acting_as(owner),
            agent_id,
            scopes=scopes or [Scope.WALLET_READ],
            expires_at=expires_at,
            settings=settings,
        )


async def presenting(db: Database, settings: Settings, key: str) -> agents.KeyOutcome:
    presented = agents.read_key(key, settings)
    assert presented is not None
    async with db.transaction() as session:
        return await agents.authenticate(session, presented)


# --- the owner's own session, checked here too -----------------------------------------------


async def test_no_agent_can_manage_agents_even_past_the_route_guard(
    db: Database, settings: Settings, maria: identity.User, agent: agents.Agent
) -> None:
    issued = await issue(db, settings, maria, agent.id)
    # An agent of the same owner, holding every scope there is.
    itself = Principal.for_agent(maria.id, agent.id, [str(scope) for scope in Scope])
    attempts: list[Callable[[AsyncSession], Awaitable[Any]]] = [
        lambda s: agents.create_agent(s, itself, name="A helper"),
        lambda s: agents.list_agents(s, itself),
        lambda s: agents.pause_agent(s, itself, agent.id),
        lambda s: agents.resume_agent(s, itself, agent.id),
        lambda s: agents.revoke_agent(s, itself, agent.id),
        lambda s: agents.issue_key(
            s, itself, agent.id, scopes=[Scope.TRANSFERS_CREATE], settings=settings
        ),
        lambda s: agents.revoke_key(s, itself, agent.id, issued.details.id),
    ]

    for attempt in attempts:
        with pytest.raises(InsufficientScope):
            async with db.transaction() as session:
                await attempt(session)

    assert await rows(db, "SELECT status FROM agents") == [{"status": "active"}]
    assert await rows(db, "SELECT revoked_at FROM agent_keys") == [{"revoked_at": None}]


async def test_the_service_refuses_an_expiry_that_does_not_say_its_timezone(
    clock: ManualClock, db: Database, settings: Settings, maria: identity.User, agent: agents.Agent
) -> None:
    naive = utcnow().replace(tzinfo=None) + timedelta(days=1)

    with pytest.raises(agents.InvalidExpiry):
        await issue(db, settings, maria, agent.id, expires_at=naive)

    assert await rows(db, "SELECT id FROM agent_keys") == []


async def test_what_issuing_returns_holds_the_key_and_does_not_print_it(
    db: Database, settings: Settings, maria: identity.User, agent: agents.Agent
) -> None:
    issued = await issue(db, settings, maria, agent.id)

    assert issued.key.startswith(f"ck_test_{issued.details.prefix}_")
    assert issued.key not in repr(issued)
    assert issued.key.split("_", 3)[3] not in f"{issued} {issued.details}"


# --- drawing a prefix ------------------------------------------------------------------------


# The real generator, held here because the tests below put another in its place.
generate_key = keys.generate


def drawing(prefixes: list[str], settings: Settings) -> Callable[[Settings], keys.GeneratedKey]:
    """A generator of real keys whose prefixes are these, in turn, and then random."""
    remaining = list(prefixes)

    def generate(_: Settings) -> keys.GeneratedKey:
        real = generate_key(settings)
        if not remaining:
            return real
        prefix = remaining.pop(0)
        mark, environment, _, secret = real.key.split("_", 3)
        return keys.GeneratedKey(
            key=f"{mark}_{environment}_{prefix}_{secret}", prefix=prefix, key_hash=real.key_hash
        )

    return generate


async def test_a_key_whose_prefix_is_taken_is_drawn_again(
    db: Database,
    settings: Settings,
    maria: identity.User,
    agent: agents.Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taken = "abcdefghijkl"
    monkeypatch.setattr(service.keys, "generate", drawing([taken, taken, taken], settings))
    first = await issue(db, settings, maria, agent.id)

    second = await issue(db, settings, maria, agent.id)

    assert (first.details.prefix, second.details.prefix != taken) == (taken, True)
    # Both keys work, and each is its own.
    outcomes = [await presenting(db, settings, issued.key) for issued in (first, second)]
    assert [outcome.key_id for outcome in outcomes] == [first.details.id, second.details.id]
    assert len(await events(db, "agent.key_created")) == 2


async def test_issuing_fails_loudly_and_stores_nothing_if_no_free_prefix_is_drawn(
    db: Database,
    settings: Settings,
    maria: identity.User,
    agent: agents.Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taken = "abcdefghijkl"
    monkeypatch.setattr(service.keys, "generate", drawing([taken] * 4, settings))
    await issue(db, settings, maria, agent.id)

    with pytest.raises(RuntimeError, match="prefix"):
        await issue(db, settings, maria, agent.id)

    assert len(await rows(db, "SELECT id FROM agent_keys")) == 1
    assert len(await events(db, "agent.key_created")) == 1


# --- telling whose a key is ------------------------------------------------------------------


async def test_a_good_key_is_accepted_as_its_agent_with_its_scopes(
    db: Database, settings: Settings, maria: identity.User, agent: agents.Agent
) -> None:
    issued = await issue(db, settings, maria, agent.id, Scope.WALLET_READ, Scope.FX_CONVERT)

    outcome = await presenting(db, settings, issued.key)

    assert outcome == agents.KeyOutcome(
        reason=None,
        agent_id=agent.id,
        key_id=issued.details.id,
        owner_user_id=maria.id,
        scopes=frozenset({"wallet:read", "fx:convert"}),
    )


async def test_each_refusal_says_why_and_a_forgery_names_no_agent(
    clock: ManualClock, db: Database, settings: Settings, maria: identity.User
) -> None:
    owner = acting_as(maria)

    async def agent_with_key(expires_at: datetime | None = None) -> tuple[uuid.UUID, str]:
        async with db.transaction() as session:
            made = await agents.create_agent(session, owner, name="Bill payer")
        return made.id, (await issue(db, settings, maria, made.id, expires_at=expires_at)).key

    revoked_key_agent, revoked_key = await agent_with_key()
    expiring_agent, expired_key = await agent_with_key(utcnow() + timedelta(minutes=1))
    paused_agent, paused_key = await agent_with_key()
    revoked_agent, revoked_agents_key = await agent_with_key()
    async with db.transaction() as session:
        (key_id,) = (
            await session.execute(
                text("SELECT id FROM agent_keys WHERE agent_id = :id"), {"id": revoked_key_agent}
            )
        ).scalars()
        await agents.revoke_key(session, owner, revoked_key_agent, key_id)
        await agents.pause_agent(session, owner, paused_agent)
        await agents.revoke_agent(session, owner, revoked_agent)
    clock.advance(minutes=1)
    mark, environment, prefix, secret = paused_key.split("_", 3)
    forged = f"{mark}_{environment}_{prefix}_{secret[::-1]}"

    outcomes = [
        await presenting(db, settings, key)
        for key in (revoked_key, expired_key, paused_key, revoked_agents_key, forged)
    ]

    assert [(outcome.reason, outcome.agent_id) for outcome in outcomes] == [
        ("key_revoked", revoked_key_agent),
        ("key_expired", expiring_agent),
        ("agent_paused", paused_agent),
        ("agent_revoked", revoked_agent),
        ("unknown_key", None),
    ]
    assert all(outcome.scopes == frozenset() for outcome in outcomes)


async def test_a_key_whose_owner_does_not_exist_is_refused(
    db: Database, settings: Settings
) -> None:
    # No user was ever registered with this id.
    nobody = Principal.for_user(new_id(), "user", new_id())
    async with db.transaction() as session:
        orphan = await agents.create_agent(session, nobody, name="Orphan")
        issued = await agents.issue_key(
            session, nobody, orphan.id, scopes=[Scope.WALLET_READ], settings=settings
        )

    outcome = await presenting(db, settings, issued.key)

    assert outcome.reason == "owner_not_active"


async def test_a_key_used_many_times_at_once_is_accepted_each_time(
    clock: ManualClock, db: Database, settings: Settings, maria: identity.User, agent: agents.Agent
) -> None:
    issued = await issue(db, settings, maria, agent.id)

    outcomes = await asyncio.gather(*(presenting(db, settings, issued.key) for _ in range(12)))

    assert [outcome.reason for outcome in outcomes] == [None] * 12
    assert await rows(db, "SELECT last_used_at FROM agent_keys") == [{"last_used_at": utcnow()}]

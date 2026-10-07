"""Agents and their keys: creating them, stopping them, and telling whose a key is.

Every function takes the caller's session and runs inside the caller's transaction.
Nothing here commits. What changes an agent is its owner's alone to do, from their own
session, and each function checks that for itself: the route guard is not the only gate.
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Any, Final, cast

from sqlalchemy import RowMapping, Table, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity
from corridor.agents import keys
from corridor.agents.errors import (
    AgentKeyNotFound,
    AgentKeysUnavailable,
    AgentNotFound,
    AgentRevoked,
    InvalidExpiry,
    InvalidScopes,
)
from corridor.agents.models import AgentKeyRow, AgentRow
from corridor.agents.types import (
    Agent,
    AgentKey,
    AgentStatus,
    IssuedKey,
    KeyOutcome,
    KeyRefusal,
    PresentedKey,
)
from corridor.identity import Principal
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.ids import new_id
from corridor.platform.pagination import (
    DEFAULT_LIMIT,
    InvalidCursor,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)

_agents = cast(Table, AgentRow.__table__)
_keys = cast(Table, AgentKeyRow.__table__)

CURSOR_KIND: Final = "agents"

# What a key that names no row is compared with. No digest is all zeros, so nothing ever
# matches it; it is there so that an unknown prefix costs the comparison a known one costs.
_NO_SUCH_DIGEST: Final = "0" * 64

# A key in steady use is not written to on every request: its last use is recorded to the
# minute.
_LAST_USED_RESOLUTION: Final = timedelta(minutes=1)

# A prefix has 36**12 values, so a collision is not expected in the life of the system.
# This is how often a new key is drawn before that is taken for a fault.
_PREFIX_ATTEMPTS: Final = 3

_REFUSED_BY_STATUS: Final[dict[str, KeyRefusal]] = {
    "paused": "agent_paused",
    "revoked": "agent_revoked",
}


# --- agents ----------------------------------------------------------------------------------


async def create_agent(session: AsyncSession, principal: Principal, *, name: str) -> Agent:
    """Create an agent owned by the principal's user. It has no key yet, so it can do nothing."""
    identity.require_user_session(principal)
    agent_id = new_id()
    row = {
        "id": agent_id,
        "owner_user_id": principal.user_id,
        "name": name,
        "status": "active",
        "created_at": utcnow(),
    }
    await session.execute(insert(_agents).values(row))
    await _record(session, principal, "agent.created", agent_id)
    return _agent(row, ())


async def list_agents(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[Agent]:
    """One page of the agents of the principal's user, newest first, each with its keys."""
    identity.require_user_session(principal)
    limit = clamp_limit(limit)
    scope = str(principal.user_id)
    query = select(_agents).where(_agents.c.owner_user_id == principal.user_id)
    if cursor is not None:
        query = query.where(_agents.c.id < _position_of(cursor, scope))
    rows = await session.execute(query.order_by(_agents.c.id.desc()).limit(limit + 1))
    found = list(rows.mappings())
    shown = found[:limit]
    keys_of = await _keys_of(session, [row["id"] for row in shown])
    return Page(
        items=tuple(_agent(row, keys_of.get(row["id"], ())) for row in shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=scope, position=str(shown[-1]["id"]))
            if len(found) > limit
            else None
        ),
    )


async def pause_agent(session: AsyncSession, principal: Principal, agent_id: uuid.UUID) -> Agent:
    """Stop an agent's keys from working until it is resumed."""
    return await _set_status(session, principal, agent_id, "paused", action="agent.paused")


async def resume_agent(session: AsyncSession, principal: Principal, agent_id: uuid.UUID) -> Agent:
    return await _set_status(session, principal, agent_id, "active", action="agent.resumed")


async def revoke_agent(session: AsyncSession, principal: Principal, agent_id: uuid.UUID) -> Agent:
    """Stop an agent for good. Its row and its keys' rows are kept."""
    return await _set_status(session, principal, agent_id, "revoked", action="agent.revoked")


async def _set_status(
    session: AsyncSession,
    principal: Principal,
    agent_id: uuid.UUID,
    status: AgentStatus,
    *,
    action: str,
) -> Agent:
    identity.require_user_session(principal)
    # One statement, so the check and the change cannot be separated by a concurrent
    # revocation. A revoked agent matches no change at all: that state is final.
    changed = await session.execute(
        update(_agents)
        .where(
            _agents.c.id == agent_id,
            _agents.c.owner_user_id == principal.user_id,
            _agents.c.status.not_in({"revoked", status}),
        )
        .values(status=status)
        .returning(*_agents.c)
    )
    row = changed.mappings().one_or_none()
    if row is None:
        # Nothing changed: it is not theirs, or it is revoked, or it is already as asked.
        row = await _own_agent(session, principal, agent_id)
        if row["status"] == "revoked" and status != "revoked":
            raise AgentRevoked
    else:
        await _record(session, principal, action, agent_id)
    return _agent(row, (await _keys_of(session, [agent_id])).get(agent_id, ()))


# --- keys ------------------------------------------------------------------------------------


async def issue_key(
    session: AsyncSession,
    principal: Principal,
    agent_id: uuid.UUID,
    *,
    scopes: Iterable[str],
    expires_at: datetime | None = None,
    settings: Settings,
) -> IssuedKey:
    """Give an agent a new key with these scopes.

    The key itself is in what this returns and nowhere else: it is not stored, logged or
    audited, so whoever calls this hands it to the owner or it is lost.
    """
    identity.require_user_session(principal)
    if not keys.is_configured(settings):
        raise AgentKeysUnavailable
    granted = _validated_scopes(scopes)
    now = utcnow()
    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise InvalidExpiry("expires_at must say which timezone it is in.", field="expires_at")
        if expires_at <= now:
            raise InvalidExpiry("expires_at must be in the future.", field="expires_at")
        # Stored and shown in UTC, like every other time, whatever offset it was given in.
        expires_at = expires_at.astimezone(UTC)
    agent = await _own_agent(session, principal, agent_id)
    if agent["status"] == "revoked":
        raise AgentRevoked

    for _ in range(_PREFIX_ATTEMPTS):
        generated = keys.generate(settings)
        row = {
            "id": new_id(),
            "agent_id": agent_id,
            "prefix": generated.prefix,
            "key_hash": generated.key_hash,
            "scopes": list(granted),
            "expires_at": expires_at,
            "revoked_at": None,
            "last_used_at": None,
            "created_at": now,
        }
        # A taken prefix is found by ON CONFLICT rather than by catching the unique
        # violation, which would abort the caller's transaction.
        inserted = await session.execute(
            insert(_keys)
            .values(row)
            .on_conflict_do_nothing(index_elements=[_keys.c.prefix])
            .returning(_keys.c.id)
        )
        if inserted.scalar_one_or_none() is not None:
            await _record(
                session,
                principal,
                "agent.key_created",
                agent_id,
                # The id of the key and what it may do. Never the key.
                details={
                    "key_id": str(row["id"]),
                    "scopes": list(granted),
                    "expires_at": None if expires_at is None else expires_at.isoformat(),
                },
            )
            return IssuedKey(details=_key(row), key=generated.key)
    raise RuntimeError("could not draw an unused key prefix")


async def revoke_key(
    session: AsyncSession, principal: Principal, agent_id: uuid.UUID, key_id: uuid.UUID
) -> AgentKey:
    """Stop one key from working. Its row is kept. Revoking it again changes nothing."""
    identity.require_user_session(principal)
    await _own_agent(session, principal, agent_id)
    revoked = await session.execute(
        update(_keys)
        .where(_keys.c.id == key_id, _keys.c.agent_id == agent_id, _keys.c.revoked_at.is_(None))
        .values(revoked_at=utcnow())
        .returning(*_keys.c)
    )
    row = revoked.mappings().one_or_none()
    if row is not None:
        await _record(
            session, principal, "agent.key_revoked", agent_id, details={"key_id": str(key_id)}
        )
        return _key(row)
    # Nothing changed: there is no such key under this agent, or it was revoked before.
    found = await session.execute(
        select(_keys).where(_keys.c.id == key_id, _keys.c.agent_id == agent_id)
    )
    row = found.mappings().one_or_none()
    if row is None:
        raise AgentKeyNotFound
    return _key(row)


async def authenticate(session: AsyncSession, presented: PresentedKey) -> KeyOutcome:
    """Say whose key this is, if it is anyone's and still good.

    The outcome is returned, never raised, and it says why a key was refused: that is for
    the log. Every refusal must look the same to the client, which is the caller's to do.

    The digest is compared before anything else is looked at, and whether or not the
    prefix names a key, so that how long a refusal takes says nothing about which keys
    exist. What is checked after it is learnt only by someone who holds the real key.
    """
    found = await session.execute(
        select(
            _keys.c.id,
            _keys.c.agent_id,
            _keys.c.key_hash,
            _keys.c.scopes,
            _keys.c.expires_at,
            _keys.c.revoked_at,
            _agents.c.owner_user_id,
            _agents.c.status,
        )
        .join(_agents, _agents.c.id == _keys.c.agent_id)
        .where(_keys.c.prefix == presented.prefix)
    )
    row = found.mappings().one_or_none()
    stored = _NO_SUCH_DIGEST if row is None else row["key_hash"]
    matches = compare_digest(stored, presented.digest)
    if row is None or not matches:
        return KeyOutcome(reason="unknown_key")

    now = utcnow()
    reason = await _refusal(session, row, now)
    if reason is not None:
        return KeyOutcome(
            reason=reason,
            agent_id=row["agent_id"],
            key_id=row["id"],
            owner_user_id=row["owner_user_id"],
        )

    # Only if the last record is a minute old, so that a busy key is read on each request
    # and written once a minute. The caller commits this before the request's own work
    # begins, so the row lock it takes is never held alongside a lock on money.
    await session.execute(
        update(_keys)
        .where(
            _keys.c.id == row["id"],
            or_(
                _keys.c.last_used_at.is_(None),
                _keys.c.last_used_at <= now - _LAST_USED_RESOLUTION,
            ),
        )
        .values(last_used_at=now)
    )
    return KeyOutcome(
        reason=None,
        agent_id=row["agent_id"],
        key_id=row["id"],
        owner_user_id=row["owner_user_id"],
        # What the key was given, less anything an agent may not hold. Issuing refuses
        # such a scope already; this holds whatever is in the row.
        scopes=frozenset(row["scopes"]) & keys.AGENT_SCOPES,
    )


async def _refusal(session: AsyncSession, row: RowMapping, now: datetime) -> KeyRefusal | None:
    """Why a genuine key is not good at this moment, or None if it is."""
    if row["revoked_at"] is not None:
        return "key_revoked"
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "key_expired"
    if row["status"] != "active":
        return _REFUSED_BY_STATUS[row["status"]]
    owner = (await identity.get_users(session, [row["owner_user_id"]])).get(row["owner_user_id"])
    # An owner who is restricted or gone has nobody to act for them either.
    if owner is None or owner.status != "active":
        return "owner_not_active"
    return None


# --- helpers ---------------------------------------------------------------------------------


async def _own_agent(
    session: AsyncSession, principal: Principal, agent_id: uuid.UUID
) -> RowMapping:
    """The principal's user's agent with this id. Another user's is no agent at all."""
    found = await session.execute(
        select(_agents).where(
            _agents.c.id == agent_id, _agents.c.owner_user_id == principal.user_id
        )
    )
    row = found.mappings().one_or_none()
    if row is None:
        raise AgentNotFound
    return row


async def _keys_of(
    session: AsyncSession, agent_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[AgentKey, ...]]:
    """The keys of each of these agents, oldest first."""
    if not agent_ids:
        return {}
    rows = await session.execute(
        select(_keys).where(_keys.c.agent_id.in_(agent_ids)).order_by(_keys.c.id)
    )
    grouped: dict[uuid.UUID, list[AgentKey]] = {}
    for row in rows.mappings():
        grouped.setdefault(row["agent_id"], []).append(_key(row))
    return {agent_id: tuple(found) for agent_id, found in grouped.items()}


def _validated_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    asked = set(scopes)
    if not asked:
        raise InvalidScopes("A key needs at least one scope.", field="scopes")
    refused = sorted(asked - keys.AGENT_SCOPES)
    if refused:
        # Named back to the client: a scope is a public name, and which one was wrong is
        # what they need to know.
        raise InvalidScopes(
            "These are not scopes a key can be given: " + ", ".join(refused[:10]) + ".",
            field="scopes",
        )
    return tuple(sorted(asked))


def _position_of(cursor: str, scope: str) -> uuid.UUID:
    position = decode_cursor(cursor, kind=CURSOR_KIND, scope=scope)
    if not isinstance(position, str):
        raise InvalidCursor
    try:
        return uuid.UUID(position)
    except ValueError:
        raise InvalidCursor from None


async def _record(
    session: AsyncSession,
    principal: Principal,
    action: str,
    agent_id: uuid.UUID,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    await audit.record(
        session,
        actor=audit.Actor.user(principal.actor_id),
        action=action,
        principal_id=principal.user_id,
        resource_type="agent",
        resource_id=agent_id,
        details=details,
    )


def _agent(row: RowMapping | dict[str, Any], agent_keys: tuple[AgentKey, ...]) -> Agent:
    return Agent(
        id=row["id"],
        owner_user_id=row["owner_user_id"],
        name=row["name"],
        status=row["status"],
        created_at=row["created_at"],
        keys=agent_keys,
    )


def _key(row: RowMapping | dict[str, Any]) -> AgentKey:
    return AgentKey(
        id=row["id"],
        agent_id=row["agent_id"],
        prefix=row["prefix"],
        scopes=tuple(sorted(row["scopes"])),
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
        created_at=row["created_at"],
    )

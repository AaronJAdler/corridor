"""Builders for risk tests: a movement in a line, and a look at what risk wrote."""

import uuid
from typing import Any

from sqlalchemy import text

from corridor import risk
from corridor.identity import Principal, User
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.risk import MoneyMovement, MovementKind


def acting_as(user: User) -> Principal:
    return Principal.for_user(user.id, user.role, new_id())


def agent_of(user: User, agent_id: uuid.UUID | None = None) -> Principal:
    """The principal of an agent acting for the user, with every scope a movement needs."""
    return Principal(
        user_id=user.id,
        actor_type="agent",
        actor_id=agent_id or new_id(),
        role="user",
        scopes=frozenset({"*"}),
        session_id=None,
    )


def movement(
    user: User,
    amount: int,
    asset: str = "USD",
    *,
    kind: MovementKind = "transfer",
    principal: Principal | None = None,
    movement_id: uuid.UUID | None = None,
) -> MoneyMovement:
    return MoneyMovement(
        kind=kind,
        user_id=user.id,
        principal=principal or acting_as(user),
        asset=asset,
        amount=amount,
        movement_id=movement_id or new_id(),
    )


async def authorize(db: Database, asked: MoneyMovement) -> risk.Decision:
    """One authorisation in a transaction of its own, committed if it was allowed."""
    async with db.transaction() as session:
        return await risk.authorize(session, asked)


async def rows(db: Database, statement: str, **parameters: Any) -> list[dict[str, Any]]:
    async with db.transaction() as session:
        found = await session.execute(text(statement), parameters)
        return [dict(row) for row in found.mappings()]


async def usage(db: Database, user: User) -> list[dict[str, Any]]:
    return await rows(db, "SELECT * FROM risk_usage WHERE user_id = :id ORDER BY id", id=user.id)


async def used(db: Database, user: User) -> int:
    """Everything the user has had authorised, in US cents."""
    return sum(int(row["usd_value"]) for row in await usage(db, user))

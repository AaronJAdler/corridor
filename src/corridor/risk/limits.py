"""Limits: the most that may move at once and within 24 hours, and what has moved.

Everything is valued in whole US cents at a fixed reference rate, rounded up, so that a
day's movements in several assets can be added together and rounding never lets more
through than a limit says.

A rule belongs to a KYC tier, to one user or to one agent, and covers one kind of movement
or every kind. The most specific wins: a user's rule over their tier's, and within the same
scope a rule for the movement's kind over a rule for every kind. An agent's rule is a
second limit inside its user's, on what that agent alone has moved: it never lets the agent
move what the user could not.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Final, Literal, cast

from sqlalchemy import RowMapping, Table, and_, func, not_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.identity import User
from corridor.platform.clock import utcnow
from corridor.platform.ids import new_id
from corridor.platform.logging import get_logger
from corridor.platform.money import MAX_MINOR_UNITS, InvalidAmount, get_asset
from corridor.platform.pagination import DEFAULT_LIMIT, Page, clamp_limit
from corridor.risk.errors import LimitExceeded
from corridor.risk.models import LimitRow, ReferenceRateRow, UsageRow
from corridor.risk.screening import page_of, position_of
from corridor.risk.types import Limit, LimitScope, MoneyMovement, MovementKind

log = get_logger(__name__)

# Core tables: every statement against them is written out below.
_limits = cast(Table, LimitRow.__table__)
_usage = cast(Table, UsageRow.__table__)
_rates = cast(Table, ReferenceRateRow.__table__)

WINDOW: Final = timedelta(hours=24)
_CENTS_PER_DOLLAR: Final = 100
CURSOR_KIND: Final = "risk_limits"


async def usd_value(session: AsyncSession, asset: str, amount: int) -> int:
    """What ``amount`` minor units of ``asset`` are worth, in whole US cents, rounded up.

    An asset with no reference rate cannot be valued, and so cannot be limited: that is a
    fault in how Corridor is set up, and the movement does not go ahead.
    """
    # First, so that an asset Corridor does not have is the client's mistake it is.
    scale = get_asset(asset).scale
    found = await session.execute(select(_rates.c.usd_per_unit).where(_rates.c.asset == asset))
    rate: Decimal | None = found.scalar_one_or_none()
    if rate is None:
        raise LookupError(f"there is no reference rate to value {asset} in USD")
    # Whole numbers throughout, so that nothing is rounded but the result, and that
    # upwards: decimal arithmetic would round a large amount to its context's precision.
    numerator, denominator = rate.as_integer_ratio()
    return -(-amount * numerator * _CENTS_PER_DOLLAR // (denominator * scale))


async def check_and_record(session: AsyncSession, movement: MoneyMovement, user: User) -> None:
    """Refuse the movement if it is over a limit, and otherwise count it against them.

    The caller holds the user's money-out lock, so no other movement of this user can be
    read as absent here and be counted a moment later. The usage is written in the
    caller's transaction: if the movement does not commit, neither does its usage.
    """
    if movement.amount > MAX_MINOR_UNITS:
        # Not an amount at all: it could not be recorded, here or anywhere else. Said as
        # that, and not as a limit, because no limit however high would let it through.
        raise InvalidAmount("The amount is too large.")
    value = await usd_value(session, movement.asset, movement.amount)
    if value > MAX_MINOR_UNITS:
        raise InvalidAmount("The amount is too large.")
    principal = movement.principal
    agent_id = principal.actor_id if principal.is_agent else None

    account_rule = await _rule_for(session, movement.kind, user=user)
    if account_rule is None:
        # Every tier has a seeded rule. Without one there is nothing to say the movement
        # is allowed, so it is not.
        raise LookupError(f"there is no limit rule for KYC tier {user.kyc_tier}")
    checks: list[tuple[Limit, Literal["account", "agent"]]] = [(account_rule, "account")]
    if agent_id is not None:
        agent_rule = await _rule_for(session, movement.kind, agent_id=agent_id)
        if agent_rule is not None:
            # The agent's own limit first: it is the one its user chose for it.
            checks.insert(0, (agent_rule, "agent"))

    for rule, scope in checks:
        if rule.per_tx_usd is not None and value > rule.per_tx_usd:
            raise _refusal(movement, "per_transaction", scope, rule.per_tx_usd)
        if rule.daily_usd is not None:
            spent = await _spent(session, movement, rule, agent_id if scope == "agent" else None)
            if spent + value > rule.daily_usd:
                raise _refusal(movement, "daily", scope, rule.daily_usd)

    # A movement with no id of its own is filed under the id of its usage, so that it is
    # still counted. One that has an id is counted once however often it is authorised.
    usage_id = new_id()
    await session.execute(
        pg_insert(_usage)
        .values(
            id=usage_id,
            user_id=movement.user_id,
            agent_id=agent_id,
            kind=movement.kind,
            asset=movement.asset,
            amount=movement.amount,
            usd_value=value,
            movement_id=movement.movement_id or usage_id,
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(constraint="uq_risk_usage_kind_movement_id")
    )


async def release_usage(session: AsyncSession, kind: MovementKind, movement_id: uuid.UUID) -> None:
    """Give back what a movement used of its user's limits, because the money did not go
    out after all: a withdrawal that was canceled, failed or released.

    The row stays, as the record of what was authorised, marked with when it was given
    back. A second call changes nothing, and neither does one for a movement that was
    never counted.
    """
    await session.execute(
        update(_usage)
        .where(
            _usage.c.kind == kind,
            _usage.c.movement_id == movement_id,
            _usage.c.released_at.is_(None),
        )
        .values(released_at=utcnow())
    )


async def set_limit(
    session: AsyncSession,
    *,
    scope: LimitScope,
    tier: int | None = None,
    user_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    kind: MovementKind | None = None,
    per_tx_usd: int | None,
    daily_usd: int | None,
) -> Limit:
    """Set the rule for a tier, a user or an agent, replacing the one it had for ``kind``.

    The amounts are whole US cents. ``None`` sets no limit of that sort, which is a
    decision and not a default: both have to be given.
    """
    subject = {"tier": tier, "user": user_id, "agent": agent_id}
    if scope not in subject or subject[scope] is None:
        raise ValueError(f"a rule of scope {scope!r} names its {scope}")
    if any(value is not None for name, value in subject.items() if name != scope):
        raise ValueError(f"a rule of scope {scope!r} names nothing but its {scope}")
    for amount in (per_tx_usd, daily_usd):
        if amount is not None and (
            isinstance(amount, bool)
            or not isinstance(amount, int)
            or not 0 <= amount <= MAX_MINOR_UNITS
        ):
            raise ValueError("a limit is a whole number of US cents, zero or more")

    stored = await session.execute(
        pg_insert(_limits)
        .values(
            id=new_id(),
            scope=scope,
            tier=tier,
            user_id=user_id,
            agent_id=agent_id,
            kind=kind,
            per_tx_usd=per_tx_usd,
            daily_usd=daily_usd,
            created_at=utcnow(),
        )
        .on_conflict_do_update(
            constraint="uq_risk_limits_subject",
            set_={"per_tx_usd": per_tx_usd, "daily_usd": daily_usd},
        )
        .returning(_limits)
    )
    return _limit(stored.mappings().one())


async def list_limits(
    session: AsyncSession, *, cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> Page[Limit]:
    """One page of every rule there is, the tiers' and the ones set since, newest first."""
    limit = clamp_limit(limit)
    query = select(_limits)
    if cursor is not None:
        query = query.where(_limits.c.id < position_of(cursor, CURSOR_KIND, "all"))
    # One more than the page, to learn whether anything follows it without a second query.
    rows = await session.execute(query.order_by(_limits.c.id.desc()).limit(limit + 1))
    return page_of([_limit(row) for row in rows.mappings()], limit, CURSOR_KIND, "all")


async def _rule_for(
    session: AsyncSession,
    kind: MovementKind,
    *,
    user: User | None = None,
    agent_id: uuid.UUID | None = None,
) -> Limit | None:
    """The one rule that applies to a movement of ``kind`` by the user, or by the agent."""
    if user is not None:
        subject = or_(
            and_(_limits.c.scope == "user", _limits.c.user_id == user.id),
            and_(_limits.c.scope == "tier", _limits.c.tier == user.kyc_tier),
        )
    else:
        subject = and_(_limits.c.scope == "agent", _limits.c.agent_id == agent_id)
    found = await session.execute(
        select(_limits).where(subject, or_(_limits.c.kind == kind, _limits.c.kind.is_(None)))
    )
    rules = [_limit(row) for row in found.mappings()]
    if not rules:
        return None
    # A user's own rule before their tier's, and then a rule for this kind of movement
    # before a rule for every kind.
    return min(rules, key=lambda rule: (rule.scope == "tier", rule.kind is None))


async def _spent(
    session: AsyncSession, movement: MoneyMovement, rule: Limit, agent_id: uuid.UUID | None
) -> int:
    """What already counts against ``rule`` in the 24 hours up to now, in US cents: the
    user's movements, or the agent's alone when it is the agent's rule."""
    conditions = [
        _usage.c.user_id == movement.user_id,
        # A movement made exactly 24 hours ago has left the window.
        _usage.c.created_at > utcnow() - WINDOW,
        # A movement that was given back moved nothing out.
        _usage.c.released_at.is_(None),
    ]
    if agent_id is not None:
        conditions.append(_usage.c.agent_id == agent_id)
    if rule.kind is not None:
        # A rule for one kind of movement is a limit on that kind.
        conditions.append(_usage.c.kind == rule.kind)
    if movement.movement_id is not None:
        # Authorised before, in a transaction that committed: it is not added to itself.
        conditions.append(
            not_(
                and_(
                    _usage.c.kind == movement.kind,
                    _usage.c.movement_id == movement.movement_id,
                )
            )
        )
    total = await session.execute(
        select(func.coalesce(func.sum(_usage.c.usd_value), 0)).where(*conditions)
    )
    return int(total.scalar_one())


def _refusal(
    movement: MoneyMovement,
    limit: Literal["per_transaction", "daily"],
    scope: Literal["account", "agent"],
    usd_cents: int,
) -> LimitExceeded:
    log.info(
        "risk.limit_exceeded",
        kind=movement.kind,
        asset=movement.asset,
        limit=limit,
        scope=scope,
    )
    return LimitExceeded(limit=limit, scope=scope, usd_cents=usd_cents)


def _limit(row: RowMapping) -> Limit:
    return Limit(
        id=row["id"],
        scope=row["scope"],
        tier=row["tier"],
        user_id=row["user_id"],
        agent_id=row["agent_id"],
        kind=row["kind"],
        per_tx_usd=row["per_tx_usd"],
        daily_usd=row["daily_usd"],
    )

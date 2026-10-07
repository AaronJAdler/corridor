"""Adjustments: a journal entry written by hand, which takes two people.

One admin asks for it, with a reason and the postings; a different admin approves it, and
the approval is what posts the entry. Nothing is posted for an adjustment that is only
asked for, or that is rejected. Releasing money from suspense, to a user or back to where
it came from, is an adjustment whose postings are worked out here instead of typed.

Each function takes the caller's session and runs inside the caller's transaction.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Final, cast

from sqlalchemy import RowMapping, Table, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, ledger, payments, wallets
from corridor.identity import Principal
from corridor.ledger import AccountKind, Direction, EntryDraft, PostingDraft
from corridor.ops.errors import (
    AdjustmentNotFound,
    AdjustmentNotPending,
    InvalidAdjustment,
    SelfApproval,
)
from corridor.ops.models import AdjustmentRow
from corridor.ops.types import Adjustment, AdjustmentStatus, Leg
from corridor.platform.clock import utcnow
from corridor.platform.money import MAX_MINOR_UNITS, get_asset
from corridor.platform.pagination import (
    DEFAULT_LIMIT,
    InvalidCursor,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)

# A Core table: every statement against it is written out below.
_adjustments = cast(Table, AdjustmentRow.__table__)

SOURCE_TYPE: Final = "adjustment"
ENTRY_KIND: Final = "adjustment"
CURSOR_KIND: Final = "adjustments"
MAX_REASON_LENGTH: Final = 500
MAX_LEGS: Final = 50


async def request_adjustment(
    session: AsyncSession,
    principal: Principal,
    *,
    adjustment_id: uuid.UUID,
    reason: str,
    legs: Sequence[Leg],
) -> Adjustment:
    """Ask for a journal entry. Nothing is posted until another admin approves it.

    The postings are checked now, so that what waits for approval is something that can
    be posted: each names an existing account once, in the asset it says, and they balance
    in every asset.
    """
    identity.require_admin(principal)
    reason = reason.strip()
    if not 0 < len(reason) <= MAX_REASON_LENGTH:
        raise InvalidAdjustment(
            f"A reason of 1 to {MAX_REASON_LENGTH} characters is required.", field="reason"
        )
    await _check(session, legs)

    inserted = await session.execute(
        insert(_adjustments)
        .values(
            id=adjustment_id,
            requested_by=principal.user_id,
            approved_by=None,
            status="pending",
            reason=reason,
            legs=[
                {
                    "account_id": str(leg.account_id),
                    "asset": leg.asset,
                    "direction": leg.direction.value,
                    # A string: a JSON number would lose precision above 2^53.
                    "amount": str(leg.amount),
                }
                for leg in legs
            ],
            entry_id=None,
            created_at=utcnow(),
            decided_at=None,
        )
        .returning(_adjustments)
    )
    adjustment = _adjustment(inserted.mappings().one())
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="adjustment.requested",
        resource_type="adjustment",
        resource_id=adjustment.id,
        details={"reason": reason, "legs": len(legs)},
    )
    return adjustment


async def request_suspense_release(
    session: AsyncSession,
    principal: Principal,
    *,
    adjustment_id: uuid.UUID,
    reason: str,
    asset: str,
    amount: int,
    user_id: uuid.UUID,
) -> Adjustment:
    """Ask for money in suspense to be credited to a user's available balance."""
    identity.require_admin(principal)
    suspense = await _suspense_holding(session, asset, amount)
    wallet = await wallets.resolve(session, user_id, asset)
    return await request_adjustment(
        session,
        principal,
        adjustment_id=adjustment_id,
        reason=reason,
        legs=(
            Leg(suspense, asset, Direction.DEBIT, amount),
            Leg(wallet.available_account_id, asset, Direction.CREDIT, amount),
        ),
    )


async def request_suspense_return(
    session: AsyncSession,
    principal: Principal,
    *,
    adjustment_id: uuid.UUID,
    reason: str,
    asset: str,
    amount: int,
) -> Adjustment:
    """Ask for money in suspense to be taken off the books as sent back through the
    provider it arrived at. Sending it is the operator's to do with the provider."""
    identity.require_admin(principal)
    suspense = await _suspense_holding(session, asset, amount)
    if get_asset(asset).kind == "fiat":
        kind, provider = AccountKind.BANK_SETTLEMENT, payments.BANK_PROVIDER
    else:
        kind, provider = AccountKind.CUSTODY_OMNIBUS, payments.CUSTODY_PROVIDER
    arrived_at = await ledger.find_account(session, kind, asset, provider=provider)
    if arrived_at is None:
        raise InvalidAdjustment("Nothing has arrived through a provider in this asset.")
    return await request_adjustment(
        session,
        principal,
        adjustment_id=adjustment_id,
        reason=reason,
        legs=(
            Leg(suspense, asset, Direction.DEBIT, amount),
            Leg(arrived_at.id, asset, Direction.CREDIT, amount),
        ),
    )


async def approve_adjustment(
    session: AsyncSession, principal: Principal, adjustment_id: uuid.UUID
) -> Adjustment:
    """Approve a pending adjustment and post its entry, once.

    The row is locked and its status looked at before anything is posted, so of two
    approvals at once, one posts and the other finds the adjustment decided. The approver
    is never the requester: that is refused here, and by the table.
    """
    identity.require_admin(principal)
    row = await _lock(session, adjustment_id)
    if row["status"] != "pending":
        raise AdjustmentNotPending
    if row["requested_by"] == principal.user_id:
        raise SelfApproval

    pending = _adjustment(row)
    entry = await ledger.post_entry(
        session,
        EntryDraft(
            kind=ENTRY_KIND,
            source_type=SOURCE_TYPE,
            source_id=str(adjustment_id),
            postings=tuple(
                PostingDraft(leg.account_id, leg.direction, leg.amount) for leg in pending.legs
            ),
            metadata={
                "requested_by": str(pending.requested_by),
                "approved_by": str(principal.user_id),
            },
        ),
    )
    approved = await _decide(
        session, adjustment_id, status="approved", approved_by=principal.user_id, entry_id=entry.id
    )
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="adjustment.approved",
        resource_type="adjustment",
        resource_id=adjustment_id,
        details={"entry_id": str(entry.id), "requested_by": str(pending.requested_by)},
    )
    return approved


async def reject_adjustment(
    session: AsyncSession, principal: Principal, adjustment_id: uuid.UUID
) -> Adjustment:
    """Turn a pending adjustment down. Any admin may, the requester included: withdrawing
    one's own request moves nothing."""
    identity.require_admin(principal)
    row = await _lock(session, adjustment_id)
    if row["status"] != "pending":
        raise AdjustmentNotPending
    rejected = await _decide(session, adjustment_id, status="rejected")
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="adjustment.rejected",
        resource_type="adjustment",
        resource_id=adjustment_id,
        details={"requested_by": str(rejected.requested_by)},
    )
    return rejected


async def get_adjustment(
    session: AsyncSession, principal: Principal, adjustment_id: uuid.UUID
) -> Adjustment:
    identity.require_admin(principal)
    rows = await session.execute(select(_adjustments).where(_adjustments.c.id == adjustment_id))
    row = rows.mappings().one_or_none()
    if row is None:
        raise AdjustmentNotFound
    return _adjustment(row)


async def list_adjustments(
    session: AsyncSession,
    principal: Principal,
    *,
    status: AdjustmentStatus | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[Adjustment]:
    """One page of adjustments, newest first. All of them, or those in one status."""
    identity.require_admin(principal)
    limit = clamp_limit(limit)
    # The cursor is tied to the filter, so one from another list is refused.
    scope = status or "all"
    query = select(_adjustments)
    if status is not None:
        query = query.where(_adjustments.c.status == status)
    if cursor is not None:
        query = query.where(_adjustments.c.id < _position(cursor, scope))
    # One more than the page, to learn whether anything follows it without a second query.
    rows = await session.execute(query.order_by(_adjustments.c.id.desc()).limit(limit + 1))
    found = [_adjustment(row) for row in rows.mappings()]
    shown = found[:limit]
    return Page(
        items=tuple(shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=scope, position=str(shown[-1].id))
            if len(found) > limit
            else None
        ),
    )


async def _check(session: AsyncSession, legs: Sequence[Leg]) -> None:
    """Refuse postings the ledger would refuse, with a refusal a client can read."""
    if not 2 <= len(legs) <= MAX_LEGS:
        raise InvalidAdjustment(f"An adjustment has 2 to {MAX_LEGS} legs.", field="legs")
    net: dict[str, int] = defaultdict(int)
    seen: set[uuid.UUID] = set()
    for leg in legs:
        if not 0 < leg.amount <= MAX_MINOR_UNITS:
            raise InvalidAdjustment("A leg moves a positive amount.", field="legs")
        if leg.account_id in seen:
            raise InvalidAdjustment("An account appears once in an adjustment.", field="legs")
        seen.add(leg.account_id)
        try:
            account = await ledger.get_account(session, leg.account_id)
        except ledger.UnknownAccount:
            raise InvalidAdjustment(
                "A leg names an account that does not exist.", field="legs"
            ) from None
        if account.asset_code != leg.asset:
            # The amount was written for another asset, and would be another amount here.
            raise InvalidAdjustment("A leg names an account in another asset.", field="legs")
        net[account.asset_code] += leg.amount if leg.direction is Direction.DEBIT else -leg.amount
    unbalanced = sorted(asset for asset, total in net.items() if total != 0)
    if unbalanced:
        raise InvalidAdjustment(
            f"Debits and credits differ in {', '.join(unbalanced)}.", field="legs"
        )


async def _suspense_holding(session: AsyncSession, asset: str, amount: int) -> uuid.UUID:
    """The suspense account of an asset, provided it holds at least ``amount`` now."""
    get_asset(asset)
    suspense = await ledger.find_account(session, AccountKind.SUSPENSE, asset)
    held = await ledger.get_balance(session, suspense.id) if suspense is not None else 0
    if suspense is None or amount > held:
        raise InvalidAdjustment("Suspense does not hold that much in this asset.", field="amount")
    return suspense.id


async def _lock(session: AsyncSession, adjustment_id: uuid.UUID) -> RowMapping:
    rows = await session.execute(
        select(_adjustments).where(_adjustments.c.id == adjustment_id).with_for_update()
    )
    row = rows.mappings().one_or_none()
    if row is None:
        raise AdjustmentNotFound
    return row


async def _decide(
    session: AsyncSession,
    adjustment_id: uuid.UUID,
    *,
    status: AdjustmentStatus,
    approved_by: uuid.UUID | None = None,
    entry_id: uuid.UUID | None = None,
) -> Adjustment:
    """Write the decision to an adjustment whose row the caller has locked."""
    updated = await session.execute(
        update(_adjustments)
        .where(_adjustments.c.id == adjustment_id)
        .values(status=status, approved_by=approved_by, entry_id=entry_id, decided_at=utcnow())
        .returning(_adjustments)
    )
    return _adjustment(updated.mappings().one())


def _position(cursor: str, scope: str) -> uuid.UUID:
    position = decode_cursor(cursor, kind=CURSOR_KIND, scope=scope)
    if not isinstance(position, str):
        raise InvalidCursor
    try:
        return uuid.UUID(position)
    except ValueError:
        raise InvalidCursor from None


def _adjustment(row: RowMapping) -> Adjustment:
    return Adjustment(
        id=row["id"],
        requested_by=row["requested_by"],
        approved_by=row["approved_by"],
        status=row["status"],
        reason=row["reason"],
        legs=tuple(
            Leg(
                account_id=uuid.UUID(leg["account_id"]),
                asset=leg["asset"],
                direction=Direction(leg["direction"]),
                amount=int(leg["amount"]),
            )
            for leg in row["legs"]
        ),
        entry_id=row["entry_id"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )

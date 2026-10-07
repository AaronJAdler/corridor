"""Adjustments: a journal entry written by hand, which takes two people.

One admin asks for it, with a reason and the postings; a different admin approves it, and
the approval is what posts the entry. Nothing is posted for an adjustment that is only
asked for, or that is rejected.

Money leaves suspense as the deposit it arrived as, and in no other way. Releasing a
deposit to a user, or sending it back to where it came from, is an adjustment that names
the deposit: its postings are worked out here instead of typed, and the approval hands it
to payments, which looks at the deposit under its lock and moves it out of suspense once.
An adjustment written by hand cannot debit suspense at all, nor a held balance: what is on
hold belongs to a withdrawal, and leaves as that withdrawal ends.

Each function takes the caller's session and runs inside the caller's transaction.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Final, cast

from sqlalchemy import RowMapping, Table, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, ledger, payments, risk, wallets
from corridor.identity import Principal
from corridor.ledger import AccountKind, Direction, EntryDraft, PostingDraft
from corridor.ops.errors import (
    AdjustmentNotFound,
    AdjustmentNotPending,
    InvalidAdjustment,
    SelfApproval,
)
from corridor.ops.models import AdjustmentRow
from corridor.ops.types import Adjustment, AdjustmentKind, AdjustmentStatus, Leg
from corridor.platform.clock import utcnow
from corridor.platform.db import advisory_xact_lock, lock_key
from corridor.platform.money import MAX_MINOR_UNITS
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
    in every asset. None of them takes money out of suspense: that is asked for by naming
    the deposit, with ``request_suspense_release`` or ``request_suspense_return``. Nor
    does any take money off hold, which belongs to the withdrawals that reserved it.
    """
    identity.require_admin(principal)
    await _check(session, legs)
    await _refuse_reserved_debit(session, legs)
    return await _record(session, principal, adjustment_id, reason, legs, kind="manual")


async def request_suspense_release(
    session: AsyncSession,
    principal: Principal,
    *,
    adjustment_id: uuid.UUID,
    reason: str,
    deposit_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Adjustment:
    """Ask for a deposit in suspense to be credited to a user's available balance.

    The deposit must be in suspense now and the user must be able to receive it. Both are
    looked at again when the adjustment is approved, which may be much later.
    """
    identity.require_admin(principal)
    deposit = await payments.get_suspense_deposit(session, deposit_id)
    if (await identity.get_user(session, user_id)).status == "closed":
        raise payments.DepositOwnerClosed
    suspense = await ledger.open_account(session, AccountKind.SUSPENSE, deposit.asset)
    wallet = await wallets.resolve(session, user_id, deposit.asset)
    return await _record(
        session,
        principal,
        adjustment_id,
        reason,
        (
            Leg(suspense.id, deposit.asset, Direction.DEBIT, deposit.amount),
            Leg(wallet.available_account_id, deposit.asset, Direction.CREDIT, deposit.amount),
        ),
        kind="suspense_release",
        deposit_id=deposit_id,
        user_id=user_id,
    )


async def request_suspense_return(
    session: AsyncSession,
    principal: Principal,
    *,
    adjustment_id: uuid.UUID,
    reason: str,
    deposit_id: uuid.UUID,
) -> Adjustment:
    """Ask for a deposit in suspense to be taken off the books as sent back through the
    provider it arrived at. Sending it is the operator's to do with the provider."""
    identity.require_admin(principal)
    deposit = await payments.get_suspense_deposit(session, deposit_id)
    suspense = await ledger.open_account(session, AccountKind.SUSPENSE, deposit.asset)
    arrived_at = await ledger.open_account(
        session,
        AccountKind.BANK_SETTLEMENT if deposit.kind == "bank" else AccountKind.CUSTODY_OMNIBUS,
        deposit.asset,
        provider=deposit.provider,
    )
    return await _record(
        session,
        principal,
        adjustment_id,
        reason,
        (
            Leg(suspense.id, deposit.asset, Direction.DEBIT, deposit.amount),
            Leg(arrived_at.id, deposit.asset, Direction.CREDIT, deposit.amount),
        ),
        kind="suspense_return",
        deposit_id=deposit_id,
    )


async def approve_adjustment(
    session: AsyncSession, principal: Principal, adjustment_id: uuid.UUID
) -> Adjustment:
    """Approve a pending adjustment and post its entry, once.

    The row is locked and its status looked at before anything is posted, so of two
    approvals at once, one posts and the other finds the adjustment decided. The approver
    is never the requester: that is refused here, and by the table.

    An adjustment that debits a user's available balance takes money out of it, as a
    transfer does, so that user's money-out lock is taken first, before the adjustment's
    row and before any balance, in the order every money path takes its locks. The legs
    are written once, with the request, so they are read for that without a lock.

    One that takes a deposit out of suspense is carried out by payments, after the
    adjustment's row and under the deposit's: the deposit must still be in suspense, and
    it leaves suspense in this transaction. So two adjustments for one deposit, or an
    adjustment and a cleared review, or an adjustment and the bank's own return, move the
    money once, whichever comes first. The one that comes second is refused and stays
    pending, for an admin to reject.
    """
    identity.require_admin(principal)
    await advisory_xact_lock(session, await _money_out_locks(session, adjustment_id))
    row = await _lock(session, adjustment_id)
    if row["status"] != "pending":
        raise AdjustmentNotPending
    if row["requested_by"] == principal.user_id:
        raise SelfApproval

    pending = _adjustment(row)
    actor = audit.Actor.admin(principal.user_id)
    metadata = {
        "adjustment_id": str(adjustment_id),
        "requested_by": str(pending.requested_by),
        "approved_by": str(principal.user_id),
    }
    if pending.deposit_id is None:
        # Asked for before suspense and held balances were closed to adjustments written
        # by hand, perhaps.
        await _refuse_reserved_debit(session, pending.legs)
        entry = await ledger.post_entry(
            session,
            EntryDraft(
                kind=ENTRY_KIND,
                source_type=SOURCE_TYPE,
                source_id=str(adjustment_id),
                postings=tuple(
                    PostingDraft(leg.account_id, leg.direction, leg.amount) for leg in pending.legs
                ),
                metadata=metadata,
            ),
        )
        entry_id = entry.id
    elif pending.kind == "suspense_return":
        returned = await payments.return_from_suspense(
            session, pending.deposit_id, actor=actor, metadata=metadata
        )
        entry_id = returned.entry_id
    elif pending.user_id is not None:
        released = await payments.release_from_suspense(
            session, pending.deposit_id, pending.user_id, actor=actor, metadata=metadata
        )
        entry_id = released.entry_id
    else:  # pragma: no cover - the table refuses a release that names nobody
        raise RuntimeError(f"adjustment {adjustment_id} releases a deposit to nobody")
    approved = await _decide(
        session, adjustment_id, status="approved", approved_by=principal.user_id, entry_id=entry_id
    )
    await audit.record(
        session,
        actor=actor,
        action="adjustment.approved",
        resource_type="adjustment",
        resource_id=adjustment_id,
        details={
            "entry_id": str(entry_id),
            "requested_by": str(pending.requested_by),
            "kind": pending.kind,
            **({} if pending.deposit_id is None else {"deposit_id": str(pending.deposit_id)}),
        },
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
    """One adjustment, for an admin. That it was read is written to the audit log."""
    identity.require_admin(principal)
    rows = await session.execute(select(_adjustments).where(_adjustments.c.id == adjustment_id))
    row = rows.mappings().one_or_none()
    if row is None:
        raise AdjustmentNotFound
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="adjustment.read",
        resource_type="adjustment",
        resource_id=adjustment_id,
    )
    return _adjustment(row)


async def list_adjustments(
    session: AsyncSession,
    principal: Principal,
    *,
    status: AdjustmentStatus | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[Adjustment]:
    """One page of adjustments, newest first, for an admin. All of them, or those in one
    status. That they were read is written to the audit log."""
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
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="adjustment.listed",
        resource_type="adjustment",
        details={"status": scope, "returned": len(shown)},
    )
    return Page(
        items=tuple(shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=scope, position=str(shown[-1].id))
            if len(found) > limit
            else None
        ),
    )


async def _record(
    session: AsyncSession,
    principal: Principal,
    adjustment_id: uuid.UUID,
    reason: str,
    legs: Sequence[Leg],
    *,
    kind: AdjustmentKind,
    deposit_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> Adjustment:
    """Write a pending adjustment and the audit event that says who asked for it."""
    reason = reason.strip()
    if not 0 < len(reason) <= MAX_REASON_LENGTH:
        raise InvalidAdjustment(
            f"A reason of 1 to {MAX_REASON_LENGTH} characters is required.", field="reason"
        )
    inserted = await session.execute(
        insert(_adjustments)
        .values(
            id=adjustment_id,
            requested_by=principal.user_id,
            approved_by=None,
            status="pending",
            kind=kind,
            deposit_id=deposit_id,
            user_id=user_id,
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
        principal_id=user_id,
        resource_type="adjustment",
        resource_id=adjustment.id,
        details={
            "reason": reason,
            "legs": len(legs),
            "kind": kind,
            **({} if deposit_id is None else {"deposit_id": str(deposit_id)}),
        },
    )
    return adjustment


# Why each kind of account that a hand-written adjustment may not debit is closed to it.
_NOT_BY_HAND: Final = {
    AccountKind.SUSPENSE: (
        "Money leaves suspense by releasing or returning the deposit it arrived as."
    ),
    AccountKind.USER_HELD: (
        "Money on hold leaves it as the withdrawal it is reserved for is settled, canceled"
        " or released."
    ),
}


async def _refuse_reserved_debit(session: AsyncSession, legs: Sequence[Leg]) -> None:
    """Refuse postings written by hand that take money out of suspense or off hold.

    Suspense holds deposits, each of which leaves it once, under its own row's lock. A
    debit that names no deposit would take the money and leave the deposit there, to be
    released or returned a second time.

    A held balance is what the user's withdrawals in flight have reserved, each of which
    takes its amount back out once, under its own row's lock. A debit that names no
    withdrawal would leave one that is still to be paid out with nothing behind it.
    """
    for leg in legs:
        if leg.direction is not Direction.DEBIT:
            continue
        refusal = _NOT_BY_HAND.get((await ledger.get_account(session, leg.account_id)).kind)
        if refusal is not None:
            raise InvalidAdjustment(refusal, field="legs")


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


async def _money_out_locks(session: AsyncSession, adjustment_id: uuid.UUID) -> list[int]:
    """The money-out lock of every user whose available balance an adjustment debits.
    ``advisory_xact_lock`` takes them in ascending order."""
    rows = await session.execute(
        select(_adjustments.c.legs).where(_adjustments.c.id == adjustment_id)
    )
    legs = rows.scalar_one_or_none()
    if legs is None:
        raise AdjustmentNotFound
    keys: list[int] = []
    for leg in legs:
        if leg["direction"] != Direction.DEBIT.value:
            continue
        account = await ledger.get_account(session, uuid.UUID(leg["account_id"]))
        if account.kind is AccountKind.USER_AVAILABLE and account.owner_id is not None:
            keys.append(lock_key(risk.MONEY_OUT_LOCK, account.owner_id))
    return keys


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
        kind=row["kind"],
        deposit_id=row["deposit_id"],
        user_id=row["user_id"],
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

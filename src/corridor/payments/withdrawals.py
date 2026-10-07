"""Withdrawals: reserving the funds, and what the user can still do while they are reserved.

A withdrawal is a saga. This module is its first step, taken in the caller's one
transaction: the amount and the fee move from the user's available balance to their held
balance, the withdrawal is recorded as ``held``, and an outbox event asks for it to be sent
to the provider. Sending, settling and releasing are in ``handlers``.

``held`` means reserved and never sent. Sending begins by recording the withdrawal as
``submitting``, and from then on the provider may have it.

Every change of state happens with the withdrawal's row locked and after a look at the
state it is in, so an event that is late, repeated or out of order finds nothing to do.
"""

import uuid
from datetime import datetime
from typing import Any, Final, cast

from sqlalchemy import RowMapping, Table, and_, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, ledger, outbox, risk, wallets
from corridor.identity import Principal, Scope
from corridor.ledger import EntryDraft, credit, debit
from corridor.payments import beneficiaries, fees
from corridor.payments.deposits import position_of
from corridor.payments.errors import (
    BeneficiaryAssetMismatch,
    BeneficiaryNotFound,
    DuplicateWithdrawal,
    InvalidAddress,
    InvalidWithdrawalTarget,
    WithdrawalNotAgents,
    WithdrawalNotCancelable,
    WithdrawalNotFound,
)
from corridor.payments.models import WithdrawalRow
from corridor.payments.transfers import MONEY_OUT_LOCK
from corridor.payments.types import CUSTODY_PROVIDER, FlowKind, Withdrawal
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import advisory_xact_lock, lock_key
from corridor.platform.money import MAX_MINOR_UNITS, InvalidAmount, get_asset
from corridor.platform.pagination import DEFAULT_LIMIT, Page, clamp_limit, encode_cursor
from corridor.providers import is_valid_address
from corridor.risk import MoneyMovement

# A Core table: every statement against it is written out below.
_withdrawals = cast(Table, WithdrawalRow.__table__)

SOURCE_TYPE: Final = "withdrawal"
HOLD_ENTRY_KIND: Final = "withdrawal_hold"
RELEASE_ENTRY_KIND: Final = "withdrawal_release"
WITHDRAWAL_SUBMIT: Final = "withdrawal.submit"
# Why a withdrawal that was never sent was given back: an operator rejected its review.
REVIEW_REJECTED: Final = "review_rejected"
CURSOR_KIND: Final = "withdrawals"


async def request_withdrawal(
    session: AsyncSession,
    principal: Principal,
    *,
    withdrawal_id: uuid.UUID,
    asset: str,
    amount: int,
    beneficiary_id: uuid.UUID | None = None,
    to_address: str | None = None,
    settings: Settings,
) -> Withdrawal:
    """Reserve ``amount`` and the fee for a withdrawal, and ask for it to be sent.

    A bank asset goes to one of the user's own beneficiaries and a stablecoin to an
    address. The caller makes ``withdrawal_id``, a new one for each attempt; it becomes the
    reference and the idempotency key the provider is given.

    The steps run in the order the locks have to be taken in, as a transfer's do. The
    target is checked before any lock, so a request that names nowhere to send the money
    never queues behind the user's other movements. Every refusal comes before the first
    write.
    """
    identity.require_scope(principal, Scope.WITHDRAWALS_CREATE)
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise InvalidAmount("The amount must be greater than zero.")
    user_id = principal.user_id

    kind: FlowKind
    if get_asset(asset).kind == "fiat":
        if beneficiary_id is None or to_address is not None:
            raise InvalidWithdrawalTarget(
                "A withdrawal of this asset goes to a saved beneficiary.", field="beneficiary_id"
            )
        beneficiary = await beneficiaries.find_own(session, user_id, beneficiary_id)
        if beneficiary is None:
            raise BeneficiaryNotFound
        if beneficiary.asset != asset:
            raise BeneficiaryAssetMismatch
        kind, provider = "bank", beneficiary.provider
        # The account number went to the provider and was not kept: the holder is what
        # there is to screen.
        screened = await risk.screen_party(session, kind="name", value=beneficiary.holder_name)
    else:
        if to_address is None or beneficiary_id is not None:
            raise InvalidWithdrawalTarget(
                "A withdrawal of this asset goes to an address.", field="to_address"
            )
        if not is_valid_address(to_address):
            raise InvalidAddress
        kind, provider = "chain", CUSTODY_PROVIDER
        screened = await risk.screen_party(session, kind="address", value=to_address)
    if screened == "deny":
        # Part of checking the target, and so before any lock and before anything is held.
        raise risk.PartyDenied

    # Balance rows are per asset and limits are not: this is what makes one user's outgoing
    # movements queue, whatever assets they are in.
    await advisory_xact_lock(session, [lock_key(MONEY_OUT_LOCK, user_id)])

    await risk.authorize(
        session,
        MoneyMovement(
            kind="withdrawal",
            user_id=user_id,
            principal=principal,
            asset=asset,
            amount=amount,
            movement_id=withdrawal_id,
        ),
    )

    fee = fees.withdrawal_fee(amount, asset, settings)
    reserved = amount + fee
    if reserved > MAX_MINOR_UNITS:
        # Each is storable alone, but they are held as their sum in one posting.
        raise InvalidAmount("The amount is too large.")

    wallet = await wallets.resolve(session, user_id, asset)
    entry = await ledger.post_entry(
        session,
        EntryDraft(
            kind=HOLD_ENTRY_KIND,
            source_type=SOURCE_TYPE,
            source_id=str(withdrawal_id),
            postings=(
                debit(wallet.available_account_id, reserved),
                credit(wallet.held_account_id, reserved),
            ),
        ),
    )
    if not entry.created:
        # The ledger found this id already held and moved nothing. Inserting the row again
        # would only fail later, as a primary-key violation with no explanation.
        raise DuplicateWithdrawal(f"withdrawal {withdrawal_id} already exists")

    now = utcnow()
    inserted = await session.execute(
        insert(_withdrawals)
        .values(
            id=withdrawal_id,
            user_id=user_id,
            asset_code=asset,
            amount=amount,
            fee=fee,
            kind=kind,
            beneficiary_id=beneficiary_id,
            to_address=to_address,
            status="held",
            provider=provider,
            hold_entry_id=entry.id,
            initiated_by_type=principal.actor_type,
            initiated_by_id=principal.actor_id,
            created_at=now,
            updated_at=now,
        )
        .returning(_withdrawals)
    )
    withdrawal = as_withdrawal(inserted.mappings().one())
    if screened == "review":
        # Held like any other, with the funds reserved. Whether it may be sent yet is
        # what ``risk.is_cleared`` answers, and it says no until an operator has cleared it.
        await risk.open_review(
            session,
            subject_type="withdrawal",
            subject_id=withdrawal.id,
            outcome="review",
            user_id=user_id,
        )

    await ask_to_be_sent(session, withdrawal.id)
    await audit.record(
        session,
        actor=_actor(principal),
        action="withdrawal.requested",
        principal_id=user_id,
        resource_type="withdrawal",
        resource_id=withdrawal.id,
        details={
            "asset": asset,
            "amount": str(amount),
            "fee": str(fee),
            "kind": kind,
            "provider": provider,
        },
    )
    return withdrawal


async def ask_to_be_sent(session: AsyncSession, withdrawal_id: uuid.UUID) -> None:
    """Write the event that has a withdrawal sent to its provider.

    Only the id: the handler reads the row, and finds whatever state it is in by then. So
    the event can be written again for a withdrawal whose first one never ran its course.
    """
    await outbox.enqueue(session, WITHDRAWAL_SUBMIT, {"withdrawal_id": str(withdrawal_id)})


async def send_cleared_withdrawal(session: AsyncSession, withdrawal_id: uuid.UUID) -> Withdrawal:
    """Ask again for a held withdrawal to be sent, now that its review has been cleared.

    The event written with the request found the review open and left the withdrawal as it
    was, so nothing else would ever send it. A withdrawal that is no longer held, because
    its user canceled it or it was given back while it waited, is left alone.
    """
    row = await lock(session, withdrawal_id)
    if row is None:
        # The caller got the id from a review of this withdrawal, so this is a bug.
        raise LookupError(f"there is no withdrawal {withdrawal_id} to send")
    if row["status"] == "held":
        await ask_to_be_sent(session, withdrawal_id)
    return as_withdrawal(row)


async def reject_held_withdrawal(
    session: AsyncSession, withdrawal_id: uuid.UUID, *, actor: audit.Actor
) -> Withdrawal:
    """Give back a held withdrawal whose review an operator rejected, and end it as failed.

    Only while it is ``held``, the one state in which the provider is certain not to have
    it. One that has ended some other way already is left as it ended.
    """
    row = await lock(session, withdrawal_id)
    if row is None:
        raise LookupError(f"there is no withdrawal {withdrawal_id} to reject")
    if row["status"] != "held":
        return as_withdrawal(row)
    failed = await release(session, row, status="failed", failure_reason=REVIEW_REJECTED)
    await audit.record(
        session,
        actor=actor,
        action="withdrawal.failed",
        principal_id=row["user_id"],
        resource_type="withdrawal",
        resource_id=withdrawal_id,
        details={"provider": row["provider"], "reason": REVIEW_REJECTED},
    )
    return failed


async def cancel_withdrawal(
    session: AsyncSession, principal: Principal, withdrawal_id: uuid.UUID
) -> Withdrawal:
    """Call back a withdrawal that has not been sent yet, and release its funds.

    Only the user it belongs to can, and only while it is ``held``: once it is being sent
    to the provider, or has ended some other way, the answer is a conflict. ``held`` is the
    one state in which the provider is certain not to have it, which is what makes giving
    the funds back safe.

    An agent calls back only what it asked for itself. The scope that lets it withdraw
    does not make its owner's withdrawals, or another agent's, its own to stop.
    """
    identity.require_scope(principal, Scope.WITHDRAWALS_CREATE)
    # Locked only if it is this user's: nobody can hold a lock on someone else's row.
    rows = await session.execute(
        select(_withdrawals)
        .where(_withdrawals.c.id == withdrawal_id, _withdrawals.c.user_id == principal.user_id)
        .with_for_update()
    )
    row = rows.mappings().one_or_none()
    if row is None:
        raise WithdrawalNotFound
    if principal.is_agent and (row["initiated_by_type"], row["initiated_by_id"]) != (
        "agent",
        principal.actor_id,
    ):
        raise WithdrawalNotAgents
    if row["status"] != "held":
        raise WithdrawalNotCancelable
    canceled = await release(session, row, status="canceled")
    await audit.record(
        session,
        actor=_actor(principal),
        action="withdrawal.canceled",
        principal_id=principal.user_id,
        resource_type="withdrawal",
        resource_id=withdrawal_id,
    )
    return canceled


async def get_withdrawal(
    session: AsyncSession, principal: Principal, withdrawal_id: uuid.UUID
) -> Withdrawal:
    """A withdrawal of the principal's user. Anyone else's is answered exactly as one that
    does not exist."""
    identity.require_scope(principal, Scope.WITHDRAWALS_READ)
    rows = await session.execute(select(_withdrawals).where(_withdrawals.c.id == withdrawal_id))
    row = rows.mappings().one_or_none()
    if row is None or row["user_id"] != principal.user_id:
        raise WithdrawalNotFound
    return as_withdrawal(row)


async def list_withdrawals(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[Withdrawal]:
    """One page of the withdrawals of the principal's user, newest first."""
    identity.require_scope(principal, Scope.WITHDRAWALS_READ)
    limit = clamp_limit(limit)
    scope = str(principal.user_id)
    query = select(_withdrawals).where(_withdrawals.c.user_id == principal.user_id)
    if cursor is not None:
        query = query.where(_withdrawals.c.id < position_of(cursor, CURSOR_KIND, scope))
    rows = await session.execute(query.order_by(_withdrawals.c.id.desc()).limit(limit + 1))
    found = [as_withdrawal(row) for row in rows.mappings()]
    shown = found[:limit]
    return Page(
        items=tuple(shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=scope, position=str(shown[-1].id))
            if len(found) > limit
            else None
        ),
    )


async def release(
    session: AsyncSession, row: RowMapping, *, status: str, failure_reason: str | None = None
) -> Withdrawal:
    """Return a withdrawal's reserved funds to its user and end it as ``status``.

    The caller holds the row's lock and has checked that the funds are still reserved.
    Nothing went out, so what the withdrawal used of its user's limits is given back with
    the funds: every way a withdrawal is released comes through here.
    """
    await risk.release_usage(session, "withdrawal", row["id"])
    reserved: int = row["amount"] + row["fee"]
    wallet = await wallets.resolve(session, row["user_id"], row["asset_code"])
    entry = await ledger.post_entry(
        session,
        EntryDraft(
            kind=RELEASE_ENTRY_KIND,
            source_type=SOURCE_TYPE,
            source_id=str(row["id"]),
            postings=(
                debit(wallet.held_account_id, reserved),
                credit(wallet.available_account_id, reserved),
            ),
        ),
    )
    return await advance(
        session, row["id"], status=status, final_entry_id=entry.id, failure_reason=failure_reason
    )


async def advance(session: AsyncSession, withdrawal_id: uuid.UUID, **values: Any) -> Withdrawal:
    """Write new values to a withdrawal whose row the caller has locked."""
    updated = await session.execute(
        update(_withdrawals)
        .where(_withdrawals.c.id == withdrawal_id)
        .values(updated_at=utcnow(), **values)
        .returning(_withdrawals)
    )
    return as_withdrawal(updated.mappings().one())


async def find(session: AsyncSession, withdrawal_id: uuid.UUID) -> Withdrawal | None:
    rows = await session.execute(select(_withdrawals).where(_withdrawals.c.id == withdrawal_id))
    row = rows.mappings().one_or_none()
    return as_withdrawal(row) if row is not None else None


async def overdue(
    session: AsyncSession, before: datetime, *, after: uuid.UUID | None = None, limit: int
) -> list[Withdrawal]:
    """One batch of the withdrawals a provider may have and should have been heard about
    by now, oldest first: submitted before ``before`` and not settled, or marked as being
    sent before it and never recorded as sent.

    ``after`` is the id the batch before this one ended on. Without it every call would
    return the same first rows, and a batch of withdrawals that cannot be advanced would
    keep every later one from ever being looked at.

    A withdrawal that is only held is not among them however old it is: it has never been
    sent, so there is nothing a provider could say about it.
    """
    query = select(_withdrawals).where(
        or_(
            and_(_withdrawals.c.status == "submitted", _withdrawals.c.submitted_at <= before),
            # The mark, or the sweeper's last request to send it again, whichever is later.
            and_(_withdrawals.c.status == "submitting", _withdrawals.c.updated_at <= before),
        )
    )
    if after is not None:
        query = query.where(_withdrawals.c.id > after)
    rows = await session.execute(query.order_by(_withdrawals.c.id).limit(limit))
    return [as_withdrawal(row) for row in rows.mappings()]


async def lock_held(session: AsyncSession, user_id: uuid.UUID, asset: str) -> list[RowMapping]:
    """A user's withdrawals of one asset that are reserved and were never sent, locked for
    the rest of the transaction, oldest first. The provider is certain not to have them."""
    rows = await session.execute(
        select(_withdrawals)
        .where(
            _withdrawals.c.user_id == user_id,
            _withdrawals.c.asset_code == asset,
            _withdrawals.c.status == "held",
        )
        .order_by(_withdrawals.c.id)
        .with_for_update()
    )
    return list(rows.mappings())


async def lock(session: AsyncSession, withdrawal_id: uuid.UUID) -> RowMapping | None:
    """A withdrawal's row, locked for the rest of the transaction."""
    rows = await session.execute(
        select(_withdrawals).where(_withdrawals.c.id == withdrawal_id).with_for_update()
    )
    return rows.mappings().one_or_none()


def as_withdrawal(row: RowMapping) -> Withdrawal:
    return Withdrawal(
        id=row["id"],
        user_id=row["user_id"],
        asset=row["asset_code"],
        amount=row["amount"],
        fee=row["fee"],
        kind=row["kind"],
        beneficiary_id=row["beneficiary_id"],
        to_address=row["to_address"],
        status=row["status"],
        provider=row["provider"],
        provider_ref=row["provider_ref"],
        provider_fee=row["provider_fee"],
        failure_reason=row["failure_reason"],
        hold_entry_id=row["hold_entry_id"],
        final_entry_id=row["final_entry_id"],
        initiated_by_type=row["initiated_by_type"],
        initiated_by_id=row["initiated_by_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        submitted_at=row["submitted_at"],
    )


def _actor(principal: Principal) -> audit.Actor:
    if principal.is_agent:
        return audit.Actor.agent(principal.actor_id)
    return audit.Actor.user(principal.actor_id)

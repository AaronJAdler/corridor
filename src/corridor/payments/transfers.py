"""Transfers between users: the whole movement in the caller's one transaction.

Every function takes the caller's session and never commits. A transfer, its journal entry,
its outbox event and its audit event are therefore committed together or not at all.
"""

import uuid
from typing import Any, Final, cast

from sqlalchemy import ColumnElement, RowMapping, Select, Table, insert, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, ledger, outbox, risk, wallets
from corridor.identity import Principal, Scope
from corridor.ledger import AccountKind, EntryDraft, credit, debit
from corridor.payments import fees
from corridor.payments.errors import (
    MAX_MEMO_LENGTH,
    CannotTransferToSelf,
    DuplicateTransfer,
    InvalidMemo,
    RecipientNotFound,
    TransferNotFound,
)
from corridor.payments.models import TransferRow
from corridor.payments.types import Transfer
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import advisory_xact_lock, lock_key
from corridor.platform.money import InvalidAmount
from corridor.platform.pagination import (
    DEFAULT_LIMIT,
    InvalidCursor,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)
from corridor.risk import MoneyMovement

# A Core table: every statement against it is written out below.
_transfers = cast(Table, TransferRow.__table__)

ENTRY_KIND: Final = "transfer"
SOURCE_TYPE: Final = "transfer"
TRANSFER_COMPLETED: Final = "transfer.completed"
CURSOR_KIND: Final = "transfers"

# The namespace of the per-user lock that every movement of money out of a wallet takes.
MONEY_OUT_LOCK: Final = "money_out"


async def create_transfer(
    session: AsyncSession,
    principal: Principal,
    *,
    transfer_id: uuid.UUID,
    recipient: str,
    asset: str,
    amount: int,
    memo: str | None,
    settings: Settings,
) -> Transfer:
    """Send ``amount`` minor units of ``asset`` from the principal's user to ``recipient``.

    The sender pays the amount and the fee; the recipient receives the amount. The caller
    makes ``transfer_id``, a new one for each attempt.

    The steps run in the order the locks have to be taken in: the sender's money-out lock
    before anything is decided about their money, and the balance rows, inside
    ``ledger.post_entry``, after it. Every refusal comes before the first write, so it
    leaves the caller's transaction usable and nothing behind.
    """
    identity.require_scope(principal, Scope.TRANSFERS_CREATE)
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise InvalidAmount("The amount must be greater than zero.")
    if memo is not None and len(memo) > MAX_MEMO_LENGTH:
        raise InvalidMemo

    sender_id = principal.user_id
    payee = await identity.find_user(session, recipient)
    if payee is None:
        raise RecipientNotFound
    if payee.id == sender_id:
        raise CannotTransferToSelf

    # Only the sender moves money out, so only the sender is locked. Balance rows are per
    # asset and limits are not: this is what makes one user's outgoing movements queue,
    # whatever assets they are in.
    await advisory_xact_lock(session, [lock_key(MONEY_OUT_LOCK, sender_id)])

    await risk.authorize(
        session,
        MoneyMovement(
            kind="transfer",
            user_id=sender_id,
            principal=principal,
            asset=asset,
            amount=amount,
            counterparty_id=payee.id,
        ),
    )

    fee = fees.transfer_fee(amount, settings)

    source = await wallets.resolve(session, sender_id, asset)
    destination = await wallets.resolve(session, payee.id, asset)
    postings = [
        debit(source.available_account_id, amount + fee),
        credit(destination.available_account_id, amount),
    ]
    if fee > 0:
        revenue = await ledger.open_account(session, AccountKind.FEE_REVENUE, asset)
        postings.append(credit(revenue.id, fee))
    entry = await ledger.post_entry(
        session,
        EntryDraft(
            kind=ENTRY_KIND,
            source_type=SOURCE_TYPE,
            source_id=str(transfer_id),
            postings=tuple(postings),
        ),
    )

    if not entry.created:
        # The ledger found this id already posted and moved nothing. Inserting the row
        # again would only fail later, as a primary-key violation with no explanation.
        raise DuplicateTransfer(f"transfer {transfer_id} already exists")

    inserted = await session.execute(
        insert(_transfers)
        .values(
            id=transfer_id,
            sender_id=sender_id,
            recipient_id=payee.id,
            asset_code=asset,
            amount=amount,
            fee=fee,
            status="completed",
            entry_id=entry.id,
            memo=memo,
            initiated_by_type=principal.actor_type,
            initiated_by_id=principal.actor_id,
            created_at=utcnow(),
        )
        .returning(_transfers)
    )
    transfer = _transfer(inserted.mappings().one())

    await outbox.enqueue(
        session,
        TRANSFER_COMPLETED,
        {
            "transfer_id": str(transfer.id),
            "sender_id": str(transfer.sender_id),
            "recipient_id": str(transfer.recipient_id),
            "entry_id": str(transfer.entry_id),
            "asset": transfer.asset,
            # Minor units as a string: a JSON number would lose precision above 2^53.
            "amount": str(transfer.amount),
            "fee": str(transfer.fee),
        },
    )
    await audit.record(
        session,
        actor=(
            audit.Actor.agent(principal.actor_id)
            if principal.is_agent
            else audit.Actor.user(principal.actor_id)
        ),
        action="transfer.created",
        principal_id=sender_id,
        resource_type="transfer",
        resource_id=transfer.id,
        details={
            "recipient_id": str(transfer.recipient_id),
            "asset": transfer.asset,
            "amount": str(transfer.amount),
            "fee": str(transfer.fee),
        },
    )
    return transfer


async def get_transfer(
    session: AsyncSession, principal: Principal, transfer_id: uuid.UUID
) -> Transfer:
    """A transfer the principal's user sent or received.

    Anyone else's transfer is answered exactly as one that does not exist, so that an id
    cannot be probed to learn whether a transfer was made.
    """
    identity.require_scope(principal, Scope.TRANSFERS_READ)
    rows = await session.execute(select(_transfers).where(_transfers.c.id == transfer_id))
    row = rows.mappings().one_or_none()
    if row is None or principal.user_id not in (row["sender_id"], row["recipient_id"]):
        raise TransferNotFound
    return _transfer(row)


async def list_transfers(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[Transfer]:
    """One page of what the principal's user sent and received, newest first.

    Pages are cut on the transfer id, which is a UUIDv7 and so in order of creation. A
    transfer made after a page was read is newer than everything on it and belongs to no
    later page.
    """
    identity.require_scope(principal, Scope.TRANSFERS_READ)
    limit = clamp_limit(limit)
    user_id = principal.user_id
    # The cursor is tied to the user, so one from another user's list is refused instead of
    # being read as a place in this one.
    scope = str(user_id)
    before = _position(cursor, scope) if cursor is not None else None

    def side(party: ColumnElement[uuid.UUID]) -> Select[Any]:
        # One more than the page, to learn whether anything follows it without a second query.
        newest = select(_transfers).where(party == user_id)
        if before is not None:
            newest = newest.where(_transfers.c.id < before)
        return newest.order_by(_transfers.c.id.desc()).limit(limit + 1)

    # The two sides are read separately, each through its own (party, id) index, and then
    # merged. A single OR over both columns could use neither index for the order. No
    # transfer is on both sides: the sender is never the recipient.
    merged = union_all(side(_transfers.c.sender_id), side(_transfers.c.recipient_id)).subquery()
    rows = await session.execute(select(merged).order_by(merged.c.id.desc()).limit(limit + 1))
    found = [_transfer(row) for row in rows.mappings()]

    shown = found[:limit]
    more = len(found) > limit
    return Page(
        items=tuple(shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=scope, position=str(shown[-1].id))
            if more
            else None
        ),
    )


def _position(cursor: str, scope: str) -> uuid.UUID:
    position = decode_cursor(cursor, kind=CURSOR_KIND, scope=scope)
    if not isinstance(position, str):
        raise InvalidCursor
    try:
        return uuid.UUID(position)
    except ValueError:
        raise InvalidCursor from None


def _transfer(row: RowMapping) -> Transfer:
    return Transfer(
        id=row["id"],
        sender_id=row["sender_id"],
        recipient_id=row["recipient_id"],
        asset=row["asset_code"],
        amount=row["amount"],
        fee=row["fee"],
        status=row["status"],
        entry_id=row["entry_id"],
        memo=row["memo"],
        initiated_by_type=row["initiated_by_type"],
        initiated_by_id=row["initiated_by_id"],
        created_at=row["created_at"],
    )

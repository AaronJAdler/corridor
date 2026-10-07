"""Deposits: money that arrives at a provider and is credited when Corridor hears of it.

A deposit begins outside Corridor, so everything here is driven by provider events, which
arrive at least once and in no particular order. Each ``apply_*`` function is an entry
point that owns its transaction, and each can be given the same event any number of times:
one provider deposit is one row, found by the provider's id for it, and the row's status
under a lock decides whether an event still has anything to do.

A deposit is attributed to a user by one thing only: the virtual account or the address it
arrived at, looked up among the instructions Corridor itself issued. Nothing the event says
about whose it is, is believed.
"""

import uuid
from collections.abc import Mapping
from typing import Any, Final, cast

from pydantic import Field
from sqlalchemy import RowMapping, Table, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, ledger, outbox, risk, wallets
from corridor.identity import Principal, Scope
from corridor.ledger import AccountKind, EntryDraft, credit, debit
from corridor.payments import instructions
from corridor.payments.errors import (
    DepositNotFound,
    DepositNotInSuspense,
    DepositOwnerClosed,
    MalformedProviderEvent,
    ProviderEventMismatch,
)
from corridor.payments.events import ProviderEvent, amount_of, parse, reason_code
from corridor.payments.models import DepositRow
from corridor.payments.types import (
    BANK_PROVIDER,
    CUSTODY_PROVIDER,
    Deposit,
    FlowKind,
    SuspenseSettlement,
)
from corridor.platform.clock import utcnow
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.logging import get_logger
from corridor.platform.money import get_asset
from corridor.platform.pagination import (
    DEFAULT_LIMIT,
    InvalidCursor,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)

log = get_logger(__name__)

# A Core table: every statement against it is written out below.
_deposits = cast(Table, DepositRow.__table__)

SOURCE_TYPE: Final = "deposit"
ENTRY_KIND: Final = "deposit"
SUSPENSE_ENTRY_KIND: Final = "deposit_suspense"
RELEASE_ENTRY_KIND: Final = "deposit_release"
DEPOSIT_COMPLETED: Final = "deposit.completed"
CURSOR_KIND: Final = "deposits"

# Where the money of a deposit is, on Corridor's side of the provider.
ASSET_ACCOUNT: Final[Mapping[str, AccountKind]] = {
    "bank": AccountKind.BANK_SETTLEMENT,
    "chain": AccountKind.CUSTODY_OMNIBUS,
}
# What each kind of provider moves: a bank moves fiat and a chain moves stablecoins.
_ASSET_KIND: Final = {"bank": "fiat", "chain": "stablecoin"}


class _BankDepositReceived(ProviderEvent):
    """``deposit.received``. The contract's ``customer_reference`` is left out on purpose:
    a field that is not read cannot be believed by mistake."""

    deposit_id: str = Field(min_length=1)
    virtual_account_id: str = Field(min_length=1)
    asset: str
    amount: str
    sender_name: str
    reference: str


class _ChainDeposit(ProviderEvent):
    """``deposit.detected`` and ``deposit.confirmed``, which carry the same fields. As with
    a bank deposit, ``customer_reference`` is not read."""

    deposit_id: str = Field(min_length=1)
    address_id: str = Field(min_length=1)
    address: str
    asset: str
    amount: str
    tx_hash: str = Field(min_length=1)
    from_address: str
    confirmations: int = Field(ge=0)


class _ChainDepositFailed(ProviderEvent):
    """``deposit.failed``."""

    deposit_id: str = Field(min_length=1)
    asset: str
    amount: str
    tx_hash: str
    reason: str


async def apply_bank_deposit_received(db: Database, data: Mapping[str, Any]) -> None:
    """A bank deposit has arrived: record it and credit it, once.

    The row is inserted before anything is posted, and an event whose row already exists
    credits nothing. A concurrent copy of the event waits on that insert until the first
    has committed, and then finds the row.

    The row that exists is still compared with the event. A second event under the same
    deposit id with another asset or another amount is not a repeat of the first, and is
    refused aloud instead of being taken for one.
    """
    event = parse(_BankDepositReceived, data)
    amount = _amount(event.amount, event.asset, "bank")

    async def work(session: AsyncSession) -> None:
        user_id = await _attribute(session, BANK_PROVIDER, event.virtual_account_id, event.asset)
        screened = await risk.screen_party(session, kind="name", value=event.sender_name)
        deposit = await _insert(
            session,
            user_id=user_id,
            asset=event.asset,
            amount=amount,
            provider=BANK_PROVIDER,
            provider_ref=event.deposit_id,
            kind="bank",
            status="pending",
            tx_hash=None,
        )
        if deposit is None:
            check_same(await _lock(session, BANK_PROVIDER, event.deposit_id), event.asset, amount)
            return
        await _credit_screened(session, deposit, user_id, screened)

    await db.run(work)


async def apply_chain_deposit_detected(db: Database, data: Mapping[str, Any]) -> None:
    """A transaction has been seen, with no confirmations. It is recorded as pending so the
    user can see it coming, and moves nothing: the funds are not final."""
    event = parse(_ChainDeposit, data)
    amount = _amount(event.amount, event.asset, "chain")

    async def work(session: AsyncSession) -> None:
        await _insert_chain(session, event, amount)

    await db.run(work)


async def apply_chain_deposit_confirmed(db: Database, data: Mapping[str, Any]) -> None:
    """A transaction is final: credit it, unless it already was.

    The detection may never have arrived, so the row is inserted here too if it is
    missing. The row is then locked, and only a pending deposit is credited.
    """
    event = parse(_ChainDeposit, data)
    amount = _amount(event.amount, event.asset, "chain")

    async def work(session: AsyncSession) -> None:
        await _insert_chain(session, event, amount)
        deposit = await _lock(session, CUSTODY_PROVIDER, event.deposit_id)
        check_same(deposit, event.asset, amount)
        if deposit["status"] != "pending":
            if deposit["status"] == "failed":
                log.error("deposit.confirmed_after_failure", deposit_id=str(deposit["id"]))
            return
        # Decided now, from the address it arrived at, whatever the detection recorded.
        user_id = await _attribute(session, CUSTODY_PROVIDER, event.address_id, event.asset)
        screened = await risk.screen_party(session, kind="address", value=event.from_address)
        await _credit_screened(session, deposit, user_id, screened)

    await db.run(work)


async def apply_chain_deposit_failed(db: Database, data: Mapping[str, Any]) -> None:
    """A transaction was dropped before it was final. It was never credited, so there is
    nothing to undo: the deposit is marked failed, and stays failed."""
    event = parse(_ChainDepositFailed, data)
    amount = _amount(event.amount, event.asset, "chain")

    async def work(session: AsyncSession) -> None:
        # If the detection never arrived, the failure is what records the deposit, so that
        # a detection delivered late finds it already failed and does not reopen it.
        inserted = await _insert(
            session,
            user_id=None,
            asset=event.asset,
            amount=amount,
            provider=CUSTODY_PROVIDER,
            provider_ref=event.deposit_id,
            kind="chain",
            status="failed",
            tx_hash=event.tx_hash or None,
        )
        if inserted is not None:
            return
        deposit = await _lock(session, CUSTODY_PROVIDER, event.deposit_id)
        if deposit["status"] == "pending":
            await set_status(session, deposit["id"], "failed")
            await audit.record(
                session,
                actor=audit.Actor.provider(CUSTODY_PROVIDER),
                action="deposit.failed",
                principal_id=deposit["user_id"],
                resource_type="deposit",
                resource_id=deposit["id"],
                details={"reason": reason_code(event.reason)},
            )
        elif deposit["status"] != "failed":
            # The provider says a deposit it confirmed was dropped, which its contract says
            # cannot happen. The credit stands; a person has to look at this one.
            log.error(
                "deposit.failed_after_credit",
                deposit_id=str(deposit["id"]),
                status=deposit["status"],
            )

    await db.run(work)


async def apply_statement_deposit(
    db: Database,
    *,
    provider: str,
    provider_ref: str,
    account_ref: str,
    asset: str,
    amount: int,
    tx_hash: str | None = None,
    sender: str | None = None,
) -> None:
    """A deposit read from a provider's statement, whose event never arrived: record it and
    credit it, unless it already was. For reconciliation.

    An entry point, and idempotent as the events are: the row and its status under a lock
    decide whether there is anything left to do, so this can race the late event, or
    another run, and the deposit is still credited once.

    A statement says less than an event does. The account or address the money arrived
    at is on it, so the deposit is attributed exactly as an event's is. Who sent it usually
    is not. ``sender`` is the name, for a bank, or the address, for a chain, when the
    statement does give it, and is then screened as an event's is. When it is not given
    there is nothing to ask the deny list about, and the deposit goes where one whose
    sender was given as nothing goes: to the user whose instruction it arrived at, or to
    suspense when it arrived at nobody's. That nothing was screened is written to the
    audit record of the credit, as ``screened: false``, for whoever reviews it later.
    """
    kind: FlowKind
    party: risk.PartyKind
    if provider == BANK_PROVIDER:
        kind, party = "bank", "name"
    elif provider == CUSTODY_PROVIDER:
        kind, party = "chain", "address"
    else:
        raise ValueError(f"no deposits arrive through {provider!r}")
    if not provider_ref:
        raise MalformedProviderEvent("a statement line with no id")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise MalformedProviderEvent("a deposit of no amount")
    if get_asset(asset).kind != _ASSET_KIND[kind]:
        raise MalformedProviderEvent(f"an asset that does not move by {kind}")
    can_screen = sender is not None and sender.strip() != ""

    async def work(session: AsyncSession) -> None:
        await _insert(
            session,
            user_id=await _attribute(session, provider, account_ref, asset),
            asset=asset,
            amount=amount,
            provider=provider,
            provider_ref=provider_ref,
            kind=kind,
            status="pending",
            tx_hash=tx_hash,
        )
        deposit = await _lock(session, provider, provider_ref)
        check_same(deposit, asset, amount)
        if deposit["status"] != "pending":
            # Credited by its event after all, or failed, or returned before it was seen.
            return
        # Decided now, from where it arrived, whatever an earlier detection recorded.
        user_id = await _attribute(session, provider, account_ref, asset)
        screened: risk.ScreeningOutcome = "clear"
        if can_screen and sender is not None:
            screened = await risk.screen_party(session, kind=party, value=sender)
        await _credit_screened(session, deposit, user_id, screened, was_screened=can_screen)

    await db.run(work)


async def get_suspense_deposit(session: AsyncSession, deposit_id: uuid.UUID) -> Deposit:
    """A deposit that is in suspense now, for an operator who asks to release or return it.

    Read without a lock: what is asked for waits for another operator, and whoever carries
    it out looks at the deposit again, under its lock.
    """
    rows = await session.execute(select(_deposits).where(_deposits.c.id == deposit_id))
    row = rows.mappings().one_or_none()
    if row is None:
        raise DepositNotFound
    if row["status"] != "suspense":
        raise DepositNotInSuspense
    return as_deposit(row)


async def lock_in_suspense(session: AsyncSession, deposit_id: uuid.UUID) -> RowMapping:
    """A deposit's row, locked for the rest of the transaction, provided it is in suspense.

    Every way money leaves suspense comes through here first: an operator clearing a
    review, an approved adjustment that releases a deposit and one that returns it. The
    caller changes the status in the same transaction, so of any two of them one finds the
    deposit in suspense and the other is refused, and the money leaves once.
    """
    rows = await session.execute(
        select(_deposits).where(_deposits.c.id == deposit_id).with_for_update()
    )
    deposit = rows.mappings().one_or_none()
    if deposit is None:
        # The caller got the id from a review or an adjustment of this deposit: a bug.
        raise LookupError(f"there is no deposit {deposit_id} in suspense")
    if deposit["status"] != "suspense":
        raise DepositNotInSuspense
    return deposit


async def release_from_suspense(
    session: AsyncSession,
    deposit_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    actor: audit.Actor,
    metadata: Mapping[str, str] | None = None,
) -> SuspenseSettlement:
    """Credit a deposit that is in suspense to ``user_id``, and make it theirs.

    For an operator who has cleared the review that kept it there, and for an approved
    adjustment that releases it. The money moves from suspense to the user's available
    balance in one entry, the deposit is recorded as completed and as that user's, and
    from then on it is a deposit like any other of theirs: a bank that takes it back takes
    it from them. The row is locked and its status looked at first, so a deposit is
    released once: one that is no longer in suspense, because it was released or returned
    already or the bank took it back, is refused.

    A closed account receives nothing. A restricted one does: the restriction is on money
    going out. ``metadata`` is added to the journal entry's.
    """
    deposit = await lock_in_suspense(session, deposit_id)
    if (await identity.get_user(session, user_id)).status == "closed":
        raise DepositOwnerClosed
    asset, amount, provider = deposit["asset_code"], deposit["amount"], deposit["provider"]
    suspense = await ledger.open_account(session, AccountKind.SUSPENSE, asset)
    wallet = await wallets.resolve(session, user_id, asset)
    entry = await ledger.post_entry(
        session,
        EntryDraft(
            kind=RELEASE_ENTRY_KIND,
            source_type=SOURCE_TYPE,
            source_id=ledger_source_id(provider, deposit["provider_ref"]),
            postings=(debit(suspense.id, amount), credit(wallet.available_account_id, amount)),
            metadata={**(metadata or {}), "provider": provider, "deposit_id": str(deposit_id)},
        ),
    )
    # ``entry_id`` stays the entry that brought the money onto the books: reconciliation
    # reads it as when the deposit reached the provider's account in the ledger.
    updated = await session.execute(
        update(_deposits)
        .where(_deposits.c.id == deposit_id)
        .values(status="completed", user_id=user_id, updated_at=utcnow())
        .returning(_deposits)
    )
    await audit.record(
        session,
        actor=actor,
        action="deposit.released",
        principal_id=user_id,
        resource_type="deposit",
        resource_id=deposit_id,
        details={"asset": asset, "amount": str(amount), "entry_id": str(entry.id)},
    )
    await outbox.enqueue(
        session,
        DEPOSIT_COMPLETED,
        {
            "deposit_id": str(deposit_id),
            "user_id": str(user_id),
            "asset": asset,
            "amount": str(amount),
            "entry_id": str(entry.id),
        },
    )
    return SuspenseSettlement(deposit=as_deposit(updated.mappings().one()), entry_id=entry.id)


async def get_deposit(
    session: AsyncSession, principal: Principal, deposit_id: uuid.UUID
) -> Deposit:
    """A deposit credited, or still on its way, to the principal's user.

    Anyone else's deposit, and one in suspense, is answered exactly as one that does not
    exist.
    """
    identity.require_scope(principal, Scope.DEPOSITS_READ)
    rows = await session.execute(select(_deposits).where(_deposits.c.id == deposit_id))
    row = rows.mappings().one_or_none()
    if row is None or row["user_id"] != principal.user_id:
        raise DepositNotFound
    return as_deposit(row)


async def list_deposits(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[Deposit]:
    """One page of the deposits of the principal's user, newest first.

    Pages are cut on the deposit id, which is a UUIDv7 and so in order of creation.
    """
    identity.require_scope(principal, Scope.DEPOSITS_READ)
    limit = clamp_limit(limit)
    # The cursor is tied to the user, so one from another user's list is refused.
    scope = str(principal.user_id)
    query = select(_deposits).where(_deposits.c.user_id == principal.user_id)
    if cursor is not None:
        query = query.where(_deposits.c.id < position_of(cursor, CURSOR_KIND, scope))
    # One more than the page, to learn whether anything follows it without a second query.
    rows = await session.execute(query.order_by(_deposits.c.id.desc()).limit(limit + 1))
    found = [as_deposit(row) for row in rows.mappings()]
    shown = found[:limit]
    return Page(
        items=tuple(shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=scope, position=str(shown[-1].id))
            if len(found) > limit
            else None
        ),
    )


def position_of(cursor: str, kind: str, scope: str) -> uuid.UUID:
    """The id a cursor of one of this module's lists names."""
    position = decode_cursor(cursor, kind=kind, scope=scope)
    if not isinstance(position, str):
        raise InvalidCursor
    try:
        return uuid.UUID(position)
    except ValueError:
        raise InvalidCursor from None


async def lock_deposit(
    session: AsyncSession, provider: str, provider_ref: str
) -> RowMapping | None:
    """A provider's deposit, locked for the rest of the transaction, if it was recorded."""
    rows = await session.execute(
        select(_deposits)
        .where(_deposits.c.provider == provider, _deposits.c.provider_ref == provider_ref)
        .with_for_update()
    )
    return rows.mappings().one_or_none()


async def find_deposit(
    session: AsyncSession, provider: str, provider_ref: str
) -> RowMapping | None:
    rows = await session.execute(
        select(_deposits).where(
            _deposits.c.provider == provider, _deposits.c.provider_ref == provider_ref
        )
    )
    return rows.mappings().one_or_none()


async def record_returned_unseen(
    session: AsyncSession, provider_ref: str, asset: str, amount: int
) -> RowMapping | None:
    """Record a bank deposit that was returned before it was ever received, as returned.

    Events arrive in no particular order, and a return can overtake its deposit. The row
    written here is what stops the deposit when it does arrive: it finds itself recorded,
    and returned, and credits nothing. Returns None if the deposit turned out to be
    recorded after all, by a transaction that committed while this one waited.
    """
    return await _insert(
        session,
        user_id=None,
        asset=asset,
        amount=amount,
        provider=BANK_PROVIDER,
        provider_ref=provider_ref,
        kind="bank",
        status="returned",
        tx_hash=None,
    )


def bank_amount(text: str, asset: str) -> int:
    """An amount of a bank event in minor units, refusing an asset no bank moves."""
    return _amount(text, asset, "bank")


def ledger_source_id(provider: str, provider_ref: str) -> str:
    """What a deposit's journal entries are filed under: the provider with its id for the
    deposit. Each provider numbers its own deposits, so the id alone could be two deposits,
    and the second would be taken for a repeat of the first and never credited."""
    return f"{provider}:{provider_ref}"


def _amount(text: str, asset: str, kind: FlowKind) -> int:
    amount = amount_of(text, asset)
    if get_asset(asset).kind != _ASSET_KIND[kind]:
        # A bank reporting a stablecoin, or a chain reporting pesos. There is no account
        # such money could be in.
        raise MalformedProviderEvent(f"an asset that does not move by {kind}")
    return amount


def check_same(deposit: RowMapping, asset: str, amount: int) -> None:
    """Refuse an event that names a recorded deposit and gives it another asset or amount."""
    if (deposit["asset_code"], deposit["amount"]) != (asset, amount):
        raise ProviderEventMismatch(
            f"deposit {deposit['id']}: the event's asset or amount is not the recorded one"
        )


async def _attribute(
    session: AsyncSession, provider: str, account_ref: str, asset: str
) -> uuid.UUID | None:
    """Whose deposit this is: the user Corridor gave this account or address to, for this
    asset. Nobody's, if there is no such instruction."""
    instruction = await instructions.find_by_provider_ref(session, provider, account_ref)
    if instruction is None or instruction.asset != asset:
        return None
    return instruction.user_id


async def _insert_chain(session: AsyncSession, event: _ChainDeposit, amount: int) -> None:
    await _insert(
        session,
        user_id=await _attribute(session, CUSTODY_PROVIDER, event.address_id, event.asset),
        asset=event.asset,
        amount=amount,
        provider=CUSTODY_PROVIDER,
        provider_ref=event.deposit_id,
        kind="chain",
        status="pending",
        tx_hash=event.tx_hash,
    )


async def _insert(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    asset: str,
    amount: int,
    provider: str,
    provider_ref: str,
    kind: FlowKind,
    status: str,
    tx_hash: str | None,
) -> RowMapping | None:
    """Record a provider's deposit, or return None if it is recorded already.

    This is what makes every event idempotent. A second transaction inserting the same
    deposit waits here for the first to finish, and then inserts nothing.
    """
    now = utcnow()
    inserted = await session.execute(
        pg_insert(_deposits)
        .values(
            id=new_id(),
            user_id=user_id,
            asset_code=asset,
            amount=amount,
            provider=provider,
            provider_ref=provider_ref,
            kind=kind,
            status=status,
            entry_id=None,
            tx_hash=tx_hash,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_deposits_provider_provider_ref")
        .returning(_deposits)
    )
    row: RowMapping | None = inserted.mappings().one_or_none()
    return row


async def _lock(session: AsyncSession, provider: str, provider_ref: str) -> RowMapping:
    deposit = await lock_deposit(session, provider, provider_ref)
    if deposit is None:
        raise RuntimeError(f"deposit {provider_ref} of {provider} was recorded and is gone")
    return deposit


async def set_status(session: AsyncSession, deposit_id: uuid.UUID, status: str) -> RowMapping:
    updated = await session.execute(
        update(_deposits)
        .where(_deposits.c.id == deposit_id)
        .values(status=status, updated_at=utcnow())
        .returning(_deposits)
    )
    row: RowMapping = updated.mappings().one()
    return row


async def _credit_screened(
    session: AsyncSession,
    deposit: RowMapping,
    user_id: uuid.UUID | None,
    screened: risk.ScreeningOutcome,
    *,
    was_screened: bool | None = None,
) -> None:
    """Credit a pending deposit to its user, unless screening stopped its sender.

    Money from a sender on the deny list did arrive, so it is booked, but to suspense and
    to nobody, exactly as a deposit that could not be attributed. The review is what
    remembers whose it would have been, for the operator who releases or returns it.
    """
    if screened == "clear":
        await _credit(session, deposit, user_id, was_screened=was_screened)
        return
    await _credit(session, deposit, None, was_screened=was_screened)
    await risk.open_review(
        session,
        subject_type="deposit",
        subject_id=deposit["id"],
        outcome=screened,
        user_id=user_id,
    )


async def _credit(
    session: AsyncSession,
    deposit: RowMapping,
    user_id: uuid.UUID | None,
    *,
    was_screened: bool | None = None,
) -> None:
    """Post the entry for a pending deposit whose row this transaction holds, and close it.

    To the available balance of ``user_id`` if the deposit is somebody's, and to suspense
    if it is nobody's. Either way the provider's side is debited: the money did arrive.
    ``was_screened`` is given for a deposit read from a statement, and is written to the
    audit record: such a deposit may have had no sender to screen.
    """
    asset, amount, provider = deposit["asset_code"], deposit["amount"], deposit["provider"]
    received = await ledger.open_account(
        session, ASSET_ACCOUNT[deposit["kind"]], asset, provider=provider
    )
    if user_id is not None:
        wallet = await wallets.resolve(session, user_id, asset)
        target, entry_kind, status = wallet.available_account_id, ENTRY_KIND, "completed"
    else:
        suspense = await ledger.open_account(session, AccountKind.SUSPENSE, asset)
        target, entry_kind, status = suspense.id, SUSPENSE_ENTRY_KIND, "suspense"

    entry = await ledger.post_entry(
        session,
        EntryDraft(
            kind=entry_kind,
            source_type=SOURCE_TYPE,
            source_id=ledger_source_id(provider, deposit["provider_ref"]),
            postings=(debit(received.id, amount), credit(target, amount)),
            metadata={"provider": provider, "deposit_id": str(deposit["id"])},
        ),
    )
    await session.execute(
        update(_deposits)
        .where(_deposits.c.id == deposit["id"])
        .values(status=status, user_id=user_id, entry_id=entry.id, updated_at=utcnow())
    )
    await audit.record(
        session,
        actor=audit.Actor.provider(provider),
        action="deposit.completed" if user_id is not None else "deposit.suspended",
        principal_id=user_id,
        resource_type="deposit",
        resource_id=deposit["id"],
        details={
            "asset": asset,
            "amount": str(amount),
            "entry_id": str(entry.id),
            **({} if was_screened is None else {"screened": was_screened}),
        },
    )
    if user_id is not None:
        await outbox.enqueue(
            session,
            DEPOSIT_COMPLETED,
            {
                "deposit_id": str(deposit["id"]),
                "user_id": str(user_id),
                "asset": asset,
                # Minor units as a string: a JSON number would lose precision above 2^53.
                "amount": str(amount),
                "entry_id": str(entry.id),
            },
        )


def as_deposit(row: RowMapping) -> Deposit:
    return Deposit(
        id=row["id"],
        user_id=row["user_id"],
        asset=row["asset_code"],
        amount=row["amount"],
        kind=row["kind"],
        status=row["status"],
        provider=row["provider"],
        provider_ref=row["provider_ref"],
        entry_id=row["entry_id"],
        tx_hash=row["tx_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

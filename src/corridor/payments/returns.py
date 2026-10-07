"""Returned deposits: a bank takes back a deposit, days after it was credited.

By then the user may have spent it. The return takes what is still in the wallet, books
the rest as what the user owes, and restricts the user until that is settled. The bank's
side is credited with the whole amount either way: that money is gone.

Two more returns are here, because they post the same entry. An operator sends back a
deposit that is in suspense, and reconciliation records one the bank took back before
Corridor had heard of it.
"""

import uuid
from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import Field
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, ledger, risk, wallets
from corridor.ledger import AccountKind, EntryDraft, PostingDraft, credit, debit
from corridor.payments import deposits, withdrawals
from corridor.payments.errors import DepositNotReceived
from corridor.payments.events import ProviderEvent, parse, reason_code
from corridor.payments.transfers import MONEY_OUT_LOCK
from corridor.payments.types import BANK_PROVIDER, SuspenseSettlement
from corridor.platform.db import Database, advisory_xact_lock, lock_key
from corridor.platform.logging import get_logger

log = get_logger(__name__)

RETURN_ENTRY_KIND: Final = "deposit_return"
RESTRICTION_REASON: Final = "returned deposit shortfall"
# Why a withdrawal that was never sent was given back: the money it reserved is the bank's.
WITHDRAWAL_RELEASE_REASON: Final = "deposit_returned"


class _BankDepositReturned(ProviderEvent):
    """``deposit.returned``."""

    deposit_id: str = Field(min_length=1)
    asset: str
    amount: str
    reason: str


async def apply_bank_deposit_returned(db: Database, data: Mapping[str, Any]) -> None:
    """The sending bank recalled a deposit: reverse it, once.

    An entry point. The locks are taken in the order every money path takes them: the
    user's money-out lock, so that nothing else is spending the balance this is about to
    take from, and then the deposit's row. Only a deposit that is still credited is
    reversed, so a repeated return finds it returned and does nothing.

    A return can arrive before its deposit. There is then nothing to reverse, and the
    deposit is recorded as returned already, so that it credits nothing when it arrives.
    """
    event = parse(_BankDepositReturned, data)
    amount = deposits.bank_amount(event.amount, event.asset)
    reason = reason_code(event.reason)

    async def work(session: AsyncSession) -> None:
        # Read without a lock first, only to learn whose money-out lock comes before the
        # row lock. A deposit's user changes once at most, when an operator releases it
        # from suspense, and that is looked for again below, under the row's lock.
        seen = await deposits.find_deposit(session, BANK_PROVIDER, event.deposit_id)
        if seen is None:
            unseen = await deposits.record_returned_unseen(
                session, event.deposit_id, event.asset, amount
            )
            if unseen is None:
                # Received by a transaction that committed between the read and the
                # insert. Raised so that the return is delivered again, and finds it.
                raise DepositNotReceived(
                    "a deposit was received while its return was being recorded"
                )
            log.warning("deposit.returned_before_received", deposit_id=str(unseen["id"]))
            await audit.record(
                session,
                actor=audit.Actor.provider(BANK_PROVIDER),
                action="deposit.returned",
                principal_id=None,
                resource_type="deposit",
                resource_id=unseen["id"],
                details={
                    "asset": event.asset,
                    "amount": str(amount),
                    "shortfall": "0",
                    "reason": reason,
                    "received": False,
                },
            )
            return
        if seen["user_id"] is not None:
            await advisory_xact_lock(session, [lock_key(MONEY_OUT_LOCK, seen["user_id"])])
        deposit = await deposits.lock_deposit(session, BANK_PROVIDER, event.deposit_id)
        if deposit is None:
            raise RuntimeError(f"deposit {seen['id']} was recorded and is gone")
        deposits.check_same(deposit, event.asset, amount)
        if deposit["user_id"] != seen["user_id"]:
            # Released from suspense to a user between the read and the lock, so the
            # money-out lock held here is not that user's. Raised so that the return is
            # delivered again, and takes the right one.
            raise DepositNotReceived("a deposit was released while its return was being recorded")

        if deposit["status"] == "completed":
            shortfall = await _take_back(session, deposit)
        elif deposit["status"] == "suspense":
            shortfall = 0
            await _post_out_of_suspense(session, deposit)
        else:
            if deposit["status"] != "returned":
                log.error(
                    "deposit.returned_in_unexpected_state",
                    deposit_id=str(deposit["id"]),
                    status=deposit["status"],
                )
            return

        await deposits.set_status(session, deposit["id"], "returned")
        await audit.record(
            session,
            actor=audit.Actor.provider(BANK_PROVIDER),
            action="deposit.returned",
            principal_id=deposit["user_id"],
            resource_type="deposit",
            resource_id=deposit["id"],
            details={
                "asset": event.asset,
                "amount": str(amount),
                "shortfall": str(shortfall),
                "reason": reason,
            },
        )

    await db.run(work)


async def return_from_suspense(
    session: AsyncSession,
    deposit_id: uuid.UUID,
    *,
    actor: audit.Actor,
    metadata: Mapping[str, str] | None = None,
) -> SuspenseSettlement:
    """Take a deposit that is in suspense off the books, as sent back through the provider
    it arrived at. Sending it is the operator's to do with the provider.

    For an approved adjustment. The row is locked and its status looked at first, as for a
    release, and the deposit is recorded as returned in the transaction that posts the
    entry: one that was released, or returned, or taken back by its bank already is
    refused, and one returned here has nothing left for any of those to take.
    ``metadata`` is added to the journal entry's.
    """
    deposit = await deposits.lock_in_suspense(session, deposit_id)
    entry_id = await _post_out_of_suspense(session, deposit, metadata=metadata)
    returned = await deposits.set_status(session, deposit_id, "returned")
    await audit.record(
        session,
        actor=actor,
        action="deposit.returned",
        principal_id=None,
        resource_type="deposit",
        resource_id=deposit_id,
        details={
            "asset": deposit["asset_code"],
            "amount": str(deposit["amount"]),
            "shortfall": "0",
            "reason": "operator_return",
            "entry_id": str(entry_id),
        },
    )
    return SuspenseSettlement(deposit=deposits.as_deposit(returned), entry_id=entry_id)


async def apply_statement_return(
    db: Database, *, provider: str, provider_ref: str, asset: str, amount: int
) -> Literal["recorded", "known"]:
    """A bank deposit that a statement shows as received and as returned, and that was
    never recorded here: record it as returned already. For reconciliation.

    An entry point. Nothing is credited and nothing is posted: the money came and went at
    the bank, and neither event reached Corridor. The row is what stops either of them
    when it does arrive, exactly as when a return overtakes its deposit. A deposit that is
    recorded after all is left to its own events, and the answer says so.
    """
    if provider != BANK_PROVIDER:
        raise ValueError(f"no deposits are returned through {provider!r}")

    async def work(session: AsyncSession) -> Literal["recorded", "known"]:
        unseen = await deposits.record_returned_unseen(session, provider_ref, asset, amount)
        if unseen is None:
            return "known"
        await audit.record(
            session,
            actor=audit.Actor.system("recon.repair"),
            action="deposit.returned",
            principal_id=None,
            resource_type="deposit",
            resource_id=unseen["id"],
            details={
                "asset": asset,
                "amount": str(amount),
                "shortfall": "0",
                "reason": "statement",
                "received": False,
            },
        )
        return "recorded"

    return await db.run(work)


async def _post_out_of_suspense(
    session: AsyncSession, deposit: RowMapping, *, metadata: Mapping[str, str] | None = None
) -> uuid.UUID:
    suspense = await ledger.open_account(session, AccountKind.SUSPENSE, deposit["asset_code"])
    return await _post(session, deposit, [debit(suspense.id, deposit["amount"])], metadata)


async def _take_back(session: AsyncSession, deposit: RowMapping) -> int:
    """Reverse a credited deposit out of its user's wallet. Returns the shortfall: how much
    of it was no longer there to take."""
    user_id, asset = deposit["user_id"], deposit["asset_code"]
    amount: int = deposit["amount"]
    await _release_held_withdrawals(session, user_id, asset)
    wallet = await wallets.resolve(session, user_id, asset)
    # Under the money-out lock this can only have grown by the time it is posted.
    available = await ledger.get_balance(session, wallet.available_account_id)
    taken = min(amount, available)
    shortfall = amount - taken

    # A posting of zero is not a posting, so each side is there only if it moves something.
    postings: list[PostingDraft] = []
    if taken > 0:
        postings.append(debit(wallet.available_account_id, taken))
    if shortfall > 0:
        owed = await ledger.open_account(
            session, AccountKind.USER_RECEIVABLE, asset, owner_id=user_id
        )
        postings.append(debit(owed.id, shortfall))
    await _post(session, deposit, postings)

    if shortfall > 0:
        user = await identity.get_user(session, user_id)
        # A closed account moves no money already, and cannot be restricted.
        if user.status != "closed":
            await risk.restrict_user(session, user_id, RESTRICTION_REASON)
    return shortfall


async def _release_held_withdrawals(session: AsyncSession, user_id: uuid.UUID, asset: str) -> None:
    """Give back what the user's unsent withdrawals of the asset have reserved, so that it
    is there to be taken.

    Money reserved for a withdrawal is out of the available balance, and a withdrawal that
    went on to be paid would send the bank's money away while the user is booked as owing
    it. Only ``held`` ones: those the provider is certain not to have. The caller holds
    the user's money-out lock, so no new one appears in between.
    """
    for row in await withdrawals.lock_held(session, user_id, asset):
        await withdrawals.release(
            session, row, status="failed", failure_reason=WITHDRAWAL_RELEASE_REASON
        )
        await audit.record(
            session,
            actor=audit.Actor.system("deposit.return"),
            action="withdrawal.failed",
            principal_id=user_id,
            resource_type="withdrawal",
            resource_id=row["id"],
            details={"provider": row["provider"], "reason": WITHDRAWAL_RELEASE_REASON},
        )


async def _post(
    session: AsyncSession,
    deposit: RowMapping,
    debits: list[PostingDraft],
    metadata: Mapping[str, str] | None = None,
) -> uuid.UUID:
    """Post the entry that takes a deposit back out through the provider it arrived at."""
    provider = deposit["provider"]
    received = await ledger.open_account(
        session, deposits.ASSET_ACCOUNT[deposit["kind"]], deposit["asset_code"], provider=provider
    )
    entry = await ledger.post_entry(
        session,
        EntryDraft(
            kind=RETURN_ENTRY_KIND,
            source_type=deposits.SOURCE_TYPE,
            source_id=deposits.ledger_source_id(provider, deposit["provider_ref"]),
            postings=(*debits, credit(received.id, deposit["amount"])),
            metadata={**(metadata or {}), "provider": provider, "deposit_id": str(deposit["id"])},
        ),
    )
    return entry.id

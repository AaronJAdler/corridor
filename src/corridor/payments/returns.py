"""Returned deposits: a bank takes back a deposit, days after it was credited.

By then the user may have spent it. The return takes what is still in the wallet, books
the rest as what the user owes, and restricts the user until that is settled. The bank's
side is credited with the whole amount either way: that money is gone.
"""

from collections.abc import Mapping
from typing import Any, Final

from pydantic import Field
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, ledger, wallets
from corridor.ledger import AccountKind, EntryDraft, PostingDraft, credit, debit
from corridor.payments import deposits
from corridor.payments.errors import DepositNotReceived
from corridor.payments.events import ProviderEvent, amount_of, parse
from corridor.payments.transfers import MONEY_OUT_LOCK
from corridor.payments.types import BANK_PROVIDER
from corridor.platform.db import Database, advisory_xact_lock, lock_key
from corridor.platform.logging import get_logger

log = get_logger(__name__)

RETURN_ENTRY_KIND: Final = "deposit_return"
RESTRICTION_REASON: Final = "returned deposit shortfall"


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
    """
    event = parse(_BankDepositReturned, data)
    amount = amount_of(event.amount, event.asset)

    async def work(session: AsyncSession) -> None:
        # Read without a lock first, only to learn whose money-out lock comes before the
        # row lock. A bank deposit's user never changes once its row exists.
        seen = await deposits.find_deposit(session, BANK_PROVIDER, event.deposit_id)
        if seen is None:
            raise DepositNotReceived(
                "a deposit was returned before it was received; the return has to wait for it"
            )
        if seen["user_id"] is not None:
            await advisory_xact_lock(session, [lock_key(MONEY_OUT_LOCK, seen["user_id"])])
        deposit = await deposits.lock_deposit(session, BANK_PROVIDER, event.deposit_id)
        if deposit is None:
            raise RuntimeError(f"deposit {seen['id']} was recorded and is gone")
        deposits.check_same(deposit, event.asset, amount)

        if deposit["status"] == "completed":
            shortfall = await _take_back(session, deposit)
        elif deposit["status"] == "suspense":
            shortfall = 0
            await _post(
                session,
                deposit,
                [
                    debit(
                        (await ledger.open_account(session, AccountKind.SUSPENSE, event.asset)).id,
                        amount,
                    )
                ],
            )
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
                "reason": event.reason,
            },
        )

    await db.run(work)


async def _take_back(session: AsyncSession, deposit: RowMapping) -> int:
    """Reverse a credited deposit out of its user's wallet. Returns the shortfall: how much
    of it was no longer there to take."""
    user_id, asset = deposit["user_id"], deposit["asset_code"]
    amount: int = deposit["amount"]
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
            await identity.restrict_user(session, user_id, RESTRICTION_REASON)
    return shortfall


async def _post(session: AsyncSession, deposit: RowMapping, debits: list[PostingDraft]) -> None:
    settlement = await ledger.open_account(
        session, AccountKind.BANK_SETTLEMENT, deposit["asset_code"], provider=BANK_PROVIDER
    )
    await ledger.post_entry(
        session,
        EntryDraft(
            kind=RETURN_ENTRY_KIND,
            source_type=deposits.SOURCE_TYPE,
            source_id=deposit["provider_ref"],
            postings=(*debits, credit(settlement.id, deposit["amount"])),
            metadata={"provider": BANK_PROVIDER, "deposit_id": str(deposit["id"])},
        ),
    )

"""The payout sweeper: a scheduled job that asks a provider what became of a withdrawal
when nothing has been heard.

Webhooks get lost, and so can the answer to the request that sends a withdrawal, and so
can the event that was to send it. None of them may leave a user's funds reserved for
ever. The sweeper finds the withdrawals that have been in flight for too long and reads
their payouts from the provider, then applies what it reads through the same functions the
webhooks use, so it can be run at any time, any number of times, alongside them. It also
finds the withdrawals that are still only held and that nothing is going to send, and asks
for them again.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from corridor.payments import handlers, withdrawals
from corridor.payments.errors import ProviderEventMismatch
from corridor.payments.types import Withdrawal
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.logging import get_logger
from corridor.providers import BankRail, Custodian, Payout, ProviderError
from corridor.providers import Withdrawal as CustodyWithdrawal

log = get_logger(__name__)

# How many withdrawals are read at a time. A run reads batch after batch until it has
# looked at every one that is overdue.
BATCH_SIZE: Final = 100


async def sweep_payouts(
    db: Database, bank: BankRail | None, custody: Custodian | None, settings: Settings
) -> int:
    """Advance every overdue withdrawal the provider can account for. Returns how many moved.

    A withdrawal is overdue once it has been ``submitted``, or left ``submitting``, for
    ``payout_sweep_after_seconds``. The provider is read with no transaction open. A
    provider that cannot be reached for one withdrawal costs that one its turn and no more,
    and so does a provider this process was not given.

    Every overdue withdrawal gets its turn in every run. They are read in batches, each
    starting after the id the last one ended on, so the ones that cannot be advanced do
    not stand in front of the ones that can.

    Before that, the withdrawals that are still only held, and have been for as long, are
    looked at: one that is free to go and has no event left to send it is asked for again.
    That asks nothing of a provider and moves nothing, so it is not counted.
    """
    before = utcnow() - timedelta(seconds=settings.payout_sweep_after_seconds)
    await _resend_held(db, before)

    advanced = 0
    last_seen: uuid.UUID | None = None
    while True:
        due = await db.run(_batch_after(last_seen, before))
        for withdrawal in due:
            try:
                advanced += await _sweep(db, bank, custody, withdrawal, before)
            except ProviderError as error:
                log.warning(
                    "payout_sweep.provider_failed",
                    withdrawal_id=str(withdrawal.id),
                    provider=error.provider,
                    operation=error.operation,
                )
            except ProviderEventMismatch:
                # What the provider holds is not this withdrawal. Nothing was moved, and
                # the next withdrawal is no worse for it.
                log.error("payout_sweep.mismatch", withdrawal_id=str(withdrawal.id))
        if len(due) < BATCH_SIZE:
            return advanced
        last_seen = due[-1].id


async def _resend_held(db: Database, before: datetime) -> None:
    """Write a new submission event for every held withdrawal that nothing will send."""
    last_seen: uuid.UUID | None = None
    while True:
        after = last_seen

        async def read(session: AsyncSession, after: uuid.UUID | None = after) -> list[Withdrawal]:
            return await withdrawals.waiting_unsent(session, before, after=after, limit=BATCH_SIZE)

        waiting = await db.run(read)
        for withdrawal in waiting:
            if await handlers.resend_held(db, withdrawal.id, before):
                log.info("payout_sweep.resent_held", withdrawal_id=str(withdrawal.id))
        if len(waiting) < BATCH_SIZE:
            return
        last_seen = waiting[-1].id


def _batch_after(
    after: uuid.UUID | None, before: datetime
) -> Callable[[AsyncSession], Awaitable[list[Withdrawal]]]:
    """The read of one batch, as work for a transaction."""

    async def read(session: AsyncSession) -> list[Withdrawal]:
        return await withdrawals.overdue(session, before, after=after, limit=BATCH_SIZE)

    return read


async def _sweep(
    db: Database,
    bank: BankRail | None,
    custody: Custodian | None,
    withdrawal: Withdrawal,
    before: datetime,
) -> bool:
    reference = str(withdrawal.id)
    found: Payout | CustodyWithdrawal
    moved = False

    if withdrawal.provider_ref is None:
        # Marked as being sent, and nothing recorded since: the worker that was sending it
        # may have died, or its event may be dead. If the provider has a payout under this
        # reference, the submission did happen and only its record is missing. If it has
        # none, nothing was sent, and the withdrawal is asked for again: its own event
        # may never run. Nothing has moved by that, so it does not count as advanced.
        candidates = await _find(bank, custody, withdrawal)
        if not candidates:
            if await handlers.resubmit(db, withdrawal.id, before):
                log.info("payout_sweep.resubmitted", withdrawal_id=reference)
            return False
        if len(candidates) > 1:
            # One key makes one payout. More than one is not something to choose among.
            log.error("payout_sweep.several_payouts", withdrawal_id=reference)
            return False
        found = candidates[0]
        await handlers.record_submission(db, withdrawal.id, found.id)
        moved = True
    else:
        found = await _get(bank, custody, withdrawal, withdrawal.provider_ref)
        if found.reference != reference:
            raise ProviderEventMismatch(f"payout {found.id} is not withdrawal {reference}'s")

    if found.status == "completed":
        await handlers.settle(
            db,
            withdrawal_id=withdrawal.id,
            kind=withdrawal.kind,
            asset=found.asset_code,
            amount=found.amount,
            provider_ref=found.id,
            provider_fee=found.fee if isinstance(found, Payout) else found.network_fee,
        )
        return True
    if found.status == "failed":
        await handlers.fail(
            db,
            withdrawal_id=withdrawal.id,
            kind=withdrawal.kind,
            asset=found.asset_code,
            amount=found.amount,
            provider_ref=found.id,
            reason=found.failure_reason or "failed",
        )
        return True
    # Still on its way at the provider. Leave it for the webhook, or the next run.
    return moved


async def _find(
    bank: BankRail | None, custody: Custodian | None, withdrawal: Withdrawal
) -> tuple[Payout, ...] | tuple[CustodyWithdrawal, ...]:
    """What the withdrawal's provider holds under its reference."""
    if withdrawal.kind == "bank":
        if bank is None:
            raise handlers.not_configured("bank")
        return await bank.find_payouts(str(withdrawal.id))
    if custody is None:
        raise handlers.not_configured("chain")
    return await custody.find_withdrawals(str(withdrawal.id))


async def _get(
    bank: BankRail | None, custody: Custodian | None, withdrawal: Withdrawal, provider_ref: str
) -> Payout | CustodyWithdrawal:
    """The payout the withdrawal was recorded as, read from its provider."""
    if withdrawal.kind == "bank":
        if bank is None:
            raise handlers.not_configured("bank")
        return await bank.get_payout(provider_ref)
    if custody is None:
        raise handlers.not_configured("chain")
    return await custody.get_withdrawal(provider_ref)

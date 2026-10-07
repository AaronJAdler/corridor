"""The payout sweeper: a scheduled job that asks a provider what became of a withdrawal
when nothing has been heard.

Webhooks get lost, and so can the answer to the request that sends a withdrawal. Neither
may leave a user's funds reserved for ever. The sweeper finds the withdrawals that have
been in flight for too long and reads their payouts from the provider, then applies what
it reads through the same functions the webhooks use, so it can be run at any time, any
number of times, alongside them.
"""

from datetime import timedelta
from typing import Final

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

# How many withdrawals one run looks at. The rest are still overdue at the next.
BATCH_SIZE: Final = 100


async def sweep_payouts(
    db: Database, bank: BankRail | None, custody: Custodian | None, settings: Settings
) -> int:
    """Advance every overdue withdrawal the provider can account for. Returns how many moved.

    A withdrawal is overdue once it has been ``submitted``, or left ``submitting``, for
    ``payout_sweep_after_seconds``. The provider is read with no transaction open. A
    provider that cannot be reached for one withdrawal costs that one its turn and no more,
    and so does a provider this process was not given.
    """
    before = utcnow() - timedelta(seconds=settings.payout_sweep_after_seconds)
    due = await db.run(lambda session: withdrawals.overdue(session, before, limit=BATCH_SIZE))

    advanced = 0
    for withdrawal in due:
        try:
            advanced += await _sweep(db, bank, custody, withdrawal)
        except ProviderError as error:
            log.warning(
                "payout_sweep.provider_failed",
                withdrawal_id=str(withdrawal.id),
                provider=error.provider,
                operation=error.operation,
            )
        except ProviderEventMismatch:
            # What the provider holds is not this withdrawal. Nothing was moved, and the
            # next withdrawal is no worse for it.
            log.error("payout_sweep.mismatch", withdrawal_id=str(withdrawal.id))
    return advanced


async def _sweep(
    db: Database, bank: BankRail | None, custody: Custodian | None, withdrawal: Withdrawal
) -> bool:
    reference = str(withdrawal.id)
    found: Payout | CustodyWithdrawal
    moved = False

    if withdrawal.provider_ref is None:
        # Marked as being sent, and nothing recorded since: the worker that was sending it
        # may have died, or its event may be dead. If the provider has a payout under this
        # reference, the submission did happen and only its record is missing. If it has
        # none, the withdrawal is left as it is for its event to send.
        candidates = await _find(bank, custody, withdrawal)
        if not candidates:
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

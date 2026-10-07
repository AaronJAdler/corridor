"""Repair: the two kinds of break a run can put right by itself.

Both are an event that never arrived. The provider's own record of it is given to
payments: a payout's result through the function its webhook would have reached, and a
deposit through the one for deposits read from a statement. Both are idempotent, so a
repair that races the late webhook, the payout sweeper or another run changes nothing
twice. Nothing else is repaired: every other break needs a person to decide what is true.

A break is closed by looking at Corridor's own records afterwards, and not on the word of
the repair. That also closes a break that something else put right in the meantime.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import payments
from corridor.platform.db import Database
from corridor.platform.logging import get_logger
from corridor.platform.money import format_amount
from corridor.providers import ProviderTransaction
from corridor.recon import breaks
from corridor.recon.types import Break, BreakKind, Finding, Sent

log = get_logger(__name__)

REPAIRABLE: Final[tuple[BreakKind, ...]] = ("missing_deposit", "missing_payout_result")

# How many withdrawals in flight one run looks at, oldest first. A run that meets more is
# recorded as incomplete.
MAX_IN_FLIGHT: Final = 1000


async def repair(db: Database, found: Sequence[tuple[Break, Finding]], *, complete: bool) -> int:
    """Put right what a run found and can, then close every repairable break that is now
    settled. Returns how many were closed.

    An entry point: each record is applied in a transaction of its own, by the payments
    function that owns it, and the breaks are closed in one more. ``complete`` says whether
    the run read everything it asked the providers for; when it did not, breaks about
    payouts are left as they are.
    """
    for _, finding in found:
        try:
            if finding.kind == "missing_deposit" and finding.transaction is not None:
                await _record_deposit(db, finding.provider, finding.transaction)
            elif finding.kind == "missing_payout_result" and finding.sent is not None:
                await _apply_result(db, finding.provider, finding.sent)
        except (payments.MalformedProviderEvent, payments.ProviderEventMismatch) as refusal:
            # Payments would not take the provider's record. The break stays open, for a
            # person, and the next one still gets its turn.
            log.error(
                "recon.repair_refused",
                kind=finding.kind,
                provider=finding.provider,
                provider_ref=finding.provider_ref,
                reason=type(refusal).__name__,
            )

    async def close(session: AsyncSession) -> int:
        return await _close_settled(session, found, complete=complete)

    return await db.run(close)


async def _record_deposit(db: Database, provider: str, line: ProviderTransaction) -> None:
    """Hand a statement line to payments as a deposit read from a statement.

    Not as the event its webhook would have been: a statement says less than a webhook
    does, and payments is told so instead of being shown empty fields to take for a
    sender it screened. What attributes a deposit, the account or address it arrived at,
    is on the line. Who sent it is not, on either provider's statement.
    """
    await payments.apply_statement_deposit(
        db,
        provider=provider,
        provider_ref=line.id,
        account_ref=line.related_id or "",
        asset=line.asset_code,
        amount=line.amount,
        tx_hash=line.tx_hash,
        sender=None,
    )


async def _apply_result(db: Database, provider: str, sent: Sent) -> None:
    """Hand a provider's payout to the function its webhook would have reached."""
    amount = format_amount(sent.amount, sent.asset)
    said = {"reference": sent.reference, "asset": sent.asset, "amount": amount}
    if provider == payments.BANK_PROVIDER:
        if sent.status == "completed":
            await payments.apply_payout_completed(
                db,
                {
                    **said,
                    "payout_id": sent.id,
                    "fee": format_amount(sent.fee, sent.asset),
                    "settled_at": sent.settled_at.isoformat() if sent.settled_at else "",
                },
            )
        elif sent.status == "failed":
            await payments.apply_payout_failed(
                db,
                {**said, "payout_id": sent.id, "failure_reason": sent.failure_reason or "failed"},
            )
        return
    if sent.status == "completed":
        await payments.apply_withdrawal_completed(
            db,
            {
                **said,
                "withdrawal_id": sent.id,
                "network_fee": format_amount(sent.fee, sent.asset),
                "tx_hash": sent.tx_hash or "",
            },
        )
    elif sent.status == "failed":
        await payments.apply_withdrawal_failed(
            db,
            {**said, "withdrawal_id": sent.id, "failure_reason": sent.failure_reason or "failed"},
        )


async def _close_settled(
    session: AsyncSession, found: Sequence[tuple[Break, Finding]], *, complete: bool
) -> int:
    still_open = await breaks.lock_open(session, REPAIRABLE)
    closed = 0

    missing: dict[str, list[Break]] = defaultdict(list)
    for item in still_open:
        if item.kind == "missing_deposit":
            missing[item.provider].append(item)
    for provider, items in missing.items():
        recorded = await payments.find_deposits(
            session, provider, [item.provider_ref for item in items]
        )
        for item in items:
            deposit = recorded.get(item.provider_ref)
            # A journal entry is the proof: the row alone may be a deposit still pending.
            if deposit is not None and deposit.entry_id is not None:
                await breaks.resolve_as_system(session, item.id, "The deposit is on the books.")
                closed += 1

    waiting = [item for item in still_open if item.kind == "missing_payout_result"]
    if not waiting or not complete:
        return closed
    in_flight = await payments.withdrawals_in_flight(session, limit=MAX_IN_FLIGHT)
    # The payouts that still belong to a withdrawal in flight: by the id recorded on it,
    # or, for one that never recorded its payout, by the id this run read at the provider.
    ids: set[uuid.UUID] = {withdrawal.id for withdrawal in in_flight}
    unsettled = {
        (withdrawal.provider, withdrawal.provider_ref)
        for withdrawal in in_flight
        if withdrawal.provider_ref is not None
    } | {
        (finding.provider, finding.sent.id)
        for _, finding in found
        if finding.sent is not None and finding.withdrawal_id in ids
    }
    for item in waiting:
        if (item.provider, item.provider_ref) not in unsettled:
            await breaks.resolve_as_system(
                session, item.id, "The withdrawal is no longer in flight."
            )
            closed += 1
    return closed

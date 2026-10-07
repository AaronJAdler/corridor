"""The rest of a withdrawal's saga: sending it to its provider, and settling or releasing
it when the provider says what became of it.

Everything here is an entry point that owns its transactions. A provider is only ever
called between two of them, with the withdrawal's own id as the idempotency key, so that
the call can be repeated after any failure without paying out twice.

Each transition takes the withdrawal's row ``FOR UPDATE`` and looks at its state first.
That is the whole defence against events that are late, repeated or out of order: the
second and the stale find the withdrawal already past them and do nothing.
"""

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from pydantic import Field
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, ledger, risk, wallets
from corridor.ledger import AccountKind, EntryDraft, PostingDraft, credit, debit
from corridor.payments import beneficiaries, withdrawals
from corridor.payments.errors import MalformedProviderEvent, ProviderEventMismatch
from corridor.payments.events import ProviderEvent, amount_of, parse, reason_code
from corridor.payments.types import BANK_PROVIDER, CUSTODY_PROVIDER, FlowKind, Withdrawal
from corridor.platform.clock import utcnow
from corridor.platform.db import Database
from corridor.platform.logging import get_logger
from corridor.providers import (
    BankRail,
    Custodian,
    Payout,
    ProviderMisconfigured,
    ProviderOutcomeUnknown,
    ProviderRejected,
)
from corridor.providers import Withdrawal as CustodyWithdrawal

log = get_logger(__name__)

SETTLE_ENTRY_KIND: Final = "withdrawal_settle"

# The refusal that is not one. The provider answers it when the key is already bound to a
# request with another body: it says something about the key, and nothing about whether a
# payout for this withdrawal exists.
_IDEMPOTENCY_CONFLICT: Final = "idempotency_conflict"

# Why a withdrawal that was never sent was given back: its user stopped being allowed to
# move money out between asking for it and the worker reaching it.
ACCOUNT_NOT_ACTIVE: Final = "account_not_active"

# The states in which a withdrawal's funds are still reserved, and so can still be paid
# out or given back. Everything else is final.
_RESERVED: Final = frozenset({"held", "submitting", "submitted"})

_PROVIDER_ACCOUNT: Final[Mapping[str, AccountKind]] = {
    "bank": AccountKind.BANK_SETTLEMENT,
    "chain": AccountKind.CUSTODY_OMNIBUS,
}
_PROVIDER_OF: Final[Mapping[str, str]] = {"bank": BANK_PROVIDER, "chain": CUSTODY_PROVIDER}


class _PayoutCompleted(ProviderEvent):
    """The bank's ``payout.completed``."""

    payout_id: str = Field(min_length=1)
    reference: str
    asset: str
    amount: str
    fee: str
    settled_at: str


class _PayoutFailed(ProviderEvent):
    """The bank's ``payout.failed``."""

    payout_id: str = Field(min_length=1)
    reference: str
    asset: str
    amount: str
    failure_reason: str


class _WithdrawalCompleted(ProviderEvent):
    """The custodian's ``withdrawal.completed``."""

    withdrawal_id: str = Field(min_length=1)
    reference: str
    asset: str
    amount: str
    network_fee: str
    tx_hash: str


class _WithdrawalFailed(ProviderEvent):
    """The custodian's ``withdrawal.failed``."""

    withdrawal_id: str = Field(min_length=1)
    reference: str
    asset: str
    amount: str
    failure_reason: str


async def submit_withdrawal(
    db: Database, bank: BankRail | None, custody: Custodian | None, withdrawal_id: uuid.UUID
) -> None:
    """Send a held withdrawal to its provider. The body of the ``withdrawal.submit`` handler.

    In three steps, and the order is the point. The withdrawal is first recorded as
    ``submitting``, in a transaction that commits before the provider is asked. Only a
    ``held`` withdrawal can be called back, so a cancellation either commits before that
    record, and then nothing is sent, or finds it and is refused: no payout can exist for
    funds that have been given back. Then the provider is asked, with no transaction open,
    and then the answer is recorded.

    Idempotent on the withdrawal id. A withdrawal found ``submitting`` is one an earlier
    attempt left there, and is sent again under the same idempotency key, so the provider
    answers with the payout it already has. One in any other state is left alone.

    A refusal releases the funds, but not on its own word. A refusal answers the request it
    was given: an earlier attempt may have made the payout and never heard so, and a 4xx
    can come from something between Corridor and the provider. So the provider is first
    asked what it holds under this withdrawal's reference, and the funds go back only if it
    holds nothing. An unknown outcome is raised, with the funds still reserved, for the
    outbox to try again with the same key; so is a fault in Corridor's own configuration,
    which no retry will mend until someone has. A provider this process was not given is
    such a fault, and is raised before anything is sent or changed.

    A withdrawal whose review is not cleared is not sent. It is left ``held``, and the
    handler returns as if it had nothing to do, which until an operator decides it has not.

    The user is looked at again before the mark. The request was authorised when it was
    made, and the account may have been restricted or closed since: a returned deposit
    does that. A held withdrawal of such an account is given back and never sent.
    """

    async def begin(session: AsyncSession) -> tuple[Withdrawal, str | None] | None:
        row = await withdrawals.lock(session, withdrawal_id)
        if row is None:
            # The event is written with the row, so this is a bug and not a race.
            raise LookupError(f"there is no withdrawal {withdrawal_id} to submit")
        if row["status"] == "held":
            if (await identity.get_user(session, row["user_id"])).status != "active":
                await withdrawals.release(
                    session, row, status="failed", failure_reason=ACCOUNT_NOT_ACTIVE
                )
                await audit.record(
                    session,
                    actor=audit.Actor.system("withdrawal.submit"),
                    action="withdrawal.failed",
                    principal_id=row["user_id"],
                    resource_type="withdrawal",
                    resource_id=withdrawal_id,
                    details={"provider": row["provider"], "reason": ACCOUNT_NOT_ACTIVE},
                )
                return None
            if not await risk.is_cleared(session, "withdrawal", withdrawal_id):
                # Screening wants an operator to see it first, or one has rejected it.
                # It stays held and this event is done: clearing the review writes
                # another, and rejecting it gives the funds back.
                return None
            if (bank if row["kind"] == "bank" else custody) is None:
                # Before the mark: the withdrawal stays held, and its user can still cancel.
                raise not_configured(row["kind"])
            withdrawal = await withdrawals.advance(session, withdrawal_id, status="submitting")
        elif row["status"] == "submitting":
            withdrawal = withdrawals.as_withdrawal(row)
        else:
            return None
        token: str | None = None
        if withdrawal.beneficiary_id is not None:
            token = (await beneficiaries.get(session, withdrawal.beneficiary_id)).provider_ref
        return withdrawal, token

    begun = await db.run(begin)
    if begun is None:
        return
    withdrawal, token = begun

    reference = str(withdrawal.id)
    try:
        # No transaction is open here. The amount alone goes out: the fee stays.
        if withdrawal.kind == "bank":
            if bank is None:
                # Held by a process that had a bank and retried by one that has none.
                raise not_configured("bank")
            if token is None:
                raise LookupError(f"withdrawal {withdrawal_id} has no beneficiary to pay")
            provider_ref = (
                await bank.create_payout(
                    beneficiary_id=token,
                    asset_code=withdrawal.asset,
                    amount=withdrawal.amount,
                    reference=reference,
                    idempotency_key=reference,
                )
            ).id
        else:
            if custody is None:
                raise not_configured("chain")
            if withdrawal.to_address is None:
                raise LookupError(f"withdrawal {withdrawal_id} has no address to send to")
            provider_ref = (
                await custody.create_withdrawal(
                    asset_code=withdrawal.asset,
                    amount=withdrawal.amount,
                    to_address=withdrawal.to_address,
                    reference=reference,
                    idempotency_key=reference,
                )
            ).id
    except ProviderRejected as refusal:
        if refusal.code == _IDEMPOTENCY_CONFLICT:
            # Not a refusal of this payout: the key is taken, perhaps by this very payout
            # as an earlier attempt sent it. Releasing the funds here could pay out twice.
            raise ProviderOutcomeUnknown(
                "the idempotency key is bound to another request",
                provider=refusal.provider,
                operation=refusal.operation,
            ) from refusal
        # Refused this time, and perhaps accepted before: whether a payout exists is the
        # provider's to say. If it cannot be asked, that is raised and nothing changes,
        # which leaves the funds reserved for the next attempt.
        already_sent = await _sent_before(bank, custody, withdrawal)
        if already_sent is not None:
            await db.run(lambda session: _record_submission(session, withdrawal_id, already_sent))
            return
        code = refusal.code
        await db.run(lambda session: _reject(session, withdrawal_id, code))
        return

    await db.run(lambda session: _record_submission(session, withdrawal_id, provider_ref))


async def _sent_before(
    bank: BankRail | None, custody: Custodian | None, withdrawal: Withdrawal
) -> str | None:
    """The provider's id for the payout it already holds for a withdrawal, if it holds one.

    Read with no transaction open. More than one is not something to choose among: it is
    raised as an unknown outcome, and the funds stay reserved until a person has looked.
    """
    reference = str(withdrawal.id)
    found: tuple[Payout, ...] | tuple[CustodyWithdrawal, ...]
    if withdrawal.kind == "bank":
        if bank is None:
            raise not_configured("bank")
        found = await bank.find_payouts(reference)
    else:
        if custody is None:
            raise not_configured("chain")
        found = await custody.find_withdrawals(reference)
    if len(found) > 1:
        raise ProviderOutcomeUnknown(
            "several payouts under one reference",
            provider=_PROVIDER_OF[withdrawal.kind],
            operation="find_payouts" if withdrawal.kind == "bank" else "find_withdrawals",
        )
    return found[0].id if found else None


async def apply_payout_completed(db: Database, data: Mapping[str, Any]) -> None:
    """The bank paid a withdrawal out: settle it, once."""
    event = parse(_PayoutCompleted, data)
    await settle(
        db,
        withdrawal_id=_reference(event.reference),
        kind="bank",
        asset=event.asset,
        amount=amount_of(event.amount, event.asset),
        provider_ref=event.payout_id,
        provider_fee=amount_of(event.fee, event.asset, allow_zero=True),
    )


async def apply_withdrawal_completed(db: Database, data: Mapping[str, Any]) -> None:
    """The custodian's withdrawal is final on its chain: settle it, once."""
    event = parse(_WithdrawalCompleted, data)
    await settle(
        db,
        withdrawal_id=_reference(event.reference),
        kind="chain",
        asset=event.asset,
        amount=amount_of(event.amount, event.asset),
        provider_ref=event.withdrawal_id,
        provider_fee=amount_of(event.network_fee, event.asset, allow_zero=True),
    )


async def apply_payout_failed(db: Database, data: Mapping[str, Any]) -> None:
    """The bank could not pay a withdrawal out: give the funds back, once."""
    event = parse(_PayoutFailed, data)
    await fail(
        db,
        withdrawal_id=_reference(event.reference),
        kind="bank",
        asset=event.asset,
        amount=amount_of(event.amount, event.asset),
        provider_ref=event.payout_id,
        reason=event.failure_reason,
    )


async def apply_withdrawal_failed(db: Database, data: Mapping[str, Any]) -> None:
    """The network rejected a withdrawal: give the funds back, once."""
    event = parse(_WithdrawalFailed, data)
    await fail(
        db,
        withdrawal_id=_reference(event.reference),
        kind="chain",
        asset=event.asset,
        amount=amount_of(event.amount, event.asset),
        provider_ref=event.withdrawal_id,
        reason=event.failure_reason,
    )


async def settle(
    db: Database,
    *,
    withdrawal_id: uuid.UUID,
    kind: FlowKind,
    asset: str,
    amount: int,
    provider_ref: str,
    provider_fee: int,
) -> None:
    """Close a withdrawal the provider has paid out. Shared by the webhooks and the sweeper.

    From ``submitting`` as well as from ``submitted``: the provider's word that it paid
    can arrive before its answer to the request has been recorded.
    """

    async def work(session: AsyncSession) -> None:
        row = await _lock_and_check(session, withdrawal_id, kind, asset, amount, provider_ref)
        if row["status"] not in _RESERVED:
            if row["status"] != "completed":
                # Paid out by the provider after the funds went back to the user. Nothing
                # here can put that right; a person has to.
                log.error(
                    "withdrawal.paid_out_after_release",
                    withdrawal_id=str(withdrawal_id),
                    status=row["status"],
                )
            return

        fee: int = row["fee"]
        wallet = await wallets.resolve(session, row["user_id"], asset)
        paid_from = await ledger.open_account(
            session, _PROVIDER_ACCOUNT[kind], asset, provider=row["provider"]
        )
        # A posting of zero is not a posting, so each fee is there only if there was one.
        postings: list[PostingDraft] = [debit(wallet.held_account_id, amount + fee)]
        if provider_fee > 0:
            expense = await ledger.open_account(session, AccountKind.PROVIDER_FEE_EXPENSE, asset)
            postings.append(debit(expense.id, provider_fee))
        postings.append(credit(paid_from.id, amount + provider_fee))
        if fee > 0:
            revenue = await ledger.open_account(session, AccountKind.FEE_REVENUE, asset)
            postings.append(credit(revenue.id, fee))
        entry = await ledger.post_entry(
            session,
            EntryDraft(
                kind=SETTLE_ENTRY_KIND,
                source_type=withdrawals.SOURCE_TYPE,
                source_id=str(withdrawal_id),
                postings=tuple(postings),
                metadata={"provider": row["provider"], "provider_ref": provider_ref},
            ),
        )
        await withdrawals.advance(
            session,
            withdrawal_id,
            status="completed",
            provider_ref=provider_ref,
            provider_fee=provider_fee,
            final_entry_id=entry.id,
        )
        await audit.record(
            session,
            actor=audit.Actor.provider(row["provider"]),
            action="withdrawal.completed",
            principal_id=row["user_id"],
            resource_type="withdrawal",
            resource_id=withdrawal_id,
            details={"provider_ref": provider_ref, "provider_fee": str(provider_fee)},
        )

    await db.run(work)


async def fail(
    db: Database,
    *,
    withdrawal_id: uuid.UUID,
    kind: FlowKind,
    asset: str,
    amount: int,
    provider_ref: str,
    reason: str,
) -> None:
    """Close a withdrawal the provider could not pay out, and give its funds back. Shared by
    the webhooks and the sweeper."""
    # The provider's own words, kept only if they are a code.
    reason = reason_code(reason)

    async def work(session: AsyncSession) -> None:
        row = await _lock_and_check(session, withdrawal_id, kind, asset, amount, provider_ref)
        if row["status"] not in _RESERVED:
            if row["status"] == "completed":
                log.error("withdrawal.failed_after_settlement", withdrawal_id=str(withdrawal_id))
            return
        await withdrawals.release(session, row, status="failed", failure_reason=reason)
        await withdrawals.advance(session, withdrawal_id, provider_ref=provider_ref)
        await audit.record(
            session,
            actor=audit.Actor.provider(row["provider"]),
            action="withdrawal.failed",
            principal_id=row["user_id"],
            resource_type="withdrawal",
            resource_id=withdrawal_id,
            details={"provider_ref": provider_ref, "reason": reason},
        )

    await db.run(work)


async def record_submission(db: Database, withdrawal_id: uuid.UUID, provider_ref: str) -> None:
    """Note that the provider has a withdrawal that is still recorded as being sent. For
    the sweeper, when it finds at the provider what the submission never got to record."""
    await db.run(lambda session: _record_submission(session, withdrawal_id, provider_ref))


async def resubmit(db: Database, withdrawal_id: uuid.UUID, before: datetime) -> bool:
    """Ask again for a withdrawal to be sent that was marked as being sent before
    ``before`` and that the provider knows nothing of. For the sweeper. Says whether it did.

    The event that was sending it may be dead, and nothing else would ever send it. A
    second submission is safe: it goes out under the same idempotency key, the withdrawal's
    id, so the provider makes one payout however many times it is asked.

    The row is touched as the event is written, and it is by that time that the sweeper
    finds a withdrawal overdue, so one withdrawal is asked for again at most once in each
    ``payout_sweep_after_seconds``.
    """

    async def work(session: AsyncSession) -> bool:
        row = await _lock(session, withdrawal_id)
        if row["status"] != "submitting" or row["updated_at"] > before:
            # Sent, settled or released since the sweeper read it, or already asked for.
            return False
        await withdrawals.ask_to_be_sent(session, withdrawal_id)
        await withdrawals.advance(session, withdrawal_id)
        return True

    return await db.run(work)


async def _record_submission(
    session: AsyncSession, withdrawal_id: uuid.UUID, provider_ref: str
) -> None:
    row = await _lock(session, withdrawal_id)
    if row["status"] != "submitting":
        if row["status"] not in ("submitted", "completed", "failed"):
            # The provider has a payout for a withdrawal that was never marked as sent, or
            # whose funds went back to the user. Neither can happen while the mark is
            # written before the provider is asked; a person has to look at this.
            log.error(
                "withdrawal.sent_without_reservation",
                withdrawal_id=str(withdrawal_id),
                status=row["status"],
            )
        return
    await withdrawals.advance(
        session, withdrawal_id, status="submitted", provider_ref=provider_ref, submitted_at=utcnow()
    )
    await audit.record(
        session,
        actor=audit.Actor.system("withdrawal.submit"),
        action="withdrawal.submitted",
        principal_id=row["user_id"],
        resource_type="withdrawal",
        resource_id=withdrawal_id,
        details={"provider": row["provider"], "provider_ref": provider_ref},
    )


async def _reject(session: AsyncSession, withdrawal_id: uuid.UUID, code: str) -> None:
    """The provider refused the request and holds no payout for it: the funds go back."""
    row = await _lock(session, withdrawal_id)
    if row["status"] != "submitting":
        return
    reason = reason_code(code)
    await withdrawals.release(session, row, status="failed", failure_reason=reason)
    await audit.record(
        session,
        actor=audit.Actor.system("withdrawal.submit"),
        action="withdrawal.failed",
        principal_id=row["user_id"],
        resource_type="withdrawal",
        resource_id=withdrawal_id,
        details={"provider": row["provider"], "reason": reason},
    )


def not_configured(kind: FlowKind) -> ProviderMisconfigured:
    """What is raised when a withdrawal needs a provider this process was not given."""
    return ProviderMisconfigured(
        "no bank rail is configured" if kind == "bank" else "no custodian is configured",
        provider=_PROVIDER_OF[kind],
        operation="create_payout" if kind == "bank" else "create_withdrawal",
    )


async def _lock(session: AsyncSession, withdrawal_id: uuid.UUID) -> RowMapping:
    row = await withdrawals.lock(session, withdrawal_id)
    if row is None:
        raise ProviderEventMismatch(f"there is no withdrawal {withdrawal_id}")
    return row


async def _lock_and_check(
    session: AsyncSession,
    withdrawal_id: uuid.UUID,
    kind: FlowKind,
    asset: str,
    amount: int,
    provider_ref: str,
) -> RowMapping:
    """Lock the withdrawal a provider's word is about, and refuse the word if it is not
    about that withdrawal: another provider, another asset, another amount or another
    payout. Money is never settled or released on a mismatch."""
    row = await _lock(session, withdrawal_id)
    if (row["kind"], row["provider"]) != (kind, _PROVIDER_OF[kind]):
        raise ProviderEventMismatch(f"withdrawal {withdrawal_id} did not go out by {kind}")
    if (row["asset_code"], row["amount"]) != (asset, amount):
        raise ProviderEventMismatch(
            f"withdrawal {withdrawal_id}: the provider's asset or amount is not the recorded one"
        )
    if row["provider_ref"] is not None and row["provider_ref"] != provider_ref:
        raise ProviderEventMismatch(
            f"withdrawal {withdrawal_id} was sent as another payout than the provider names"
        )
    return row


def _reference(text: str) -> uuid.UUID:
    """The withdrawal a provider's event refers to: Corridor's own id, sent as the reference."""
    try:
        return uuid.UUID(text)
    except ValueError:
        raise MalformedProviderEvent("a reference that is not a withdrawal id") from None

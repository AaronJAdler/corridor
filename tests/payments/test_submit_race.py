"""A user cancelling a withdrawal while the worker is sending it.

Before a provider is asked, the withdrawal is marked ``submitting`` in a transaction of its
own. A cancellation is allowed only from ``held``, so it either commits before that mark,
and then nothing is sent, or finds the mark and is refused. There is no order in which a
payout exists at the provider with nothing reserved for it here.
"""

import asyncio
import uuid
from typing import Any

import pytest

from corridor import payments
from corridor.identity import User
from corridor.payments import WithdrawalNotCancelable
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.providers import (
    Payout,
    ProviderOutcomeUnknown,
    ProviderRejected,
    SimBank,
    SimCustody,
)
from corridor.providers import Withdrawal as CustodyWithdrawal
from tests.payments.support import (
    acting_as,
    add_beneficiary,
    available,
    count,
    deposit,
    held,
    held_bank_withdrawal,
    held_chain_withdrawal,
    rows,
    settlement,
    withdraw,
    withdrawal_row,
)
from tests.support.providers import EXTERNAL_ADDRESS, Sim, advance, with_providers

PAYOUTS = "/bank/v1/payouts"


@pytest.fixture(name="settings")
def without_a_minimum_fee(settings: Settings) -> Settings:
    """The suite's settings with no least withdrawal fee, so that the amounts in this
    module are the ones each test names. The minimum has tests of its own."""
    return settings.model_copy(update={"withdrawal_min_fee": {}})


@pytest.fixture
def charging(settings: Settings) -> Settings:
    """The test settings with a 1.5% withdrawal fee."""
    return settings.model_copy(update={"withdrawal_fee_bps": 150})


async def cancel(db: Database, user: User, withdrawal_id: uuid.UUID) -> payments.Withdrawal:
    async with db.transaction() as session:
        return await payments.cancel_withdrawal(session, acting_as(user), withdrawal_id)


class InterruptedBank:
    """A bank in the middle of whose call something else happens: ``meanwhile`` runs after
    the request has been decided on and before it reaches the bank."""

    name = "simbank"

    def __init__(self, inner: SimBank, meanwhile: Any) -> None:
        self._inner, self._meanwhile = inner, meanwhile

    def __getattr__(self, name: str) -> Any:
        # Everything but the request itself is the bank's own.
        return getattr(self._inner, name)

    async def create_payout(self, **arguments: Any) -> Payout:
        await self._meanwhile()
        return await self._inner.create_payout(**arguments)


class InterruptedCustody:
    name = "simcustody"

    def __init__(self, inner: SimCustody, meanwhile: Any) -> None:
        self._inner, self._meanwhile = inner, meanwhile

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def create_withdrawal(self, **arguments: Any) -> CustodyWithdrawal:
        await self._meanwhile()
        return await self._inner.create_withdrawal(**arguments)


# --- the mark --------------------------------------------------------------------------------


async def test_a_withdrawal_is_committed_as_submitting_before_the_bank_is_asked(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    seen: list[tuple[str, int]] = []

    async def look() -> None:
        # Through another connection: only what is committed can be seen from here.
        row = await withdrawal_row(db, withdrawal.id)
        seen.append((row["status"], len(await sim.payouts())))

    asked: Any = InterruptedBank(bank, look)
    await payments.submit_withdrawal(db, asked, custody, withdrawal.id)

    assert seen == [("submitting", 0)]
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["submitted_at"] is not None) == ("submitted", True)


async def test_a_chain_withdrawal_is_committed_as_submitting_before_the_custodian_is_asked(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_chain_withdrawal(db, charging, maria, EXTERNAL_ADDRESS)
    seen: list[tuple[str, int]] = []

    async def look() -> None:
        row = await withdrawal_row(db, withdrawal.id)
        seen.append((row["status"], len(await sim.withdrawals())))

    asked: Any = InterruptedCustody(custody, look)
    await payments.submit_withdrawal(db, bank, asked, withdrawal.id)

    assert seen == [("submitting", 0)]
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"


# --- one order, then the other, by construction ----------------------------------------------


async def test_a_cancel_after_the_mark_is_refused_and_the_payout_stays_reserved(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    refusals: list[WithdrawalNotCancelable] = []

    async def user_cancels() -> None:
        with pytest.raises(WithdrawalNotCancelable) as refusal:
            await cancel(db, maria, withdrawal.id)
        refusals.append(refusal.value)

    racing: Any = InterruptedBank(bank, user_cancels)
    await payments.submit_withdrawal(db, racing, custody, withdrawal.id)

    assert [(refusal.status, refusal.code) for refusal in refusals] == [
        (409, "withdrawal_not_cancelable")
    ]
    (payout,) = await sim.payouts()
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"]) == ("submitted", payout["id"])
    # The payout exists, and what pays for it is still reserved: nothing went back.
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)
    assert await count(db, "journal_entries") == 2
    assert await rows(db, "SELECT 1 FROM audit_events WHERE action = 'withdrawal.canceled'") == []


async def test_a_cancel_after_the_mark_is_refused_for_a_chain_withdrawal_too(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_chain_withdrawal(db, charging, maria, EXTERNAL_ADDRESS)

    async def user_cancels() -> None:
        with pytest.raises(WithdrawalNotCancelable):
            await cancel(db, maria, withdrawal.id)

    racing: Any = InterruptedCustody(custody, user_cancels)
    await payments.submit_withdrawal(db, bank, racing, withdrawal.id)

    assert len(await sim.withdrawals()) == 1
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"
    assert (await available(db, maria, "USDC"), await held(db, maria, "USDC")) == (
        24_625_000,
        25_375_000,
    )


async def test_a_cancel_before_the_mark_wins_and_nothing_is_sent(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await cancel(db, maria, withdrawal.id)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert sim.recorder.sent("POST", PAYOUTS) == []
    assert await sim.payouts() == []
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "canceled"
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)


async def test_a_cancel_that_holds_the_row_makes_the_submission_wait_and_then_do_nothing(
    db: Database,
    impatient_db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)

    async with db.transaction() as session:
        await payments.cancel_withdrawal(session, acting_as(maria), withdrawal.id)
        # The cancellation has not committed. A submission through connections that give
        # up on a lock at once shows that the mark waits for the row.
        with pytest.raises(Exception, match="lock"):
            await payments.submit_withdrawal(impatient_db, bank, custody, withdrawal.id)
        assert sim.recorder.sent("POST", PAYOUTS) == []
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert await sim.payouts() == []
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "canceled"
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)


# --- both at once ----------------------------------------------------------------------------


async def test_cancelling_and_submitting_at_once_never_pays_out_what_was_released(
    db: Database, settings: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    requested = [
        await withdraw(db, settings, maria, 1_00, beneficiary=beneficiary) for _ in range(40)
    ]

    async def try_cancel(withdrawal: payments.Withdrawal) -> bool:
        try:
            await cancel(db, maria, withdrawal.id)
        except WithdrawalNotCancelable:
            return False
        return True

    async def race(index: int, withdrawal: payments.Withdrawal) -> bool:
        # Which of the two starts first alternates, so both orders are tried.
        submit = payments.submit_withdrawal(db, bank, custody, withdrawal.id)
        first, second = (submit, try_cancel(withdrawal))[:: 1 if index % 2 else -1]
        outcome = await asyncio.gather(first, second)
        return bool(outcome[1 if index % 2 else 0])

    canceled = await asyncio.gather(
        *(race(index, withdrawal) for index, withdrawal in enumerate(requested))
    )

    paid = {payout["reference"] for payout in await sim.payouts()}
    assert len(await sim.payouts()) == len(paid)
    for withdrawal, was_canceled in zip(requested, canceled, strict=True):
        status = (await withdrawal_row(db, withdrawal.id))["status"]
        # Released, or sent: exactly one of the two, and the row says which.
        assert (status, str(withdrawal.id) in paid) == (
            ("canceled", False) if was_canceled else ("submitted", True)
        )
    # What is still reserved is exactly what the bank was asked to pay.
    assert await held(db, maria) == len(paid) * 1_00
    assert await available(db, maria) == 500_00 - len(paid) * 1_00


# --- a submission that is repeated -----------------------------------------------------------


async def test_a_submission_that_died_after_the_mark_is_sent_by_the_retry(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)

    async def die() -> None:
        raise RuntimeError("the worker died before the bank was asked")

    dying: Any = InterruptedBank(bank, die)
    with pytest.raises(RuntimeError):
        await payments.submit_withdrawal(db, dying, custody, withdrawal.id)
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitting"
    assert await sim.payouts() == []
    with pytest.raises(WithdrawalNotCancelable):
        await cancel(db, maria, withdrawal.id)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    (payout,) = await sim.payouts()
    assert payout["idempotency_key"] == str(withdrawal.id)
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)


async def test_a_refusal_releases_a_submitting_withdrawal_once(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)

    async def refuse() -> None:
        raise ProviderRejected(
            "invalid_amount", "refused", 422, provider="simbank", operation="create_payout"
        )

    refusing: Any = InterruptedBank(bank, refuse)
    await payments.submit_withdrawal(db, refusing, custody, withdrawal.id)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "invalid_amount")
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert await count(db, "journal_entries") == 3
    assert await sim.payouts() == []


async def test_a_refusal_does_not_reopen_a_withdrawal_the_provider_failed_meanwhile(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)

    async def fails_and_then_is_refused() -> None:
        # The provider's word that the payout failed is applied while the request is
        # still out, and the request then comes back refused.
        await payments.apply_payout_failed(
            db,
            {
                "payout_id": "po_early",
                "reference": str(withdrawal.id),
                "asset": "USD",
                "amount": "100.00",
                "failure_reason": "account_closed",
            },
        )
        raise ProviderRejected(
            "invalid_amount", "refused", 422, provider="simbank", operation="create_payout"
        )

    racing: Any = InterruptedBank(bank, fails_and_then_is_refused)
    await payments.submit_withdrawal(db, racing, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "account_closed")
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert await count(db, "journal_entries") == 3
    failed = await rows(db, "SELECT 1 FROM audit_events WHERE action = 'withdrawal.failed'")
    assert len(failed) == 1


# --- a refusal -------------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["error_after_effect", "timeout_after_effect"])
async def test_a_refused_retry_keeps_the_funds_reserved_when_the_first_attempt_made_a_payout(
    db: Database,
    charging: Settings,
    sim: Sim,
    custody: SimCustody,
    maria: User,
    first: str,
) -> None:
    impatient = SimBank(with_providers(charging, provider_timeout_seconds=0.2), client=sim.http)
    withdrawal = await held_bank_withdrawal(db, charging, impatient, maria)
    await sim.inject("bank.create_payout", first, hang_seconds=30)
    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, impatient, custody, withdrawal.id)
    # The bank refuses the retry as it would refuse any request just now. That says
    # nothing about the payout it made the first time.
    await sim.inject("bank.create_payout", "error", status=429)

    await payments.submit_withdrawal(db, impatient, custody, withdrawal.id)

    (payout,) = await sim.payouts()
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"], row["failure_reason"]) == (
        "submitted",
        payout["id"],
        None,
    )
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)
    assert await count(db, "journal_entries") == 2
    (looked,) = sim.recorder.sent("GET", PAYOUTS)
    assert looked.url.params["reference"] == str(withdrawal.id)


async def test_a_refused_retry_releases_the_funds_when_the_provider_has_no_payout(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await sim.inject("bank.create_payout", "error")
    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await sim.inject("bank.create_payout", "error", status=422)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "injected_fault")
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert await sim.payouts() == []
    assert len(sim.recorder.sent("GET", PAYOUTS)) == 1


async def test_a_refused_retry_changes_nothing_while_the_provider_cannot_be_asked_what_it_has(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await sim.inject("bank.create_payout", "error_after_effect")
    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await sim.inject("bank.create_payout", "error", status=429)
    await sim.inject("bank.get_payout", "error")

    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitting"
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)


async def test_a_refused_retry_of_a_chain_withdrawal_finds_what_the_custodian_already_has(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_chain_withdrawal(db, charging, maria, EXTERNAL_ADDRESS)
    await sim.inject("custody.create_withdrawal", "error_after_effect")
    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await sim.inject("custody.create_withdrawal", "error", status=429)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    (sent,) = await sim.withdrawals()
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"]) == ("submitted", sent["id"])
    assert await held(db, maria, "USDC") == 25_375_000


@pytest.mark.parametrize("status", [422, 429])
async def test_a_refusal_from_a_provider_that_made_the_payout_all_the_same_keeps_it_reserved(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    status: int,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    # The first and only attempt, refused with a 4xx by a provider that had already made
    # the payout. Its contract says that cannot happen. The funds do not depend on it.
    await sim.inject("bank.create_payout", "error_after_effect", status=status)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    (payout,) = await sim.payouts()
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"]) == ("submitted", payout["id"])
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)
    assert await count(db, "journal_entries") == 2


async def test_a_refusal_is_checked_against_the_provider_before_the_funds_go_back(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await sim.inject("bank.create_payout", "error", status=422)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    (looked,) = sim.recorder.sent("GET", PAYOUTS)
    assert looked.url.params["reference"] == str(withdrawal.id)
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "failed"
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)


# --- what the provider says while the withdrawal is submitting --------------------------------


async def test_a_failure_that_arrives_while_the_withdrawal_is_submitting_releases_it(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await sim.inject("bank.create_payout", "error_after_effect")
    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    (payout,) = await sim.payouts()
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitting"

    await payments.apply_payout_failed(
        db,
        {
            "payout_id": payout["id"],
            "reference": str(withdrawal.id),
            "asset": "USD",
            "amount": "100.00",
            "failure_reason": "account_closed",
        },
    )
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "account_closed")
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert len(sim.recorder.sent("POST", PAYOUTS)) == 1


async def test_a_completion_that_arrives_while_the_withdrawal_is_submitting_settles_it(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await sim.inject("bank.create_payout", "error_after_effect")
    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, 30)

    await payments.apply_payout_completed(db, await sim.last_event("payout.completed"))
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert (await withdrawal_row(db, withdrawal.id))["status"] == "completed"
    assert (await available(db, maria), await held(db, maria)) == (398_50, 0)
    assert await settlement(db) == 399_75
    assert len(sim.recorder.sent("POST", PAYOUTS)) == 1

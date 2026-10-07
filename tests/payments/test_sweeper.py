"""The payout sweeper: what advances a withdrawal when its webhook never arrives."""

from typing import Any

import pytest
from sqlalchemy import text

from corridor import payments
from corridor.identity import User
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.providers import ProviderOutcomeUnknown, SimBank, SimCustody
from tests.payments.support import (
    add_beneficiary,
    available,
    count,
    held,
    held_bank_withdrawal,
    held_chain_withdrawal,
    leave_submitting,
    revenue_and_expense,
    settlement,
    withdraw,
    withdrawal_row,
)
from tests.support.providers import (
    CLOSED_ACCOUNT_NUMBER,
    EXTERNAL_ADDRESS,
    Sim,
    advance,
)

PAYOUTS = "/bank/v1/payouts"
SWEEP_AFTER = 120


@pytest.fixture
def charging(settings: Settings) -> Settings:
    """The test settings with a 1.5% withdrawal fee, sweeping after two minutes."""
    return settings.model_copy(
        update={"withdrawal_fee_bps": 150, "payout_sweep_after_seconds": SWEEP_AFTER}
    )


def polls(sim: Sim) -> list[Any]:
    """Every read the sweeper made of the bank's payouts."""
    return [
        request
        for request in sim.recorder.requests
        if request.method == "GET" and request.url.path.startswith(PAYOUTS)
    ]


async def test_a_submitted_withdrawal_whose_webhook_was_dropped_is_settled_by_the_sweeper(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, SWEEP_AFTER)

    advanced = await payments.sweep_payouts(db, bank, custody, charging)

    assert advanced == 1
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_fee"]) == ("completed", 25)
    assert (await available(db, maria), await held(db, maria)) == (398_50, 0)
    assert await settlement(db) == 399_75
    assert await revenue_and_expense(db) == (1_50, 25)
    (poll,) = polls(sim)
    assert poll.url.path == f"{PAYOUTS}/{row['provider_ref']}"


async def test_the_sweeper_leaves_a_withdrawal_that_was_submitted_recently(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, SWEEP_AFTER - 1)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 0

    assert polls(sim) == []
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"


async def test_the_sweeper_releases_a_withdrawal_whose_payout_failed(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(
        db, charging, bank, maria, account_number=CLOSED_ACCOUNT_NUMBER
    )
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, SWEEP_AFTER)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 1

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "account_closed")
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert await settlement(db) == 500_00


async def test_the_sweeper_leaves_a_payout_the_provider_has_not_finished(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    # Only Corridor's clock: at the bank the payout has not reached its settlement time.
    clock.advance(seconds=SWEEP_AFTER)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 0

    assert len(polls(sim)) == 1
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"
    assert await held(db, maria) == 101_50


async def test_the_sweeper_does_nothing_to_a_withdrawal_that_is_settled(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, SWEEP_AFTER)
    await payments.apply_payout_completed(db, await sim.last_event("payout.completed"))
    before = await withdrawal_row(db, withdrawal.id)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 0
    await advance(sim, clock, SWEEP_AFTER)
    assert await payments.sweep_payouts(db, bank, custody, charging) == 0

    assert polls(sim) == []
    assert await withdrawal_row(db, withdrawal.id) == before
    assert await count(db, "journal_entries") == 3
    assert await settlement(db) == 399_75


async def test_sweeping_twice_settles_once(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, SWEEP_AFTER)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 1
    assert await payments.sweep_payouts(db, bank, custody, charging) == 0
    await payments.apply_payout_completed(db, await sim.last_event("payout.completed"))

    assert await settlement(db) == 399_75
    assert await count(db, "journal_entries") == 3


async def test_a_withdrawal_left_submitting_that_the_provider_has_is_found_and_advanced(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    # The submission reached the bank and its answer never arrived, and its event is dead.
    await sim.inject("bank.create_payout", "error_after_effect")
    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    (payout,) = await sim.payouts()
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitting"
    clock.advance(seconds=SWEEP_AFTER)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 1
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"], row["submitted_at"]) == (
        "submitted",
        payout["id"],
        clock.now(),
    )
    assert await held(db, maria) == 101_50

    await advance(sim, clock, SWEEP_AFTER)
    assert await payments.sweep_payouts(db, bank, custody, charging) == 1
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "completed"
    assert (await available(db, maria), await held(db, maria)) == (398_50, 0)


async def test_a_withdrawal_left_submitting_and_found_paid_out_is_settled_in_the_same_sweep(
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
    await advance(sim, clock, SWEEP_AFTER)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 1

    assert (await withdrawal_row(db, withdrawal.id))["status"] == "completed"
    assert await settlement(db) == 399_75


async def test_the_sweeper_never_asks_about_a_withdrawal_that_is_only_held(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    clock.advance(seconds=10 * SWEEP_AFTER)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 0

    # Held means never sent: there is nothing a provider could say about it.
    assert polls(sim) == []
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "held"
    assert await held(db, maria) == 101_50


async def test_a_withdrawal_left_submitting_that_the_provider_has_never_seen_stays_as_it_is(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await leave_submitting(db, withdrawal.id)
    clock.advance(seconds=SWEEP_AFTER - 1)
    assert await payments.sweep_payouts(db, bank, custody, charging) == 0
    assert polls(sim) == []

    clock.advance(seconds=1)
    assert await payments.sweep_payouts(db, bank, custody, charging) == 0

    (poll,) = polls(sim)
    assert poll.url.params["reference"] == str(withdrawal.id)
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitting"
    assert await held(db, maria) == 101_50
    # The funds stay reserved, and the submission can still go out.
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"


async def test_a_provider_that_fails_for_one_withdrawal_does_not_stop_the_others(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    first = await held_bank_withdrawal(db, charging, bank, maria, 10_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    second = await withdraw(db, charging, maria, 20_00, beneficiary=beneficiary)
    for withdrawal in (first, second):
        await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, SWEEP_AFTER)
    await sim.inject("bank.get_payout", "error", times=1)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 1

    assert (await withdrawal_row(db, first.id))["status"] == "submitted"
    assert (await withdrawal_row(db, second.id))["status"] == "completed"
    assert await payments.sweep_payouts(db, bank, custody, charging) == 1
    assert (await withdrawal_row(db, first.id))["status"] == "completed"


async def test_the_sweeper_settles_a_chain_withdrawal_from_the_custodians_record(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_chain_withdrawal(db, charging, maria, EXTERNAL_ADDRESS)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, SWEEP_AFTER)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 1

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_fee"]) == ("completed", 150_000)
    assert (await available(db, maria, "USDC"), await held(db, maria, "USDC")) == (24_625_000, 0)
    assert polls(sim) == []


async def test_a_payout_that_is_another_withdrawals_settles_nothing(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    first = await held_bank_withdrawal(db, charging, bank, maria, 10_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    second = await withdraw(db, charging, maria, 10_00, beneficiary=beneficiary)
    await payments.submit_withdrawal(db, bank, custody, first.id)
    await payments.submit_withdrawal(db, bank, custody, second.id)
    others = (await withdrawal_row(db, second.id))["provider_ref"]
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE withdrawals SET provider_ref = :ref, status = 'failed' WHERE id = :id"),
            {"ref": "po_gone", "id": second.id},
        )
        # Recorded against the wrong payout: the same amount, but not this withdrawal's.
        await session.execute(
            text("UPDATE withdrawals SET provider_ref = :ref WHERE id = :id"),
            {"ref": others, "id": first.id},
        )
    await advance(sim, clock, SWEEP_AFTER)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 0

    assert (await withdrawal_row(db, first.id))["status"] == "submitted"
    assert await settlement(db) == 500_00


async def test_two_payouts_under_one_reference_are_not_chosen_between(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await leave_submitting(db, withdrawal.id)
    (token,) = sim.app.state.sim.bank._beneficiaries
    for key in ("one", "two"):
        await bank.create_payout(
            beneficiary_id=token,
            asset_code="USD",
            amount=100_00,
            reference=str(withdrawal.id),
            idempotency_key=key,
        )
    await advance(sim, clock, SWEEP_AFTER)

    assert await payments.sweep_payouts(db, bank, custody, charging) == 0

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"]) == ("submitting", None)
    assert await held(db, maria) == 101_50

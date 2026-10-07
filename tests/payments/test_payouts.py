"""Sending a withdrawal to its provider, and settling or releasing it when the provider says
what became of it. The provider is the simulator; its events are applied as it made them."""

import asyncio
import uuid
from typing import Any

import pytest
from pydantic import SecretStr

from corridor import identity, payments
from corridor.identity import User
from corridor.payments import (
    MalformedProviderEvent,
    ProviderEventMismatch,
    WithdrawalNotCancelable,
)
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import (
    Payout,
    ProviderMisconfigured,
    ProviderOutcomeUnknown,
    ProviderRejected,
    SimBank,
    SimCustody,
)
from tests.payments.support import (
    acting_as,
    available,
    count,
    entries,
    held,
    held_bank_withdrawal,
    held_chain_withdrawal,
    leave_submitting,
    omnibus,
    revenue_and_expense,
    rows,
    settlement,
    withdrawal_row,
)
from tests.support.providers import (
    CLOSED_ACCOUNT_NUMBER,
    EXTERNAL_ADDRESS,
    REJECTED_ADDRESS,
    WRONG_API_KEY,
    Sim,
    advance,
    with_providers,
)

PAYOUTS = "/bank/v1/payouts"
WITHDRAWALS = "/custody/v1/withdrawals"


@pytest.fixture
def charging(settings: Settings) -> Settings:
    """The test settings with a 1.5% withdrawal fee."""
    return settings.model_copy(update={"withdrawal_fee_bps": 150})


async def submitted_bank(
    db: Database,
    charging: Settings,
    bank: SimBank,
    custody: SimCustody,
    user: User,
    **more: Any,
) -> payments.Withdrawal:
    withdrawal = await held_bank_withdrawal(db, charging, bank, user, **more)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    return withdrawal


async def payout_completed(sim: Sim, clock: ManualClock) -> dict[str, Any]:
    """Let the ACH rail settle, and return the ``payout.completed`` data."""
    await advance(sim, clock, 30)
    return await sim.last_event("payout.completed")


async def status_of(db: Database, withdrawal: payments.Withdrawal) -> str:
    return str((await withdrawal_row(db, withdrawal.id))["status"])


# --- submission ------------------------------------------------------------------------------


async def test_a_held_bank_withdrawal_is_sent_once_under_its_own_id(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    clock.advance(seconds=7)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    (payout,) = await sim.payouts()
    assert payout["idempotency_key"] == str(withdrawal.id)
    assert payout["reference"] == str(withdrawal.id)
    # The fee stays with Corridor: the provider is asked to pay out the amount alone.
    assert (payout["asset"], payout["amount"]) == ("USD", "100.00")
    (request,) = sim.recorder.sent("POST", PAYOUTS)
    assert request.headers["Idempotency-Key"] == str(withdrawal.id)
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"]) == ("submitted", payout["id"])
    assert row["submitted_at"] == clock.now()
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'withdrawal.submitted'")
    assert (audited["actor_type"], audited["resource_id"]) == ("system", str(withdrawal.id))


async def test_a_held_chain_withdrawal_is_sent_once_under_its_own_id(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_chain_withdrawal(db, charging, maria, EXTERNAL_ADDRESS)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    (sent,) = await sim.withdrawals()
    assert sent["idempotency_key"] == str(withdrawal.id)
    assert (sent["reference"], sent["to_address"]) == (str(withdrawal.id), EXTERNAL_ADDRESS)
    assert (sent["asset"], sent["amount"]) == ("USDC", "25.000000")
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"]) == ("submitted", sent["id"])
    assert sim.recorder.sent("POST", PAYOUTS) == []


async def test_submitting_twice_asks_the_provider_once(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await submitted_bank(db, charging, bank, custody, maria)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert len(sim.recorder.sent("POST", PAYOUTS)) == 1
    assert len(await sim.payouts()) == 1
    assert await status_of(db, withdrawal) == "submitted"


async def test_a_canceled_withdrawal_is_never_sent(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    async with db.transaction() as session:
        await payments.cancel_withdrawal(session, acting_as(maria), withdrawal.id)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert sim.recorder.sent("POST", PAYOUTS) == []
    assert await status_of(db, withdrawal) == "canceled"
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)


async def test_a_withdrawal_that_does_not_exist_cannot_be_submitted(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody
) -> None:
    with pytest.raises(LookupError):
        await payments.submit_withdrawal(db, bank, custody, new_id())

    assert sim.recorder.requests == []


async def test_a_payout_the_bank_refuses_releases_the_funds(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    # A definite refusal with the contract's error body, as the bank gives for a bad request.
    await sim.inject("bank.create_payout", "error", status=422)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "injected_fault")
    assert (row["provider_ref"], row["submitted_at"]) == (None, None)
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    _hold, release = await entries(db, "withdrawal", str(withdrawal.id))
    assert (release["id"], release["kind"]) == (row["final_entry_id"], "withdrawal_release")
    assert release["postings"] == [("user_held", "D", 101_50), ("user_available", "C", 101_50)]
    assert await sim.payouts() == []


async def test_a_beneficiary_the_bank_does_not_know_fails_the_withdrawal(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    sim.app.state.sim.bank._beneficiaries.clear()

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "beneficiary_not_found")
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert len(sim.recorder.sent("POST", PAYOUTS)) == 1


async def test_a_withdrawal_the_custodian_refuses_releases_the_funds(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_chain_withdrawal(db, charging, maria, EXTERNAL_ADDRESS)
    await sim.inject("custody.create_withdrawal", "error", status=422)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert await status_of(db, withdrawal) == "failed"
    assert (await available(db, maria, "USDC"), await held(db, maria, "USDC")) == (50_000_000, 0)


async def test_a_key_the_provider_says_was_used_for_something_else_is_not_a_refusal(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    (token,) = sim.app.state.sim.bank._beneficiaries
    # The key is already bound at the bank, to a payout of another amount.
    await bank.create_payout(
        beneficiary_id=token,
        asset_code="USD",
        amount=1_00,
        reference=str(withdrawal.id),
        idempotency_key=str(withdrawal.id),
    )

    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("submitting", None)
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)
    assert await count(db, "journal_entries") == 2


@pytest.mark.parametrize("mode", ["error_after_effect", "timeout_after_effect"])
async def test_an_accepted_payout_whose_answer_was_lost_is_found_by_the_retry(
    db: Database,
    charging: Settings,
    sim: Sim,
    custody: SimCustody,
    maria: User,
    mode: str,
) -> None:
    impatient = SimBank(with_providers(charging, provider_timeout_seconds=0.2), client=sim.http)
    withdrawal = await held_bank_withdrawal(db, charging, impatient, maria)
    await sim.inject("bank.create_payout", mode, hang_seconds=30)

    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, impatient, custody, withdrawal.id)
    assert await status_of(db, withdrawal) == "submitting"
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)

    await payments.submit_withdrawal(db, impatient, custody, withdrawal.id)

    (payout,) = await sim.payouts()
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"]) == ("submitted", payout["id"])
    assert len(sim.recorder.sent("POST", PAYOUTS)) == 2


@pytest.mark.parametrize("mode", ["timeout", "error"])
async def test_a_provider_that_does_not_answer_leaves_the_funds_reserved_and_is_retried(
    db: Database,
    charging: Settings,
    sim: Sim,
    custody: SimCustody,
    maria: User,
    mode: str,
) -> None:
    impatient = SimBank(with_providers(charging, provider_timeout_seconds=0.2), client=sim.http)
    withdrawal = await held_bank_withdrawal(db, charging, impatient, maria)
    await sim.inject("bank.create_payout", mode, hang_seconds=30)

    with pytest.raises(ProviderOutcomeUnknown):
        await payments.submit_withdrawal(db, impatient, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"], row["failure_reason"]) == (
        "submitting",
        None,
        None,
    )
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)
    assert await sim.payouts() == []

    await payments.submit_withdrawal(db, impatient, custody, withdrawal.id)

    (payout,) = await sim.payouts()
    assert payout["idempotency_key"] == str(withdrawal.id)
    assert await status_of(db, withdrawal) == "submitted"


async def test_a_provider_that_rejects_corridors_key_fails_nothing_and_is_raised(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    locked_out = SimBank(
        with_providers(charging, bank_rail_api_key=SecretStr(WRONG_API_KEY)), client=sim.http
    )

    with pytest.raises(ProviderMisconfigured):
        await payments.submit_withdrawal(db, locked_out, custody, withdrawal.id)

    assert await status_of(db, withdrawal) == "submitting"
    assert await held(db, maria) == 101_50


# --- settlement ------------------------------------------------------------------------------


async def test_a_completed_payout_settles_with_both_fees(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await submitted_bank(db, charging, bank, custody, maria)
    data = await payout_completed(sim, clock)

    await payments.apply_payout_completed(db, data)

    # 500.00 came in. 100.00 went to the beneficiary and 0.25 to the bank; Corridor kept 1.50.
    assert (await available(db, maria), await held(db, maria)) == (398_50, 0)
    assert await settlement(db) == 500_00 - 100_00 - 25
    assert await revenue_and_expense(db) == (1_50, 25)
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_fee"], row["provider_ref"]) == (
        "completed",
        25,
        data["payout_id"],
    )
    _hold, settle = await entries(db, "withdrawal", str(withdrawal.id))
    assert (settle["id"], settle["kind"]) == (row["final_entry_id"], "withdrawal_settle")
    assert settle["postings"] == [
        ("user_held", "D", 101_50),
        ("provider_fee_expense", "D", 25),
        ("bank_settlement", "C", 100_25),
        ("fee_revenue", "C", 1_50),
    ]
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'withdrawal.completed'")
    assert (audited["actor_type"], audited["actor_id"]) == ("provider", "simbank")
    assert (audited["principal_id"], audited["resource_id"]) == (maria.id, str(withdrawal.id))


async def test_a_completed_payout_without_a_corridor_fee_has_no_revenue_posting(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    free = settings.model_copy(update={"withdrawal_min_fee": {}})
    withdrawal = await submitted_bank(db, free, bank, custody, maria)

    await payments.apply_payout_completed(db, await payout_completed(sim, clock))

    _hold, settle = await entries(db, "withdrawal", str(withdrawal.id))
    assert settle["postings"] == [
        ("user_held", "D", 100_00),
        ("provider_fee_expense", "D", 25),
        ("bank_settlement", "C", 100_25),
    ]
    assert await revenue_and_expense(db) == (0, 25)


async def test_a_payout_the_provider_charged_nothing_for_has_no_expense_posting(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await submitted_bank(db, charging, bank, custody, maria)
    data = {**await payout_completed(sim, clock), "fee": "0.00"}

    await payments.apply_payout_completed(db, data)

    _hold, settle = await entries(db, "withdrawal", str(withdrawal.id))
    assert settle["postings"] == [
        ("user_held", "D", 101_50),
        ("bank_settlement", "C", 100_00),
        ("fee_revenue", "C", 1_50),
    ]
    assert (await withdrawal_row(db, withdrawal.id))["provider_fee"] == 0


async def test_a_completed_chain_withdrawal_settles_against_the_omnibus_account(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_chain_withdrawal(db, charging, maria, EXTERNAL_ADDRESS)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await sim.mine(4)
    data = await sim.last_event("withdrawal.completed")

    await payments.apply_withdrawal_completed(db, data)

    assert (await available(db, maria, "USDC"), await held(db, maria, "USDC")) == (24_625_000, 0)
    # The test's funding came through the bank account, so the omnibus account shows only
    # what left it: the amount and the network fee.
    assert await omnibus(db) == -(25_000_000 + 150_000)
    assert await revenue_and_expense(db, "USDC") == (375_000, 150_000)
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_fee"]) == ("completed", 150_000)
    _hold, settle = await entries(db, "withdrawal", str(withdrawal.id))
    assert settle["postings"] == [
        ("user_held", "D", 25_375_000),
        ("provider_fee_expense", "D", 150_000),
        ("custody_omnibus", "C", 25_150_000),
        ("fee_revenue", "C", 375_000),
    ]


class OvertakenBank:
    """A bank whose settlement webhook is applied before its answer to the payout request
    has been recorded: the order the contract warns can happen."""

    name = "simbank"

    def __init__(self, inner: SimBank, db: Database, sim: Sim, clock: ManualClock) -> None:
        self._inner, self._db, self._sim, self._clock = inner, db, sim, clock

    async def create_payout(self, **arguments: Any) -> Payout:
        payout = await self._inner.create_payout(**arguments)
        await advance(self._sim, self._clock, 30)
        await payments.apply_payout_completed(
            self._db, await self._sim.last_event("payout.completed")
        )
        return payout


async def test_a_settlement_that_arrives_before_the_submission_is_recorded_still_completes(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    overtaken: Any = OvertakenBank(bank, db, sim, clock)

    await payments.submit_withdrawal(db, overtaken, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    (payout,) = await sim.payouts()
    assert (row["status"], row["provider_ref"], row["provider_fee"]) == (
        "completed",
        payout["id"],
        25,
    )
    # Settled straight from held, and the submission that came late changed nothing.
    assert row["submitted_at"] is None
    assert (await available(db, maria), await held(db, maria)) == (398_50, 0)
    assert await settlement(db) == 399_75
    assert await count(db, "journal_entries") == 3
    assert await rows(db, "SELECT 1 FROM audit_events WHERE action = 'withdrawal.submitted'") == []


async def test_a_completion_applied_many_times_settles_once(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    await submitted_bank(db, charging, bank, custody, maria)
    data = await payout_completed(sim, clock)

    await asyncio.gather(*(payments.apply_payout_completed(db, data) for _ in range(20)))
    for _ in range(3):
        await payments.apply_payout_completed(db, data)

    assert await settlement(db) == 399_75
    assert await revenue_and_expense(db) == (1_50, 25)
    assert await count(db, "journal_entries") == 3
    assert (
        len(await rows(db, "SELECT 1 FROM audit_events WHERE action = 'withdrawal.completed'")) == 1
    )


async def test_a_failed_payout_releases_the_funds(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await submitted_bank(
        db, charging, bank, custody, maria, account_number=CLOSED_ACCOUNT_NUMBER
    )
    await advance(sim, clock, 30)
    data = await sim.last_event("payout.failed")

    await payments.apply_payout_failed(db, data)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "account_closed")
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert await settlement(db) == 500_00
    assert await revenue_and_expense(db) == (0, 0)
    _hold, release = await entries(db, "withdrawal", str(withdrawal.id))
    assert (release["id"], release["kind"]) == (row["final_entry_id"], "withdrawal_release")
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'withdrawal.failed'")
    assert audited["actor_id"] == "simbank"


async def test_a_chain_withdrawal_rejected_by_the_network_releases_the_funds(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_chain_withdrawal(db, charging, maria, REJECTED_ADDRESS)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await sim.mine(1)

    await payments.apply_withdrawal_failed(db, await sim.last_event("withdrawal.failed"))

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "rejected_by_network")
    assert (await available(db, maria, "USDC"), await held(db, maria, "USDC")) == (50_000_000, 0)


async def test_a_failure_that_arrives_before_the_submission_is_recorded_still_releases(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)

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
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert await status_of(db, withdrawal) == "failed"
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert sim.recorder.sent("POST", PAYOUTS) == []


async def test_repeated_and_reordered_events_change_nothing_after_the_first(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await submitted_bank(db, charging, bank, custody, maria)
    completed = await payout_completed(sim, clock)
    failed = {
        "payout_id": completed["payout_id"],
        "reference": completed["reference"],
        "asset": "USD",
        "amount": "100.00",
        "failure_reason": "account_closed",
    }

    await payments.apply_payout_completed(db, completed)
    await payments.apply_payout_failed(db, failed)
    await payments.apply_payout_failed(db, failed)
    await payments.apply_payout_completed(db, completed)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert await status_of(db, withdrawal) == "completed"
    assert (await available(db, maria), await held(db, maria)) == (398_50, 0)
    assert await settlement(db) == 399_75
    assert await count(db, "journal_entries") == 3


async def test_a_completion_after_a_failure_does_not_settle_what_was_released(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await submitted_bank(
        db, charging, bank, custody, maria, account_number=CLOSED_ACCOUNT_NUMBER
    )
    await advance(sim, clock, 30)
    failed = await sim.last_event("payout.failed")
    completed = {
        "payout_id": failed["payout_id"],
        "reference": failed["reference"],
        "asset": "USD",
        "amount": "100.00",
        "fee": "0.25",
        "settled_at": "2026-01-15T12:00:30Z",
    }

    await asyncio.gather(*(payments.apply_payout_failed(db, failed) for _ in range(20)))
    await payments.apply_payout_completed(db, completed)

    assert await status_of(db, withdrawal) == "failed"
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert await settlement(db) == 500_00
    assert await count(db, "journal_entries") == 3


async def test_a_completion_for_a_canceled_withdrawal_moves_nothing(
    db: Database, charging: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    async with db.transaction() as session:
        await payments.cancel_withdrawal(session, acting_as(maria), withdrawal.id)

    await payments.apply_payout_completed(
        db,
        {
            "payout_id": "po_late",
            "reference": str(withdrawal.id),
            "asset": "USD",
            "amount": "100.00",
            "fee": "0.25",
            "settled_at": "2026-01-15T12:00:30Z",
        },
    )

    assert await status_of(db, withdrawal) == "canceled"
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert await settlement(db) == 500_00


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "100.01"},
        {"amount": "99.99"},
        {"amount": "101.50"},
        {"asset": "MXN"},
        {"payout_id": "po_another"},
    ],
)
async def test_a_completion_that_disagrees_with_the_withdrawal_is_refused_and_nothing_moves(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
    change: dict[str, Any],
) -> None:
    withdrawal = await submitted_bank(db, charging, bank, custody, maria)
    data = {**await payout_completed(sim, clock), **change}

    with pytest.raises(ProviderEventMismatch):
        await payments.apply_payout_completed(db, data)
    failed = {
        "payout_id": data["payout_id"],
        "reference": data["reference"],
        "asset": data["asset"],
        "amount": data["amount"],
        "failure_reason": "account_closed",
    }
    with pytest.raises(ProviderEventMismatch):
        await payments.apply_payout_failed(db, failed)

    assert await status_of(db, withdrawal) == "submitted"
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)
    assert await settlement(db) == 500_00
    assert await count(db, "journal_entries") == 2


async def test_an_event_of_the_other_provider_does_not_settle_a_withdrawal(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await submitted_bank(db, charging, bank, custody, maria)
    completed = await payout_completed(sim, clock)
    as_custody = {
        "withdrawal_id": completed["payout_id"],
        "reference": completed["reference"],
        "asset": "USD",
        "amount": "100.00",
        "network_fee": "0.25",
        "tx_hash": "00" * 32,
    }

    with pytest.raises((ProviderEventMismatch, MalformedProviderEvent)):
        await payments.apply_withdrawal_completed(db, as_custody)
    with pytest.raises(ProviderEventMismatch):
        await payments.apply_withdrawal_completed(
            db, {**as_custody, "asset": "USDC", "amount": "100.000000"}
        )

    assert await status_of(db, withdrawal) == "submitted"
    assert await held(db, maria) == 101_50


async def test_an_event_for_a_reference_that_is_not_a_withdrawal_is_refused(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await submitted_bank(db, charging, bank, custody, maria)
    data = await payout_completed(sim, clock)

    with pytest.raises(ProviderEventMismatch):
        await payments.apply_payout_completed(db, {**data, "reference": str(uuid.uuid4())})
    for change in ({"reference": "INV-2041"}, {"amount": 100}, {"fee": None}, {"payout_id": ""}):
        with pytest.raises(MalformedProviderEvent):
            await payments.apply_payout_completed(db, {**data, **change})

    assert await status_of(db, withdrawal) == "submitted"
    assert await held(db, maria) == 101_50


class CancelingMeanwhileBank:
    """A bank during whose call the user tries to cancel the withdrawal, and which then
    answers: with the payout it accepted, or with a refusal."""

    name = "simbank"

    def __init__(self, inner: SimBank, db: Database, user: User, *, refuse: bool) -> None:
        self._inner, self._db, self._user, self._refuse = inner, db, user, refuse

    def __getattr__(self, name: str) -> Any:
        # Everything but the request itself is the bank's own.
        return getattr(self._inner, name)

    async def create_payout(self, **arguments: Any) -> Payout:
        with pytest.raises(WithdrawalNotCancelable):
            async with self._db.transaction() as session:
                await payments.cancel_withdrawal(
                    session, acting_as(self._user), uuid.UUID(arguments["reference"])
                )
        if self._refuse:
            raise ProviderRejected(
                "invalid_amount", "refused", 422, provider=self.name, operation="create_payout"
            )
        return await self._inner.create_payout(**arguments)


@pytest.mark.parametrize(
    ("refuse", "outcome", "balances", "entries_posted"),
    [
        (True, ("failed", "invalid_amount"), (500_00, 0), 3),
        (False, ("submitted", None), (398_50, 101_50), 2),
    ],
)
async def test_a_cancellation_during_the_providers_call_is_refused_and_the_answer_decides(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    refuse: bool,
    outcome: tuple[str, str | None],
    balances: tuple[int, int],
    entries_posted: int,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    racing: Any = CancelingMeanwhileBank(bank, db, maria, refuse=refuse)

    await payments.submit_withdrawal(db, racing, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == outcome
    assert (await available(db, maria), await held(db, maria)) == balances
    # The funds went back at most once, and only because the provider refused.
    assert await count(db, "journal_entries") == entries_posted
    assert len(await sim.payouts()) == (0 if refuse else 1)


# --- an account that stopped being active while its withdrawal was held ----------------------


async def test_a_held_withdrawal_of_a_user_restricted_since_is_given_back_and_never_sent(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "under review")

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "account_not_active")
    assert row["provider_ref"] is None
    assert await sim.payouts() == []
    assert sim.recorder.sent("POST", PAYOUTS) == []
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    (_, release) = await entries(db, "withdrawal", str(withdrawal.id))
    assert release["kind"] == "withdrawal_release"
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'withdrawal.failed'")
    assert (audited["actor_type"], audited["principal_id"]) == ("system", maria.id)
    assert audited["details"]["reason"] == "account_not_active"


async def test_a_withdrawal_given_back_for_a_restricted_user_stays_given_back(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "under review")
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    async with db.transaction() as session:
        await identity.lift_restriction(session, maria.id)

    # The event is delivered again after the restriction was lifted.
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert await status_of(db, withdrawal) == "failed"
    assert await sim.payouts() == []
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)


async def test_a_withdrawal_already_being_sent_is_still_sent_for_a_user_restricted_since(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    # Past the mark the provider may have it, and giving the funds back could pay it twice.
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    await leave_submitting(db, withdrawal.id)
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "under review")

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert await status_of(db, withdrawal) == "submitted"
    assert len(await sim.payouts()) == 1


# --- what a provider says about why -----------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        ("account_closed", "account_closed"),
        ("r01:insufficient-funds.v2", "r01:insufficient-funds.v2"),
        ("Account Closed", "unspecified"),
        ("<script>alert(1)</script>", "unspecified"),
        ("", "unspecified"),
        ("a" * 65, "unspecified"),
        ("line\nbreak", "unspecified"),
    ],
)
async def test_a_failure_reason_is_kept_only_if_it_is_a_plain_code(
    db: Database,
    charging: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
    said: str,
    kept: str,
) -> None:
    withdrawal = await submitted_bank(
        db, charging, bank, custody, maria, account_number=CLOSED_ACCOUNT_NUMBER
    )
    await advance(sim, clock, 30)
    failed = {**await sim.last_event("payout.failed"), "failure_reason": said}

    await payments.apply_payout_failed(db, failed)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", kept)
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'withdrawal.failed'")
    assert audited["details"]["reason"] == kept


class RefusingBank:
    """A bank that refuses every payout in words of its own choosing, and holds none."""

    name = "simbank"

    def __init__(self, code: str) -> None:
        self._code = code

    async def create_payout(self, **_arguments: Any) -> Payout:
        raise ProviderRejected(
            self._code, "No.", 422, provider=self.name, operation="create_payout"
        )

    async def find_payouts(self, _reference: str) -> tuple[Payout, ...]:
        return ()


@pytest.mark.parametrize(
    ("said", "kept"),
    [("beneficiary_closed", "beneficiary_closed"), ("Beneficiary closed; call us", "unspecified")],
)
async def test_the_code_of_a_refusal_is_kept_only_if_it_is_a_plain_code(
    db: Database,
    charging: Settings,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    said: str,
    kept: str,
) -> None:
    withdrawal = await held_bank_withdrawal(db, charging, bank, maria)
    refusing: Any = RefusingBank(said)

    await payments.submit_withdrawal(db, refusing, custody, withdrawal.id)

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", kept)
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'withdrawal.failed'")
    assert audited["details"]["reason"] == kept

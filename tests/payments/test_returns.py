"""Returned deposits: a bank takes back money that was already credited, and perhaps spent."""

import asyncio
import contextlib
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from corridor import payments
from corridor.identity import User
from corridor.ledger import AccountKind, InsufficientFunds
from corridor.payments import DepositNotReceived, MalformedProviderEvent, ProviderEventMismatch
from corridor.platform.config import Settings
from corridor.platform.db import (
    LOCK_NOT_AVAILABLE,
    Database,
    advisory_xact_lock,
    lock_key,
    sqlstate_of,
)
from corridor.providers import SimBank, SimCustody
from corridor.risk import UserRestricted
from tests.payments.support import (
    add_beneficiary,
    available,
    balance_of,
    count,
    deposit,
    entries,
    held,
    instruction_for,
    rows,
    send,
    settlement,
    suspense,
    user_status,
    withdraw,
    withdrawal_row,
)
from tests.support.providers import EXTERNAL_ADDRESS, Sim


async def credited(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, user: User, amount: str = "100.00"
) -> dict[str, Any]:
    """A bank deposit, received and credited. Returns its ``deposit.received`` data."""
    instruction = await instruction_for(db, user, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, amount)
    await payments.apply_bank_deposit_received(db, data)
    return data


async def receivable(db: Database, user: User) -> int:
    return await balance_of(db, AccountKind.USER_RECEIVABLE, owner=user)


async def status_of(db: Database) -> str:
    (row,) = await rows(db, "SELECT status FROM deposits")
    return str(row["status"])


async def test_a_return_with_the_funds_intact_reverses_the_deposit(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await credited(db, sim, bank, custody, maria)

    await payments.apply_bank_deposit_returned(
        db, await sim.return_bank_deposit(data["deposit_id"])
    )

    assert await available(db, maria) == 0
    assert await settlement(db) == 0
    assert await receivable(db, maria) == 0
    assert await status_of(db) == "returned"
    assert await user_status(db, maria) == "active"
    credit, reversal = await entries(db, "deposit", f"simbank:{data['deposit_id']}")
    assert (credit["kind"], reversal["kind"]) == ("deposit", "deposit_return")
    assert reversal["postings"] == [
        ("user_available", "D", 100_00),
        ("bank_settlement", "C", 100_00),
    ]
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'deposit.returned'")
    assert (audited["actor_id"], audited["principal_id"]) == ("simbank", maria.id)
    assert audited["details"]["shortfall"] == "0"


async def test_a_return_after_spending_books_the_shortfall_and_restricts_the_user(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    joao: User,
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    await send(db, settings, maria, joao, 70_00)

    await payments.apply_bank_deposit_returned(
        db, await sim.return_bank_deposit(data["deposit_id"])
    )

    assert await available(db, maria) == 0
    assert await receivable(db, maria) == 70_00
    assert await settlement(db) == 0
    assert await available(db, joao) == 70_00
    assert await status_of(db) == "returned"
    (_, reversal) = await entries(db, "deposit", f"simbank:{data['deposit_id']}")
    assert reversal["postings"] == [
        ("user_available", "D", 30_00),
        ("user_receivable", "D", 70_00),
        ("bank_settlement", "C", 100_00),
    ]
    (user,) = await rows(
        db, "SELECT status, restricted_reason FROM users WHERE id = :id", id=maria.id
    )
    assert (user["status"], user["restricted_reason"]) == (
        "restricted",
        "returned deposit shortfall",
    )
    with pytest.raises(UserRestricted):
        await send(db, settings, maria, joao, 1)


async def test_a_return_after_spending_everything_is_all_shortfall(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    joao: User,
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    await send(db, settings, maria, joao, 100_00)

    await payments.apply_bank_deposit_returned(
        db, await sim.return_bank_deposit(data["deposit_id"])
    )

    assert await receivable(db, maria) == 100_00
    (_, reversal) = await entries(db, "deposit", f"simbank:{data['deposit_id']}")
    assert reversal["postings"] == [
        ("user_receivable", "D", 100_00),
        ("bank_settlement", "C", 100_00),
    ]
    assert await user_status(db, maria) == "restricted"


async def test_a_return_takes_only_what_the_returned_deposit_was(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    first = await credited(db, sim, bank, custody, maria, "100.00")
    await credited(db, sim, bank, custody, maria, "50.00")

    await payments.apply_bank_deposit_returned(
        db, await sim.return_bank_deposit(first["deposit_id"])
    )

    assert await available(db, maria) == 50_00
    assert await settlement(db) == 50_00
    assert await receivable(db, maria) == 0
    assert await user_status(db, maria) == "active"


async def test_a_repeated_return_changes_nothing(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    joao: User,
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    await send(db, settings, maria, joao, 70_00)
    returned = await sim.return_bank_deposit(data["deposit_id"])
    await payments.apply_bank_deposit_returned(db, returned)
    journal = await count(db, "journal_entries")

    for _ in range(3):
        await payments.apply_bank_deposit_returned(db, returned)
    await asyncio.gather(*(payments.apply_bank_deposit_returned(db, returned) for _ in range(10)))

    assert await count(db, "journal_entries") == journal
    assert await receivable(db, maria) == 70_00
    assert await settlement(db) == 0
    assert len(await rows(db, "SELECT 1 FROM audit_events WHERE action = 'deposit.returned'")) == 1


async def test_twenty_returns_at_once_reverse_the_deposit_once(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    await credited(db, sim, bank, custody, maria, "40.00")
    returned = await sim.return_bank_deposit(data["deposit_id"])

    await asyncio.gather(*(payments.apply_bank_deposit_returned(db, returned) for _ in range(20)))

    assert await available(db, maria) == 40_00
    assert await settlement(db) == 40_00
    assert await receivable(db, maria) == 0


async def test_a_returned_deposit_that_was_in_suspense_leaves_suspense(
    db: Database, sim: Sim
) -> None:
    data = await sim.bank_deposit("va_nobody", "40.00")
    await payments.apply_bank_deposit_received(db, data)

    returned = await sim.return_bank_deposit(data["deposit_id"])
    await payments.apply_bank_deposit_returned(db, returned)
    await payments.apply_bank_deposit_returned(db, returned)

    assert await suspense(db) == 0
    assert await settlement(db) == 0
    assert await status_of(db) == "returned"
    (_, reversal) = await entries(db, "deposit", f"simbank:{data['deposit_id']}")
    assert reversal["kind"] == "deposit_return"
    assert reversal["postings"] == [("suspense", "D", 40_00), ("bank_settlement", "C", 40_00)]


async def test_a_return_that_arrives_before_its_deposit_records_it_as_returned(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "100.00")
    returned = await sim.return_bank_deposit(data["deposit_id"])

    await payments.apply_bank_deposit_returned(db, returned)

    (row,) = await rows(db, "SELECT * FROM deposits")
    assert (row["status"], row["provider_ref"]) == ("returned", data["deposit_id"])
    assert (row["user_id"], row["entry_id"], int(row["amount"])) == (None, None, 100_00)
    assert await count(db, "journal_entries") == 0
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'deposit.returned'")
    assert audited["details"]["received"] is False


async def test_a_deposit_that_arrives_after_its_return_credits_nothing(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "100.00")
    returned = await sim.return_bank_deposit(data["deposit_id"])
    await payments.apply_bank_deposit_returned(db, returned)

    await payments.apply_bank_deposit_received(db, data)
    await payments.apply_bank_deposit_returned(db, returned)

    assert await available(db, maria) == 0
    assert await settlement(db) == 0
    assert await status_of(db) == "returned"
    assert await count(db, "journal_entries") == 0
    assert await count(db, "outbox_events") == 0
    assert await user_status(db, maria) == "active"


async def test_twenty_returns_at_once_before_the_deposit_record_it_once(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "100.00")
    returned = await sim.return_bank_deposit(data["deposit_id"])

    outcomes = await asyncio.gather(
        *(payments.apply_bank_deposit_returned(db, returned) for _ in range(20)),
        return_exceptions=True,
    )

    # The ones that lost the race are delivered again, and then find it returned.
    assert {type(outcome) for outcome in outcomes} <= {type(None), DepositNotReceived}
    await payments.apply_bank_deposit_returned(db, returned)
    assert await status_of(db) == "returned"
    assert await count(db, "journal_entries") == 0
    assert len(await rows(db, "SELECT 1 FROM audit_events WHERE action = 'deposit.returned'")) == 1


async def test_a_return_before_its_deposit_in_an_asset_no_bank_moves_is_refused(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "100.00")
    returned = {**await sim.return_bank_deposit(data["deposit_id"]), "asset": "USDC"}

    with pytest.raises(MalformedProviderEvent):
        await payments.apply_bank_deposit_returned(db, returned)

    assert await count(db, "deposits") == 0


@pytest.mark.parametrize("change", [{"amount": "999.00"}, {"asset": "MXN"}])
async def test_a_return_for_another_amount_or_asset_than_the_deposit_moves_nothing(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    change: dict[str, Any],
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    returned = {**await sim.return_bank_deposit(data["deposit_id"]), **change}

    with pytest.raises(ProviderEventMismatch):
        await payments.apply_bank_deposit_returned(db, returned)

    assert await available(db, maria) == 100_00
    assert await status_of(db) == "completed"


async def test_a_malformed_return_is_refused(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    returned = {**await sim.return_bank_deposit(data["deposit_id"]), "amount": 100}

    with pytest.raises(MalformedProviderEvent):
        await payments.apply_bank_deposit_returned(db, returned)

    assert await available(db, maria) == 100_00


async def test_a_return_racing_a_transfer_out_never_overdraws_and_loses_nothing(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    joao: User,
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    returned = await sim.return_bank_deposit(data["deposit_id"])

    async def spend() -> None:
        # Whichever goes first, the other sees what it left: the transfer either moved
        # 60.00 before the return, or finds nothing left to move.
        with contextlib.suppress(InsufficientFunds):
            await send(db, settings, maria, joao, 60_00)

    await asyncio.gather(payments.apply_bank_deposit_returned(db, returned), spend())

    assert await settlement(db) == 0
    assert await available(db, maria) == 0
    assert await available(db, joao) == await receivable(db, maria)
    assert await status_of(db) == "returned"


async def test_a_return_waits_for_the_users_money_out_lock(
    db: Database,
    impatient_db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    returned = await sim.return_bank_deposit(data["deposit_id"])

    async with db.transaction() as holder:
        await advisory_xact_lock(holder, [lock_key("money_out", maria.id)])
        with pytest.raises(DBAPIError) as failure:
            await payments.apply_bank_deposit_returned(impatient_db, returned)

    assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
    assert await available(db, maria) == 100_00
    assert await status_of(db) == "completed"


async def test_twenty_returns_at_once_of_a_deposit_in_suspense_reverse_it_once(
    db: Database, sim: Sim
) -> None:
    data = await sim.bank_deposit("va_nobody", "40.00")
    await payments.apply_bank_deposit_received(db, data)
    returned = await sim.return_bank_deposit(data["deposit_id"])

    await asyncio.gather(*(payments.apply_bank_deposit_returned(db, returned) for _ in range(20)))

    assert await suspense(db) == 0
    assert await settlement(db) == 0
    assert len(await rows(db, "SELECT 1 FROM audit_events WHERE action = 'deposit.returned'")) == 1


# --- withdrawals that are held when the return arrives -----------------------------------------


async def test_a_return_gives_back_a_held_withdrawal_before_it_takes_the_money(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    beneficiary = await add_beneficiary(db, bank, maria)
    withdrawal = await withdraw(db, settings, maria, 80_00, beneficiary=beneficiary)

    await payments.apply_bank_deposit_returned(
        db, await sim.return_bank_deposit(data["deposit_id"])
    )

    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "deposit_returned")
    assert (await available(db, maria), await held(db, maria)) == (0, 0)
    assert await receivable(db, maria) == 0
    assert await settlement(db) == 0
    assert await user_status(db, maria) == "active"
    # The event that would have sent it finds it given back, and sends nothing.
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    assert await sim.payouts() == []


async def test_a_return_leaves_a_withdrawal_that_is_already_with_the_provider(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    beneficiary = await add_beneficiary(db, bank, maria)
    withdrawal = await withdraw(db, settings, maria, 80_00, beneficiary=beneficiary)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    await payments.apply_bank_deposit_returned(
        db, await sim.return_bank_deposit(data["deposit_id"])
    )

    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"
    assert await held(db, maria) == 80_00 + withdrawal.fee
    assert await receivable(db, maria) == 80_00 + withdrawal.fee
    assert await user_status(db, maria) == "restricted"


async def test_a_return_leaves_a_held_withdrawal_of_another_asset(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    await deposit(db, maria, 50_000_000, "USDC")
    withdrawal = await withdraw(
        db, settings, maria, 25_000_000, asset="USDC", to_address=EXTERNAL_ADDRESS
    )

    await payments.apply_bank_deposit_returned(
        db, await sim.return_bank_deposit(data["deposit_id"])
    )

    assert (await withdrawal_row(db, withdrawal.id))["status"] == "held"
    assert await user_status(db, maria) == "active"


@pytest.mark.parametrize(
    ("said", "kept"),
    [("recalled", "recalled"), ("Recalled by the sender!", "unspecified"), ("", "unspecified")],
)
async def test_the_reason_of_a_return_is_kept_only_if_it_is_a_plain_code(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    said: str,
    kept: str,
) -> None:
    data = await credited(db, sim, bank, custody, maria)
    returned = {**await sim.return_bank_deposit(data["deposit_id"]), "reason": said}

    await payments.apply_bank_deposit_returned(db, returned)

    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'deposit.returned'")
    assert audited["details"]["reason"] == kept

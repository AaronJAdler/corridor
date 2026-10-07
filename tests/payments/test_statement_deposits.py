"""Deposits read from a provider's statement, because their event never arrived.

A statement gives the account or address the money arrived at and usually not who sent
it. What it gives is screened; what it does not is recorded as not screened."""

from typing import Any

import pytest

from corridor import payments, risk
from corridor.identity import User
from corridor.payments import MalformedProviderEvent, ProviderEventMismatch
from corridor.platform.db import Database
from corridor.providers import SimBank, SimCustody
from tests.payments.support import (
    available,
    count,
    entries,
    instruction_for,
    omnibus,
    rows,
    settlement,
    suspense,
)
from tests.support.providers import EXTERNAL_ADDRESS, Sim

SENDER = "Maria Silva"


async def bank_line(
    db: Database, bank: SimBank, custody: SimCustody, user: User, **changes: Any
) -> dict[str, Any]:
    """What a bank statement says of a deposit of 250.00 USD to the user's account."""
    instruction = await instruction_for(db, user, "USD", bank, custody)
    return {
        "provider": "simbank",
        "provider_ref": "dep_statement_1",
        "account_ref": instruction.provider_ref,
        "asset": "USD",
        "amount": 250_00,
        **changes,
    }


async def listed(db: Database, kind: str, value: str, outcome: str = "deny") -> None:
    async with db.transaction() as session:
        await risk.add_to_denylist(session, kind=kind, value=value, outcome=outcome)  # type: ignore[arg-type]


async def credit_audit(db: Database, action: str = "deposit.completed") -> dict[str, Any]:
    (event,) = await rows(
        db, "SELECT actor_id, details FROM audit_events WHERE action = :action", action=action
    )
    return event


async def test_a_statement_deposit_with_no_sender_is_credited_and_recorded_as_not_screened(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    line = await bank_line(db, bank, custody, maria)

    await payments.apply_statement_deposit(db, **line)

    assert await available(db, maria) == 250_00
    assert await settlement(db) == 250_00
    (row,) = await rows(db, "SELECT * FROM deposits")
    assert (row["status"], row["user_id"], row["provider_ref"]) == (
        "completed",
        maria.id,
        "dep_statement_1",
    )
    (entry,) = await entries(db, "deposit", "simbank:dep_statement_1")
    assert entry["postings"] == [("bank_settlement", "D", 250_00), ("user_available", "C", 250_00)]
    event = await credit_audit(db)
    assert (event["actor_id"], event["details"]["screened"]) == ("simbank", False)
    assert await count(db, "risk_reviews") == 0


async def test_a_statement_deposit_whose_sender_is_given_and_listed_goes_to_suspense(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    await listed(db, "name", SENDER)
    line = await bank_line(db, bank, custody, maria, sender="maria  SILVA")

    await payments.apply_statement_deposit(db, **line)

    assert (await available(db, maria), await suspense(db)) == (0, 250_00)
    (row,) = await rows(db, "SELECT * FROM deposits")
    assert (row["status"], row["user_id"]) == ("suspense", None)
    (review,) = await rows(db, "SELECT * FROM risk_reviews")
    assert (review["subject_id"], review["user_id"], review["outcome"], review["status"]) == (
        row["id"],
        maria.id,
        "deny",
        "open",
    )
    assert (await credit_audit(db, "deposit.suspended"))["details"]["screened"] is True


async def test_a_statement_deposit_whose_sender_is_given_and_not_listed_is_credited_as_screened(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    await listed(db, "name", "Someone Else")
    line = await bank_line(db, bank, custody, maria, sender=SENDER)

    await payments.apply_statement_deposit(db, **line)

    assert await available(db, maria) == 250_00
    assert (await credit_audit(db))["details"]["screened"] is True


@pytest.mark.parametrize("sender", [None, "", "   "])
async def test_a_listed_sender_the_statement_does_not_name_is_not_found_and_that_is_recorded(
    db: Database, bank: SimBank, custody: SimCustody, maria: User, sender: str | None
) -> None:
    # The rule for a party there is nothing to screen: the deposit goes where it arrived.
    await listed(db, "name", SENDER)
    line = await bank_line(db, bank, custody, maria, sender=sender)

    await payments.apply_statement_deposit(db, **line)

    assert await available(db, maria) == 250_00
    assert (await credit_audit(db))["details"]["screened"] is False


async def test_a_statement_deposit_to_an_account_nobody_was_given_goes_to_suspense(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    line = await bank_line(db, bank, custody, maria, account_ref="va_nobody")

    await payments.apply_statement_deposit(db, **line)

    assert (await available(db, maria), await suspense(db)) == (0, 250_00)
    assert (await credit_audit(db, "deposit.suspended"))["details"]["screened"] is False
    # Nobody's, and nothing was screened: there is no review to clear either.
    assert await count(db, "risk_reviews") == 0


async def test_a_statement_deposit_applied_twice_is_credited_once(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    line = await bank_line(db, bank, custody, maria)

    await payments.apply_statement_deposit(db, **line)
    await payments.apply_statement_deposit(db, **line)

    assert await available(db, maria) == 250_00
    assert await count(db, "deposits") == 1
    assert len(await entries(db, "deposit", "simbank:dep_statement_1")) == 1


async def test_a_statement_deposit_whose_event_arrived_first_changes_nothing(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "250.00")
    await payments.apply_bank_deposit_received(db, data)

    await payments.apply_statement_deposit(
        db,
        provider="simbank",
        provider_ref=data["deposit_id"],
        account_ref=instruction.provider_ref,
        asset="USD",
        amount=250_00,
    )
    # And the event after the statement, the other way about.
    await payments.apply_bank_deposit_received(db, data)

    assert await available(db, maria) == 250_00
    assert len(await entries(db, "deposit", f"simbank:{data['deposit_id']}")) == 1


async def test_a_chain_deposit_that_was_only_detected_is_credited_from_the_statement(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USDC", bank, custody)
    detected = await sim.chain_deposit(instruction.details["address"], "25")
    await payments.apply_chain_deposit_detected(db, detected)
    assert await available(db, maria, "USDC") == 0

    await payments.apply_statement_deposit(
        db,
        provider="simcustody",
        provider_ref=detected["deposit_id"],
        account_ref=detected["address_id"],
        asset="USDC",
        amount=25_000_000,
        tx_hash=detected["tx_hash"],
    )

    assert await available(db, maria, "USDC") == 25_000_000
    assert await omnibus(db) == 25_000_000
    (row,) = await rows(db, "SELECT status FROM deposits")
    assert row["status"] == "completed"


async def test_a_chain_statement_deposit_from_a_listed_address_goes_to_suspense(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USDC", bank, custody)
    await listed(db, "address", EXTERNAL_ADDRESS, "review")

    await payments.apply_statement_deposit(
        db,
        provider="simcustody",
        provider_ref="cdep_statement_1",
        account_ref=instruction.provider_ref,
        asset="USDC",
        amount=25_000_000,
        tx_hash="0xabc",
        sender=EXTERNAL_ADDRESS.upper(),
    )

    assert (await available(db, maria, "USDC"), await suspense(db, "USDC")) == (0, 25_000_000)
    (review,) = await rows(db, "SELECT outcome, user_id FROM risk_reviews")
    assert (review["outcome"], review["user_id"]) == ("review", maria.id)


async def test_a_statement_line_with_another_amount_than_the_recorded_deposit_is_refused(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    line = await bank_line(db, bank, custody, maria)
    await payments.apply_statement_deposit(db, **line)

    with pytest.raises(ProviderEventMismatch):
        await payments.apply_statement_deposit(db, **{**line, "amount": 999_00})

    assert await available(db, maria) == 250_00


@pytest.mark.parametrize(
    "change",
    [
        {"amount": 0},
        {"amount": -1},
        {"amount": True},
        {"asset": "USDC"},
        {"provider_ref": ""},
    ],
)
async def test_a_statement_line_that_could_not_be_a_deposit_is_refused_and_nothing_is_recorded(
    db: Database, bank: SimBank, custody: SimCustody, maria: User, change: dict[str, Any]
) -> None:
    line = await bank_line(db, bank, custody, maria, **change)

    with pytest.raises(MalformedProviderEvent):
        await payments.apply_statement_deposit(db, **line)

    assert await count(db, "deposits") == 0
    assert await count(db, "journal_entries") == 0


async def test_a_statement_of_a_provider_no_deposit_arrives_through_is_a_bug(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    line = await bank_line(db, bank, custody, maria, provider="simfx")

    with pytest.raises(ValueError, match="simfx"):
        await payments.apply_statement_deposit(db, **line)

    assert await count(db, "deposits") == 0


async def test_a_deposit_returned_before_it_was_seen_is_not_credited_from_the_statement(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    line = await bank_line(db, bank, custody, maria)
    await payments.apply_bank_deposit_returned(
        db,
        {"deposit_id": "dep_statement_1", "asset": "USD", "amount": "250.00", "reason": "recalled"},
    )

    await payments.apply_statement_deposit(db, **line)

    assert await available(db, maria) == 0
    (row,) = await rows(db, "SELECT status, entry_id FROM deposits")
    assert (row["status"], row["entry_id"]) == ("returned", None)

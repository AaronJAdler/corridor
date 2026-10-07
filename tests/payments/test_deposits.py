"""Deposits: money that arrives at a provider and is credited when Corridor hears of it.

Each test hands the entry points the ``data`` of a webhook event exactly as the simulator
made it, as the webhook slice will once it has verified a delivery.
"""

import asyncio
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor import identity, payments, risk
from corridor.identity import InsufficientScope, Scope, User
from corridor.payments import DepositNotFound, MalformedProviderEvent, ProviderEventMismatch
from corridor.platform.db import (
    LOCK_NOT_AVAILABLE,
    Database,
    advisory_xact_lock,
    lock_key,
    sqlstate_of,
)
from corridor.platform.pagination import InvalidCursor
from corridor.providers import SimBank, SimCustody
from tests.identity.support import close_account
from tests.payments.support import (
    acting_as,
    agent_of,
    available,
    count,
    entries,
    instruction_for,
    omnibus,
    rows,
    settlement,
    suspense,
    user_status,
)
from tests.support.providers import Sim


async def received(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, user: User, amount: str = "250.00"
) -> dict[str, Any]:
    """A bank deposit to the user's virtual account, as its ``deposit.received`` data."""
    instruction = await instruction_for(db, user, "USD", bank, custody)
    return await sim.bank_deposit(instruction.provider_ref, amount)


async def detected(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, user: User, amount: str = "25"
) -> dict[str, Any]:
    """A transaction to the user's deposit address, as its ``deposit.detected`` data."""
    instruction = await instruction_for(db, user, "USDC", bank, custody)
    return await sim.chain_deposit(instruction.details["address"], amount)


async def confirmed(sim: Sim) -> dict[str, Any]:
    await sim.mine(3)
    return await sim.last_event("deposit.confirmed")


async def deposit_rows(db: Database) -> list[dict[str, Any]]:
    return await rows(db, "SELECT * FROM deposits ORDER BY id")


# --- bank deposits ---------------------------------------------------------------------------


async def test_a_bank_deposit_is_credited_to_the_user_whose_account_it_arrived_at(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await received(db, sim, bank, custody, maria)

    await payments.apply_bank_deposit_received(db, data)

    assert await available(db, maria) == 250_00
    assert await settlement(db) == 250_00
    (row,) = await deposit_rows(db)
    assert (row["user_id"], row["asset_code"], row["amount"]) == (maria.id, "USD", 250_00)
    assert (row["provider"], row["provider_ref"]) == ("simbank", data["deposit_id"])
    assert (row["kind"], row["status"], row["tx_hash"]) == ("bank", "completed", None)
    (entry,) = await entries(db, "deposit", f"simbank:{data['deposit_id']}")
    assert (entry["id"], entry["kind"]) == (row["entry_id"], "deposit")
    assert entry["postings"] == [
        ("bank_settlement", "D", 250_00),
        ("user_available", "C", 250_00),
    ]


async def test_a_credited_deposit_is_announced_and_audited(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await received(db, sim, bank, custody, maria)

    await payments.apply_bank_deposit_received(db, data)

    (row,) = await deposit_rows(db)
    (event,) = await rows(db, "SELECT topic, payload FROM outbox_events")
    assert event["topic"] == "deposit.completed"
    assert event["payload"] == {
        "deposit_id": str(row["id"]),
        "user_id": str(maria.id),
        "asset": "USD",
        "amount": "25000",
        "entry_id": str(row["entry_id"]),
    }
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'deposit.completed'")
    assert (audited["actor_type"], audited["actor_id"]) == ("provider", "simbank")
    assert (audited["principal_id"], audited["resource_id"]) == (maria.id, str(row["id"]))


async def test_a_bank_deposit_applied_five_times_is_credited_once(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await received(db, sim, bank, custody, maria)

    for _ in range(5):
        await payments.apply_bank_deposit_received(db, data)

    assert await available(db, maria) == 250_00
    assert await settlement(db) == 250_00
    assert await count(db, "deposits") == 1
    assert await count(db, "journal_entries") == 1
    assert await count(db, "outbox_events") == 1


async def test_a_bank_deposit_applied_twenty_times_at_once_is_credited_once(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await received(db, sim, bank, custody, maria)

    await asyncio.gather(*(payments.apply_bank_deposit_received(db, data) for _ in range(20)))

    assert await available(db, maria) == 250_00
    assert await count(db, "deposits") == 1
    assert await count(db, "journal_entries") == 1
    assert await count(db, "outbox_events") == 1


async def test_two_deposits_to_one_account_are_two_credits(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    first = await received(db, sim, bank, custody, maria, "10.00")
    second = await received(db, sim, bank, custody, maria, "5.50")

    await payments.apply_bank_deposit_received(db, first)
    await payments.apply_bank_deposit_received(db, second)

    assert await available(db, maria) == 15_50
    assert await count(db, "deposits") == 2


async def test_a_deposit_to_an_account_nobody_was_given_goes_to_suspense(
    db: Database, sim: Sim, maria: User
) -> None:
    data = await sim.bank_deposit("va_nobody", "40.00")

    await payments.apply_bank_deposit_received(db, data)

    assert await suspense(db) == 40_00
    assert await settlement(db) == 40_00
    assert await available(db, maria) == 0
    (row,) = await deposit_rows(db)
    assert (row["user_id"], row["status"], row["amount"]) == (None, "suspense", 40_00)
    (entry,) = await entries(db, "deposit", f"simbank:{data['deposit_id']}")
    assert entry["kind"] == "deposit_suspense"
    assert entry["postings"] == [("bank_settlement", "D", 40_00), ("suspense", "C", 40_00)]
    assert await count(db, "outbox_events") == 0


async def test_a_deposit_in_another_asset_than_its_account_goes_to_suspense(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = {**await received(db, sim, bank, custody, maria), "asset": "MXN"}

    await payments.apply_bank_deposit_received(db, data)

    assert await suspense(db, "MXN") == 250_00
    assert (await available(db, maria, "USD"), await available(db, maria, "MXN")) == (0, 0)
    (row,) = await deposit_rows(db)
    assert (row["user_id"], row["status"], row["asset_code"]) == (None, "suspense", "MXN")


async def test_the_customer_reference_in_the_payload_does_not_decide_who_is_credited(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User, joao: User
) -> None:
    data = {**await received(db, sim, bank, custody, maria), "customer_reference": str(joao.id)}

    await payments.apply_bank_deposit_received(db, data)

    assert (await available(db, maria), await available(db, joao)) == (250_00, 0)


async def test_a_customer_reference_does_not_attribute_a_deposit_to_an_unknown_account(
    db: Database, sim: Sim, maria: User
) -> None:
    data = {**await sim.bank_deposit("va_nobody", "40.00"), "customer_reference": str(maria.id)}

    await payments.apply_bank_deposit_received(db, data)

    assert await available(db, maria) == 0
    assert await suspense(db) == 40_00


async def test_a_custody_address_id_does_not_attribute_a_bank_deposit(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    address = await instruction_for(db, maria, "USDC", bank, custody)
    data = await sim.bank_deposit(address.provider_ref, "40.00")

    await payments.apply_bank_deposit_received(db, data)

    assert await available(db, maria) == 0
    assert await suspense(db) == 40_00


async def test_an_account_of_the_same_name_at_another_provider_attributes_nothing(
    db: Database, sim: Sim, joao: User
) -> None:
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO deposit_instructions"
                " (user_id, asset_code, provider, provider_ref, details, created_at)"
                " VALUES (:user_id, 'USD', 'otherbank', 'va_same', '{}', now())"
            ),
            {"user_id": joao.id},
        )
    data = await sim.bank_deposit("va_same", "40.00")

    await payments.apply_bank_deposit_received(db, data)

    assert await available(db, joao) == 0
    assert await suspense(db) == 40_00


async def test_a_restricted_user_can_still_receive_a_deposit(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "under review")
    data = await received(db, sim, bank, custody, maria)

    await payments.apply_bank_deposit_received(db, data)

    assert await available(db, maria) == 250_00
    assert await user_status(db, maria) == "restricted"


@pytest.mark.parametrize(
    "change",
    [
        {"amount": 250.0},
        {"amount": 25000},
        {"amount": "250.005"},
        {"amount": "0.00"},
        {"amount": "-1.00"},
        {"asset": "EUR"},
        {"asset": "USDC"},
        {"deposit_id": ""},
        {"deposit_id": None},
        {"virtual_account_id": 7},
    ],
)
async def test_a_bank_deposit_that_is_not_what_the_contract_says_is_refused(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    change: dict[str, Any],
) -> None:
    data = {**await received(db, sim, bank, custody, maria), **change}

    with pytest.raises(MalformedProviderEvent):
        await payments.apply_bank_deposit_received(db, data)

    assert await count(db, "deposits") == 0
    assert await count(db, "journal_entries") == 0


async def test_a_bank_deposit_with_a_field_missing_is_refused_without_repeating_the_payload(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await received(db, sim, bank, custody, maria)
    del data["amount"]

    with pytest.raises(MalformedProviderEvent) as refusal:
        await payments.apply_bank_deposit_received(db, data)

    assert "amount" in str(refusal.value)
    assert data["deposit_id"] not in str(refusal.value)
    assert await count(db, "deposits") == 0


# --- on-chain deposits -----------------------------------------------------------------------


async def test_a_detected_deposit_is_pending_and_touches_no_ledger(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await detected(db, sim, bank, custody, maria)

    await payments.apply_chain_deposit_detected(db, data)

    (row,) = await deposit_rows(db)
    assert (row["user_id"], row["asset_code"], row["amount"]) == (maria.id, "USDC", 25_000_000)
    assert (row["provider"], row["provider_ref"]) == ("simcustody", data["deposit_id"])
    assert (row["kind"], row["status"], row["entry_id"]) == ("chain", "pending", None)
    assert row["tx_hash"] == data["tx_hash"]
    assert await count(db, "journal_entries") == 0
    assert await count(db, "postings") == 0
    assert await available(db, maria, "USDC") == 0


async def test_a_deposit_detected_twice_is_one_deposit(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await detected(db, sim, bank, custody, maria)

    await asyncio.gather(*(payments.apply_chain_deposit_detected(db, data) for _ in range(5)))
    await payments.apply_chain_deposit_detected(db, data)

    assert await count(db, "deposits") == 1


async def test_a_confirmed_deposit_is_credited(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    await payments.apply_chain_deposit_detected(db, await detected(db, sim, bank, custody, maria))
    data = await confirmed(sim)

    await payments.apply_chain_deposit_confirmed(db, data)

    assert await available(db, maria, "USDC") == 25_000_000
    assert await omnibus(db) == 25_000_000
    (row,) = await deposit_rows(db)
    assert (row["status"], row["user_id"]) == ("completed", maria.id)
    (entry,) = await entries(db, "deposit", f"simcustody:{data['deposit_id']}")
    assert (entry["id"], entry["kind"]) == (row["entry_id"], "deposit")
    assert entry["postings"] == [
        ("custody_omnibus", "D", 25_000_000),
        ("user_available", "C", 25_000_000),
    ]
    (event,) = await rows(db, "SELECT topic FROM outbox_events")
    assert event["topic"] == "deposit.completed"


async def test_a_confirmation_applied_many_times_credits_once(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    await payments.apply_chain_deposit_detected(db, await detected(db, sim, bank, custody, maria))
    data = await confirmed(sim)

    for _ in range(5):
        await payments.apply_chain_deposit_confirmed(db, data)
    await asyncio.gather(*(payments.apply_chain_deposit_confirmed(db, data) for _ in range(20)))

    assert await available(db, maria, "USDC") == 25_000_000
    assert await count(db, "journal_entries") == 1
    assert await count(db, "outbox_events") == 1


async def test_a_confirmation_that_arrives_before_the_detection_credits_exactly_once(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    seen = await detected(db, sim, bank, custody, maria)
    final = await confirmed(sim)

    await payments.apply_chain_deposit_confirmed(db, final)
    await payments.apply_chain_deposit_detected(db, seen)
    await payments.apply_chain_deposit_confirmed(db, final)

    assert await available(db, maria, "USDC") == 25_000_000
    assert await omnibus(db) == 25_000_000
    (row,) = await deposit_rows(db)
    assert (row["status"], row["user_id"], row["kind"]) == ("completed", maria.id, "chain")
    assert await count(db, "journal_entries") == 1


async def test_twenty_confirmations_at_once_with_no_detection_credit_once(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    await detected(db, sim, bank, custody, maria)
    final = await confirmed(sim)

    await asyncio.gather(*(payments.apply_chain_deposit_confirmed(db, final) for _ in range(20)))

    assert await available(db, maria, "USDC") == 25_000_000
    assert await count(db, "deposits") == 1
    assert await count(db, "journal_entries") == 1


async def test_twenty_confirmations_at_once_of_a_detected_deposit_credit_and_announce_once(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    await payments.apply_chain_deposit_detected(db, await detected(db, sim, bank, custody, maria))
    final = await confirmed(sim)

    await asyncio.gather(*(payments.apply_chain_deposit_confirmed(db, final) for _ in range(20)))

    assert await available(db, maria, "USDC") == 25_000_000
    assert await count(db, "journal_entries") == 1
    assert await count(db, "outbox_events") == 1
    assert len(await rows(db, "SELECT 1 FROM audit_events WHERE action = 'deposit.completed'")) == 1


async def test_a_deposit_dropped_after_detection_fails_and_leaves_no_ledger_effect(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    seen = await detected(db, sim, bank, custody, maria)
    await payments.apply_chain_deposit_detected(db, seen)
    dropped = await sim.drop_chain_deposit(seen["deposit_id"])

    await payments.apply_chain_deposit_failed(db, dropped)
    await payments.apply_chain_deposit_failed(db, dropped)

    (row,) = await deposit_rows(db)
    assert (row["status"], row["entry_id"]) == ("failed", None)
    assert await count(db, "journal_entries") == 0
    assert await available(db, maria, "USDC") == 0


async def test_a_failed_deposit_is_not_credited_by_a_late_confirmation(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    seen = await detected(db, sim, bank, custody, maria)
    await payments.apply_chain_deposit_detected(db, seen)
    await payments.apply_chain_deposit_failed(db, await sim.drop_chain_deposit(seen["deposit_id"]))

    await payments.apply_chain_deposit_confirmed(db, {**seen, "confirmations": 3})

    (row,) = await deposit_rows(db)
    assert row["status"] == "failed"
    assert await count(db, "journal_entries") == 0
    assert await available(db, maria, "USDC") == 0


async def test_a_failure_that_arrives_before_the_detection_keeps_the_deposit_failed(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    seen = await detected(db, sim, bank, custody, maria)
    dropped = await sim.drop_chain_deposit(seen["deposit_id"])

    await payments.apply_chain_deposit_failed(db, dropped)
    await payments.apply_chain_deposit_detected(db, seen)

    (row,) = await deposit_rows(db)
    assert (row["status"], row["provider_ref"]) == ("failed", seen["deposit_id"])
    assert await count(db, "journal_entries") == 0


async def test_a_failure_for_a_deposit_already_credited_changes_nothing(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    seen = await detected(db, sim, bank, custody, maria)
    await payments.apply_chain_deposit_confirmed(db, await confirmed(sim))

    await payments.apply_chain_deposit_failed(
        db,
        {
            "deposit_id": seen["deposit_id"],
            "asset": "USDC",
            "amount": seen["amount"],
            "tx_hash": seen["tx_hash"],
            "reason": "dropped",
        },
    )

    (row,) = await deposit_rows(db)
    assert row["status"] == "completed"
    assert await available(db, maria, "USDC") == 25_000_000


async def test_a_confirmation_for_another_amount_than_was_detected_is_refused(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    seen = await detected(db, sim, bank, custody, maria)
    await payments.apply_chain_deposit_detected(db, seen)

    with pytest.raises(ProviderEventMismatch):
        await payments.apply_chain_deposit_confirmed(
            db, {**seen, "amount": "2500.000000", "confirmations": 3}
        )

    (row,) = await deposit_rows(db)
    assert row["status"] == "pending"
    assert await count(db, "journal_entries") == 0


async def test_a_confirmed_deposit_to_an_address_nobody_was_given_goes_to_suspense(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    seen = await detected(db, sim, bank, custody, maria)
    data = {**seen, "address_id": "addr_nobody", "confirmations": 3}

    await payments.apply_chain_deposit_confirmed(db, data)

    assert await available(db, maria, "USDC") == 0
    assert await suspense(db, "USDC") == 25_000_000
    assert await omnibus(db) == 25_000_000
    (row,) = await deposit_rows(db)
    assert (row["user_id"], row["status"]) == (None, "suspense")


async def test_the_customer_reference_does_not_attribute_a_chain_deposit(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User, joao: User
) -> None:
    seen = await detected(db, sim, bank, custody, maria)
    data = {**seen, "customer_reference": str(joao.id), "confirmations": 3}

    await payments.apply_chain_deposit_confirmed(db, data)

    assert await available(db, maria, "USDC") == 25_000_000
    assert await available(db, joao, "USDC") == 0


@pytest.mark.parametrize(
    "change",
    [
        {"amount": 25},
        {"asset": "USD"},
        {"confirmations": "3"},
        {"tx_hash": None},
        {"address_id": ""},
    ],
)
async def test_a_chain_deposit_that_is_not_what_the_contract_says_is_refused(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    change: dict[str, Any],
) -> None:
    data = {**await detected(db, sim, bank, custody, maria), **change}

    with pytest.raises(MalformedProviderEvent):
        await payments.apply_chain_deposit_detected(db, data)
    with pytest.raises(MalformedProviderEvent):
        await payments.apply_chain_deposit_confirmed(db, data)

    assert await count(db, "deposits") == 0


# --- two providers ---------------------------------------------------------------------------


async def test_two_providers_that_give_their_deposits_the_same_id_are_both_credited(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    at_bank = await received(db, sim, bank, custody, maria)
    await detected(db, sim, bank, custody, maria)
    # Each provider numbers its own deposits, and nothing keeps their numbers apart.
    on_chain = {**await confirmed(sim), "deposit_id": at_bank["deposit_id"]}

    await payments.apply_bank_deposit_received(db, at_bank)
    await payments.apply_chain_deposit_confirmed(db, on_chain)

    assert await available(db, maria) == 250_00
    assert await available(db, maria, "USDC") == 25_000_000
    assert [(row["provider"], row["status"]) for row in await deposit_rows(db)] == [
        ("simbank", "completed"),
        ("simcustody", "completed"),
    ]
    (bank_entry,) = await entries(db, "deposit", f"simbank:{at_bank['deposit_id']}")
    (chain_entry,) = await entries(db, "deposit", f"simcustody:{at_bank['deposit_id']}")
    assert bank_entry["id"] != chain_entry["id"]
    assert await entries(db, "deposit", at_bank["deposit_id"]) == []


# --- reading ---------------------------------------------------------------------------------


async def test_a_user_reads_their_own_deposit_and_nobody_elses(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User, joao: User
) -> None:
    await payments.apply_bank_deposit_received(db, await received(db, sim, bank, custody, maria))
    (row,) = await deposit_rows(db)

    async with db.transaction() as session:
        deposit = await payments.get_deposit(session, acting_as(maria), row["id"])
        with pytest.raises(DepositNotFound):
            await payments.get_deposit(session, acting_as(joao), row["id"])
        with pytest.raises(DepositNotFound):
            await payments.get_deposit(session, acting_as(maria), row["entry_id"])

    assert (deposit.id, deposit.user_id, deposit.asset, deposit.amount) == (
        row["id"],
        maria.id,
        "USD",
        250_00,
    )
    assert (deposit.kind, deposit.status) == ("bank", "completed")


async def test_deposits_are_listed_newest_first_a_page_at_a_time(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User, joao: User
) -> None:
    for amount in ("1.00", "2.00", "3.00"):
        await payments.apply_bank_deposit_received(
            db, await received(db, sim, bank, custody, maria, amount)
        )
    await payments.apply_bank_deposit_received(db, await received(db, sim, bank, custody, joao))
    await payments.apply_bank_deposit_received(db, await sim.bank_deposit("va_nobody", "9.00"))

    async with db.transaction() as session:
        first = await payments.list_deposits(session, acting_as(maria), limit=2)
        assert first.next_cursor is not None
        second = await payments.list_deposits(
            session, acting_as(maria), limit=2, cursor=first.next_cursor
        )
        with pytest.raises(InvalidCursor):
            await payments.list_deposits(session, acting_as(joao), cursor=first.next_cursor)

    assert [deposit.amount for deposit in first.items] == [3_00, 2_00]
    assert [deposit.amount for deposit in second.items] == [1_00]
    assert second.next_cursor is None


async def test_reading_deposits_needs_the_deposits_scope(db: Database, maria: User) -> None:
    async with db.transaction() as session:
        with pytest.raises(InsufficientScope):
            await payments.list_deposits(session, agent_of(maria, Scope.WALLET_READ))
        with pytest.raises(InsufficientScope):
            await payments.get_deposit(session, agent_of(maria, Scope.WALLET_READ), maria.id)
        page = await payments.list_deposits(session, agent_of(maria, Scope.DEPOSITS_READ))

    assert page.items == ()


@pytest.mark.parametrize("change", [{"amount": "999.00"}, {"asset": "MXN"}])
async def test_a_repeated_bank_deposit_with_another_amount_or_asset_is_refused(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    change: dict[str, Any],
) -> None:
    data = await received(db, sim, bank, custody, maria)
    await payments.apply_bank_deposit_received(db, data)

    with pytest.raises(ProviderEventMismatch):
        await payments.apply_bank_deposit_received(db, {**data, **change})

    assert await available(db, maria) == 250_00
    assert await count(db, "deposits") == 1
    assert await count(db, "journal_entries") == 1


@pytest.mark.parametrize(
    ("said", "kept"),
    [("dropped", "dropped"), ("Dropped from the mempool", "unspecified")],
)
async def test_the_reason_of_a_failed_deposit_is_kept_only_if_it_is_a_plain_code(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    said: str,
    kept: str,
) -> None:
    seen = await detected(db, sim, bank, custody, maria)
    await payments.apply_chain_deposit_detected(db, seen)
    dropped = {**await sim.drop_chain_deposit(seen["deposit_id"]), "reason": said}

    await payments.apply_chain_deposit_failed(db, dropped)

    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'deposit.failed'")
    assert audited["details"]["reason"] == kept


# --- a closed account receives nothing -------------------------------------------------------


async def close(db: Database, user: User) -> None:
    async with db.transaction() as session:
        await close_account(session, user.id)


async def review_of(db: Database) -> dict[str, Any]:
    (review,) = await rows(db, "SELECT * FROM risk_reviews")
    return review


async def test_a_bank_deposit_for_a_closed_account_goes_to_suspense_with_a_review(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await received(db, sim, bank, custody, maria)
    await close(db, maria)

    await payments.apply_bank_deposit_received(db, data)

    # The money did arrive, so it is booked: to nobody, for an operator to send back.
    assert (await available(db, maria), await suspense(db), await settlement(db)) == (
        0,
        250_00,
        250_00,
    )
    (row,) = await deposit_rows(db)
    assert (row["user_id"], row["status"]) == (None, "suspense")
    review = await review_of(db)
    assert (review["subject_type"], review["subject_id"]) == ("deposit", row["id"])
    # The review remembers whose it would have been.
    assert (review["user_id"], review["outcome"], review["status"]) == (maria.id, "review", "open")
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'deposit.suspended'")
    assert audited["details"]["reason"] == "owner_closed"
    assert await count(db, "outbox_events") == 0


async def test_a_confirmed_chain_deposit_for_a_closed_account_goes_to_suspense_with_a_review(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    await payments.apply_chain_deposit_detected(db, await detected(db, sim, bank, custody, maria))
    await close(db, maria)

    await payments.apply_chain_deposit_confirmed(db, await confirmed(sim))

    assert (await available(db, maria, "USDC"), await suspense(db, "USDC")) == (0, 25_000_000)
    (row,) = await deposit_rows(db)
    assert (row["user_id"], row["status"]) == (None, "suspense")
    assert (await review_of(db))["user_id"] == maria.id


async def test_a_statement_deposit_for_a_closed_account_goes_to_suspense_with_a_review(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USD", bank, custody)
    await close(db, maria)

    await payments.apply_statement_deposit(
        db,
        provider="simbank",
        provider_ref="dep_statement_1",
        account_ref=instruction.provider_ref,
        asset="USD",
        amount=250_00,
    )

    assert (await available(db, maria), await suspense(db)) == (0, 250_00)
    assert (await review_of(db))["user_id"] == maria.id


async def test_a_deposit_from_a_denied_sender_to_a_closed_account_keeps_the_screening_outcome(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await received(db, sim, bank, custody, maria)
    async with db.transaction() as session:
        await risk.add_to_denylist(session, kind="name", value=data["sender_name"], outcome="deny")
    await close(db, maria)

    await payments.apply_bank_deposit_received(db, data)

    assert (await review_of(db))["outcome"] == "deny"


async def test_a_restricted_account_still_receives_its_deposits(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    data = await received(db, sim, bank, custody, maria)
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "under review")

    await payments.apply_bank_deposit_received(db, data)

    assert (await available(db, maria), await suspense(db)) == (250_00, 0)


@pytest.mark.parametrize("arrives", ["bank", "chain", "statement"])
async def test_a_deposit_waits_for_its_owners_money_out_lock(
    db: Database,
    impatient_db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    arrives: str,
) -> None:
    """Closing an account holds this lock while it reads the balances, so a deposit that
    is being credited cannot slip in between that read and the account being closed."""
    if arrives == "bank":
        data = await received(db, sim, bank, custody, maria)
    elif arrives == "chain":
        await detected(db, sim, bank, custody, maria)
        data = await confirmed(sim)
    else:
        instruction = await instruction_for(db, maria, "USD", bank, custody)

    async with db.transaction() as holder:
        await advisory_xact_lock(holder, [lock_key("money_out", maria.id)])
        with pytest.raises(DBAPIError) as failure:
            if arrives == "bank":
                await payments.apply_bank_deposit_received(impatient_db, data)
            elif arrives == "chain":
                await payments.apply_chain_deposit_confirmed(impatient_db, data)
            else:
                await payments.apply_statement_deposit(
                    impatient_db,
                    provider="simbank",
                    provider_ref="dep_statement_1",
                    account_ref=instruction.provider_ref,
                    asset="USD",
                    amount=250_00,
                )

    assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
    assert await count(db, "journal_entries") == 0

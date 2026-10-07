"""Screening: the deny list, the reviews it opens, and what it does to money on its way
in and out."""

from typing import Any

import pytest

from corridor import payments, risk
from corridor.identity import User
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import SimBank, SimCustody
from corridor.risk import PartyDenied, ReviewAlreadyResolved, ReviewNotFound
from tests.payments.support import (
    acting_as,
    add_beneficiary,
    available,
    count,
    deposit,
    entries,
    held,
    instruction_for,
    omnibus,
    rows,
    settlement,
    suspense,
    withdraw,
)
from tests.support.providers import EXTERNAL_ADDRESS, Sim, address_for

OTHER_ADDRESS = address_for("another".ljust(32, "b"))


async def listed(db: Database, kind: str, value: str, outcome: str = "deny") -> None:
    async with db.transaction() as session:
        await risk.add_to_denylist(session, kind=kind, value=value, outcome=outcome)  # type: ignore[arg-type]


async def screen(db: Database, kind: str, value: str) -> str:
    async with db.transaction() as session:
        return await risk.screen_party(session, kind=kind, value=value)  # type: ignore[arg-type]


async def reviews(db: Database) -> list[dict[str, Any]]:
    return await rows(db, "SELECT * FROM risk_reviews ORDER BY id")


# --- the deny list ---------------------------------------------------------------------------


async def test_a_party_that_is_not_listed_is_clear(db: Database) -> None:
    await listed(db, "name", "Someone Else")

    assert await screen(db, "name", "Maria Silva") == "clear"


@pytest.mark.parametrize("outcome", ["deny", "review"])
async def test_a_listed_party_gets_the_outcome_it_was_listed_with(
    db: Database, outcome: str
) -> None:
    await listed(db, "name", "Maria Silva", outcome)

    assert await screen(db, "name", "Maria Silva") == outcome


@pytest.mark.parametrize(
    ("kind", "as_listed", "as_met"),
    [
        ("name", "Maria Silva", "maria silva"),
        ("name", "Maria Silva", "  MARIA \t  SILVA \n"),
        ("name", "Maria Silva", "\uff2d\uff41\uff52\uff49\uff41 \uff33\uff49\uff4c\uff56\uff41"),
        ("name", "José Straße", "JOSÉ STRASSE"),
        ("address", EXTERNAL_ADDRESS, f" {EXTERNAL_ADDRESS.upper()} "),
        ("account", "0001-2345 6789", "000123456789"),
        ("account", "gb82 west 1234", "GB82WEST1234"),
    ],
)
async def test_a_listed_party_is_found_however_it_is_written(
    db: Database, kind: str, as_listed: str, as_met: str
) -> None:
    await listed(db, kind, as_listed)

    assert await screen(db, kind, as_met) == "deny"


async def test_a_name_is_not_found_by_part_of_it_or_with_its_words_run_together(
    db: Database,
) -> None:
    await listed(db, "name", "Maria Silva")

    assert await screen(db, "name", "Maria") == "clear"
    assert await screen(db, "name", "MariaSilva") == "clear"


async def test_a_party_listed_as_one_kind_is_not_listed_as_another(db: Database) -> None:
    await listed(db, "name", EXTERNAL_ADDRESS)

    assert await screen(db, "address", EXTERNAL_ADDRESS) == "clear"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
async def test_a_party_with_no_value_is_clear_and_cannot_be_listed(
    db: Database, value: str
) -> None:
    assert await screen(db, "name", value) == "clear"

    with pytest.raises(ValueError, match="has a value"):
        await listed(db, "name", value)


async def test_listing_a_party_again_changes_what_the_list_says_of_it(db: Database) -> None:
    await listed(db, "name", "Maria Silva", "review")
    await listed(db, "name", "MARIA SILVA", "deny")

    assert await screen(db, "name", "Maria Silva") == "deny"
    assert await count(db, "risk_denylist") == 1


async def test_a_kind_or_an_outcome_screening_does_not_know_is_a_bug(db: Database) -> None:
    with pytest.raises(ValueError, match="does not know a party of kind 'email'"):
        await screen(db, "email", "maria@example.com")
    with pytest.raises(ValueError, match="does not know a party of kind 'email'"):
        await listed(db, "email", "maria@example.com")
    with pytest.raises(ValueError, match="denied or reviewed"):
        await listed(db, "name", "Maria Silva", "clear")


# --- reviews ---------------------------------------------------------------------------------


async def cleared(db: Database, subject_id: Any, subject_type: str = "withdrawal") -> bool:
    async with db.transaction() as session:
        return await risk.is_cleared(session, subject_type, subject_id)  # type: ignore[arg-type]


async def opened(db: Database, subject_id: Any, **more: Any) -> risk.Review:
    async with db.transaction() as session:
        return await risk.open_review(
            session, subject_type="withdrawal", subject_id=subject_id, outcome="review", **more
        )


async def resolved(db: Database, subject_id: Any, *, cleared: bool) -> risk.Review:
    async with db.transaction() as session:
        return await risk.resolve_review(
            session, subject_type="withdrawal", subject_id=subject_id, cleared=cleared
        )


async def test_a_movement_that_was_never_put_under_review_is_cleared(db: Database) -> None:
    assert await cleared(db, new_id())


async def test_a_movement_under_an_open_review_is_not_cleared(
    db: Database, clock: ManualClock
) -> None:
    subject, user = new_id(), new_id()

    review = await opened(db, subject, user_id=user)

    assert (review.subject_type, review.subject_id, review.user_id) == ("withdrawal", subject, user)
    assert (review.outcome, review.status) == ("review", "open")
    assert (review.created_at, review.resolved_at) == (clock.now(), None)
    assert not await cleared(db, subject)
    # A review of a withdrawal says nothing about a deposit that happens to share its id.
    assert await cleared(db, subject, "deposit")


async def test_a_review_an_operator_cleared_lets_the_movement_go_ahead(
    db: Database, clock: ManualClock
) -> None:
    subject = new_id()
    await opened(db, subject)
    clock.advance(minutes=5)

    review = await resolved(db, subject, cleared=True)

    assert (review.status, review.resolved_at) == ("cleared", clock.now())
    assert await cleared(db, subject)


async def test_a_review_an_operator_rejected_never_lets_the_movement_go_ahead(
    db: Database,
) -> None:
    subject = new_id()
    await opened(db, subject)

    review = await resolved(db, subject, cleared=False)

    assert review.status == "rejected"
    assert not await cleared(db, subject)


@pytest.mark.parametrize("first", [True, False])
async def test_a_review_is_resolved_once(db: Database, first: bool) -> None:
    subject = new_id()
    await opened(db, subject)
    await resolved(db, subject, cleared=first)

    with pytest.raises(ReviewAlreadyResolved) as refusal:
        await resolved(db, subject, cleared=not first)

    assert (refusal.value.status, refusal.value.code) == (409, "review_already_resolved")
    assert await cleared(db, subject) is first


async def test_a_review_that_does_not_exist_cannot_be_resolved(db: Database) -> None:
    with pytest.raises(ReviewNotFound) as refusal:
        await resolved(db, new_id(), cleared=True)

    assert (refusal.value.status, refusal.value.code) == (404, "review_not_found")


async def test_opening_a_review_again_does_not_reopen_one_that_was_resolved(
    db: Database,
) -> None:
    subject = new_id()
    first = await opened(db, subject)
    await resolved(db, subject, cleared=False)

    again = await opened(db, subject)

    assert (again.id, again.status) == (first.id, "rejected")
    assert len(await reviews(db)) == 1


# --- withdrawals -----------------------------------------------------------------------------


async def nothing_is_held(db: Database, user: User, asset: str = "USD") -> None:
    assert await held(db, user, asset) == 0
    assert await count(db, "withdrawals") == 0
    assert await count(db, "outbox_events") == 0
    assert await count(db, "risk_usage") == 0
    assert await reviews(db) == []


async def test_a_withdrawal_to_a_denied_address_is_refused_before_anything_is_held(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 50_000_000, "USDC")
    await listed(db, "address", EXTERNAL_ADDRESS)

    with pytest.raises(PartyDenied) as refusal:
        await withdraw(db, settings, maria, 1_000_000, asset="USDC", to_address=EXTERNAL_ADDRESS)

    assert isinstance(refusal.value, risk.Denied)
    assert (refusal.value.status, refusal.value.code) == (403, "party_not_allowed")
    # It does not say that there is a list, or that the address is on it.
    assert refusal.value.detail == "Money cannot be sent to this destination."
    assert await available(db, maria, "USDC") == 50_000_000
    await nothing_is_held(db, maria, "USDC")


async def test_a_withdrawal_to_a_denied_account_holder_is_refused_before_anything_is_held(
    db: Database,
    settings: Settings,
    bank: SimBank,
    maria: User,
) -> None:
    await deposit(db, maria, 100_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    await listed(db, "name", beneficiary.holder_name)

    with pytest.raises(PartyDenied):
        await withdraw(db, settings, maria, 10_00, beneficiary=beneficiary)

    assert await available(db, maria) == 100_00
    await nothing_is_held(db, maria)


async def test_a_withdrawal_to_an_address_that_is_not_listed_is_held_with_no_review(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 50_000_000, "USDC")
    await listed(db, "address", OTHER_ADDRESS)

    withdrawal = await withdraw(
        db, settings, maria, 1_000_000, asset="USDC", to_address=EXTERNAL_ADDRESS
    )

    assert withdrawal.status == "held"
    assert await reviews(db) == []
    assert await cleared(db, withdrawal.id)


async def test_a_withdrawal_to_an_address_listed_for_review_is_held_for_an_operator(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 50_000_000, "USDC")
    await listed(db, "address", EXTERNAL_ADDRESS, "review")

    withdrawal = await withdraw(
        db, settings, maria, 1_000_000, asset="USDC", to_address=EXTERNAL_ADDRESS
    )

    # The funds are reserved, as for any withdrawal, and nothing may send it yet.
    assert withdrawal.status == "held"
    assert await held(db, maria, "USDC") == 1_000_000
    (review,) = await reviews(db)
    assert (review["subject_type"], review["subject_id"]) == ("withdrawal", withdrawal.id)
    assert (review["user_id"], review["outcome"], review["status"]) == (maria.id, "review", "open")
    assert not await cleared(db, withdrawal.id)


async def test_a_withdrawal_to_an_account_holder_listed_for_review_is_held_for_an_operator(
    db: Database,
    settings: Settings,
    bank: SimBank,
    maria: User,
) -> None:
    await deposit(db, maria, 100_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    await listed(db, "name", beneficiary.holder_name, "review")

    withdrawal = await withdraw(db, settings, maria, 10_00, beneficiary=beneficiary)

    assert withdrawal.status == "held"
    assert not await cleared(db, withdrawal.id)


async def test_a_withdrawal_under_review_that_is_rolled_back_leaves_no_review(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 50_000_000, "USDC")
    await listed(db, "address", EXTERNAL_ADDRESS, "review")

    class Abandoned(Exception):
        pass

    with pytest.raises(Abandoned):
        async with db.transaction() as session:
            await payments.request_withdrawal(
                session,
                acting_as(maria),
                withdrawal_id=new_id(),
                asset="USDC",
                amount=1_000_000,
                to_address=EXTERNAL_ADDRESS,
                settings=settings,
            )
            raise Abandoned

    await nothing_is_held(db, maria, "USDC")


# --- deposits --------------------------------------------------------------------------------


async def bank_deposit(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    user: User,
) -> dict[str, Any]:
    instruction = await instruction_for(db, user, "USD", bank, custody)
    return await sim.bank_deposit(instruction.provider_ref, "250.00")


async def confirmed_chain_deposit(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    user: User,
) -> dict[str, Any]:
    instruction = await instruction_for(db, user, "USDC", bank, custody)
    await sim.chain_deposit(instruction.details["address"], "25")
    await sim.mine(3)
    return await sim.last_event("deposit.confirmed")


@pytest.mark.parametrize("outcome", ["deny", "review"])
async def test_a_bank_deposit_from_a_listed_sender_goes_to_suspense_for_an_operator(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    outcome: str,
) -> None:
    data = await bank_deposit(db, sim, bank, custody, maria)
    await listed(db, "name", data["sender_name"], outcome)

    await payments.apply_bank_deposit_received(db, data)

    # The money did arrive, and it is nobody's until an operator has looked.
    assert await available(db, maria) == 0
    assert await suspense(db) == 250_00
    assert await settlement(db) == 250_00
    (row,) = await rows(db, "SELECT * FROM deposits")
    assert (row["status"], row["amount"]) == ("suspense", 250_00)
    (entry,) = await entries(db, "deposit", f"simbank:{data['deposit_id']}")
    assert entry["kind"] == "deposit_suspense"
    # The user is told nothing: no event that a deposit completed.
    assert await count(db, "outbox_events") == 0
    # The review remembers whose it would have been, and why it is there.
    (review,) = await reviews(db)
    assert (review["subject_type"], review["subject_id"]) == ("deposit", row["id"])
    assert (review["user_id"], review["outcome"], review["status"]) == (maria.id, outcome, "open")


async def test_a_bank_deposit_from_a_listed_sender_delivered_twice_is_suspended_once(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    data = await bank_deposit(db, sim, bank, custody, maria)
    await listed(db, "name", data["sender_name"])

    await payments.apply_bank_deposit_received(db, data)
    await payments.apply_bank_deposit_received(db, data)

    assert await suspense(db) == 250_00
    assert len(await reviews(db)) == 1


async def test_a_bank_deposit_from_a_sender_who_is_not_listed_is_credited(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    data = await bank_deposit(db, sim, bank, custody, maria)
    await listed(db, "name", "Someone Else")

    await payments.apply_bank_deposit_received(db, data)

    assert await available(db, maria) == 250_00
    assert await suspense(db) == 0
    assert await reviews(db) == []


@pytest.mark.parametrize("outcome", ["deny", "review"])
async def test_a_chain_deposit_from_a_listed_address_goes_to_suspense_when_it_is_confirmed(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    outcome: str,
) -> None:
    data = await confirmed_chain_deposit(db, sim, bank, custody, maria)
    await listed(db, "address", data["from_address"], outcome)

    await payments.apply_chain_deposit_confirmed(db, data)

    assert await available(db, maria, "USDC") == 0
    assert await suspense(db, "USDC") == 25_000_000
    assert await omnibus(db) == 25_000_000
    (row,) = await rows(db, "SELECT * FROM deposits")
    assert row["status"] == "suspense"
    assert await count(db, "outbox_events") == 0
    (review,) = await reviews(db)
    assert (review["subject_type"], review["subject_id"]) == ("deposit", row["id"])
    assert (review["user_id"], review["outcome"], review["status"]) == (maria.id, outcome, "open")


async def test_a_chain_deposit_from_an_address_that_is_not_listed_is_credited(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    data = await confirmed_chain_deposit(db, sim, bank, custody, maria)
    await listed(db, "address", OTHER_ADDRESS)

    await payments.apply_chain_deposit_confirmed(db, data)

    assert await available(db, maria, "USDC") == 25_000_000
    assert await reviews(db) == []

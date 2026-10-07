"""Taking a deposit out of suspense: to a user, or back through the provider it arrived at.

Each way out names the deposit and looks at it under its lock, so whichever of an approved
adjustment, a cleared review and the bank's own return comes first, the money leaves
suspense once, and whatever comes after finds it gone.
"""

import asyncio
import secrets
from typing import Any

import pytest
from sqlalchemy import text

from corridor import audit, identity, ledger, ops, payments, risk, wallets
from corridor.identity import Principal, User
from corridor.ledger import AccountKind, Direction
from corridor.ops import Adjustment, Leg
from corridor.payments import DepositNotFound, DepositNotInSuspense, DepositOwnerClosed
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import SimBank, SimCustody
from corridor.wallets import WalletNotFound
from tests.ops.support import audited, deposit_row, recalled, suspended
from tests.ops.test_reviews import clear, deposit_under_review
from tests.payments.support import (
    add_person,
    available,
    balance_of,
    entries,
    omnibus,
    rows,
    send,
    settlement,
    suspense,
    user_status,
)
from tests.support.providers import Sim


async def ask_release(
    db: Database, admin: Principal, deposit: dict[str, Any], user: User, reason: str = "it is hers"
) -> Adjustment:
    async with db.transaction() as session:
        return await ops.request_suspense_release(
            session,
            admin,
            adjustment_id=new_id(),
            reason=reason,
            deposit_id=deposit["id"],
            user_id=user.id,
        )


async def ask_return(db: Database, admin: Principal, deposit: dict[str, Any]) -> Adjustment:
    async with db.transaction() as session:
        return await ops.request_suspense_return(
            session, admin, adjustment_id=new_id(), reason="unclaimed", deposit_id=deposit["id"]
        )


async def approve(db: Database, admin: Principal, adjustment: Adjustment) -> Adjustment:
    async with db.transaction() as session:
        return await ops.approve_adjustment(session, admin, adjustment.id)


async def statuses(db: Database) -> list[str]:
    found = await rows(db, "SELECT status FROM ops_adjustments ORDER BY id")
    return [str(row["status"]) for row in found]


async def deposit_entries(db: Database, deposit: dict[str, Any]) -> list[dict[str, Any]]:
    return await entries(db, "deposit", f"{deposit['provider']}:{deposit['provider_ref']}")


async def chain_deposit_in_suspense(db: Database) -> dict[str, Any]:
    """25 USDC confirmed at an address the custodian never issued: in suspense, nobody's."""
    reference = f"cdep_{secrets.token_hex(6)}"
    await payments.apply_chain_deposit_confirmed(
        db,
        {
            "deposit_id": reference,
            "address_id": f"addr_{secrets.token_hex(6)}",
            "address": "sim1nobodysaddress",
            "asset": "USDC",
            "amount": "25.000000",
            "tx_hash": f"0x{secrets.token_hex(16)}",
            "from_address": "sim1somebodyelse",
            "confirmations": 3,
        },
    )
    return await deposit_row(db, reference)


# --- releasing and returning -------------------------------------------------------------------


async def test_a_deposit_in_suspense_is_released_to_a_user_by_a_second_admins_approval(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    deposit = await suspended(db)
    pending = await ask_release(db, ana, deposit, maria)
    assert (pending.kind, pending.deposit_id, pending.user_id) == (
        "suspense_release",
        deposit["id"],
        maria.id,
    )
    assert [(leg.direction, leg.amount) for leg in pending.legs] == [
        (Direction.DEBIT, 75_00),
        (Direction.CREDIT, 75_00),
    ]
    assert (await suspense(db), await available(db, maria)) == (75_00, 0)

    with pytest.raises(ops.SelfApproval):
        await approve(db, ana, pending)
    approved = await approve(db, bruno, pending)

    arrived, released = await deposit_entries(db, deposit)
    assert (arrived["kind"], released["kind"]) == ("deposit_suspense", "deposit_release")
    assert released["id"] == approved.entry_id
    assert released["postings"] == [("suspense", "D", 75_00), ("user_available", "C", 75_00)]
    assert (await suspense(db), await available(db, maria)) == (0, 75_00)
    after = await deposit_row(db, deposit["provider_ref"])
    assert (after["status"], after["user_id"]) == ("completed", maria.id)
    (event,) = await audited(db, "adjustment.approved")
    assert event["details"]["deposit_id"] == str(deposit["id"])


async def test_a_deposit_in_suspense_is_booked_as_returned_through_the_bank_it_arrived_at(
    db: Database, ana: Principal, bruno: Principal
) -> None:
    deposit = await suspended(db)
    pending = await ask_return(db, ana, deposit)
    assert (pending.kind, pending.deposit_id, pending.user_id) == (
        "suspense_return",
        deposit["id"],
        None,
    )

    approved = await approve(db, bruno, pending)

    _, returned = await deposit_entries(db, deposit)
    assert (returned["id"], returned["kind"]) == (approved.entry_id, "deposit_return")
    assert returned["postings"] == [("suspense", "D", 75_00), ("bank_settlement", "C", 75_00)]
    assert (await suspense(db), await settlement(db)) == (0, 0)
    after = await deposit_row(db, deposit["provider_ref"])
    assert (after["status"], after["user_id"]) == ("returned", None)


async def test_a_chain_deposit_in_suspense_is_returned_through_the_custodian(
    db: Database, ana: Principal, bruno: Principal
) -> None:
    deposit = await chain_deposit_in_suspense(db)
    assert (deposit["status"], await suspense(db, "USDC")) == ("suspense", 25_000_000)

    await approve(db, bruno, await ask_return(db, ana, deposit))

    _, returned = await deposit_entries(db, deposit)
    assert returned["postings"] == [
        ("suspense", "D", 25_000_000),
        ("custody_omnibus", "C", 25_000_000),
    ]
    assert (await suspense(db, "USDC"), await omnibus(db)) == (0, 0)


# --- once, whatever comes first ----------------------------------------------------------------


@pytest.mark.parametrize("second", ["release", "return"])
async def test_of_two_adjustments_for_one_deposit_the_second_is_refused_and_stays_pending(
    db: Database, ana: Principal, bruno: Principal, maria: User, second: str
) -> None:
    deposit = await suspended(db)
    first = await ask_release(db, ana, deposit, maria)
    # Asked for while the deposit was still in suspense: only the approval can refuse it.
    other = (
        await ask_release(db, ana, deposit, maria, "again")
        if second == "release"
        else await ask_return(db, ana, deposit)
    )

    await approve(db, bruno, first)
    with pytest.raises(DepositNotInSuspense) as refusal:
        await approve(db, bruno, other)

    assert (refusal.value.status, refusal.value.code) == (409, "deposit_not_in_suspense")
    assert (await suspense(db), await available(db, maria)) == (0, 75_00)
    assert await statuses(db) == ["approved", "pending"]
    assert len(await deposit_entries(db, deposit)) == 2


async def test_two_adjustments_for_one_deposit_approved_at_once_release_it_once(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    deposit = await suspended(db)
    pending = [await ask_release(db, ana, deposit, maria, f"request {n}") for n in range(4)]

    outcomes = await asyncio.gather(
        *(approve(db, bruno, adjustment) for adjustment in pending), return_exceptions=True
    )

    approved = [outcome for outcome in outcomes if isinstance(outcome, Adjustment)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, DepositNotInSuspense)]
    assert (len(approved), len(refused)) == (1, 3)
    assert (await suspense(db), await available(db, maria)) == (0, 75_00)
    assert sorted(await statuses(db)) == ["approved", "pending", "pending", "pending"]


async def test_a_deposit_released_by_an_adjustment_cannot_be_released_again_by_clearing_its_review(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    bruno: Principal,
    maria: User,
) -> None:
    deposit, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    await approve(db, bruno, await ask_release(db, ana, deposit, maria))
    assert (await available(db, maria), await suspense(db)) == (250_00, 0)

    with pytest.raises(DepositNotInSuspense):
        await clear(db, ana, review_id)

    assert (await available(db, maria), await suspense(db)) == (250_00, 0)
    (review,) = await rows(db, "SELECT status FROM risk_reviews")
    assert review["status"] == "open"


async def test_a_deposit_released_by_clearing_its_review_cannot_be_released_again_by_an_adjustment(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    bruno: Principal,
    maria: User,
) -> None:
    deposit, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    pending = await ask_release(db, ana, deposit, maria)
    await clear(db, ana, review_id)

    with pytest.raises(DepositNotInSuspense):
        await approve(db, bruno, pending)
    with pytest.raises(DepositNotInSuspense):
        # And a new one cannot even be asked for.
        await ask_return(db, ana, deposit)

    assert (await available(db, maria), await suspense(db)) == (250_00, 0)
    assert await statuses(db) == ["pending"]


async def test_the_bank_taking_back_a_released_deposit_takes_it_from_the_user_it_went_to(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    deposit = await suspended(db)
    await approve(db, bruno, await ask_release(db, ana, deposit, maria))

    await payments.apply_bank_deposit_returned(db, recalled(deposit))

    assert (await available(db, maria), await suspense(db), await settlement(db)) == (0, 0, 0)
    *_, reversal = await deposit_entries(db, deposit)
    assert reversal["postings"] == [("user_available", "D", 75_00), ("bank_settlement", "C", 75_00)]
    assert (await deposit_row(db, deposit["provider_ref"]))["status"] == "returned"
    assert await user_status(db, maria) == "active"


async def test_the_bank_taking_back_a_released_deposit_that_was_spent_books_what_is_owed(
    db: Database, settings: Settings, ana: Principal, bruno: Principal, maria: User
) -> None:
    async with db.transaction() as session:
        joao = await add_person(session, "joao")
    deposit = await suspended(db)
    await approve(db, bruno, await ask_release(db, ana, deposit, maria))
    await send(db, settings, maria, joao, 50_00)

    await payments.apply_bank_deposit_returned(db, recalled(deposit))

    assert (await available(db, maria), await suspense(db), await settlement(db)) == (0, 0, 0)
    assert await balance_of(db, AccountKind.USER_RECEIVABLE, owner=maria) == 50_00
    assert await user_status(db, maria) == "restricted"
    assert await available(db, joao) == 50_00


async def test_the_bank_taking_back_a_deposit_an_adjustment_returned_takes_nothing_more(
    db: Database, ana: Principal, bruno: Principal
) -> None:
    deposit = await suspended(db)
    await approve(db, bruno, await ask_return(db, ana, deposit))

    await payments.apply_bank_deposit_returned(db, recalled(deposit))

    assert (await suspense(db), await settlement(db)) == (0, 0)
    assert len(await deposit_entries(db, deposit)) == 2


async def test_a_deposit_the_bank_took_back_can_no_longer_be_released_or_returned(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    deposit = await suspended(db)
    release, back = await ask_release(db, ana, deposit, maria), await ask_return(db, ana, deposit)
    await payments.apply_bank_deposit_returned(db, recalled(deposit))

    for pending in (release, back):
        with pytest.raises(DepositNotInSuspense):
            await approve(db, bruno, pending)

    assert (await suspense(db), await settlement(db), await available(db, maria)) == (0, 0, 0)
    assert await statuses(db) == ["pending", "pending"]


# --- suspense is closed to adjustments written by hand -------------------------------------------


async def suspense_debit(db: Database, maria: User, amount: int = 75_00) -> list[Leg]:
    """Postings that would move money from suspense to a user with no deposit named."""
    async with db.transaction() as session:
        held = await ledger.open_account(session, AccountKind.SUSPENSE, "USD")
        wallet = await wallets.get_wallet(session, maria.id, "USD")
    return [
        Leg(held.id, "USD", Direction.DEBIT, amount),
        Leg(wallet.available_account_id, "USD", Direction.CREDIT, amount),
    ]


async def test_an_adjustment_written_by_hand_cannot_take_money_out_of_suspense(
    db: Database, ana: Principal, maria: User
) -> None:
    await suspended(db)
    legs = await suspense_debit(db, maria)

    with pytest.raises(ops.InvalidAdjustment) as refusal:
        async with db.transaction() as session:
            await ops.request_adjustment(
                session, ana, adjustment_id=new_id(), reason="by hand", legs=legs
            )

    assert refusal.value.extra == {"field": "legs"}
    assert await statuses(db) == []


async def test_an_adjustment_written_by_hand_may_put_money_into_suspense(
    db: Database, ana: Principal, bruno: Principal
) -> None:
    await suspended(db)
    async with db.transaction() as session:
        held = await ledger.open_account(session, AccountKind.SUSPENSE, "USD")
        at_the_bank = await ledger.open_account(
            session, AccountKind.BANK_SETTLEMENT, "USD", provider="simbank"
        )
        pending = await ops.request_adjustment(
            session,
            ana,
            adjustment_id=new_id(),
            reason="received and not reported",
            legs=[
                Leg(at_the_bank.id, "USD", Direction.DEBIT, 10_00),
                Leg(held.id, "USD", Direction.CREDIT, 10_00),
            ],
        )

    await approve(db, bruno, pending)

    assert await suspense(db) == 85_00


async def test_a_pending_adjustment_from_before_that_debits_suspense_cannot_be_approved(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    await suspended(db)
    legs = await suspense_debit(db, maria)
    adjustment_id = new_id()
    async with db.transaction() as session:
        # As one asked for before suspense was closed to adjustments written by hand.
        await session.execute(
            text(
                "INSERT INTO ops_adjustments (id, requested_by, status, kind, reason, legs,"
                " created_at) VALUES (:id, :by, 'pending', 'manual', 'from before',"
                " CAST(:legs AS jsonb), :now)"
            ),
            {
                "id": adjustment_id,
                "by": ana.user_id,
                "now": utcnow(),
                "legs": "["
                + ",".join(
                    f'{{"account_id": "{leg.account_id}", "asset": "USD",'
                    f' "direction": "{leg.direction.value}", "amount": "{leg.amount}"}}'
                    for leg in legs
                )
                + "]",
            },
        )

    with pytest.raises(ops.InvalidAdjustment):
        async with db.transaction() as session:
            await ops.approve_adjustment(session, bruno, adjustment_id)

    assert (await suspense(db), await available(db, maria)) == (75_00, 0)
    assert await statuses(db) == ["pending"]


# --- what can be asked for -----------------------------------------------------------------------


async def test_only_a_deposit_that_is_in_suspense_can_be_asked_for(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    deposit = await suspended(db)
    await approve(db, bruno, await ask_return(db, ana, deposit))

    with pytest.raises(DepositNotInSuspense):
        await ask_release(db, ana, deposit, maria)
    with pytest.raises(DepositNotInSuspense):
        await ask_return(db, ana, deposit)
    with pytest.raises(DepositNotFound):
        await ask_release(db, ana, {"id": new_id()}, maria)
    with pytest.raises(DepositNotFound):
        await ask_return(db, ana, {"id": new_id()})

    assert await statuses(db) == ["approved"]


async def test_a_release_names_a_user_who_exists_and_has_a_wallet(
    db: Database, ana: Principal
) -> None:
    deposit = await suspended(db)
    async with db.transaction() as session:
        without_wallets = await identity.register(
            session,
            email="nowallet@example.com",
            handle="nowallet",
            display_name="No Wallet",
            password_hash="x",
        )

    with pytest.raises(identity.UserNotFound):
        async with db.transaction() as session:
            await ops.request_suspense_release(
                session,
                ana,
                adjustment_id=new_id(),
                reason="nobody",
                deposit_id=deposit["id"],
                user_id=new_id(),
            )
    with pytest.raises(WalletNotFound):
        await ask_release(db, ana, deposit, without_wallets)

    assert await statuses(db) == []


async def test_releasing_or_returning_a_deposit_that_does_not_exist_is_a_bug_in_the_caller(
    db: Database, ana: Principal, maria: User
) -> None:
    actor = audit.Actor.admin(ana.user_id)

    async with db.transaction() as session:
        with pytest.raises(LookupError):
            await payments.release_from_suspense(session, new_id(), maria.id, actor=actor)
        with pytest.raises(LookupError):
            await payments.return_from_suspense(session, new_id(), actor=actor)


# --- closed and restricted accounts --------------------------------------------------------------


async def close(db: Database, user: User) -> None:
    async with db.transaction() as session:
        await identity.close_user(session, user.id)


async def restrict(db: Database, user: User) -> None:
    async with db.transaction() as session:
        await risk.restrict_user(session, user.id, "under investigation")


async def test_a_release_to_a_closed_account_cannot_be_asked_for(
    db: Database, ana: Principal, maria: User
) -> None:
    deposit = await suspended(db)
    await close(db, maria)

    with pytest.raises(DepositOwnerClosed) as refusal:
        await ask_release(db, ana, deposit, maria)

    assert (refusal.value.status, refusal.value.code) == (409, "deposit_owner_closed")
    assert await statuses(db) == []


async def test_a_release_to_an_account_closed_since_it_was_asked_for_is_refused_at_approval(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    deposit = await suspended(db)
    pending = await ask_release(db, ana, deposit, maria)
    await close(db, maria)

    with pytest.raises(DepositOwnerClosed):
        await approve(db, bruno, pending)

    assert (await suspense(db), await available(db, maria)) == (75_00, 0)
    assert (await deposit_row(db, deposit["provider_ref"]))["status"] == "suspense"
    assert await statuses(db) == ["pending"]


async def test_a_deposit_is_released_to_a_restricted_account(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    deposit = await suspended(db)
    await restrict(db, maria)

    await approve(db, bruno, await ask_release(db, ana, deposit, maria))

    assert (await suspense(db), await available(db, maria)) == (0, 75_00)


async def test_clearing_the_review_of_a_deposit_for_a_closed_account_is_refused(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, ana: Principal, maria: User
) -> None:
    _, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    await close(db, maria)

    with pytest.raises(DepositOwnerClosed):
        await clear(db, ana, review_id)

    assert (await available(db, maria), await suspense(db)) == (0, 250_00)
    (review,) = await rows(db, "SELECT status FROM risk_reviews")
    assert review["status"] == "open"


async def test_clearing_the_review_of_a_deposit_for_a_restricted_account_releases_it(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, ana: Principal, maria: User
) -> None:
    _, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    await restrict(db, maria)

    await clear(db, ana, review_id)

    assert (await available(db, maria), await suspense(db)) == (250_00, 0)

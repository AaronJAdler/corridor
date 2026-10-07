"""Adjustments: a balanced entry asked for by one admin and posted by another's approval.

The requester cannot approve their own, and two approvals at once post one entry. Taking a
deposit out of suspense is in ``test_suspense.py``.
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor import ledger, ops, risk, wallets
from corridor.identity import Principal, User
from corridor.ledger import AccountKind, Direction
from corridor.ops import Adjustment, Leg
from corridor.platform.clock import utcnow
from corridor.platform.db import (
    LOCK_NOT_AVAILABLE,
    Database,
    advisory_xact_lock,
    lock_key,
    sqlstate_of,
)
from corridor.platform.errors import PermissionDenied
from corridor.platform.ids import new_id
from tests.identity.support import add_user
from tests.ops.support import audited
from tests.payments.support import acting_as, available, balance_of, entries, rows
from tests.support.ledger import fund

BANK = "simbank"


async def accounts(db: Database, user: User, asset: str = "USD") -> tuple[uuid.UUID, uuid.UUID]:
    """The user's available account and the bank settlement account of an asset."""
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user.id, asset)
        settlement = await ledger.open_account(
            session, AccountKind.BANK_SETTLEMENT, asset, provider=BANK
        )
    return wallet.available_account_id, settlement.id


def goodwill(user_account: uuid.UUID, settlement: uuid.UUID, amount: int = 25_00) -> list[Leg]:
    """A credit to a user against the bank: the simplest adjustment there is."""
    return [
        Leg(settlement, "USD", Direction.DEBIT, amount),
        Leg(user_account, "USD", Direction.CREDIT, amount),
    ]


async def request(
    db: Database, principal: Principal, legs: list[Leg], reason: str = "goodwill credit"
) -> Adjustment:
    async with db.transaction() as session:
        return await ops.request_adjustment(
            session, principal, adjustment_id=new_id(), reason=reason, legs=legs
        )


async def approve(db: Database, principal: Principal, adjustment: Adjustment) -> Adjustment:
    async with db.transaction() as session:
        return await ops.approve_adjustment(session, principal, adjustment.id)


async def stored(db: Database) -> list[dict[str, Any]]:
    return await rows(db, "SELECT * FROM ops_adjustments ORDER BY id")


# --- asking ----------------------------------------------------------------------------------


async def test_a_requested_adjustment_is_pending_and_moves_nothing(
    db: Database, ana: Principal, maria: User
) -> None:
    user_account, settlement = await accounts(db, maria)

    adjustment = await request(db, ana, goodwill(user_account, settlement), "  goodwill credit ")

    assert (adjustment.status, adjustment.requested_by) == ("pending", ana.user_id)
    assert (adjustment.approved_by, adjustment.entry_id, adjustment.decided_at) == (
        None,
        None,
        None,
    )
    assert adjustment.reason == "goodwill credit"
    assert list(adjustment.legs) == goodwill(user_account, settlement)
    assert await available(db, maria) == 0
    assert await entries(db, "adjustment", str(adjustment.id)) == []
    (event,) = await audited(db, "adjustment.requested")
    assert (event["actor_type"], event["actor_id"], event["resource_id"]) == (
        "admin",
        str(ana.user_id),
        str(adjustment.id),
    )


async def test_only_an_admin_can_ask_for_approve_reject_or_read_an_adjustment(
    db: Database, ana: Principal, maria: User
) -> None:
    user_account, settlement = await accounts(db, maria)
    pending = await request(db, ana, goodwill(user_account, settlement))
    user = acting_as(maria)

    async with db.transaction() as session:
        with pytest.raises(PermissionDenied):
            await ops.request_adjustment(
                session,
                user,
                adjustment_id=new_id(),
                reason="for me",
                legs=goodwill(user_account, settlement),
            )
        with pytest.raises(PermissionDenied):
            await ops.request_suspense_release(
                session,
                user,
                adjustment_id=new_id(),
                reason="for me",
                deposit_id=new_id(),
                user_id=maria.id,
            )
        with pytest.raises(PermissionDenied):
            await ops.request_suspense_return(
                session, user, adjustment_id=new_id(), reason="for me", deposit_id=new_id()
            )
        with pytest.raises(PermissionDenied):
            await ops.approve_adjustment(session, user, pending.id)
        with pytest.raises(PermissionDenied):
            await ops.reject_adjustment(session, user, pending.id)
        with pytest.raises(PermissionDenied):
            await ops.get_adjustment(session, user, pending.id)
        with pytest.raises(PermissionDenied):
            await ops.list_adjustments(session, user)

    assert [row["status"] for row in await stored(db)] == ["pending"]
    assert await available(db, maria) == 0


def unbalanced(user_account: uuid.UUID, settlement: uuid.UUID) -> list[Leg]:
    return [
        Leg(settlement, "USD", Direction.DEBIT, 25_00),
        Leg(user_account, "USD", Direction.CREDIT, 24_99),
    ]


def one_sided(user_account: uuid.UUID, settlement: uuid.UUID) -> list[Leg]:
    return [Leg(user_account, "USD", Direction.CREDIT, 25_00)]


def repeated(user_account: uuid.UUID, settlement: uuid.UUID) -> list[Leg]:
    return [
        Leg(settlement, "USD", Direction.DEBIT, 25_00),
        Leg(user_account, "USD", Direction.CREDIT, 10_00),
        Leg(user_account, "USD", Direction.CREDIT, 15_00),
    ]


def to_nowhere(user_account: uuid.UUID, settlement: uuid.UUID) -> list[Leg]:
    return [
        Leg(settlement, "USD", Direction.DEBIT, 25_00),
        Leg(uuid.uuid4(), "USD", Direction.CREDIT, 25_00),
    ]


def of_nothing(user_account: uuid.UUID, settlement: uuid.UUID) -> list[Leg]:
    return [
        Leg(settlement, "USD", Direction.DEBIT, 0),
        Leg(user_account, "USD", Direction.CREDIT, 0),
    ]


def in_the_wrong_asset(user_account: uuid.UUID, settlement: uuid.UUID) -> list[Leg]:
    # Balanced as written, and 25 USDC would be twenty-five millionths of one.
    return [
        Leg(settlement, "USDC", Direction.DEBIT, 25),
        Leg(user_account, "USDC", Direction.CREDIT, 25),
    ]


def none_at_all(user_account: uuid.UUID, settlement: uuid.UUID) -> list[Leg]:
    return []


@pytest.mark.parametrize(
    "legs",
    [unbalanced, one_sided, none_at_all, repeated, to_nowhere, of_nothing, in_the_wrong_asset],
)
async def test_an_adjustment_that_could_not_be_posted_is_refused_when_it_is_asked_for(
    db: Database, ana: Principal, maria: User, legs: Any
) -> None:
    user_account, settlement = await accounts(db, maria)

    with pytest.raises(ops.InvalidAdjustment):
        await request(db, ana, legs(user_account, settlement))

    assert await stored(db) == []
    assert await audited(db, "adjustment.requested") == []


async def test_an_adjustment_has_at_most_fifty_legs(
    db: Database, ana: Principal, maria: User
) -> None:
    _, settlement = await accounts(db, maria)
    async with db.transaction() as session:
        strangers = [
            (
                await ledger.open_account(
                    session, AccountKind.USER_AVAILABLE, "USD", owner_id=new_id()
                )
            ).id
            for _ in range(ops.MAX_LEGS)
        ]

    def spread(over: list[uuid.UUID]) -> list[Leg]:
        return [
            Leg(settlement, "USD", Direction.DEBIT, len(over)),
            *(Leg(account, "USD", Direction.CREDIT, 1) for account in over),
        ]

    with pytest.raises(ops.InvalidAdjustment):
        await request(db, ana, spread(strangers))
    accepted = await request(db, ana, spread(strangers[:-1]))

    assert len(accepted.legs) == ops.MAX_LEGS


async def test_legs_that_balance_across_assets_and_not_within_each_are_refused(
    db: Database, ana: Principal, maria: User
) -> None:
    user_account, _ = await accounts(db, maria)
    async with db.transaction() as session:
        pesos = (await wallets.get_wallet(session, maria.id, "MXN")).available_account_id

    with pytest.raises(ops.InvalidAdjustment, match="MXN, USD"):
        await request(
            db,
            ana,
            [
                Leg(pesos, "MXN", Direction.DEBIT, 25_00),
                Leg(user_account, "USD", Direction.CREDIT, 25_00),
            ],
        )


@pytest.mark.parametrize("reason", ["", "   ", "x" * 501])
async def test_an_adjustment_needs_a_reason_of_a_sensible_length(
    db: Database, ana: Principal, maria: User, reason: str
) -> None:
    user_account, settlement = await accounts(db, maria)

    with pytest.raises(ops.InvalidAdjustment):
        await request(db, ana, goodwill(user_account, settlement), reason)

    assert await stored(db) == []


# --- deciding --------------------------------------------------------------------------------


async def test_another_admins_approval_posts_the_entry(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    user_account, settlement = await accounts(db, maria)
    pending = await request(db, ana, goodwill(user_account, settlement))

    approved = await approve(db, bruno, pending)

    assert (approved.status, approved.requested_by, approved.approved_by) == (
        "approved",
        ana.user_id,
        bruno.user_id,
    )
    assert approved.decided_at is not None
    (posted,) = await entries(db, "adjustment", str(pending.id))
    assert (posted["id"], posted["kind"]) == (approved.entry_id, "adjustment")
    assert posted["postings"] == [("bank_settlement", "D", 25_00), ("user_available", "C", 25_00)]
    assert await available(db, maria) == 25_00
    (event,) = await audited(db, "adjustment.approved")
    assert (event["actor_type"], event["actor_id"], event["resource_id"]) == (
        "admin",
        str(bruno.user_id),
        str(pending.id),
    )
    assert event["details"] == {
        "entry_id": str(approved.entry_id),
        "requested_by": str(ana.user_id),
        "kind": "manual",
    }


async def test_the_requester_cannot_approve_their_own_adjustment(
    db: Database, ana: Principal, maria: User
) -> None:
    user_account, settlement = await accounts(db, maria)
    pending = await request(db, ana, goodwill(user_account, settlement))

    with pytest.raises(ops.SelfApproval) as refusal:
        await approve(db, ana, pending)

    assert (refusal.value.status, refusal.value.code) == (403, "self_approval")
    assert [row["status"] for row in await stored(db)] == ["pending"]
    assert await entries(db, "adjustment", str(pending.id)) == []
    assert await available(db, maria) == 0
    assert await audited(db, "adjustment.approved") == []


async def test_an_adjustment_is_decided_once(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    user_account, settlement = await accounts(db, maria)
    approved = await request(db, ana, goodwill(user_account, settlement))
    rejected = await request(db, ana, goodwill(user_account, settlement))
    await approve(db, bruno, approved)
    async with db.transaction() as session:
        await ops.reject_adjustment(session, bruno, rejected.id)

    for decided in (approved, rejected):
        with pytest.raises(ops.AdjustmentNotPending):
            await approve(db, bruno, decided)
        with pytest.raises(ops.AdjustmentNotPending):
            async with db.transaction() as session:
                await ops.reject_adjustment(session, bruno, decided.id)

    assert [row["status"] for row in await stored(db)] == ["approved", "rejected"]
    assert await available(db, maria) == 25_00
    assert len(await rows(db, "SELECT 1 FROM journal_entries WHERE kind = 'adjustment'")) == 1


async def test_two_approvals_at_once_post_one_entry_and_one_of_them_is_refused(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    user_account, settlement = await accounts(db, maria)
    pending = await request(db, ana, goodwill(user_account, settlement))
    async with db.transaction() as session:
        carla = acting_as(await add_user(session, "carla", role="admin"))

    async def approval(approver: Principal) -> str:
        try:
            await approve(db, approver, pending)
        except ops.AdjustmentNotPending:
            return "refused"
        return "approved"

    outcomes = await asyncio.gather(*(approval(who) for who in (bruno, carla) * 5))

    assert sorted(outcomes) == ["approved"] + ["refused"] * 9
    assert len(await entries(db, "adjustment", str(pending.id))) == 1
    assert await available(db, maria) == 25_00
    assert len(await audited(db, "adjustment.approved")) == 1


async def test_a_rejected_adjustment_moves_nothing_and_the_requester_may_withdraw_their_own(
    db: Database, ana: Principal, maria: User
) -> None:
    user_account, settlement = await accounts(db, maria)
    pending = await request(db, ana, goodwill(user_account, settlement))

    async with db.transaction() as session:
        rejected = await ops.reject_adjustment(session, ana, pending.id)

    assert (rejected.status, rejected.approved_by, rejected.entry_id) == ("rejected", None, None)
    assert rejected.decided_at is not None
    assert await available(db, maria) == 0
    assert await entries(db, "adjustment", str(pending.id)) == []
    (event,) = await audited(db, "adjustment.rejected")
    assert (event["actor_id"], event["resource_id"]) == (str(ana.user_id), str(pending.id))


async def test_an_approval_the_ledger_refuses_leaves_the_adjustment_pending(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    user_account, settlement = await accounts(db, maria)
    # Taking from a user what the user does not have.
    clawback = [
        Leg(user_account, "USD", Direction.DEBIT, 25_00),
        Leg(settlement, "USD", Direction.CREDIT, 25_00),
    ]
    pending = await request(db, ana, clawback, "reverse a duplicate credit")

    with pytest.raises(ledger.InsufficientFunds):
        await approve(db, bruno, pending)

    assert [row["status"] for row in await stored(db)] == ["pending"]
    assert await entries(db, "adjustment", str(pending.id)) == []


async def test_deciding_an_adjustment_that_does_not_exist_is_not_found(
    db: Database, ana: Principal
) -> None:
    async with db.transaction() as session:
        with pytest.raises(ops.AdjustmentNotFound):
            await ops.approve_adjustment(session, ana, new_id())
        with pytest.raises(ops.AdjustmentNotFound):
            await ops.reject_adjustment(session, ana, new_id())
        with pytest.raises(ops.AdjustmentNotFound):
            await ops.get_adjustment(session, ana, new_id())


# --- reading ---------------------------------------------------------------------------------


async def test_adjustments_are_listed_newest_first_by_status_and_in_pages(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    user_account, settlement = await accounts(db, maria)
    made = [await request(db, ana, goodwill(user_account, settlement)) for _ in range(4)]
    await approve(db, bruno, made[0])

    async with db.transaction() as session:
        everything = await ops.list_adjustments(session, ana)
        pending = await ops.list_adjustments(session, ana, status="pending", limit=2)
        rest = await ops.list_adjustments(
            session, ana, status="pending", limit=2, cursor=pending.next_cursor
        )
        approved = await ops.list_adjustments(session, ana, status="approved")
        one = await ops.get_adjustment(session, ana, made[0].id)

    assert [item.id for item in everything.items] == [item.id for item in made[::-1]]
    assert everything.next_cursor is None
    assert [item.id for item in pending.items] == [made[3].id, made[2].id]
    assert ([item.id for item in rest.items], rest.next_cursor) == ([made[1].id], None)
    assert [item.id for item in approved.items] == [made[0].id]
    assert (one.id, one.status) == (made[0].id, "approved")


async def test_reading_adjustments_is_audited(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    pending = await request(db, ana, goodwill(*await accounts(db, maria)))

    async with db.transaction() as session:
        await ops.get_adjustment(session, bruno, pending.id)
        await ops.list_adjustments(session, bruno, status="pending")
        await ops.list_adjustments(session, bruno)

    (read,) = await audited(db, "adjustment.read")
    assert (read["actor_type"], read["actor_id"]) == ("admin", str(bruno.user_id))
    assert (read["resource_type"], read["resource_id"]) == ("adjustment", str(pending.id))
    by_status, everything = await audited(db, "adjustment.listed")
    assert (by_status["actor_type"], by_status["actor_id"]) == ("admin", str(bruno.user_id))
    assert by_status["details"] == {"status": "pending", "returned": 1}
    assert everything["details"] == {"status": "all", "returned": 1}


async def test_an_adjustment_that_is_not_there_is_not_audited_as_read(
    db: Database, ana: Principal
) -> None:
    with pytest.raises(ops.AdjustmentNotFound):
        async with db.transaction() as session:
            await ops.get_adjustment(session, ana, new_id())

    assert await audited(db, "adjustment.read") == []


# --- the lock order --------------------------------------------------------------------------


def clawback(user_account: uuid.UUID, settlement: uuid.UUID, amount: int = 25_00) -> list[Leg]:
    """A debit of a user's available balance against the bank."""
    return [
        Leg(user_account, "USD", Direction.DEBIT, amount),
        Leg(settlement, "USD", Direction.CREDIT, amount),
    ]


async def test_approving_a_debit_of_a_users_balance_waits_for_their_money_out_lock(
    db: Database, impatient_db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    user_account, settlement = await accounts(db, maria)
    await approve(db, bruno, await request(db, ana, goodwill(user_account, settlement)))
    pending = await request(db, ana, clawback(user_account, settlement))

    async with db.transaction() as holder:
        # What a transfer or a withdrawal of this user holds while it decides.
        await advisory_xact_lock(holder, [lock_key(risk.MONEY_OUT_LOCK, maria.id)])
        with pytest.raises(DBAPIError) as failure:
            await approve(impatient_db, bruno, pending)

    assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
    assert await available(db, maria) == 25_00
    # Once the lock is free it goes through.
    assert (await approve(db, bruno, pending)).status == "approved"
    assert await available(db, maria) == 0


async def test_approving_a_credit_to_a_user_does_not_wait_for_their_money_out_lock(
    db: Database, impatient_db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    pending = await request(db, ana, goodwill(*await accounts(db, maria)))

    async with db.transaction() as holder:
        await advisory_xact_lock(holder, [lock_key(risk.MONEY_OUT_LOCK, maria.id)])
        approved = await approve(impatient_db, bruno, pending)

    assert approved.status == "approved"
    assert await available(db, maria) == 25_00


async def test_two_adjustments_that_debit_the_same_two_users_in_opposite_orders_both_post(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    async with db.transaction() as session:
        joao = await add_user(session, "joao")
        await wallets.provision(session, joao.id)
    marias, settlement = await accounts(db, maria)
    joaos, _ = await accounts(db, joao)
    for account in (marias, joaos):
        await approve(db, bruno, await request(db, ana, goodwill(account, settlement, 100_00)))

    def both(first: uuid.UUID, second: uuid.UUID) -> list[Leg]:
        return [
            Leg(first, "USD", Direction.DEBIT, 1_00),
            Leg(second, "USD", Direction.DEBIT, 1_00),
            Leg(settlement, "USD", Direction.CREDIT, 2_00),
        ]

    pending = [
        await request(db, ana, both(marias, joaos) if turn % 2 else both(joaos, marias))
        for turn in range(10)
    ]

    done = await asyncio.gather(*(approve(db, bruno, adjustment) for adjustment in pending))

    assert [adjustment.status for adjustment in done] == ["approved"] * 10
    assert await available(db, maria) == 90_00
    assert await balance_of(db, AccountKind.USER_AVAILABLE, owner=joao) == 90_00


async def test_approving_an_adjustment_that_does_not_exist_is_not_found(
    db: Database, bruno: Principal
) -> None:
    with pytest.raises(ops.AdjustmentNotFound):
        async with db.transaction() as session:
            await ops.approve_adjustment(session, bruno, new_id())


# --- what is reserved for a withdrawal is not an adjustment's to take ----------------------------


async def held_debit(db: Database, user: User, amount: int = 25_00) -> list[Leg]:
    """Postings that give a user back what is on hold for them, written by hand."""
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user.id, "USD")
        # Something on hold, as a withdrawal that was asked for leaves it.
        await fund(session, wallet.held_account_id, amount)
    return [
        Leg(wallet.held_account_id, "USD", Direction.DEBIT, amount),
        Leg(wallet.available_account_id, "USD", Direction.CREDIT, amount),
    ]


async def test_an_adjustment_written_by_hand_cannot_take_money_off_hold(
    db: Database, ana: Principal, maria: User
) -> None:
    legs = await held_debit(db, maria)

    with pytest.raises(ops.InvalidAdjustment) as refusal:
        await request(db, ana, legs)

    assert refusal.value.extra == {"field": "legs"}
    assert "withdrawal" in refusal.value.detail
    assert await stored(db) == []


async def test_a_pending_adjustment_from_before_that_debits_a_held_balance_cannot_be_approved(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    legs = await held_debit(db, maria)
    adjustment_id = new_id()
    async with db.transaction() as session:
        # As one asked for before held balances were closed to adjustments written by hand.
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
                "legs": json.dumps(
                    [
                        {
                            "account_id": str(leg.account_id),
                            "asset": leg.asset,
                            "direction": leg.direction.value,
                            "amount": str(leg.amount),
                        }
                        for leg in legs
                    ]
                ),
            },
        )

    with pytest.raises(ops.InvalidAdjustment):
        async with db.transaction() as session:
            await ops.approve_adjustment(session, bruno, adjustment_id)

    assert await available(db, maria) == 0
    assert [row["status"] for row in await stored(db)] == ["pending"]

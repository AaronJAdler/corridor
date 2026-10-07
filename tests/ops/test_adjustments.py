"""Adjustments: a balanced entry asked for by one admin and posted by another's approval.

The requester cannot approve their own, two approvals at once post one entry, and money in
suspense is released to a user or sent back through the same path.
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from corridor import ledger, ops, risk, wallets
from corridor.identity import Principal, User
from corridor.ledger import AccountKind, Direction, EntryDraft, credit, debit
from corridor.ops import Adjustment, Leg
from corridor.platform.db import (
    LOCK_NOT_AVAILABLE,
    Database,
    advisory_xact_lock,
    lock_key,
    sqlstate_of,
)
from corridor.platform.errors import PermissionDenied
from corridor.platform.ids import new_id
from corridor.platform.money import UnknownAsset
from corridor.wallets import WalletNotFound
from tests.identity.support import add_user
from tests.ops.support import audited
from tests.payments.support import acting_as, available, balance_of, entries, rows, suspense

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


async def in_suspense(db: Database, amount: int, asset: str = "USD") -> None:
    """Money that arrived at the bank and could not be attributed to anyone."""
    async with db.transaction() as session:
        settlement = await ledger.open_account(
            session, AccountKind.BANK_SETTLEMENT, asset, provider=BANK
        )
        held = await ledger.open_account(session, AccountKind.SUSPENSE, asset)
        await ledger.post_entry(
            session,
            EntryDraft(
                kind="deposit_suspense",
                source_type="test_deposit",
                source_id=str(new_id()),
                postings=(debit(settlement.id, amount), credit(held.id, amount)),
            ),
        )


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
                asset="USD",
                amount=1,
                user_id=maria.id,
            )
        with pytest.raises(PermissionDenied):
            await ops.request_suspense_return(
                session, user, adjustment_id=new_id(), reason="for me", asset="USD", amount=1
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


# --- suspense --------------------------------------------------------------------------------


async def test_suspense_is_released_to_a_user_by_a_second_admins_approval(
    db: Database, ana: Principal, bruno: Principal, maria: User
) -> None:
    await in_suspense(db, 75_00)
    async with db.transaction() as session:
        pending = await ops.request_suspense_release(
            session,
            ana,
            adjustment_id=new_id(),
            reason="the sender confirmed it was for maria",
            asset="USD",
            amount=75_00,
            user_id=maria.id,
        )
    assert (await suspense(db), await available(db, maria)) == (75_00, 0)

    with pytest.raises(ops.SelfApproval):
        await approve(db, ana, pending)
    approved = await approve(db, bruno, pending)

    (posted,) = await entries(db, "adjustment", str(pending.id))
    assert posted["id"] == approved.entry_id
    assert posted["postings"] == [("suspense", "D", 75_00), ("user_available", "C", 75_00)]
    assert (await suspense(db), await available(db, maria)) == (0, 75_00)


async def test_suspense_is_booked_as_returned_through_the_provider_it_arrived_at(
    db: Database, ana: Principal, bruno: Principal
) -> None:
    await in_suspense(db, 75_00)
    async with db.transaction() as session:
        pending = await ops.request_suspense_return(
            session,
            ana,
            adjustment_id=new_id(),
            reason="nobody claimed it",
            asset="USD",
            amount=30_00,
        )

    await approve(db, bruno, pending)

    (posted,) = await entries(db, "adjustment", str(pending.id))
    assert posted["postings"] == [("suspense", "D", 30_00), ("bank_settlement", "C", 30_00)]
    assert await suspense(db) == 45_00
    assert await balance_of(db, AccountKind.BANK_SETTLEMENT, provider=BANK) == 45_00


@pytest.mark.parametrize("amount", [75_01, 1_000_00])
async def test_more_than_suspense_holds_cannot_be_asked_for(
    db: Database, ana: Principal, maria: User, amount: int
) -> None:
    await in_suspense(db, 75_00)

    async with db.transaction() as session:
        with pytest.raises(ops.InvalidAdjustment):
            await ops.request_suspense_release(
                session,
                ana,
                adjustment_id=new_id(),
                reason="too much",
                asset="USD",
                amount=amount,
                user_id=maria.id,
            )
        with pytest.raises(ops.InvalidAdjustment):
            await ops.request_suspense_return(
                session, ana, adjustment_id=new_id(), reason="too much", asset="USD", amount=amount
            )

    assert await stored(db) == []


async def test_a_suspense_release_names_a_supported_asset_and_a_user_with_a_wallet(
    db: Database, ana: Principal, maria: User
) -> None:
    await in_suspense(db, 75_00)

    async with db.transaction() as session:
        with pytest.raises(ops.InvalidAdjustment):
            # Nothing was ever put in suspense in pesos.
            await ops.request_suspense_release(
                session,
                ana,
                adjustment_id=new_id(),
                reason="wrong asset",
                asset="MXN",
                amount=1_00,
                user_id=maria.id,
            )
        with pytest.raises(UnknownAsset):
            await ops.request_suspense_return(
                session, ana, adjustment_id=new_id(), reason="no such", asset="EUR", amount=1_00
            )
        with pytest.raises(WalletNotFound):
            await ops.request_suspense_release(
                session,
                ana,
                adjustment_id=new_id(),
                reason="nobody",
                asset="USD",
                amount=1_00,
                user_id=new_id(),
            )

    assert await stored(db) == []


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

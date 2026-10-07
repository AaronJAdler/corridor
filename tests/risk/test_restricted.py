"""A restricted user: money still comes in, and none goes out by any path.

Each way out has a test of its own, through the real use case, so that a path which
stopped asking risk would be noticed by the test of that path.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor import identity, payments, risk
from corridor.identity import User
from corridor.platform.config import Settings
from corridor.platform.db import (
    LOCK_NOT_AVAILABLE,
    Database,
    advisory_xact_lock,
    lock_key,
    sqlstate_of,
)
from corridor.platform.ids import new_id
from corridor.providers import SimBank, SimCustody
from corridor.risk import UserRestricted
from tests.fx.support import convert, quote_for, quote_status
from tests.payments.support import (
    acting_as,
    add_beneficiary,
    available,
    count,
    deposit,
    held,
    instruction_for,
    send,
    withdraw,
)
from tests.support.providers import EXTERNAL_ADDRESS, Sim


async def restrict(db: Database, user: User) -> None:
    async with db.transaction() as session:
        await identity.restrict_user(session, user.id, "unpaid receivable")


async def nothing_moved(db: Database, user: User, balance: int, asset: str = "USD") -> None:
    assert await available(db, user, asset) == balance
    assert await held(db, user, asset) == 0
    for table in ("transfers", "withdrawals", "fx_conversions", "outbox_events", "risk_usage"):
        assert await count(db, table) == 0, table


async def test_a_restricted_user_can_be_sent_money(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, joao, 100_00)
    await restrict(db, maria)

    await send(db, settings, joao, maria, 40_00)

    assert await available(db, maria) == 40_00


async def test_a_restricted_user_can_receive_a_deposit(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    instruction = await instruction_for(db, maria, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "250.00")
    await restrict(db, maria)

    await payments.apply_bank_deposit_received(db, data)

    assert await available(db, maria) == 250_00


async def test_a_restricted_user_cannot_transfer(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 100_00)
    await restrict(db, maria)

    with pytest.raises(UserRestricted) as refusal:
        await send(db, settings, maria, joao, 40_00)

    assert (refusal.value.status, refusal.value.code) == (403, "user_restricted")
    await nothing_moved(db, maria, 100_00)
    assert await available(db, joao) == 0


async def test_a_restricted_user_cannot_convert(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 100_00)
    quote = await quote_for(db, settings, maria, 40_00)
    await restrict(db, maria)

    with pytest.raises(UserRestricted):
        await convert(db, maria, quote)

    await nothing_moved(db, maria, 100_00)
    assert await available(db, maria, "MXN") == 0
    assert await quote_status(db, quote.id) == "open"


async def test_a_restricted_user_cannot_withdraw_to_a_bank_account(
    db: Database,
    settings: Settings,
    bank: SimBank,
    maria: User,
) -> None:
    await deposit(db, maria, 100_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    await restrict(db, maria)

    with pytest.raises(UserRestricted):
        await withdraw(db, settings, maria, 40_00, beneficiary=beneficiary)

    await nothing_moved(db, maria, 100_00)


async def test_a_restricted_user_cannot_withdraw_to_an_address(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 50_000_000, "USDC")
    await restrict(db, maria)

    with pytest.raises(UserRestricted):
        await withdraw(db, settings, maria, 1_000_000, asset="USDC", to_address=EXTERNAL_ADDRESS)

    await nothing_moved(db, maria, 50_000_000, "USDC")


async def test_a_user_whose_restriction_was_lifted_moves_money_out_again(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 100_00)
    await restrict(db, maria)
    async with db.transaction() as session:
        await identity.lift_restriction(session, maria.id)

    await send(db, settings, maria, joao, 40_00)

    assert await available(db, joao) == 40_00


# --- restricting waits for what is on its way out ------------------------------------------------


async def test_restricting_a_user_takes_that_users_money_out_lock(
    db: Database, maria: User
) -> None:
    # A movement out holds this lock from before it reads the account's standing until it
    # commits. Here one is under way, and the restriction cannot get past it.
    async with db.transaction() as moving:
        await advisory_xact_lock(moving, [lock_key(risk.MONEY_OUT_LOCK, maria.id)])

        with pytest.raises(DBAPIError) as failure:
            async with db.transaction() as restricting:
                await restricting.execute(text("SET LOCAL lock_timeout = '100ms'"))
                await risk.restrict_user(restricting, maria.id, "under review")

        assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
        assert (await identity.get_user(moving, maria.id)).status == "active"


async def until_a_transaction_is_waiting_for_a_lock(db: Database) -> None:
    for _ in range(1000):
        async with db.transaction() as session:
            waiting = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no transaction ever waited for a lock")


async def test_a_restriction_waits_for_a_transfer_under_way_and_stops_the_next(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 100_00)
    under_way = asyncio.Event()
    finish = asyncio.Event()

    async def transfer_slowly() -> None:
        async with db.transaction() as session:
            # Read as active, limited, posted and written: all that is left is to commit.
            await payments.create_transfer(
                session,
                acting_as(maria),
                transfer_id=new_id(),
                recipient=str(joao.id),
                asset="USD",
                amount=40_00,
                memo=None,
                settings=settings,
            )
            under_way.set()
            await finish.wait()

    async def restrict_when_under_way() -> None:
        await under_way.wait()
        async with db.transaction() as session:
            await risk.restrict_user(session, maria.id, "under review")

    moving = asyncio.create_task(transfer_slowly())
    restricting = asyncio.create_task(restrict_when_under_way())
    try:
        await until_a_transaction_is_waiting_for_a_lock(db)
        restricted_meanwhile = restricting.done()
    finally:
        finish.set()
        await asyncio.gather(moving, restricting)

    assert restricted_meanwhile is False
    # The transfer that was under way completed, and the restriction followed it.
    assert await available(db, joao) == 40_00
    with pytest.raises(UserRestricted):
        await send(db, settings, maria, joao, 10_00)
    assert await available(db, joao) == 40_00


async def test_restricting_a_user_restricts_them(db: Database, maria: User) -> None:
    async with db.transaction() as session:
        restricted = await risk.restrict_user(session, maria.id, "under review")

    assert restricted.status == "restricted"
    async with db.transaction() as session:
        assert (await identity.get_user(session, maria.id)).status == "restricted"


async def test_a_caller_that_holds_the_lock_already_is_not_made_to_wait_for_itself(
    db: Database, maria: User
) -> None:
    # The deposit-return path restricts a user whose lock it took at the start.
    async with db.transaction() as session:
        await session.execute(text("SET LOCAL lock_timeout = '100ms'"))
        await advisory_xact_lock(session, [lock_key(risk.MONEY_OUT_LOCK, maria.id)])

        restricted = await risk.restrict_user(session, maria.id, "deposit returned")

    assert restricted.status == "restricted"

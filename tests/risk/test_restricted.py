"""A restricted user: money still comes in, and none goes out by any path.

Each way out has a test of its own, through the real use case, so that a path which
stopped asking risk would be noticed by the test of that path.
"""

import pytest

from corridor import identity, payments
from corridor.identity import User
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.providers import SimBank, SimCustody
from corridor.risk import UserRestricted
from tests.fx.support import convert, quote_for, quote_status
from tests.payments.support import (
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

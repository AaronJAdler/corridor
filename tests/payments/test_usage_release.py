"""What a withdrawal that ended without paying out gives back: the part of the day's limit
it had used. Every way a withdrawal's funds are released is here."""

import pytest

from corridor import identity, payments
from corridor.identity import User
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.providers import SimBank, SimCustody
from corridor.risk import LimitExceeded
from tests.payments.support import (
    acting_as,
    add_beneficiary,
    deposit,
    rows,
    send,
    withdraw,
)
from tests.support.providers import CLOSED_ACCOUNT_NUMBER, Sim, advance

# What a user of the lowest tier may move at once, and in 24 hours, in US cents.
PER_TX = 1_000_00
DAILY = 2_500_00


@pytest.fixture(name="settings")
def without_a_minimum_fee(settings: Settings) -> Settings:
    return settings.model_copy(update={"withdrawal_min_fee": {}})


async def at_the_daily_limit(
    db: Database, settings: Settings, bank: SimBank, maria: User, joao: User, **beneficiary: str
) -> payments.Withdrawal:
    """Maria has moved all she may today, the last of it as a withdrawal that is held."""
    await deposit(db, maria, 5_000_00)
    await send(db, settings, maria, joao, PER_TX)
    await send(db, settings, maria, joao, PER_TX)
    to = await add_beneficiary(db, bank, maria, **beneficiary)
    withdrawal = await withdraw(db, settings, maria, 500_00, beneficiary=to)
    with pytest.raises(LimitExceeded):
        await send(db, settings, maria, joao, 1)
    return withdrawal


async def released(db: Database, withdrawal: payments.Withdrawal) -> bool:
    (row,) = await rows(
        db,
        "SELECT released_at FROM risk_usage WHERE kind = 'withdrawal' AND movement_id = :id",
        id=withdrawal.id,
    )
    return row["released_at"] is not None


async def test_a_user_at_the_daily_limit_can_send_again_after_cancelling_a_withdrawal(
    db: Database, settings: Settings, bank: SimBank, maria: User, joao: User
) -> None:
    withdrawal = await at_the_daily_limit(db, settings, bank, maria, joao)

    async with db.transaction() as session:
        await payments.cancel_withdrawal(session, acting_as(maria), withdrawal.id)

    await send(db, settings, maria, joao, 500_00)
    # And no more than was given back.
    with pytest.raises(LimitExceeded):
        await send(db, settings, maria, joao, 1)


async def test_a_withdrawal_that_is_paid_out_keeps_its_usage(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
    joao: User,
) -> None:
    withdrawal = await at_the_daily_limit(db, settings, bank, maria, joao)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, 30)

    await payments.apply_payout_completed(db, await sim.last_event("payout.completed"))

    assert not await released(db, withdrawal)
    with pytest.raises(LimitExceeded):
        await send(db, settings, maria, joao, 1)


async def test_a_withdrawal_the_provider_could_not_pay_out_gives_its_usage_back(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
    joao: User,
) -> None:
    withdrawal = await at_the_daily_limit(
        db, settings, bank, maria, joao, account_number=CLOSED_ACCOUNT_NUMBER
    )
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, 30)

    await payments.apply_payout_failed(db, await sim.last_event("payout.failed"))

    assert await released(db, withdrawal)
    await send(db, settings, maria, joao, 500_00)


async def test_a_withdrawal_given_back_because_its_user_was_restricted_gives_its_usage_back(
    db: Database, settings: Settings, bank: SimBank, custody: SimCustody, maria: User, joao: User
) -> None:
    withdrawal = await at_the_daily_limit(db, settings, bank, maria, joao)
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "under review")

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert await released(db, withdrawal)

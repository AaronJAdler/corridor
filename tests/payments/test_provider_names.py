"""A provider's name is the adapter's own, and everything Corridor writes down follows it.

A second bank rail, here the simulator under the name ``increase``, gets its own deposits and
its own settlement account, and an event can only act on money that went through the rail it
came from."""

import copy
from typing import Any

import pytest

from corridor import payments
from corridor.identity import User
from corridor.ledger import AccountKind
from corridor.payments import ProviderEventMismatch
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.providers import INCREASE, ProviderMisconfigured, SimBank, SimCustody
from tests.payments.support import (
    add_person,
    available,
    balance_of,
    entries,
    held_bank_withdrawal,
    held_chain_withdrawal,
    instruction_for,
    rows,
    settlement,
    withdrawal_row,
)
from tests.support.providers import EXTERNAL_ADDRESS, Sim, advance


class IncreaseNamedBank(SimBank):
    """The simulator, answering to the name of the second bank rail."""

    name = INCREASE  # type: ignore[assignment,misc]


@pytest.fixture
def second_bank(sim: Sim, provider_settings: Settings) -> SimBank:
    return IncreaseNamedBank(provider_settings, client=sim.http)


async def increase_settlement(db: Database) -> int:
    return await balance_of(db, AccountKind.BANK_SETTLEMENT, "USD", provider=INCREASE)


async def deposits_of(db: Database, provider: str) -> list[dict[str, Any]]:
    return await rows(
        db, "SELECT provider, status FROM deposits WHERE provider = :provider", provider=provider
    )


async def test_a_deposit_through_the_second_rail_is_kept_under_its_name(
    db: Database, sim: Sim, second_bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USD", second_bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "100.00")

    await payments.apply_bank_deposit_received(db, data, provider=INCREASE)

    assert instruction.provider == INCREASE
    assert await available(db, maria) == 100_00
    assert await increase_settlement(db) == 100_00
    assert await settlement(db) == 0
    assert len(await deposits_of(db, INCREASE)) == 1
    assert await deposits_of(db, "simbank") == []
    credit, *_ = await entries(db, "deposit", f"{INCREASE}:{data['deposit_id']}")
    assert credit["kind"] == "deposit"


async def test_an_event_under_another_rails_name_credits_nobody(
    db: Database, sim: Sim, second_bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USD", second_bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "100.00")

    await payments.apply_bank_deposit_received(db, data, provider="simbank")

    assert await available(db, maria) == 0
    assert await increase_settlement(db) == 0
    (unattributed,) = await deposits_of(db, "simbank")
    assert unattributed["status"] == "suspense"


async def test_one_deposit_id_at_two_rails_is_two_deposits(
    db: Database, sim: Sim, bank: SimBank, second_bank: SimBank, custody: SimCustody, maria: User
) -> None:
    async with db.transaction() as session:
        ana = await add_person(session, "ana")
    first = await sim.bank_deposit(
        (await instruction_for(db, maria, "USD", bank, custody)).provider_ref, "100.00"
    )
    second = copy.deepcopy(
        await sim.bank_deposit(
            (await instruction_for(db, ana, "USD", second_bank, custody)).provider_ref, "40.00"
        )
    )
    second["deposit_id"] = first["deposit_id"]

    await payments.apply_bank_deposit_received(db, first, provider="simbank")
    await payments.apply_bank_deposit_received(db, second, provider=INCREASE)

    assert await available(db, maria) == 100_00
    assert await available(db, ana) == 40_00
    assert await settlement(db) == 100_00
    assert await increase_settlement(db) == 40_00


async def test_a_return_from_one_rail_leaves_the_other_rails_deposit_alone(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "100.00")
    await payments.apply_bank_deposit_received(db, data, provider="simbank")
    (before,) = await deposits_of(db, "simbank")

    await payments.apply_bank_deposit_returned(
        db, await sim.return_bank_deposit(data["deposit_id"]), provider=INCREASE
    )

    assert await available(db, maria) == 100_00
    assert await deposits_of(db, "simbank") == [before]
    assert await settlement(db) == 100_00


async def test_a_payout_event_from_another_rail_cannot_settle_a_withdrawal(
    db: Database,
    sim: Sim,
    clock: ManualClock,
    settings: Settings,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, settings, bank, maria)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, 30)
    completed = await sim.last_event("payout.completed")
    submitted = (await withdrawal_row(db, withdrawal.id))["status"]

    with pytest.raises(ProviderEventMismatch):
        await payments.apply_payout_completed(db, completed, provider=INCREASE)

    assert (await withdrawal_row(db, withdrawal.id))["status"] == submitted


async def test_a_withdrawal_is_not_sent_through_an_adapter_of_another_name(
    db: Database,
    settings: Settings,
    bank: SimBank,
    second_bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, settings, bank, maria)

    with pytest.raises(ProviderMisconfigured):
        await payments.submit_withdrawal(db, second_bank, custody, withdrawal.id)

    assert (await withdrawal_row(db, withdrawal.id))["status"] == "held"


class ElsewhereCustody(SimCustody):
    """The simulator, answering to a name the withdrawal did not go out by."""

    name = "elsewhere"  # type: ignore[assignment,misc]


async def test_a_chain_withdrawal_is_not_sent_through_a_custodian_of_another_name(
    db: Database,
    sim: Sim,
    provider_settings: Settings,
    settings: Settings,
    bank: SimBank,
    maria: User,
) -> None:
    withdrawal = await held_chain_withdrawal(db, settings, maria, EXTERNAL_ADDRESS)
    other = ElsewhereCustody(provider_settings, client=sim.http)

    with pytest.raises(ProviderMisconfigured):
        await payments.submit_withdrawal(db, bank, other, withdrawal.id)

    assert (await withdrawal_row(db, withdrawal.id))["status"] == "held"


@pytest.mark.parametrize("name", ["simcustody", "simfx", "nobody", ""])
async def test_a_bank_event_under_a_name_that_is_no_bank_rail_is_refused(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User, name: str
) -> None:
    instruction = await instruction_for(db, maria, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "100.00")

    with pytest.raises(ValueError, match="not a bank rail"):
        await payments.apply_bank_deposit_received(db, data, provider=name)
    with pytest.raises(ValueError, match="not a bank rail"):
        await payments.apply_payout_completed(db, data, provider=name)

    assert await available(db, maria) == 0

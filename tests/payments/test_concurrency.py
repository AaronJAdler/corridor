"""Transfers under real contention: many transactions, one PostgreSQL, real locks."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import payments
from corridor.identity import User
from corridor.ledger import InsufficientFunds
from corridor.platform.config import Settings
from corridor.platform.db import DEADLOCK_DETECTED, Database
from corridor.platform.ids import new_id
from tests.payments.support import acting_as, available, count, deposit, fee_revenue


def deadlock_retries() -> float:
    return (
        REGISTRY.get_sample_value(
            "corridor_db_transaction_retries_total", {"sqlstate": DEADLOCK_DETECTED}
        )
        or 0.0
    )


def sending(
    settings: Settings, sender: User, recipient: User, amount: int, asset: str = "USD"
) -> Callable[[AsyncSession], Awaitable[uuid.UUID | None]]:
    """A unit of work as the HTTP handler runs one: the id of the transfer, or None if the
    balance could not cover it."""

    async def work(session: AsyncSession) -> uuid.UUID | None:
        try:
            transfer = await payments.create_transfer(
                session,
                acting_as(sender),
                transfer_id=new_id(),
                recipient=str(recipient.id),
                asset=asset,
                amount=amount,
                memo=None,
                settings=settings,
            )
        except InsufficientFunds:
            return None
        return transfer.id

    return work


async def test_100_transfers_in_opposite_directions_never_deadlock_and_conserve_money(
    db: Database, with_fee: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 1_000_00)
    await deposit(db, joao, 1_000_00)

    before = deadlock_retries()
    done = await asyncio.gather(
        *(db.run(sending(with_fee, maria, joao, 1_00)) for _ in range(60)),
        *(db.run(sending(with_fee, joao, maria, 1_00)) for _ in range(40)),
    )

    assert deadlock_retries() == before
    assert None not in done
    assert len(set(done)) == 100
    # Each transfer cost its sender 1.00 and a fee of 0.01.
    assert await available(db, maria) == 1_000_00 - 60 * 1_01 + 40 * 1_00
    assert await available(db, joao) == 1_000_00 - 40 * 1_01 + 60 * 1_00
    assert await fee_revenue(db) == 100
    assert await available(db, maria) + await available(db, joao) + await fee_revenue(db) == (
        2_000_00
    )
    assert await count(db, "transfers") == 100
    assert await count(db, "outbox_events") == 100


async def test_100_sends_from_a_balance_that_affords_30_give_exactly_30(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 30_00)

    done = await asyncio.gather(*(db.run(sending(settings, maria, joao, 1_00)) for _ in range(100)))

    assert len([transfer for transfer in done if transfer is not None]) == 30
    assert (await available(db, maria), await available(db, joao)) == (0, 30_00)
    assert await count(db, "transfers") == 30
    assert await count(db, "outbox_events") == 30


async def test_one_sender_spending_two_assets_at_once_gets_both_right(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 20_00, "USD")
    await deposit(db, maria, 20_00, "MXN")

    before = deadlock_retries()
    done = await asyncio.gather(
        *(db.run(sending(settings, maria, joao, 1_00, "USD")) for _ in range(30)),
        *(db.run(sending(settings, maria, joao, 1_00, "MXN")) for _ in range(30)),
    )

    assert deadlock_retries() == before
    assert len([transfer for transfer in done if transfer is not None]) == 40
    assert (await available(db, joao, "USD"), await available(db, joao, "MXN")) == (20_00, 20_00)

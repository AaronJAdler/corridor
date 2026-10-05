import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import (
    Column,
    MetaData,
    Table,
    TextClause,
    Uuid,
    func,
    insert,
    literal_column,
    select,
    text,
)
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.platform.db import (
    DEADLOCK_DETECTED,
    LOCK_NOT_AVAILABLE,
    MAX_RETRIES,
    SERIALIZATION_FAILURE,
    UNIQUE_VIOLATION,
    Database,
    MinorUnits,
    advisory_xact_lock,
    constraint_of,
    lock_key,
    sqlstate_of,
    try_advisory_xact_lock,
)
from corridor.platform.ids import new_id

scratch = MetaData()
amounts = Table(
    "scratch_amounts",
    scratch,
    Column("id", Uuid, primary_key=True),
    Column("amount", MinorUnits, nullable=False),
)


Work = Callable[[AsyncSession], Awaitable[None]]


@pytest.fixture
async def scratch_table(owner_db: Database) -> None:
    async with owner_db.transaction() as session:
        await session.run_sync(lambda sync: scratch.create_all(sync.connection()))


def _retries(sqlstate: str) -> float:
    return (
        REGISTRY.get_sample_value("corridor_db_transaction_retries_total", {"sqlstate": sqlstate})
        or 0.0
    )


async def _raise(session: AsyncSession, sqlstate: str) -> None:
    await session.execute(
        text(f"DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '{sqlstate}'; END $$")
    )


# --- connection settings -------------------------------------------------------------------


async def test_every_connection_is_pinned_to_utc_with_timeouts(db: Database) -> None:
    async def read(session: AsyncSession) -> dict[str, str]:
        rows = await session.execute(
            text(
                "SELECT name, setting FROM pg_settings WHERE name IN ('TimeZone', 'statement_timeout',"
                " 'lock_timeout', 'idle_in_transaction_session_timeout', 'transaction_isolation')"
            )
        )
        return {row.name: row.setting for row in rows}

    # Several connections at once, so this is about the pool and not one lucky connection.
    results = await asyncio.gather(*(db.run(read) for _ in range(5)))

    for settings in results:
        assert settings == {
            "TimeZone": "UTC",
            "statement_timeout": "10000",
            "lock_timeout": "5000",
            "idle_in_transaction_session_timeout": "15000",
            "transaction_isolation": "read committed",
        }


async def test_timestamps_come_back_timezone_aware_in_utc(db: Database) -> None:
    moment = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
    async with db.transaction() as session:
        stored = (
            await session.execute(text("SELECT CAST(:t AS timestamptz)"), {"t": moment})
        ).scalar_one()
    assert stored == moment
    assert stored.utcoffset() == UTC.utcoffset(None)


async def test_the_application_role_cannot_change_the_schema(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text("CREATE TABLE sneaky (id int)"))
    assert sqlstate_of(failure.value) == "42501"  # insufficient_privilege


# --- unit of work --------------------------------------------------------------------------


@pytest.mark.usefixtures("scratch_table")
async def test_a_transaction_commits_on_success_and_rolls_back_on_error(db: Database) -> None:
    kept, discarded = new_id(), new_id()

    async with db.transaction() as session:
        await session.execute(insert(amounts).values(id=kept, amount=1))

    with pytest.raises(RuntimeError, match="boom"):
        async with db.transaction() as session:
            await session.execute(insert(amounts).values(id=discarded, amount=2))
            raise RuntimeError("boom")

    async with db.transaction() as session:
        assert (await session.execute(select(amounts.c.id))).scalars().all() == [kept]


@pytest.mark.parametrize("sqlstate", [DEADLOCK_DETECTED, SERIALIZATION_FAILURE])
async def test_run_retries_a_deadlock_or_serialisation_failure(db: Database, sqlstate: str) -> None:
    attempts = 0
    before = _retries(sqlstate)

    async def work(session: AsyncSession) -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            await _raise(session, sqlstate)
        return "done"

    assert await db.run(work) == "done"
    assert attempts == 3
    assert _retries(sqlstate) == before + 2


async def test_run_gives_up_after_three_retries(db: Database) -> None:
    attempts = 0

    async def work(session: AsyncSession) -> None:
        nonlocal attempts
        attempts += 1
        await _raise(session, DEADLOCK_DETECTED)

    with pytest.raises(DBAPIError) as failure:
        await db.run(work)

    assert sqlstate_of(failure.value) == DEADLOCK_DETECTED
    assert attempts == 1 + MAX_RETRIES == 4


async def test_run_does_not_retry_other_errors(db: Database) -> None:
    attempts = 0

    async def work(session: AsyncSession) -> None:
        nonlocal attempts
        attempts += 1
        await _raise(session, UNIQUE_VIOLATION)

    with pytest.raises(DBAPIError):
        await db.run(work)
    assert attempts == 1


@pytest.mark.usefixtures("scratch_table")
async def test_a_retried_attempt_leaves_nothing_behind(db: Database) -> None:
    attempts = 0

    async def work(session: AsyncSession) -> None:
        nonlocal attempts
        attempts += 1
        await session.execute(insert(amounts).values(id=new_id(), amount=attempts))
        if attempts == 1:
            await _raise(session, SERIALIZATION_FAILURE)

    await db.run(work)

    async with db.transaction() as session:
        assert (await session.execute(select(amounts.c.amount))).scalars().all() == [2]


@pytest.mark.usefixtures("scratch_table")
async def test_run_recovers_from_a_real_deadlock(db: Database) -> None:
    first, second = new_id(), new_id()
    async with db.transaction() as session:
        await session.execute(
            insert(amounts), [{"id": first, "amount": 0}, {"id": second, "amount": 0}]
        )

    both_hold_one = asyncio.Barrier(2)
    met = False

    def add_one(row: uuid.UUID) -> TextClause:
        return text("UPDATE scratch_amounts SET amount = amount + 1 WHERE id = :id").bindparams(
            id=row
        )

    def worker(order: tuple[uuid.UUID, uuid.UUID]) -> Work:
        async def work(session: AsyncSession) -> None:
            nonlocal met
            await session.execute(add_one(order[0]))
            if not met:
                # Only the first attempt of each worker meets at the barrier: each holds one
                # row and wants the other's, which PostgreSQL resolves by aborting one.
                await both_hold_one.wait()
                met = True
            await session.execute(add_one(order[1]))

        return work

    before = _retries(DEADLOCK_DETECTED)
    await asyncio.gather(db.run(worker((first, second))), db.run(worker((second, first))))

    assert _retries(DEADLOCK_DETECTED) >= before + 1
    async with db.transaction() as session:
        totals = (await session.execute(select(amounts.c.amount))).scalars().all()
    assert totals == [2, 2]


# --- amounts -------------------------------------------------------------------------------


@pytest.mark.usefixtures("scratch_table")
@pytest.mark.parametrize("amount", [0, 1, 10**18 + 7, 10**38 - 1])
async def test_amounts_round_trip_as_exact_integers(db: Database, amount: int) -> None:
    row = new_id()
    async with db.transaction() as session:
        await session.execute(insert(amounts).values(id=row, amount=amount))
        stored = (
            await session.execute(select(amounts.c.amount).where(amounts.c.id == row))
        ).scalar_one()

    assert stored == amount
    assert type(stored) is int


@pytest.mark.usefixtures("scratch_table")
@pytest.mark.parametrize("value", [1.5, Decimal("1.5"), Decimal(2), "3", True])
async def test_binding_anything_but_an_int_as_an_amount_is_an_error(
    db: Database, value: object
) -> None:
    with pytest.raises(StatementError, match="an amount must be an int"):
        async with db.transaction() as session:
            await session.execute(insert(amounts).values(id=new_id(), amount=value))


async def test_a_fractional_amount_from_the_database_is_an_error_not_a_truncation(
    db: Database,
) -> None:
    async with db.transaction() as session:
        result = await session.execute(select(literal_column("1.5", type_=MinorUnits)))
        with pytest.raises(ValueError, match="fractional amount"):
            result.scalar_one()


@pytest.mark.usefixtures("scratch_table")
async def test_sums_of_amounts_are_integers_too(db: Database) -> None:
    async with db.transaction() as session:
        await session.execute(
            insert(amounts), [{"id": new_id(), "amount": 10**20}, {"id": new_id(), "amount": 5}]
        )
        total = (await session.execute(select(func.sum(amounts.c.amount)))).scalar_one()

    assert total == 10**20 + 5
    assert type(total) is int


# --- error introspection -------------------------------------------------------------------


@pytest.mark.usefixtures("scratch_table")
async def test_a_violation_reports_its_sqlstate_and_constraint(db: Database) -> None:
    row = new_id()
    with pytest.raises(IntegrityError) as failure:
        async with db.transaction() as session:
            await session.execute(insert(amounts).values(id=row, amount=1))
            await session.execute(insert(amounts).values(id=row, amount=1))

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == "scratch_amounts_pkey"
    assert sqlstate_of(RuntimeError("not a database error")) is None
    assert constraint_of(RuntimeError("not a database error")) is None


# --- advisory locks ------------------------------------------------------------------------


def test_lock_keys_are_stable_and_namespaced() -> None:
    user = uuid.UUID("0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10")
    assert lock_key("money_out", user) == lock_key("money_out", user)
    assert lock_key("money_out", user) != lock_key("recon", user)
    assert -(2**63) <= lock_key("money_out", user) < 2**63


async def test_an_advisory_lock_is_exclusive_until_its_transaction_ends(db: Database) -> None:
    key = lock_key("test", new_id())
    async with db.transaction() as holder:
        await advisory_xact_lock(holder, [key])
        async with db.transaction() as other:
            assert await try_advisory_xact_lock(other, key) is False

    async with db.transaction() as later:
        assert await try_advisory_xact_lock(later, key) is True


async def test_opposite_lock_requests_queue_instead_of_deadlocking(db: Database) -> None:
    a, b = lock_key("test", new_id()), lock_key("test", new_id())
    before = _retries(DEADLOCK_DETECTED)
    running = 0
    peak = 0

    def worker(keys: list[int]) -> Work:
        async def work(session: AsyncSession) -> None:
            nonlocal running, peak
            await advisory_xact_lock(session, keys)
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

        return work

    await asyncio.gather(*(db.run(worker([a, b] if i % 2 else [b, a])) for i in range(20)))

    assert peak == 1
    assert _retries(DEADLOCK_DETECTED) == before


@pytest.mark.usefixtures("scratch_table")
async def test_a_blocked_statement_fails_with_lock_not_available(db: Database) -> None:
    row = new_id()
    async with db.transaction() as session:
        await session.execute(insert(amounts).values(id=row, amount=1))

    async with db.transaction() as holder:
        await holder.execute(select(amounts).where(amounts.c.id == row).with_for_update())
        with pytest.raises(DBAPIError) as failure:
            async with db.transaction() as waiter:
                await waiter.execute(text("SET LOCAL lock_timeout = '100ms'"))
                await waiter.execute(select(amounts).where(amounts.c.id == row).with_for_update())

    assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE

"""What the application role may change, column by column, on the tables whose rows it
goes on changing after they are written.

A statement that got in through the application could otherwise rewrite who a withdrawal
belongs to, how much a deposit was for, or the rate of a quote.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from corridor.platform.db import Database, sqlstate_of
from corridor.platform.ids import new_id
from tests.support import postgres

INSUFFICIENT_PRIVILEGE = "42501"
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

# The tests that write here write rows by hand, with no ledger entry behind them.
pytestmark = pytest.mark.corrupts_ledger

UPDATABLE: dict[str, set[str]] = {
    "withdrawals": {
        "status",
        "provider_ref",
        "provider_fee",
        "failure_reason",
        "final_entry_id",
        "submitted_at",
        "updated_at",
    },
    "deposits": {"status", "user_id", "entry_id", "updated_at"},
    "fx_quotes": {"status"},
    "webhook_events": {"processed_at", "outcome", "payload", "redacted_at"},
}

# One row of each table, as plain SQL, and for each a column the application has no
# reason to change and one it changes in the ordinary course of things.
ROWS: dict[str, tuple[str, dict[str, Any]]] = {
    "withdrawals": (
        "INSERT INTO withdrawals (id, user_id, asset_code, amount, fee, kind, to_address, status,"
        " provider, hold_entry_id, initiated_by_type, initiated_by_id, created_at, updated_at)"
        " VALUES (:id, :user, 'USDC', 100, 0, 'chain', 'sim1address', 'held', 'simcustody',"
        " :entry, 'user', :user, :now, :now)",
        {"user": new_id(), "entry": new_id()},
    ),
    "deposits": (
        "INSERT INTO deposits (id, user_id, asset_code, amount, provider, provider_ref, kind,"
        " status, created_at, updated_at) VALUES (:id, :user, 'USD', 100, 'simbank', 'dep_1',"
        " 'bank', 'pending', :now, :now)",
        {"user": new_id()},
    ),
    "fx_quotes": (
        "INSERT INTO fx_quotes (id, user_id, sell_asset, buy_asset, sell_amount, buy_amount,"
        " rate, mid, status, expires_at, created_at) VALUES (:id, :user, 'USD', 'MXN', 100,"
        " 1700, '17.00', '17.25', 'open', :later, :now)",
        {"user": new_id(), "later": datetime(2026, 1, 15, 12, 1, tzinfo=UTC)},
    ),
    "webhook_events": (
        "INSERT INTO webhook_events (id, provider, event_id, type, payload, received_at)"
        " VALUES (:id, 'simbank', 'evt_1', 'deposit.received', CAST('{}' AS jsonb), :now)",
        {},
    ),
}

FORBIDDEN = [
    ("withdrawals", "UPDATE withdrawals SET amount = amount + 1"),
    ("withdrawals", "UPDATE withdrawals SET user_id = :other"),
    ("withdrawals", "UPDATE withdrawals SET to_address = 'sim1elsewhere'"),
    ("withdrawals", "UPDATE withdrawals SET hold_entry_id = :other"),
    ("withdrawals", "UPDATE withdrawals SET fee = 0"),
    ("withdrawals", "UPDATE withdrawals SET initiated_by_type = 'agent'"),
    ("withdrawals", "UPDATE withdrawals SET initiated_by_id = :other"),
    ("deposits", "UPDATE deposits SET amount = amount + 1"),
    ("deposits", "UPDATE deposits SET provider_ref = 'dep_2'"),
    ("deposits", "UPDATE deposits SET asset_code = 'MXN'"),
    ("fx_quotes", "UPDATE fx_quotes SET buy_amount = buy_amount * 2"),
    ("fx_quotes", "UPDATE fx_quotes SET rate = '34.00'"),
    ("fx_quotes", "UPDATE fx_quotes SET expires_at = :far"),
    ("fx_quotes", "UPDATE fx_quotes SET user_id = :other"),
    ("webhook_events", "UPDATE webhook_events SET type = 'payout.completed'"),
    ("webhook_events", "UPDATE webhook_events SET event_id = 'evt_2'"),
    ("webhook_events", "UPDATE webhook_events SET provider = 'simcustody'"),
]

ALLOWED = [
    ("withdrawals", "UPDATE withdrawals SET status = 'submitting', updated_at = :far"),
    ("deposits", "UPDATE deposits SET status = 'failed', updated_at = :far"),
    ("fx_quotes", "UPDATE fx_quotes SET status = 'used'"),
    (
        "webhook_events",
        "UPDATE webhook_events SET processed_at = :far, outcome = 'processed', redacted_at = :far",
    ),
]

PARAMETERS = {"other": uuid.UUID(int=7), "far": datetime(2026, 2, 1, tzinfo=UTC)}


async def a_row(db: Database, table: str) -> None:
    statement, values = ROWS[table]
    async with db.transaction() as session:
        await session.execute(text(statement), {"id": new_id(), "now": NOW, **values})


def needed(statement: str) -> dict[str, Any]:
    return {name: value for name, value in PARAMETERS.items() if f":{name}" in statement}


@pytest.mark.parametrize("table", sorted(UPDATABLE))
async def test_the_application_role_updates_exactly_the_columns_it_has_a_reason_to(
    db: Database, table: str
) -> None:
    async with db.transaction() as session:
        whole_table = (
            await session.execute(
                text(
                    "SELECT privilege_type FROM information_schema.role_table_grants"
                    " WHERE grantee = :role AND table_schema = 'public' AND table_name = :t"
                ),
                {"role": postgres.APP_ROLE, "t": table},
            )
        ).scalars()
        columns = (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.column_privileges"
                    " WHERE grantee = :role AND table_schema = 'public' AND table_name = :t"
                    " AND privilege_type = 'UPDATE'"
                ),
                {"role": postgres.APP_ROLE, "t": table},
            )
        ).scalars()
        granted_on_table, granted_on_columns = set(whole_table), set(columns)

    # No UPDATE on the table as a whole: that would cover every column.
    assert "UPDATE" not in granted_on_table
    assert {"SELECT", "INSERT"} <= granted_on_table
    assert granted_on_columns == UPDATABLE[table]


@pytest.mark.parametrize(("table", "statement"), FORBIDDEN, ids=[s for _, s in FORBIDDEN])
async def test_the_application_role_cannot_rewrite_what_a_row_was_written_with(
    db: Database, table: str, statement: str
) -> None:
    await a_row(db, table)

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text(statement), needed(statement))

    assert sqlstate_of(failure.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(("table", "statement"), ALLOWED, ids=[t for t, _ in ALLOWED])
async def test_the_application_role_can_still_move_a_row_through_its_life(
    db: Database, table: str, statement: str
) -> None:
    await a_row(db, table)

    async with db.transaction() as session:
        changed = await session.execute(text(statement), needed(statement))

    assert changed.rowcount == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("table", sorted(UPDATABLE))
async def test_the_application_role_can_still_lock_a_row_it_is_about_to_change(
    db: Database, table: str
) -> None:
    # The state machines read a row FOR UPDATE before they move it. That needs the
    # privilege to update some column, and no more.
    await a_row(db, table)

    async with db.transaction() as session:
        locked = await session.execute(text(f"SELECT id FROM {table} FOR UPDATE"))  # noqa: S608

    assert len(locked.all()) == 1


# --- temporary tables ----------------------------------------------------------------------------


async def _can_create_temporary_tables(url: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            allowed = await connection.execute(
                text("SELECT has_database_privilege(current_user, current_database(), 'TEMPORARY')")
            )
            return bool(allowed.scalar_one())
    finally:
        await engine.dispose()


async def _create_temporary_table(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TEMPORARY TABLE postings (id int)"))
    finally:
        await engine.dispose()


def test_the_migration_takes_temporary_tables_from_the_application_role(
    database: postgres.TestDatabase,
) -> None:
    # A privilege on a database is not copied with it, so a test database, which is a
    # copy of the migrated template, starts with the default again. The revision is run
    # here, on this database, to see what it does to the one it is applied to.
    assert asyncio.run(_can_create_temporary_tables(database.app_url)) is True
    asyncio.run(postgres.migrate(database.owner_url, "0016", down=True))
    asyncio.run(postgres.migrate(database.owner_url, "0017"))

    assert asyncio.run(_can_create_temporary_tables(database.app_url)) is False
    with pytest.raises(DBAPIError) as failure:
        asyncio.run(_create_temporary_table(database.app_url))
    assert sqlstate_of(failure.value) == INSUFFICIENT_PRIVILEGE
    # The owner creates them still: migrations and maintenance are its work.
    assert asyncio.run(_can_create_temporary_tables(database.owner_url)) is True


def test_undoing_the_migration_gives_temporary_tables_back(
    database: postgres.TestDatabase,
) -> None:
    asyncio.run(postgres.migrate(database.owner_url, "0016", down=True))
    asyncio.run(postgres.migrate(database.owner_url, "0017"))

    asyncio.run(postgres.migrate(database.owner_url, "0016", down=True))

    assert asyncio.run(_can_create_temporary_tables(database.app_url)) is True

"""What the database itself guarantees about quotes and conversions, whatever the
application does."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor.platform.db import (
    CHECK_VIOLATION,
    UNIQUE_VIOLATION,
    Database,
    constraint_of,
    sqlstate_of,
)
from corridor.platform.ids import new_id
from tests.support import postgres

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
INSUFFICIENT_PRIVILEGE = "42501"
FOREIGN_KEY_VIOLATION = "23503"
APPEND_ONLY = "CR001"
INSERT_QUOTE = text(
    "INSERT INTO fx_quotes (id, user_id, sell_asset, buy_asset, sell_amount, buy_amount,"
    " rate, mid, status, expires_at, created_at)"
    " VALUES (:id, :user, :sell_asset, :buy_asset, :sell_amount, :buy_amount, :rate, :mid,"
    " :status, :expires_at, :now)"
)
INSERT_CONVERSION = text(
    "INSERT INTO fx_conversions (id, quote_id, user_id, entry_id, created_at)"
    " VALUES (:id, :quote, :user, :entry, :now)"
)


async def add_quote(db: Database, **overrides: Any) -> uuid.UUID:
    values = {
        "id": new_id(),
        "user": new_id(),
        "sell_asset": "USD",
        "buy_asset": "MXN",
        "sell_amount": 100_00,
        "buy_amount": 1_716_37,
        "rate": "17.16375",
        "mid": "17.25",
        "status": "open",
        "expires_at": NOW + timedelta(seconds=30),
        "now": NOW,
        **overrides,
    }
    async with db.transaction() as session:
        await session.execute(INSERT_QUOTE, values)
    quote_id: uuid.UUID = values["id"]
    return quote_id


async def add_conversion(db: Database, quote: uuid.UUID, **overrides: Any) -> uuid.UUID:
    values = {"id": new_id(), "quote": quote, "user": new_id(), "entry": new_id(), "now": NOW}
    values.update(overrides)
    async with db.transaction() as session:
        await session.execute(INSERT_CONVERSION, values)
    conversion_id: uuid.UUID = values["id"]
    return conversion_id


async def grants(db: Database, table: str) -> str:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)"
                " FROM information_schema.role_table_grants"
                " WHERE grantee = :role AND table_schema = 'public' AND table_name = :table"
            ),
            {"role": postgres.APP_ROLE, "table": table},
        )
        return str(rows.scalar_one())


async def test_the_application_role_may_add_read_advance_and_purge_quotes(
    db: Database,
) -> None:
    # On the table as a whole. A quote is advanced by its status alone, which is granted
    # as a column, and the security tests say so.
    assert await grants(db, "fx_quotes") == "DELETE,INSERT,SELECT"


async def test_the_application_role_may_add_and_read_conversions_and_nothing_else(
    db: Database,
) -> None:
    assert await grants(db, "fx_conversions") == "INSERT,SELECT"


@pytest.mark.parametrize(
    "statement",
    [
        "TRUNCATE fx_quotes CASCADE",
        "UPDATE fx_conversions SET user_id = user_id",
        "DELETE FROM fx_conversions",
        "TRUNCATE fx_conversions",
    ],
)
async def test_the_application_role_cannot_empty_the_quotes_or_rewrite_a_conversion(
    db: Database, statement: str
) -> None:
    await add_conversion(db, await add_quote(db))

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == INSUFFICIENT_PRIVILEGE


async def test_a_quote_that_was_converted_cannot_be_deleted(db: Database) -> None:
    quote = await add_quote(db, status="used")
    await add_conversion(db, quote)

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text("DELETE FROM fx_quotes WHERE id = :id"), {"id": quote})

    assert sqlstate_of(failure.value) == FOREIGN_KEY_VIOLATION
    assert constraint_of(failure.value) == "fk_fx_conversions_quote_id_fx_quotes"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE fx_conversions SET user_id = user_id",
        "UPDATE fx_conversions SET entry_id = entry_id WHERE false",
        "DELETE FROM fx_conversions",
    ],
)
async def test_even_the_owner_cannot_rewrite_or_remove_a_conversion(
    db: Database, owner_db: Database, statement: str
) -> None:
    # The owner holds every privilege on the table. The trigger is what stops it.
    await add_conversion(db, await add_quote(db))

    with pytest.raises(DBAPIError) as failure:
        async with owner_db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == APPEND_ONLY
    assert await grants(db, "fx_conversions") == "INSERT,SELECT"


async def test_a_quote_converts_into_one_conversion_only(db: Database) -> None:
    quote = await add_quote(db)
    await add_conversion(db, quote)

    with pytest.raises(DBAPIError) as failure:
        await add_conversion(db, quote)

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == "uq_fx_conversions_quote_id"


async def test_a_journal_entry_belongs_to_one_conversion_only(db: Database) -> None:
    entry = new_id()
    await add_conversion(db, await add_quote(db), entry=entry)

    with pytest.raises(DBAPIError) as failure:
        await add_conversion(db, await add_quote(db), entry=entry)

    assert constraint_of(failure.value) == "uq_fx_conversions_entry_id"


async def test_a_conversion_needs_a_quote_that_exists(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        await add_conversion(db, new_id())

    assert sqlstate_of(failure.value) == FOREIGN_KEY_VIOLATION
    assert constraint_of(failure.value) == "fk_fx_conversions_quote_id_fx_quotes"


async def test_ids_are_used_once(db: Database) -> None:
    quote = await add_quote(db)
    conversion = await add_conversion(db, quote)

    with pytest.raises(DBAPIError) as quote_failure:
        await add_quote(db, id=quote)
    with pytest.raises(DBAPIError) as conversion_failure:
        await add_conversion(db, await add_quote(db), id=conversion)

    assert constraint_of(quote_failure.value) == "pk_fx_quotes"
    assert constraint_of(conversion_failure.value) == "pk_fx_conversions"


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"sell_amount": 0}, "ck_fx_quotes_sell_amount"),
        ({"buy_amount": 0}, "ck_fx_quotes_buy_amount"),
        ({"buy_amount": -1}, "ck_fx_quotes_buy_amount"),
        ({"buy_asset": "USD"}, "ck_fx_quotes_distinct_assets"),
        ({"status": "expired"}, "ck_fx_quotes_status"),
        ({"rate": "1e3"}, "ck_fx_quotes_rate"),
        ({"rate": "-17.25"}, "ck_fx_quotes_rate"),
        ({"rate": ""}, "ck_fx_quotes_rate"),
        ({"mid": "17,25"}, "ck_fx_quotes_mid"),
        ({"expires_at": NOW}, "ck_fx_quotes_expires_at"),
    ],
)
async def test_a_row_that_could_not_be_a_quote_is_refused(
    db: Database, overrides: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        await add_quote(db, **overrides)

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == constraint

"""What the database itself guarantees about transfer rows, whatever the application does."""

import uuid
from datetime import UTC, datetime
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
INSERT = text(
    "INSERT INTO transfers (id, sender_id, recipient_id, asset_code, amount, fee, status,"
    " entry_id, memo, initiated_by_type, initiated_by_id, created_at)"
    " VALUES (:id, :sender, :recipient, 'USD', :amount, :fee, :status, :entry, :memo,"
    " :by_type, :by_id, :now)"
)


async def add_row(db: Database, **overrides: Any) -> uuid.UUID:
    sender = new_id()
    values = {
        "id": new_id(),
        "sender": sender,
        "recipient": new_id(),
        "amount": 5_00,
        "fee": 0,
        "status": "completed",
        "entry": new_id(),
        "memo": None,
        "by_type": "user",
        "by_id": sender,
        "now": NOW,
        **overrides,
    }
    async with db.transaction() as session:
        await session.execute(INSERT, values)
    transfer_id: uuid.UUID = values["id"]
    return transfer_id


async def test_the_application_role_may_add_and_read_transfers_and_nothing_else(
    db: Database,
) -> None:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)"
                " FROM information_schema.role_table_grants"
                " WHERE grantee = :role AND table_schema = 'public'"
                " AND table_name = 'transfers'"
            ),
            {"role": postgres.APP_ROLE},
        )

        assert rows.scalar_one() == "INSERT,SELECT"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE transfers SET amount = amount + 1",
        "UPDATE transfers SET memo = memo WHERE false",
        "DELETE FROM transfers",
        "TRUNCATE transfers",
    ],
)
async def test_the_application_role_cannot_rewrite_or_remove_a_transfer(
    db: Database, statement: str
) -> None:
    await add_row(db)

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == INSUFFICIENT_PRIVILEGE
    async with db.transaction() as session:
        rows = await session.execute(text("SELECT amount FROM transfers"))
        assert rows.scalars().all() == [5_00]


async def test_a_journal_entry_belongs_to_one_transfer_only(db: Database) -> None:
    entry = new_id()
    await add_row(db, entry=entry)

    with pytest.raises(DBAPIError) as failure:
        await add_row(db, entry=entry)

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == "uq_transfers_entry_id"


async def test_a_transfer_id_is_used_once(db: Database) -> None:
    transfer_id = await add_row(db)

    with pytest.raises(DBAPIError) as failure:
        await add_row(db, id=transfer_id)

    assert constraint_of(failure.value) == "pk_transfers"


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"amount": 0}, "ck_transfers_amount"),
        ({"amount": -1}, "ck_transfers_amount"),
        ({"fee": -1}, "ck_transfers_fee"),
        ({"status": "pending"}, "ck_transfers_status"),
        ({"memo": "x" * 141}, "ck_transfers_memo"),
        ({"by_type": "admin"}, "ck_transfers_initiated_by_type"),
    ],
)
async def test_a_row_that_could_not_be_a_transfer_is_refused(
    db: Database, overrides: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        await add_row(db, **overrides)

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == constraint


async def test_a_transfer_to_oneself_cannot_be_stored(db: Database) -> None:
    user = new_id()

    with pytest.raises(DBAPIError) as failure:
        await add_row(db, sender=user, recipient=user)

    assert constraint_of(failure.value) == "ck_transfers_distinct_parties"


async def test_a_memo_of_140_characters_and_a_zero_fee_are_stored(db: Database) -> None:
    await add_row(db, memo="x" * 140, fee=0)

    async with db.transaction() as session:
        rows = await session.execute(text("SELECT char_length(memo), fee FROM transfers"))
        assert tuple(rows.one()) == (140, 0)


async def test_both_sides_of_a_transfer_are_indexed_for_listing(db: Database) -> None:
    async with db.transaction() as session:
        rows = await session.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'transfers'")
        )

        assert set(rows.scalars()) == {
            "pk_transfers",
            "uq_transfers_entry_id",
            "ix_transfers_sender_id_id",
            "ix_transfers_recipient_id_id",
        }

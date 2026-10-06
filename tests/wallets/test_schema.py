"""What the database itself guarantees about wallet rows, whatever the application does."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor import wallets
from corridor.platform.db import UNIQUE_VIOLATION, Database, constraint_of, sqlstate_of
from corridor.platform.ids import new_id
from tests.support import postgres

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
INSUFFICIENT_PRIVILEGE = "42501"
INSERT = text(
    "INSERT INTO wallet_accounts (user_id, asset_code, available_account_id, held_account_id,"
    " created_at) VALUES (:user, :asset, :available, :held, :now)"
)


async def add_row(
    db: Database,
    *,
    user: uuid.UUID | None = None,
    asset: str = "USD",
    available: uuid.UUID | None = None,
    held: uuid.UUID | None = None,
) -> None:
    async with db.transaction() as session:
        await session.execute(
            INSERT,
            {
                "user": user or new_id(),
                "asset": asset,
                "available": available or new_id(),
                "held": held or new_id(),
                "now": NOW,
            },
        )


async def test_the_application_role_may_add_and_read_wallet_rows_and_nothing_else(
    db: Database,
) -> None:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)"
                " FROM information_schema.role_table_grants"
                " WHERE grantee = :role AND table_schema = 'public'"
                " AND table_name = 'wallet_accounts'"
            ),
            {"role": postgres.APP_ROLE},
        )

        assert rows.scalar_one() == "INSERT,SELECT"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE wallet_accounts SET held_account_id = available_account_id",
        "UPDATE wallet_accounts SET user_id = user_id WHERE false",
        "DELETE FROM wallet_accounts",
        "TRUNCATE wallet_accounts",
    ],
)
async def test_the_application_role_cannot_rewrite_or_remove_a_wallet_row(
    db: Database, user_id: uuid.UUID, statement: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == INSUFFICIENT_PRIVILEGE
    async with db.transaction() as session:
        assert len(await wallets.get_wallets(session, user_id)) > 0


async def test_a_user_has_one_wallet_row_per_asset(db: Database) -> None:
    user = new_id()
    await add_row(db, user=user)

    with pytest.raises(DBAPIError) as failure:
        await add_row(db, user=user)

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == "pk_wallet_accounts"


@pytest.mark.parametrize(
    ("column", "constraint"),
    [
        ("available", "uq_wallet_accounts_available_account_id"),
        ("held", "uq_wallet_accounts_held_account_id"),
    ],
)
async def test_a_ledger_account_belongs_to_one_wallet_only(
    db: Database, column: str, constraint: str
) -> None:
    account = new_id()
    await add_row(db, **{column: account})

    with pytest.raises(DBAPIError) as failure:
        await add_row(db, **{column: account})

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == constraint


async def test_a_wallets_available_and_held_accounts_are_two_accounts(db: Database) -> None:
    account = new_id()

    with pytest.raises(DBAPIError) as failure:
        await add_row(db, available=account, held=account)

    assert constraint_of(failure.value) == "ck_wallet_accounts_distinct_accounts"

"""What the database refuses on its own, with no help from the application.

These tests write to the ledger tables with plain SQL, the way a buggy or hostile caller
would, and check that PostgreSQL says no.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.platform.db import (
    CHECK_VIOLATION,
    UNIQUE_VIOLATION,
    Database,
    constraint_of,
    sqlstate_of,
)
from corridor.platform.ids import new_id
from corridor.platform.money import ASSETS
from tests.support import postgres

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
APPEND_ONLY = "CR001"
TOO_FEW_POSTINGS = "CR002"
UNBALANCED = "CR003"
FOREIGN_KEY_VIOLATION = "23503"
INSUFFICIENT_PRIVILEGE = "42501"

# These tests write rows by hand, bypassing the service, so what they leave behind is not
# what the verifier expects of the service (accounts without balance rows, for one).
pytestmark = pytest.mark.corrupts_ledger


async def add_account(
    session: AsyncSession,
    kind: str = "suspense",
    asset: str = "USD",
    *,
    owner: uuid.UUID | None = None,
    provider: str | None = None,
) -> uuid.UUID:
    chart = {
        "user_available": ("liability", "C", True),
        "user_held": ("liability", "C", True),
        "user_receivable": ("asset", "D", True),
        "bank_settlement": ("asset", "D", False),
        "custody_omnibus": ("asset", "D", False),
        "suspense": ("liability", "C", False),
        "fx_position": ("asset", "D", False),
        "fee_revenue": ("revenue", "C", False),
        "provider_fee_expense": ("expense", "D", False),
    }
    category, normal_side, constrained = chart[kind]
    account = new_id()
    await session.execute(
        text(
            "INSERT INTO ledger_accounts (id, asset_code, kind, category, normal_side, owner_id,"
            " provider, is_constrained, created_at) VALUES (:id, :asset, :kind, :category,"
            " :normal_side, :owner, :provider, :constrained, :now)"
        ),
        {
            "id": account,
            "asset": asset,
            "kind": kind,
            "category": category,
            "normal_side": normal_side,
            "owner": owner,
            "provider": provider,
            "constrained": constrained,
            "now": NOW,
        },
    )
    return account


async def add_entry(
    session: AsyncSession,
    *,
    source_id: str | None = None,
    kind: str = "test",
    reverses: uuid.UUID | None = None,
) -> uuid.UUID:
    entry = new_id()
    await session.execute(
        text(
            "INSERT INTO journal_entries (id, kind, source_type, source_id, metadata,"
            " reverses_entry_id, posted_at) VALUES (:id, :kind, 'test', :source_id,"
            " CAST('{}' AS jsonb), :reverses, :now)"
        ),
        {
            "id": entry,
            "kind": kind,
            "source_id": source_id or str(entry),
            "reverses": reverses,
            "now": NOW,
        },
    )
    return entry


async def add_posting(
    session: AsyncSession,
    entry: uuid.UUID,
    account: uuid.UUID,
    direction: str,
    amount: int,
    asset: str = "USD",
) -> None:
    await session.execute(
        text(
            "INSERT INTO postings (entry_id, account_id, asset_code, direction, amount)"
            " VALUES (:entry, :account, :asset, :direction, :amount)"
        ),
        {
            "entry": entry,
            "account": account,
            "asset": asset,
            "direction": direction,
            "amount": amount,
        },
    )


async def balanced_entry(db: Database) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Commit a balanced two-posting entry; return the entry and its two accounts."""
    async with db.transaction() as session:
        debit = await add_account(session, "bank_settlement", provider="simbank")
        credit = await add_account(session, "suspense")
        entry = await add_entry(session)
        await add_posting(session, entry, debit, "D", 500)
        await add_posting(session, entry, credit, "C", 500)
    return entry, debit, credit


async def count(db: Database, table: str) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


# --- an entry must balance -------------------------------------------------------------------


async def test_a_balanced_entry_commits(db: Database) -> None:
    await balanced_entry(db)
    assert await count(db, "journal_entries") == 1
    assert await count(db, "postings") == 2


@pytest.mark.parametrize(("debit_amount", "credit_amount"), [(500, 499), (499, 500)])
async def test_an_unbalanced_entry_is_refused_at_commit(
    db: Database, debit_amount: int, credit_amount: int
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            debit = await add_account(session, "bank_settlement", provider="simbank")
            credit = await add_account(session, "suspense")
            entry = await add_entry(session)
            await add_posting(session, entry, debit, "D", debit_amount)
            await add_posting(session, entry, credit, "C", credit_amount)

    assert sqlstate_of(failure.value) == UNBALANCED
    assert await count(db, "journal_entries") == 0
    assert await count(db, "postings") == 0


async def test_the_check_runs_at_commit_so_an_entry_can_be_built_up_in_steps(db: Database) -> None:
    async with db.transaction() as session:
        debit = await add_account(session, "bank_settlement", provider="simbank")
        credit = await add_account(session, "suspense")
        entry = await add_entry(session)
        await add_posting(session, entry, debit, "D", 500)
        # Unbalanced at this instant, and allowed to be: the transaction is not finished.
        await session.execute(text("SELECT 1"))
        await add_posting(session, entry, credit, "C", 500)

    assert await count(db, "postings") == 2


async def test_each_asset_in_an_entry_must_balance_on_its_own(db: Database) -> None:
    async def accounts(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        return (
            await add_account(session, "suspense", "USD"),
            await add_account(session, "fx_position", "USD"),
            await add_account(session, "fx_position", "USDC"),
            await add_account(session, "suspense", "USDC"),
        )

    # A conversion: 10.00 USD out, 9.990000 USDC in. Balanced in each asset.
    async with db.transaction() as session:
        usd_out, usd_position, usdc_position, usdc_in = await accounts(session)
        entry = await add_entry(session)
        await add_posting(session, entry, usd_out, "D", 1000, "USD")
        await add_posting(session, entry, usd_position, "C", 1000, "USD")
        await add_posting(session, entry, usdc_position, "D", 9_990_000, "USDC")
        await add_posting(session, entry, usdc_in, "C", 9_990_000, "USDC")

    # The same totals across assets do not make an entry balanced.
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            entry = await add_entry(session)
            await add_posting(session, entry, usd_out, "D", 1000, "USD")
            await add_posting(session, entry, usdc_in, "C", 1000, "USDC")
    assert sqlstate_of(failure.value) == UNBALANCED


@pytest.mark.parametrize("postings", [0, 1])
async def test_an_entry_needs_at_least_two_postings(db: Database, postings: int) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            account = await add_account(session)
            entry = await add_entry(session)
            for _ in range(postings):
                await add_posting(session, entry, account, "D", 500)

    assert sqlstate_of(failure.value) == TOO_FEW_POSTINGS
    assert await count(db, "journal_entries") == 0


async def test_a_posting_cannot_be_added_to_an_entry_that_already_balanced(db: Database) -> None:
    entry, debit, _ = await balanced_entry(db)

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_posting(session, entry, debit, "D", 1)

    assert sqlstate_of(failure.value) == UNBALANCED
    assert await count(db, "postings") == 2


# --- history is append-only ------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE postings SET amount = amount + 1",
        "DELETE FROM postings",
        "UPDATE journal_entries SET kind = 'edited'",
        "DELETE FROM journal_entries",
        "TRUNCATE postings",
        "TRUNCATE journal_entries CASCADE",
    ],
)
async def test_the_application_role_has_no_privilege_to_edit_history(
    db: Database, statement: str
) -> None:
    await balanced_entry(db)

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE postings SET amount = amount + 1",
        "UPDATE postings SET amount = amount + 1 WHERE false",
        "DELETE FROM postings",
        "UPDATE journal_entries SET kind = 'edited'",
        "DELETE FROM journal_entries WHERE false",
        "TRUNCATE postings",
        "TRUNCATE journal_entries CASCADE",
    ],
)
async def test_even_the_owner_cannot_edit_history(
    db: Database, owner_db: Database, statement: str
) -> None:
    # The owner holds every privilege on these tables. The trigger is what stops it.
    await balanced_entry(db)

    with pytest.raises(DBAPIError) as failure:
        async with owner_db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == APPEND_ONLY
    assert "append-only" in str(failure.value)
    assert await count(db, "postings") == 2


async def test_the_application_role_holds_exactly_the_privileges_it_needs(db: Database) -> None:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type) AS privileges"
                " FROM information_schema.role_table_grants"
                " WHERE grantee = :role AND table_schema = 'public'"
                " AND table_name IN ('assets', 'ledger_accounts', 'account_balances',"
                " 'journal_entries', 'postings') GROUP BY table_name"
            ),
            {"role": postgres.APP_ROLE},
        )
        granted = {row.table_name: row.privileges for row in rows}

    assert granted == {
        "assets": "SELECT",
        "ledger_accounts": "INSERT,SELECT",
        "account_balances": "INSERT,SELECT,UPDATE",
        "journal_entries": "INSERT,SELECT",
        "postings": "INSERT,SELECT",
    }


# --- row-level rules -------------------------------------------------------------------------


async def test_a_posting_cannot_name_an_asset_other_than_its_accounts(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            usd_account = await add_account(session, "suspense", "USD")
            other = await add_account(session, "fx_position", "USDC")
            entry = await add_entry(session)
            await add_posting(session, entry, usd_account, "D", 500, "USDC")
            await add_posting(session, entry, other, "C", 500, "USDC")

    assert sqlstate_of(failure.value) == FOREIGN_KEY_VIOLATION
    assert constraint_of(failure.value) == "fk_postings_account_id_asset_code_ledger_accounts"


@pytest.mark.parametrize("amount", [0, -1])
async def test_a_posting_amount_must_be_positive(db: Database, amount: int) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            account = await add_account(session)
            entry = await add_entry(session)
            await add_posting(session, entry, account, "D", amount)

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == "ck_postings_amount_positive"


async def test_a_posting_direction_is_debit_or_credit(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            account = await add_account(session)
            entry = await add_entry(session)
            await add_posting(session, entry, account, "X", 500)

    assert constraint_of(failure.value) == "ck_postings_direction"


async def test_a_cached_balance_cannot_be_negative(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            account = await add_account(session, "user_available", owner=new_id())
            await session.execute(
                text(
                    "INSERT INTO account_balances (account_id, balance, last_posting_seq, updated_at)"
                    " VALUES (:account, -1, 0, :now)"
                ),
                {"account": account, "now": NOW},
            )

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == "ck_account_balances_not_negative"


async def test_one_business_event_cannot_be_recorded_twice(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_entry(session, source_id="transfer-1", kind="transfer")
            await add_entry(session, source_id="transfer-1", kind="transfer")

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == "uq_journal_entries_source"


async def test_an_entry_cannot_be_reversed_twice(db: Database) -> None:
    original, _, _ = await balanced_entry(db)

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_entry(session, kind="reversal", reverses=original)
            await add_entry(session, kind="reversal", reverses=original)

    assert constraint_of(failure.value) == "uq_journal_entries_reverses_entry_id"


async def test_there_is_one_system_account_per_kind_and_asset(db: Database) -> None:
    # Owner and provider are both null here. An ordinary unique constraint would treat
    # those rows as distinct and let a second fee account in.
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_account(session, "fee_revenue", "USD")
            await add_account(session, "fee_revenue", "USD")

    assert constraint_of(failure.value) == "uq_ledger_accounts_identity"


async def test_a_user_has_one_account_per_kind_and_asset(db: Database) -> None:
    owner = new_id()
    async with db.transaction() as session:
        await add_account(session, "user_available", "USD", owner=owner)
        await add_account(session, "user_available", "MXN", owner=owner)
        await add_account(session, "user_held", "USD", owner=owner)
        await add_account(session, "user_available", "USD", owner=new_id())

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_account(session, "user_available", "USD", owner=owner)
    assert constraint_of(failure.value) == "uq_ledger_accounts_identity"


@pytest.mark.parametrize(
    ("kind", "owner", "provider"),
    [
        ("user_available", None, None),  # a user account without a user
        ("user_available", "owner", "simbank"),
        ("bank_settlement", None, None),  # a settlement account without a provider
        ("fee_revenue", "owner", None),  # a system account that claims an owner
        ("made_up_kind", None, None),
    ],
)
async def test_an_account_must_have_the_shape_of_its_kind(
    db: Database, kind: str, owner: str | None, provider: str | None
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO ledger_accounts (id, asset_code, kind, category, normal_side,"
                    " owner_id, provider, is_constrained, created_at) VALUES (:id, 'USD', :kind,"
                    " 'liability', 'C', :owner, :provider, :constrained, :now)"
                ),
                {
                    "id": new_id(),
                    "kind": kind,
                    "owner": new_id() if owner else None,
                    "provider": provider,
                    "constrained": kind.startswith("user_"),
                    "now": NOW,
                },
            )

    assert constraint_of(failure.value) == "ck_ledger_accounts_kind"


async def test_a_user_account_cannot_be_marked_unconstrained(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO ledger_accounts (id, asset_code, kind, category, normal_side,"
                    " owner_id, is_constrained, created_at) VALUES (:id, 'USD', 'user_available',"
                    " 'liability', 'C', :owner, false, :now)"
                ),
                {"id": new_id(), "owner": new_id(), "now": NOW},
            )

    assert constraint_of(failure.value) == "ck_ledger_accounts_kind"


async def test_an_account_must_be_in_a_known_asset(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_account(session, "suspense", "DOGE")

    assert sqlstate_of(failure.value) == FOREIGN_KEY_VIOLATION


async def test_the_assets_table_matches_the_assets_the_code_knows(db: Database) -> None:
    async with db.transaction() as session:
        rows = await session.execute(text("SELECT code, kind, decimals FROM assets"))
        stored = {row.code: (row.kind, row.decimals) for row in rows}

    assert stored == {asset.code: (asset.kind, asset.decimals) for asset in ASSETS.values()}

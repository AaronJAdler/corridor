"""What the database itself guarantees about deposits, instructions, beneficiaries and
withdrawals, whatever the application does."""

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
from tests.support.providers import EXTERNAL_ADDRESS

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
INSUFFICIENT_PRIVILEGE = "42501"
APPEND_ONLY = "CR001"

INSERT_DEPOSIT = text(
    "INSERT INTO deposits (id, user_id, asset_code, amount, provider, provider_ref, kind,"
    " status, entry_id, tx_hash, created_at, updated_at)"
    " VALUES (:id, :user_id, 'USD', :amount, :provider, :provider_ref, :kind, :status,"
    " :entry_id, NULL, :now, :now)"
)
INSERT_WITHDRAWAL = text(
    "INSERT INTO withdrawals (id, user_id, asset_code, amount, fee, kind, beneficiary_id,"
    " to_address, status, provider, provider_fee, hold_entry_id, final_entry_id,"
    " initiated_by_type, initiated_by_id, created_at, updated_at)"
    " VALUES (:id, :user_id, 'USD', :amount, :fee, :kind, :beneficiary_id, :to_address,"
    " :status, 'simbank', :provider_fee, :hold_entry_id, :final_entry_id, :initiated_by_type,"
    " :initiated_by_id, :now, :now)"
)
INSERT_INSTRUCTION = text(
    "INSERT INTO deposit_instructions (user_id, asset_code, provider, provider_ref, details,"
    " created_at) VALUES (:user_id, :asset, :provider, :provider_ref, '{}', :now)"
)
INSERT_BENEFICIARY = text(
    "INSERT INTO beneficiaries (id, user_id, asset_code, provider, provider_ref, holder_name,"
    " account_mask, created_at)"
    " VALUES (:id, :user_id, 'USD', 'simbank', :provider_ref, 'Maria Silva', '••••6789', :now)"
)


async def add_deposit(db: Database, **overrides: Any) -> None:
    values = {
        "id": new_id(),
        "user_id": new_id(),
        "amount": 5_00,
        "provider": "simbank",
        "provider_ref": f"dep_{new_id().hex[-8:]}",
        "kind": "bank",
        "status": "completed",
        "entry_id": None,
        "now": NOW,
        **overrides,
    }
    async with db.transaction() as session:
        await session.execute(INSERT_DEPOSIT, values)


async def add_withdrawal(db: Database, **overrides: Any) -> None:
    values = {
        "id": new_id(),
        "user_id": new_id(),
        "amount": 5_00,
        "fee": 0,
        "kind": "bank",
        "beneficiary_id": new_id(),
        "to_address": None,
        "status": "held",
        "provider_fee": None,
        "hold_entry_id": new_id(),
        "final_entry_id": None,
        "initiated_by_type": "user",
        "initiated_by_id": new_id(),
        "now": NOW,
        **overrides,
    }
    async with db.transaction() as session:
        await session.execute(INSERT_WITHDRAWAL, values)


async def add_instruction(db: Database, **overrides: Any) -> None:
    values = {
        "user_id": new_id(),
        "asset": "USD",
        "provider": "simbank",
        "provider_ref": "va_1",
        "now": NOW,
        **overrides,
    }
    async with db.transaction() as session:
        await session.execute(INSERT_INSTRUCTION, values)


async def add_beneficiary(db: Database, **overrides: Any) -> None:
    values = {"id": new_id(), "user_id": new_id(), "provider_ref": "ben_1", "now": NOW, **overrides}
    async with db.transaction() as session:
        await session.execute(INSERT_BENEFICIARY, values)


@pytest.mark.parametrize(
    ("table", "privileges"),
    [
        ("deposit_instructions", "INSERT,SELECT"),
        ("beneficiaries", "INSERT,SELECT"),
        # On the table as a whole. What it may update in these two is granted column by
        # column, and the security tests say which columns.
        ("deposits", "INSERT,SELECT"),
        ("withdrawals", "INSERT,SELECT"),
    ],
)
async def test_the_application_role_may_do_to_each_table_what_its_rows_need_and_no_more(
    db: Database, table: str, privileges: str
) -> None:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)"
                " FROM information_schema.role_table_grants"
                " WHERE grantee = :role AND table_schema = 'public' AND table_name = :table"
            ),
            {"role": postgres.APP_ROLE, "table": table},
        )

        assert rows.scalar_one() == privileges


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM deposits",
        "DELETE FROM withdrawals",
        "DELETE FROM beneficiaries",
        "DELETE FROM deposit_instructions",
        "UPDATE beneficiaries SET provider_ref = 'ben_other'",
        "UPDATE deposit_instructions SET user_id = user_id WHERE false",
        "TRUNCATE withdrawals",
    ],
)
async def test_the_application_role_cannot_remove_a_row_or_rewrite_a_write_once_one(
    db: Database, statement: str
) -> None:
    await add_deposit(db)
    await add_withdrawal(db)
    await add_beneficiary(db)
    await add_instruction(db)

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == INSUFFICIENT_PRIVILEGE


async def test_one_provider_deposit_is_one_row(db: Database) -> None:
    await add_deposit(db, provider_ref="dep_1")
    await add_deposit(db, provider_ref="dep_1", provider="simcustody", kind="chain")

    with pytest.raises(DBAPIError) as failure:
        await add_deposit(db, provider_ref="dep_1")

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == "uq_deposits_provider_provider_ref"


async def test_a_user_has_one_instruction_per_asset_and_an_account_has_one_user(
    db: Database,
) -> None:
    user = new_id()
    await add_instruction(db, user_id=user)
    await add_instruction(db, user_id=user, asset="MXN", provider_ref="va_2")

    with pytest.raises(DBAPIError) as same_asset:
        await add_instruction(db, user_id=user, provider_ref="va_3")
    with pytest.raises(DBAPIError) as same_account:
        await add_instruction(db, provider_ref="va_1")

    assert constraint_of(same_asset.value) == "pk_deposit_instructions"
    assert constraint_of(same_account.value) == "uq_deposit_instructions_provider_provider_ref"


async def test_a_provider_token_is_one_beneficiary(db: Database) -> None:
    await add_beneficiary(db)

    with pytest.raises(DBAPIError) as failure:
        await add_beneficiary(db)

    assert constraint_of(failure.value) == "uq_beneficiaries_provider_provider_ref"


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"amount": 0}, "ck_deposits_amount"),
        ({"kind": "card"}, "ck_deposits_kind"),
        ({"status": "under_review"}, "ck_deposits_status"),
    ],
)
async def test_a_row_that_could_not_be_a_deposit_is_refused(
    db: Database, overrides: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        await add_deposit(db, **overrides)

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == constraint


@pytest.mark.parametrize(
    "status",
    [
        "held",
        "under_review",
        "submitting",
        "submitted",
        "completed",
        "failed",
        "canceled",
        "released",
    ],
)
async def test_a_withdrawal_can_be_recorded_in_each_state_of_its_saga(
    db: Database, status: str
) -> None:
    await add_withdrawal(db, status=status)

    async with db.transaction() as session:
        stored = await session.execute(text("SELECT status FROM withdrawals"))
        assert stored.scalars().all() == [status]


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"amount": 0}, "ck_withdrawals_amount"),
        ({"fee": -1}, "ck_withdrawals_fee"),
        ({"provider_fee": -1}, "ck_withdrawals_provider_fee"),
        ({"kind": "card"}, "ck_withdrawals_kind"),
        ({"status": "pending"}, "ck_withdrawals_status"),
        ({"beneficiary_id": None}, "ck_withdrawals_target"),
        ({"to_address": EXTERNAL_ADDRESS}, "ck_withdrawals_target"),
        ({"kind": "chain"}, "ck_withdrawals_target"),
        ({"kind": "chain", "beneficiary_id": None}, "ck_withdrawals_target"),
        ({"kind": "chain", "to_address": EXTERNAL_ADDRESS}, "ck_withdrawals_target"),
        ({"initiated_by_type": "admin"}, "ck_withdrawals_initiated_by_type"),
    ],
)
async def test_a_row_that_could_not_be_a_withdrawal_is_refused(
    db: Database, overrides: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        await add_withdrawal(db, **overrides)

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == constraint


async def test_each_kind_of_withdrawal_is_stored_with_its_own_target(db: Database) -> None:
    await add_withdrawal(db)
    await add_withdrawal(db, kind="chain", beneficiary_id=None, to_address=EXTERNAL_ADDRESS)

    async with db.transaction() as session:
        kinds = await session.execute(text("SELECT kind FROM withdrawals ORDER BY kind"))
        assert kinds.scalars().all() == ["bank", "chain"]


async def test_a_journal_entry_belongs_to_one_deposit_and_one_withdrawal_only(
    db: Database,
) -> None:
    entry = new_id()
    await add_deposit(db, entry_id=entry)
    await add_withdrawal(db, hold_entry_id=entry, final_entry_id=entry)

    with pytest.raises(DBAPIError) as deposit:
        await add_deposit(db, entry_id=entry)
    with pytest.raises(DBAPIError) as hold:
        await add_withdrawal(db, hold_entry_id=entry)
    with pytest.raises(DBAPIError) as final:
        await add_withdrawal(db, final_entry_id=entry)

    assert constraint_of(deposit.value) == "uq_deposits_entry_id"
    assert constraint_of(hold.value) == "uq_withdrawals_hold_entry_id"
    assert constraint_of(final.value) == "uq_withdrawals_final_entry_id"


async def test_the_lists_and_the_sweeper_have_their_indexes(db: Database) -> None:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE tablename IN"
                " ('deposits', 'withdrawals', 'beneficiaries', 'deposit_instructions')"
            )
        )

        assert set(rows.scalars()) == {
            "pk_deposit_instructions",
            "uq_deposit_instructions_provider_provider_ref",
            "pk_deposits",
            "uq_deposits_provider_provider_ref",
            "uq_deposits_entry_id",
            "ix_deposits_user_id_id",
            "pk_beneficiaries",
            "uq_beneficiaries_provider_provider_ref",
            "ix_beneficiaries_user_id_id",
            "pk_withdrawals",
            "uq_withdrawals_hold_entry_id",
            "uq_withdrawals_final_entry_id",
            "ix_withdrawals_user_id_id",
            "ix_withdrawals_status_id",
        }


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE beneficiaries SET provider_ref = 'ben_other'",
        "UPDATE beneficiaries SET user_id = user_id WHERE false",
        "DELETE FROM beneficiaries",
        "UPDATE deposit_instructions SET user_id = gen_random_uuid()",
        "UPDATE deposit_instructions SET details = details WHERE false",
        "DELETE FROM deposit_instructions",
    ],
)
async def test_even_the_owner_cannot_rewrite_or_remove_a_write_once_row(
    db: Database, owner_db: Database, statement: str
) -> None:
    # The owner holds every privilege on these tables. The trigger is what stops it: a
    # beneficiary that could be rewritten is a payout that could be sent somewhere else,
    # and an instruction that could be, a deposit credited to somebody else.
    await add_beneficiary(db)
    await add_instruction(db)

    with pytest.raises(DBAPIError) as failure:
        async with owner_db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == APPEND_ONLY
    async with db.transaction() as session:
        tokens = await session.execute(text("SELECT provider_ref FROM beneficiaries"))
        accounts = await session.execute(text("SELECT provider_ref FROM deposit_instructions"))
        assert (tokens.scalars().all(), accounts.scalars().all()) == (["ben_1"], ["va_1"])


@pytest.mark.parametrize(
    "statement",
    ["UPDATE deposits SET status = 'returned'", "UPDATE withdrawals SET status = 'submitting'"],
)
async def test_the_rows_that_change_state_can_still_be_advanced(
    db: Database, statement: str
) -> None:
    await add_deposit(db)
    await add_withdrawal(db)

    async with db.transaction() as session:
        await session.execute(text(statement))

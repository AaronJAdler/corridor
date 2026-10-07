"""The verifier finds each kind of damage, and finds nothing in a sound ledger.

The damage is done as a superuser with triggers switched off, because neither the
application role nor the schema's own rules allow it. That is the situation the verifier
exists for: something got past both.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger
from corridor.ledger import AccountKind
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from tests.support import postgres
from tests.support.ledger import (
    UserAccounts,
    fund,
    funded_user,
    open_user,
    system_account,
    transfer_draft,
)

pytestmark = pytest.mark.corrupts_ledger

Damage = Callable[[AsyncSession], Awaitable[None]]


@pytest.fixture
async def books(db: Database) -> tuple[UserAccounts, UserAccounts, uuid.UUID]:
    """A small sound ledger: two users, a deposit, a transfer with a fee and a hold."""
    async with db.transaction() as session:
        maria = await funded_user(session, 100_00)
        joao = await open_user(session)
        fees = await system_account(session, AccountKind.FEE_REVENUE)
        await ledger.post_entry(
            session,
            ledger.EntryDraft(
                "transfer",
                "transfer",
                "tr_1",
                (
                    ledger.debit(maria.available, 25_30),
                    ledger.credit(joao.available, 25_00),
                    ledger.credit(fees, 30),
                ),
            ),
        )
        await ledger.post_entry(
            session,
            ledger.EntryDraft(
                "withdrawal_hold",
                "withdrawal",
                "wd_1",
                (ledger.debit(joao.available, 5_00), ledger.credit(joao.held, 5_00)),
            ),
        )
    return maria, joao, fees


async def findings(db: Database) -> list[ledger.Finding]:
    async with db.transaction() as session:
        return await ledger.verify(session)


async def damage(superuser_db: Database, statement: str, **parameters: object) -> None:
    async with superuser_db.transaction() as session:
        # Replica mode switches off ordinary triggers, including the append-only guards and
        # the balance check, for this transaction only.
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        await session.execute(text(statement), parameters)


@pytest.mark.usefixtures("books")
async def test_a_sound_ledger_has_no_findings(db: Database) -> None:
    assert await findings(db) == []


async def test_an_empty_ledger_has_no_findings(db: Database) -> None:
    assert await findings(db) == []


async def test_an_edited_posting_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    maria, _, _ = books
    await damage(
        superuser_db,
        "UPDATE postings SET amount = amount + 1 WHERE account_id = :account AND direction = 'D'",
        account=maria.available,
    )

    found = await findings(db)

    assert {finding.check for finding in found} == {
        "unbalanced_entry",
        "balance_mismatch",
        "broken_balance_chain",
    }
    unbalanced = next(finding for finding in found if finding.check == "unbalanced_entry")
    assert unbalanced.detail == "debits exceed credits by 1 in USD"
    assert {finding.subject for finding in found if finding.check != "unbalanced_entry"} == {
        str(maria.available)
    }


async def test_a_deleted_posting_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    _, _, fees = books
    await damage(superuser_db, "DELETE FROM postings WHERE account_id = :account", account=fees)

    found = await findings(db)

    assert [(finding.check, finding.detail) for finding in found] == [
        ("unbalanced_entry", "debits exceed credits by 30 in USD")
    ]


async def test_an_entry_left_with_one_posting_or_none_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    lonely, empty = new_id(), new_id()
    _, _, fees = books
    for entry, source in ((lonely, "lonely"), (empty, "empty")):
        await damage(
            superuser_db,
            "INSERT INTO journal_entries (id, kind, source_type, source_id, metadata, posted_at)"
            " VALUES (:id, 'test', 'test', :source, CAST('{}' AS jsonb), now())",
            id=entry,
            source=source,
        )
    await damage(
        superuser_db,
        "INSERT INTO postings (entry_id, account_id, asset_code, direction, amount)"
        " VALUES (:entry, :account, 'USD', 'C', 7)",
        entry=lonely,
        account=fees,
    )

    found = await findings(db)

    assert {(f.check, f.subject, f.detail) for f in found if f.check == "too_few_postings"} == {
        ("too_few_postings", str(lonely), "entry has 1 posting(s)"),
        ("too_few_postings", str(empty), "entry has 0 posting(s)"),
    }
    assert ("unbalanced_entry", str(lonely)) in {(f.check, f.subject) for f in found}


async def test_a_cached_balance_that_drifted_from_its_postings_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    maria, _, _ = books
    await damage(
        superuser_db,
        "UPDATE account_balances SET balance = balance + 1 WHERE account_id = :account",
        account=maria.available,
    )

    assert [(f.check, f.subject, f.detail) for f in await findings(db)] == [
        (
            "balance_mismatch",
            str(maria.available),
            "cached balance is 7471 but postings sum to 7470",
        )
    ]


async def test_a_negative_cached_balance_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    maria, _, _ = books
    async with superuser_db.transaction() as session:
        await session.execute(
            text("ALTER TABLE account_balances DROP CONSTRAINT ck_account_balances_not_negative")
        )
        await session.execute(
            text("UPDATE account_balances SET balance = -1 WHERE account_id = :account"),
            {"account": maria.available},
        )

    found = await findings(db)

    assert ("negative_balance", str(maria.available), "cached balance is -1") in {
        (f.check, f.subject, f.detail) for f in found
    }


async def test_a_constrained_account_without_a_balance_row_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    maria, _, _ = books
    await damage(
        superuser_db, "DELETE FROM account_balances WHERE account_id = :account", account=maria.held
    )

    assert [(f.check, f.subject) for f in await findings(db)] == [
        ("missing_balance_row", str(maria.held))
    ]


async def test_a_balance_row_on_a_system_account_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    _, _, fees = books
    await damage(
        superuser_db,
        "INSERT INTO account_balances (account_id, balance, last_posting_seq, updated_at)"
        " SELECT :account, 30, max(seq), now() FROM postings WHERE account_id = :account",
        account=fees,
    )

    assert [(f.check, f.subject) for f in await findings(db)] == [
        ("unexpected_balance_row", str(fees))
    ]


async def test_a_wrong_running_balance_on_a_posting_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    _, joao, _ = books
    await damage(
        superuser_db,
        "UPDATE postings SET balance_after = balance_after + 1"
        " WHERE seq = (SELECT min(seq) FROM postings WHERE account_id = :account)",
        account=joao.available,
    )

    found = await findings(db)

    assert [(f.check, f.subject) for f in found] == [("broken_balance_chain", str(joao.available))]
    assert "records balance 2501 where the postings before it give 2500" in found[0].detail


async def test_a_missing_running_balance_on_a_constrained_account_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    _, joao, _ = books
    await damage(
        superuser_db,
        "UPDATE postings SET balance_after = NULL WHERE account_id = :account",
        account=joao.held,
    )

    found = await findings(db)

    assert [(f.check, f.subject) for f in found] == [("broken_balance_chain", str(joao.held))]
    assert "records balance null" in found[0].detail


async def test_a_running_balance_on_a_system_account_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    _, _, fees = books
    await damage(
        superuser_db,
        "UPDATE postings SET balance_after = 30 WHERE account_id = :account",
        account=fees,
    )

    assert [(f.check, f.subject) for f in await findings(db)] == [
        ("stray_balance_after", str(fees))
    ]


async def test_a_balance_row_that_points_at_the_wrong_posting_is_found(
    db: Database, superuser_db: Database, books: tuple[UserAccounts, UserAccounts, uuid.UUID]
) -> None:
    maria, _, _ = books
    await damage(
        superuser_db,
        "UPDATE account_balances SET last_posting_seq = last_posting_seq - 1 WHERE account_id = :account",
        account=maria.available,
    )

    assert [(f.check, f.subject) for f in await findings(db)] == [
        ("stale_balance_pointer", str(maria.available))
    ]


async def test_suspense_that_holds_less_than_nothing_is_found(db: Database) -> None:
    # No damage is needed: suspense has no balance to check, so the ledger posts this.
    async with db.transaction() as session:
        suspense = await system_account(session, AccountKind.SUSPENSE)
        user = await open_user(session)
        settlement = await system_account(session, AccountKind.BANK_SETTLEMENT)
        await ledger.post_entry(
            session,
            ledger.EntryDraft(
                "deposit_suspense",
                "deposit",
                "dep_1",
                (ledger.debit(settlement, 75_00), ledger.credit(suspense, 75_00)),
            ),
        )
        for attempt in ("first", "second"):
            # The one deposit, paid out of suspense twice.
            await ledger.post_entry(
                session,
                ledger.EntryDraft(
                    "adjustment",
                    "adjustment",
                    attempt,
                    (ledger.debit(suspense, 75_00), ledger.credit(user.available, 75_00)),
                ),
            )

    found = await findings(db)

    assert [(f.check, f.subject, f.detail) for f in found] == [
        ("negative_suspense", str(suspense), "suspense holds -7500 in USD")
    ]


async def test_suspense_that_holds_nothing_or_something_is_no_finding(db: Database) -> None:
    async with db.transaction() as session:
        suspense = await system_account(session, AccountKind.SUSPENSE)
        settlement = await system_account(session, AccountKind.BANK_SETTLEMENT)
        for reference, amount in (("dep_1", 75_00), ("dep_2", 20_00)):
            await ledger.post_entry(
                session,
                ledger.EntryDraft(
                    "deposit_suspense",
                    "deposit",
                    reference,
                    (ledger.debit(settlement, amount), ledger.credit(suspense, amount)),
                ),
            )
        await ledger.post_entry(
            session,
            ledger.EntryDraft(
                "deposit_return",
                "deposit",
                "dep_1",
                (ledger.debit(suspense, 75_00), ledger.credit(settlement, 75_00)),
            ),
        )
    assert await findings(db) == []

    async with db.transaction() as session:
        await ledger.post_entry(
            session,
            ledger.EntryDraft(
                "deposit_return",
                "deposit",
                "dep_2",
                (ledger.debit(suspense, 20_00), ledger.credit(settlement, 20_00)),
            ),
        )
    assert await findings(db) == []


async def test_findings_are_capped_per_check(db: Database, superuser_db: Database) -> None:
    async with db.transaction() as session:
        users = [await funded_user(session, 1_00) for _ in range(5)]
    await damage(
        superuser_db, "UPDATE account_balances SET balance = balance + 1 WHERE balance > 0"
    )

    async with db.transaction() as session:
        assert len(await ledger.verify(session)) == len(users)
        assert len(await ledger.verify(session, limit_per_check=2)) == 2


# --- the command -----------------------------------------------------------------------------


def run_verify_ledger(
    database: postgres.TestDatabase, settings: Settings
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "CORRIDOR_DATABASE_URL": database.app_url,
        "CORRIDOR_REDIS_URL": settings.redis_url.get_secret_value(),
    }
    return subprocess.run(
        [sys.executable, "-m", "corridor", "verify-ledger"],
        cwd=postgres.REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


async def test_the_command_exits_zero_on_a_sound_ledger(
    database: postgres.TestDatabase, settings: Settings, db: Database
) -> None:
    async with db.transaction() as session:
        maria = await funded_user(session, 50_00)
        joao = await open_user(session)
        await ledger.post_entry(session, transfer_draft(maria.available, joao.available, 20_00))
        await fund(session, joao.available, 1_00)

    result = run_verify_ledger(database, settings)

    assert (result.returncode, result.stdout.strip()) == (
        0,
        "Ledger verification passed: no findings.",
    )


async def test_the_command_exits_one_and_names_each_finding(
    database: postgres.TestDatabase, settings: Settings, db: Database, superuser_db: Database
) -> None:
    async with db.transaction() as session:
        maria = await funded_user(session, 50_00)
    await damage(
        superuser_db,
        "UPDATE account_balances SET balance = 49_99 WHERE account_id = :a",
        a=maria.available,
    )

    result = run_verify_ledger(database, settings)

    assert result.returncode == 1
    assert result.stdout.strip() == (
        f"balance_mismatch: {maria.available}: cached balance is 4999 but postings sum to 5000"
    )
    assert "FAILED: 1 finding(s)" in result.stderr

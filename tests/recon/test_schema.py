"""What the database refuses on its own about runs and breaks, with no help from the
application."""

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
FOREIGN_KEY_VIOLATION = "23503"
INSUFFICIENT_PRIVILEGE = "42501"


async def add_run(db: Database, **changes: Any) -> uuid.UUID:
    values = {
        "id": new_id(),
        "window_start": NOW - timedelta(hours=1),
        "window_end": NOW,
        "status": "completed",
        "breaks_found": 0,
        "breaks_opened": 0,
        "started_at": NOW,
        "finished_at": NOW,
        **changes,
    }
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO recon_runs (id, window_start, window_end, status, breaks_found,"
                " breaks_opened, started_at, finished_at) VALUES (:id, :window_start,"
                " :window_end, :status, :breaks_found, :breaks_opened, :started_at, :finished_at)"
            ),
            values,
        )
    return values["id"]


async def add_break(db: Database, run_id: uuid.UUID, **changes: Any) -> uuid.UUID:
    values = {
        "id": new_id(),
        "run_id": run_id,
        "last_seen_run_id": run_id,
        "kind": "unknown_deposit",
        "provider": "simbank",
        "provider_ref": "dep_1",
        "asset_code": "USD",
        "expected": 100,
        "actual": None,
        "status": "open",
        "note": None,
        "resolved_by": None,
        "created_at": NOW,
        "resolved_at": None,
        **changes,
    }
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO recon_breaks (id, run_id, last_seen_run_id, kind, provider,"
                " provider_ref, asset_code, expected, actual, status, note, resolved_by,"
                " created_at, resolved_at) VALUES (:id, :run_id, :last_seen_run_id, :kind,"
                " :provider, :provider_ref, :asset_code, :expected, :actual, :status, :note,"
                " :resolved_by, :created_at, :resolved_at)"
            ),
            values,
        )
    return values["id"]


def refused(error: pytest.ExceptionInfo[DBAPIError]) -> tuple[str | None, str | None]:
    return sqlstate_of(error.value), constraint_of(error.value)


async def test_one_disagreement_has_one_open_break(db: Database) -> None:
    run_id = await add_run(db)
    await add_break(db, run_id)

    with pytest.raises(DBAPIError) as error:
        await add_break(db, run_id)

    assert refused(error) == (UNIQUE_VIOLATION, "uq_recon_breaks_open")


async def test_a_resolved_break_does_not_stop_the_same_disagreement_being_opened_again(
    db: Database,
) -> None:
    run_id = await add_run(db)
    await add_break(db, run_id, status="resolved", resolved_by="system", resolved_at=NOW)
    await add_break(db, run_id, status="resolved", resolved_by="system", resolved_at=NOW)

    await add_break(db, run_id)


async def test_the_same_reference_at_another_provider_or_of_another_kind_is_another_break(
    db: Database,
) -> None:
    run_id = await add_run(db)
    await add_break(db, run_id)

    await add_break(db, run_id, provider="simcustody")
    await add_break(db, run_id, kind="amount_mismatch")


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"kind": "status_mismatch"}, "ck_recon_breaks_kind"),
        ({"status": "ignored"}, "ck_recon_breaks_status"),
        ({"status": "resolved"}, "ck_recon_breaks_resolution"),
        ({"status": "resolved", "resolved_by": "system"}, "ck_recon_breaks_resolution"),
        ({"resolved_by": "system", "resolved_at": NOW}, "ck_recon_breaks_resolution"),
        ({"note": "x" * 501}, "ck_recon_breaks_note"),
    ],
    ids=[
        "kind",
        "status",
        "resolved-by-nobody",
        "resolved-at-no-time",
        "open-and-resolved",
        "note",
    ],
)
async def test_a_break_that_contradicts_itself_is_refused(
    db: Database, changes: dict[str, Any], constraint: str
) -> None:
    run_id = await add_run(db)

    with pytest.raises(DBAPIError) as error:
        await add_break(db, run_id, **changes)

    assert refused(error) == (CHECK_VIOLATION, constraint)


async def test_a_break_belongs_to_a_run_that_exists_by_the_time_it_is_committed(
    db: Database,
) -> None:
    with pytest.raises(DBAPIError) as error:
        await add_break(db, new_id(), last_seen_run_id=await add_run(db))

    assert refused(error) == (FOREIGN_KEY_VIOLATION, "fk_recon_breaks_run_id_recon_runs")


async def test_the_run_that_last_saw_a_break_exists_by_the_time_it_is_committed(
    db: Database,
) -> None:
    with pytest.raises(DBAPIError) as error:
        await add_break(db, await add_run(db), last_seen_run_id=new_id())

    assert refused(error) == (
        FOREIGN_KEY_VIOLATION,
        "fk_recon_breaks_last_seen_run_id_recon_runs",
    )


async def test_a_break_may_be_written_before_its_run_in_the_same_transaction(db: Database) -> None:
    run_id = new_id()
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO recon_breaks (id, run_id, last_seen_run_id, kind, provider,"
                " provider_ref, asset_code, status, created_at) VALUES (:id, :run_id, :run_id,"
                " 'unknown_deposit', 'simbank', 'dep_1', 'USD', 'open', :now)"
            ),
            {"id": new_id(), "run_id": run_id, "now": NOW},
        )
        await session.execute(
            text(
                "INSERT INTO recon_runs (id, window_start, window_end, status, breaks_found,"
                " breaks_opened, started_at, finished_at)"
                " VALUES (:id, :start, :now, 'completed', 1, 1, :now, :now)"
            ),
            {"id": run_id, "start": NOW - timedelta(hours=1), "now": NOW},
        )


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"status": "running"}, "ck_recon_runs_status"),
        ({"window_start": NOW}, "ck_recon_runs_window"),
        ({"breaks_found": 1, "breaks_opened": 2}, "ck_recon_runs_counts"),
        ({"breaks_found": -1, "breaks_opened": -1}, "ck_recon_runs_counts"),
    ],
    ids=["status", "empty-window", "opened-more-than-found", "negative"],
)
async def test_a_run_that_contradicts_itself_is_refused(
    db: Database, changes: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as error:
        await add_run(db, **changes)

    assert refused(error) == (CHECK_VIOLATION, constraint)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE recon_runs SET status = 'incomplete'",
        "DELETE FROM recon_runs",
        "DELETE FROM recon_breaks",
        "TRUNCATE recon_breaks",
    ],
)
async def test_the_application_cannot_rewrite_a_run_or_remove_a_break(
    db: Database, statement: str
) -> None:
    await add_break(db, await add_run(db))

    with pytest.raises(DBAPIError) as error:
        async with db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(error.value) == INSUFFICIENT_PRIVILEGE


async def test_the_application_role_has_only_the_privileges_reconciliation_needs(
    db: Database,
) -> None:
    async with db.transaction() as session:
        granted = await session.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
                " WHERE grantee = :role AND table_name IN ('recon_runs', 'recon_breaks')"
            ),
            {"role": postgres.APP_ROLE},
        )
        by_table: dict[str, set[str]] = {}
        for table, privilege in granted:
            by_table.setdefault(table, set()).add(privilege)

    # No UPDATE on breaks as a whole: only on the columns named below.
    assert by_table == {
        "recon_runs": {"SELECT", "INSERT"},
        "recon_breaks": {"SELECT", "INSERT"},
    }


async def test_the_application_role_updates_only_what_a_run_or_a_resolution_changes(
    db: Database,
) -> None:
    async with db.transaction() as session:
        columns = await session.execute(
            text(
                "SELECT column_name FROM information_schema.column_privileges"
                " WHERE grantee = :role AND table_schema = 'public'"
                " AND table_name = 'recon_breaks' AND privilege_type = 'UPDATE'"
            ),
            {"role": postgres.APP_ROLE},
        )

    assert set(columns.scalars()) == {
        "expected",
        "actual",
        "last_seen_run_id",
        "status",
        "note",
        "resolved_by",
        "resolved_at",
    }


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE recon_breaks SET kind = 'unknown_payout'",
        "UPDATE recon_breaks SET provider_ref = 'dep_2'",
        "UPDATE recon_breaks SET provider = 'simcustody'",
        "UPDATE recon_breaks SET asset_code = 'MXN'",
        "UPDATE recon_breaks SET run_id = last_seen_run_id",
        "UPDATE recon_breaks SET created_at = resolved_at",
    ],
)
async def test_the_application_cannot_rewrite_what_a_break_is_about(
    db: Database, statement: str
) -> None:
    await add_break(db, await add_run(db))

    with pytest.raises(DBAPIError) as error:
        async with db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(error.value) == INSUFFICIENT_PRIVILEGE


async def test_the_application_brings_an_open_break_up_to_date_and_resolves_it(
    db: Database,
) -> None:
    first, later = await add_run(db), await add_run(db)
    await add_break(db, first)

    async with db.transaction() as session:
        refreshed = await session.execute(
            text(
                "UPDATE recon_breaks SET expected = 90, actual = 10, last_seen_run_id = :run"
                " RETURNING run_id"
            ),
            {"run": later},
        )
        resolved = await session.execute(
            text(
                "UPDATE recon_breaks SET status = 'resolved', resolved_by = 'system',"
                " note = 'settled', resolved_at = :now"
            ),
            {"now": NOW},
        )

    assert refreshed.scalar_one() == first
    assert resolved.rowcount == 1  # type: ignore[attr-defined]

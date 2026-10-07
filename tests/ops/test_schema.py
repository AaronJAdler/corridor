"""What the database refuses on its own about adjustments, with no help from the
application."""

import json
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
ANA, BRUNO = new_id(), new_id()
ENTRY = new_id()
DEPOSIT, MARIA = new_id(), new_id()

APPROVED: dict[str, Any] = {
    "status": "approved",
    "approved_by": BRUNO,
    "entry_id": ENTRY,
    "decided_at": NOW,
}


async def add_adjustment(db: Database, **changes: Any) -> uuid.UUID:
    values = {
        "id": new_id(),
        "requested_by": ANA,
        "approved_by": None,
        "status": "pending",
        "kind": "manual",
        "deposit_id": None,
        "user_id": None,
        "reason": "goodwill credit",
        "legs": json.dumps([]),
        "entry_id": None,
        "created_at": NOW,
        "decided_at": None,
        **changes,
    }
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO ops_adjustments (id, requested_by, approved_by, status, kind,"
                " deposit_id, user_id, reason, legs, entry_id, created_at, decided_at) VALUES"
                " (:id, :requested_by, :approved_by, :status, :kind, :deposit_id, :user_id,"
                " :reason, CAST(:legs AS jsonb), :entry_id, :created_at, :decided_at)"
            ),
            values,
        )
    return values["id"]


def refused(error: pytest.ExceptionInfo[DBAPIError]) -> tuple[str | None, str | None]:
    return sqlstate_of(error.value), constraint_of(error.value)


async def test_a_pending_an_approved_and_a_rejected_adjustment_are_accepted(db: Database) -> None:
    await add_adjustment(db)
    await add_adjustment(db, **APPROVED)
    await add_adjustment(db, status="rejected", decided_at=NOW)


async def test_the_table_refuses_an_adjustment_approved_by_its_requester(db: Database) -> None:
    with pytest.raises(DBAPIError) as error:
        await add_adjustment(db, **{**APPROVED, "approved_by": ANA})

    assert refused(error) == (CHECK_VIOLATION, "ck_ops_adjustments_distinct_approver")


async def test_the_table_refuses_a_pending_adjustment_becoming_approved_by_its_requester(
    db: Database,
) -> None:
    adjustment_id = await add_adjustment(db)

    with pytest.raises(DBAPIError) as error:
        async with db.transaction() as session:
            await session.execute(
                text(
                    "UPDATE ops_adjustments SET status = 'approved', approved_by = requested_by,"
                    " entry_id = :entry, decided_at = :now WHERE id = :id"
                ),
                {"entry": ENTRY, "now": NOW, "id": adjustment_id},
            )

    assert refused(error) == (CHECK_VIOLATION, "ck_ops_adjustments_distinct_approver")


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"status": "posted", "decided_at": NOW}, "ck_ops_adjustments_status"),
        ({**APPROVED, "approved_by": None}, "ck_ops_adjustments_approval"),
        ({**APPROVED, "entry_id": None}, "ck_ops_adjustments_approval"),
        ({"approved_by": BRUNO}, "ck_ops_adjustments_approval"),
        ({"entry_id": ENTRY}, "ck_ops_adjustments_approval"),
        (
            {"status": "rejected", "decided_at": NOW, "entry_id": ENTRY},
            "ck_ops_adjustments_approval",
        ),
        ({"decided_at": NOW}, "ck_ops_adjustments_decided"),
        ({"status": "rejected"}, "ck_ops_adjustments_decided"),
        ({"reason": ""}, "ck_ops_adjustments_reason"),
        ({"reason": "x" * 501}, "ck_ops_adjustments_reason"),
    ],
    ids=[
        "status",
        "approved-by-nobody",
        "approved-without-an-entry",
        "pending-with-an-approver",
        "pending-with-an-entry",
        "rejected-with-an-entry",
        "pending-and-decided",
        "decided-at-no-time",
        "no-reason",
        "long-reason",
    ],
)
async def test_an_adjustment_that_contradicts_itself_is_refused(
    db: Database, changes: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as error:
        await add_adjustment(db, **changes)

    assert refused(error) == (CHECK_VIOLATION, constraint)


async def test_a_release_and_a_return_of_a_deposit_are_accepted(db: Database) -> None:
    await add_adjustment(db, kind="suspense_release", deposit_id=DEPOSIT, user_id=MARIA)
    await add_adjustment(db, kind="suspense_return", deposit_id=DEPOSIT)


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"kind": "refund"}, "ck_ops_adjustments_kind"),
        ({"kind": "suspense_release", "user_id": MARIA}, "ck_ops_adjustments_suspense"),
        ({"kind": "suspense_return"}, "ck_ops_adjustments_suspense"),
        ({"kind": "suspense_release", "deposit_id": DEPOSIT}, "ck_ops_adjustments_suspense"),
        (
            {"kind": "suspense_return", "deposit_id": DEPOSIT, "user_id": MARIA},
            "ck_ops_adjustments_suspense",
        ),
        ({"deposit_id": DEPOSIT}, "ck_ops_adjustments_suspense"),
        ({"user_id": MARIA}, "ck_ops_adjustments_suspense"),
    ],
    ids=[
        "unknown-kind",
        "release-of-no-deposit",
        "return-of-no-deposit",
        "release-to-nobody",
        "return-to-a-user",
        "by-hand-with-a-deposit",
        "by-hand-with-a-user",
    ],
)
async def test_a_suspense_adjustment_names_its_deposit_and_a_release_its_user(
    db: Database, changes: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as error:
        await add_adjustment(db, **changes)

    assert refused(error) == (CHECK_VIOLATION, constraint)


async def test_one_journal_entry_belongs_to_one_adjustment(db: Database) -> None:
    await add_adjustment(db, **APPROVED)

    with pytest.raises(DBAPIError) as error:
        await add_adjustment(db, **APPROVED)

    assert refused(error) == (UNIQUE_VIOLATION, "uq_ops_adjustments_entry_id")


@pytest.mark.parametrize("statement", ["DELETE FROM ops_adjustments", "TRUNCATE ops_adjustments"])
async def test_the_application_cannot_remove_an_adjustment(db: Database, statement: str) -> None:
    await add_adjustment(db)

    with pytest.raises(DBAPIError) as error:
        async with db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(error.value) == INSUFFICIENT_PRIVILEGE


async def test_the_application_role_has_only_the_privileges_adjustments_need(db: Database) -> None:
    async with db.transaction() as session:
        granted = await session.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants"
                " WHERE grantee = :role AND table_name = 'ops_adjustments'"
            ),
            {"role": postgres.APP_ROLE},
        )
        privileges = set(granted.scalars())

    assert privileges == {"SELECT", "INSERT", "UPDATE"}

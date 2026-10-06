"""What the outbox table enforces on its own, and what its trigger announces.

These tests write rows with plain SQL, the way a buggy caller would, and read the catalogue
for the parts of the schema that the model drift check does not compare.
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
from tests.outbox.helpers import NotifyProbe, until
from tests.support import postgres

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


async def add_event(
    session: AsyncSession,
    *,
    topic: str = "test.created",
    status: str = "pending",
    attempts: int = 0,
    dedup_key: str | None = None,
) -> uuid.UUID:
    event = new_id()
    await session.execute(
        text(
            "INSERT INTO outbox_events (id, topic, payload, status, attempts, available_at,"
            " locked_until, dedup_key, last_error, context, created_at, finished_at)"
            " VALUES (:id, :topic, CAST('{}' AS jsonb), :status, :attempts, :now, NULL,"
            " :dedup_key, NULL, CAST('{}' AS jsonb), :now, NULL)"
        ),
        {
            "id": event,
            "topic": topic,
            "status": status,
            "attempts": attempts,
            "dedup_key": dedup_key,
            "now": NOW,
        },
    )
    return event


async def count(db: Database) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text("SELECT count(*) FROM outbox_events"))).scalar_one())


# --- indexes ---------------------------------------------------------------------------------


async def test_each_index_covers_only_the_rows_it_is_for(db: Database) -> None:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes"
                " WHERE schemaname = 'public' AND tablename = 'outbox_events'"
            )
        )
        indexes = {row.indexname: row.indexdef for row in rows}

    on_table = "ON public.outbox_events USING btree"
    assert indexes == {
        "pk_outbox_events": f"CREATE UNIQUE INDEX pk_outbox_events {on_table} (id)",
        # What a claim looks for first: events that are waiting, by when they fall due.
        "ix_outbox_events_due": (
            f"CREATE INDEX ix_outbox_events_due {on_table} (available_at, id)"
            " WHERE (status = 'pending'::text)"
        ),
        # And second: claims that may have outlived their worker.
        "ix_outbox_events_claimed": (
            f"CREATE INDEX ix_outbox_events_claimed {on_table} (locked_until)"
            " WHERE (status = 'processing'::text)"
        ),
        "uq_outbox_events_topic_dedup_key": (
            f"CREATE UNIQUE INDEX uq_outbox_events_topic_dedup_key {on_table} (topic, dedup_key)"
            " WHERE (dedup_key IS NOT NULL)"
        ),
    }


# --- row-level rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "topic",
    [
        "",
        "nodot",
        "Payments.created",
        "payments.Created",
        "payments..created",
        ".created",
        "payments.",
        "payments.created.",
        "1payments.created",
        "payments.1created",
        "pay-ments.created",
        "payments created.now",
        "payments.created\n",
        "_payments.created",
    ],
)
async def test_a_topic_is_dotted_lower_case_words(db: Database, topic: str) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_event(session, topic=topic)

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == "ck_outbox_events_topic"


@pytest.mark.parametrize(
    "topic",
    ["a.b", "worker.ping", "payments.withdrawal_requested", "webhooks.bank2.event_v1"],
)
async def test_a_well_formed_topic_is_accepted(db: Database, topic: str) -> None:
    async with db.transaction() as session:
        await add_event(session, topic=topic)

    assert await count(db) == 1


@pytest.mark.parametrize("status", ["pending", "processing", "done", "dead"])
async def test_each_of_the_four_statuses_is_accepted(db: Database, status: str) -> None:
    async with db.transaction() as session:
        await add_event(session, status=status)

    assert await count(db) == 1


@pytest.mark.parametrize("status", ["", "failed", "PENDING", "retry"])
async def test_any_other_status_is_refused(db: Database, status: str) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_event(session, status=status)

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == "ck_outbox_events_status"


async def test_attempts_cannot_be_negative(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_event(session, attempts=-1)

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == "ck_outbox_events_attempts"


async def test_a_dedup_key_admits_one_event_per_topic(db: Database) -> None:
    async with db.transaction() as session:
        await add_event(session, topic="test.created", dedup_key="wd_1")

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_event(session, topic="test.created", dedup_key="wd_1")

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == "uq_outbox_events_topic_dedup_key"
    assert await count(db) == 1


async def test_the_same_key_under_another_topic_and_events_without_a_key_are_not_duplicates(
    db: Database,
) -> None:
    async with db.transaction() as session:
        await add_event(session, topic="test.created", dedup_key="wd_1")
        await add_event(session, topic="test.settled", dedup_key="wd_1")
        await add_event(session, topic="test.created", dedup_key="wd_2")
        await add_event(session, topic="test.created", dedup_key=None)
        await add_event(session, topic="test.created", dedup_key=None)

    assert await count(db) == 5


async def test_no_column_has_a_default(db: Database) -> None:
    # Every value is written by the application, which is where the clock and the ids are.
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT column_name, column_default FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'outbox_events'"
            )
        )
        defaults = {row.column_name: row.column_default for row in rows}

    assert defaults == dict.fromkeys(
        [
            "id",
            "topic",
            "payload",
            "status",
            "attempts",
            "available_at",
            "locked_until",
            "dedup_key",
            "last_error",
            "context",
            "created_at",
            "finished_at",
        ]
    )


async def test_the_application_role_has_full_row_access(db: Database) -> None:
    # Unlike the ledger's history, the outbox is a work queue: the application updates
    # events as it processes them and deletes them once they are old.
    async with db.transaction() as session:
        granted = (
            await session.execute(
                text(
                    "SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)"
                    " FROM information_schema.role_table_grants"
                    " WHERE grantee = :role AND table_schema = 'public'"
                    " AND table_name = 'outbox_events'"
                ),
                {"role": postgres.APP_ROLE},
            )
        ).scalar_one()

    assert granted == "DELETE,INSERT,SELECT,UPDATE"


# --- the notify trigger ----------------------------------------------------------------------


async def test_the_trigger_fires_once_per_insert_statement_and_for_nothing_else(
    db: Database,
) -> None:
    async with db.transaction() as session:
        definitions = (
            await session.execute(
                text(
                    "SELECT pg_get_triggerdef(oid) FROM pg_trigger"
                    " WHERE tgrelid = 'outbox_events'::regclass AND NOT tgisinternal"
                )
            )
        ).scalars()
        assert list(definitions) == [
            "CREATE TRIGGER outbox_events_notify AFTER INSERT ON public.outbox_events"
            " FOR EACH STATEMENT EXECUTE FUNCTION outbox_notify()"
        ]


async def test_a_notification_arrives_when_the_inserting_transaction_commits_and_not_before(
    db: Database, probe: NotifyProbe
) -> None:
    async with db.transaction() as session:
        await add_event(session)
        # The row is written but not committed: no worker could see it, so none is woken.
        assert await probe.received() == []

    await until(probe.received, what="the notification")
    assert await probe.received() == [""]


async def test_a_rolled_back_insert_notifies_nobody(db: Database, probe: NotifyProbe) -> None:
    with pytest.raises(RuntimeError, match="changed my mind"):
        async with db.transaction() as session:
            await add_event(session)
            raise RuntimeError("changed my mind")

    assert await probe.received() == []

    # The probe is not simply deaf: the next insert that does commit is the only one heard.
    async with db.transaction() as session:
        await add_event(session)
    await until(probe.received, what="the notification")
    assert await probe.received() == [""]


async def test_several_inserts_in_one_transaction_notify_once(
    db: Database, probe: NotifyProbe
) -> None:
    async with db.transaction() as session:
        for _ in range(3):
            await add_event(session)

    await until(probe.received, what="the notification")
    assert await probe.received() == [""]


async def test_updates_and_deletes_do_not_notify(db: Database, probe: NotifyProbe) -> None:
    async with db.transaction() as session:
        event = await add_event(session)
    await until(probe.received, what="the notification")

    # Claiming and finishing an event are updates. If they notified, every worker would be
    # woken by every other worker's progress.
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE outbox_events SET status = 'done' WHERE id = :id"), {"id": event}
        )
    async with db.transaction() as session:
        await session.execute(text("DELETE FROM outbox_events WHERE id = :id"), {"id": event})

    assert await probe.received() == [""]

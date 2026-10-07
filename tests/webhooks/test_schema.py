"""What the webhook table enforces on its own, with no help from the application."""

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

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
INSUFFICIENT_PRIVILEGE = "42501"


async def add_event(
    session: AsyncSession,
    *,
    provider: str = "simbank",
    event_id: str = "evt_1",
    event_type: str = "payout.completed",
    processed_at: datetime | None = None,
    outcome: str | None = None,
) -> uuid.UUID:
    row = new_id()
    await session.execute(
        text(
            "INSERT INTO webhook_events"
            " (id, provider, event_id, type, payload, received_at, processed_at, outcome)"
            " VALUES (:id, :provider, :event_id, :type, CAST('{}' AS jsonb), :now,"
            " :processed_at, :outcome)"
        ),
        {
            "id": row,
            "provider": provider,
            "event_id": event_id,
            "type": event_type,
            "now": NOW,
            "processed_at": processed_at,
            "outcome": outcome,
        },
    )
    return row


async def test_a_provider_delivers_an_event_id_once(db: Database) -> None:
    async with db.transaction() as session:
        await add_event(session)

    with pytest.raises(DBAPIError) as refused:
        async with db.transaction() as session:
            await add_event(session)

    assert sqlstate_of(refused.value) == UNIQUE_VIOLATION
    assert constraint_of(refused.value) == "uq_webhook_events_provider_event_id"


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"provider": "somebank"}, "ck_webhook_events_provider"),
        ({"event_id": ""}, "ck_webhook_events_event_id"),
        ({"event_type": ""}, "ck_webhook_events_type"),
        ({"processed_at": NOW, "outcome": "lost"}, "ck_webhook_events_outcome"),
        ({"processed_at": NOW}, "ck_webhook_events_processed"),
        ({"outcome": "processed"}, "ck_webhook_events_processed"),
    ],
)
async def test_a_row_that_makes_no_sense_is_refused(
    db: Database, values: dict[str, object], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as refused:
        async with db.transaction() as session:
            await add_event(session, **values)  # type: ignore[arg-type]

    assert sqlstate_of(refused.value) == CHECK_VIOLATION
    assert constraint_of(refused.value) == constraint


async def test_the_application_may_mark_an_event_processed_but_not_delete_it(db: Database) -> None:
    async with db.transaction() as session:
        row = await add_event(session)

    async with db.transaction() as session:
        await session.execute(
            text(
                "UPDATE webhook_events SET processed_at = :now, outcome = 'processed'"
                " WHERE id = :id"
            ),
            {"now": NOW, "id": row},
        )

    with pytest.raises(DBAPIError) as refused:
        async with db.transaction() as session:
            await session.execute(text("DELETE FROM webhook_events WHERE id = :id"), {"id": row})

    assert sqlstate_of(refused.value) == INSUFFICIENT_PRIVILEGE

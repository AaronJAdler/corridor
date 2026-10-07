"""Enqueueing: an event is written in the caller's transaction, and shares its fate."""

import asyncio
import uuid
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import outbox
from corridor.outbox import EventStatus, OutboxEvent
from corridor.platform.clock import ManualClock
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.logging import bind_context, clear_context
from tests.outbox.helpers import NotifyProbe, until


class Untouchable:
    """Stands in for a session and fails the test if anything at all is asked of it."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the session was used ({name}) before the topic was checked")


async def count(db: Database) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text("SELECT count(*) FROM outbox_events"))).scalar_one())


async def load(db: Database, event_id: uuid.UUID) -> OutboxEvent:
    async with db.transaction() as session:
        event = await outbox.get_event(session, event_id)
    assert event is not None
    return event


@pytest.fixture(autouse=True)
def _no_log_context() -> None:
    clear_context()


async def test_an_event_is_stored_pending_and_due_at_once(db: Database, clock: ManualClock) -> None:
    payload = {
        "withdrawal_id": "wd_1",
        "amount": "12.50",
        "attempted": 3,
        "flags": [True, None, "x"],
        "provider": {"name": "simbank", "fee": 0.5},
    }

    async with db.transaction() as session:
        event_id = await outbox.enqueue(session, "payments.withdrawal_requested", payload)

    assert event_id is not None
    assert event_id.version == 7
    assert await load(db, event_id) == OutboxEvent(
        id=event_id,
        topic="payments.withdrawal_requested",
        payload=payload,
        status=EventStatus.PENDING,
        attempts=0,
        available_at=clock.now(),
        locked_until=None,
        claim_id=None,
        dedup_key=None,
        last_error=None,
        context={},
        created_at=clock.now(),
        finished_at=None,
    )


async def test_an_event_can_be_scheduled_for_later(db: Database, clock: ManualClock) -> None:
    later = clock.now() + timedelta(minutes=30)

    async with db.transaction() as session:
        event_id = await outbox.enqueue(session, "test.created", {}, available_at=later)
    assert event_id is not None

    event = await load(db, event_id)
    assert (event.available_at, event.created_at) == (later, clock.now())


async def test_an_event_is_rolled_back_with_the_transaction_that_enqueued_it(db: Database) -> None:
    with pytest.raises(RuntimeError, match="the state change failed"):
        async with db.transaction() as session:
            event_id = await outbox.enqueue(session, "test.created", {})
            # Inside the transaction the event exists, like the state change it belongs to.
            assert await outbox.get_event(session, cast(uuid.UUID, event_id)) is not None
            raise RuntimeError("the state change failed")

    assert await count(db) == 0


async def test_enqueueing_does_not_commit_or_notify_by_itself(
    db: Database, probe: NotifyProbe
) -> None:
    async with db.transaction() as session:
        event_id = await outbox.enqueue(session, "test.created", {})
        assert event_id is not None

        # Another transaction cannot see the event, and no worker has been told about it.
        assert await count(db) == 0
        assert await probe.received() == []

    await until(probe.received, what="the notification")
    assert await count(db) == 1


async def test_an_unknown_event_is_not_found(db: Database) -> None:
    async with db.transaction() as session:
        assert await outbox.get_event(session, new_id()) is None


# --- dedup keys ------------------------------------------------------------------------------


async def test_a_dedup_key_admits_one_event_per_topic(db: Database) -> None:
    async with db.transaction() as session:
        first = await outbox.enqueue(session, "test.created", {"n": 1}, dedup_key="wd_1")
        again = await outbox.enqueue(session, "test.created", {"n": 2}, dedup_key="wd_1")
    async with db.transaction() as session:
        later = await outbox.enqueue(session, "test.created", {"n": 3}, dedup_key="wd_1")

    assert first is not None
    assert (again, later) == (None, None)
    assert await count(db) == 1
    event = await load(db, first)
    # The first event stands as it was: a duplicate neither replaces nor changes it.
    assert (event.dedup_key, event.payload) == ("wd_1", {"n": 1})


async def test_the_same_key_under_another_topic_is_another_event(db: Database) -> None:
    async with db.transaction() as session:
        created = await outbox.enqueue(session, "test.created", {}, dedup_key="wd_1")
        settled = await outbox.enqueue(session, "test.settled", {}, dedup_key="wd_1")
        other_key = await outbox.enqueue(session, "test.created", {}, dedup_key="wd_2")

    assert None not in (created, settled, other_key)
    assert len({created, settled, other_key}) == 3
    assert await count(db) == 3


async def test_events_without_a_key_are_never_duplicates(db: Database) -> None:
    async with db.transaction() as session:
        first = await outbox.enqueue(session, "test.created", {"same": True})
        second = await outbox.enqueue(session, "test.created", {"same": True})

    assert first is not None
    assert second is not None
    assert first != second
    assert await count(db) == 2


async def test_a_duplicate_does_not_break_the_transaction_it_was_enqueued_in(db: Database) -> None:
    async with db.transaction() as session:
        await outbox.enqueue(session, "test.created", {}, dedup_key="wd_1")

    async with db.transaction() as session:
        assert await outbox.enqueue(session, "test.created", {}, dedup_key="wd_1") is None
        # The caller carries on: the duplicate was refused without an error.
        other = await outbox.enqueue(session, "test.settled", {})

    assert other is not None
    assert await count(db) == 2


async def test_50_concurrent_enqueues_of_one_dedup_key_store_one_event(db: Database) -> None:
    async def enqueue(session: AsyncSession) -> uuid.UUID | None:
        return await outbox.enqueue(session, "test.created", {}, dedup_key="wd_1")

    results = await asyncio.gather(*(db.run(enqueue) for _ in range(50)))

    stored = [event_id for event_id in results if event_id is not None]
    assert len(stored) == 1
    assert results.count(None) == 49
    assert await count(db) == 1
    assert (await load(db, stored[0])).dedup_key == "wd_1"


async def test_a_key_is_free_again_if_the_transaction_that_took_it_rolls_back(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as session:
            await outbox.enqueue(session, "test.created", {}, dedup_key="wd_1")
            raise RuntimeError("rolled back")

    async with db.transaction() as session:
        assert await outbox.enqueue(session, "test.created", {}, dedup_key="wd_1") is not None


# --- what is refused -------------------------------------------------------------------------


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
async def test_a_malformed_topic_is_refused_before_any_sql(topic: str) -> None:
    with pytest.raises(ValueError, match="topic"):
        await outbox.enqueue(cast(AsyncSession, Untouchable()), topic, {})


@pytest.mark.parametrize(
    "topic",
    ["a.b", "worker.ping", "payments.withdrawal_requested", "webhooks.bank2.event_v1"],
)
async def test_a_well_formed_topic_is_accepted(db: Database, topic: str) -> None:
    async with db.transaction() as session:
        event_id = await outbox.enqueue(session, topic, {})

    assert event_id is not None
    assert (await load(db, event_id)).topic == topic


async def test_a_payload_that_is_not_json_is_refused_and_nothing_is_stored(db: Database) -> None:
    # Converting ids and timestamps to strings is the caller's job.
    with pytest.raises(StatementError, match="not JSON serializable"):
        async with db.transaction() as session:
            await outbox.enqueue(session, "test.created", {"id": new_id()})

    assert await count(db) == 0


# --- context ---------------------------------------------------------------------------------


async def test_the_request_id_in_the_log_context_travels_with_the_event(db: Database) -> None:
    bind_context(request_id="req-12345678", principal="user_1", route="/v1/transfers")
    try:
        async with db.transaction() as session:
            event_id = await outbox.enqueue(session, "test.created", {})
    finally:
        clear_context()

    assert event_id is not None
    # The request id and nothing else: the rest of the log context stays in the request.
    assert (await load(db, event_id)).context == {"request_id": "req-12345678"}


async def test_an_event_enqueued_outside_a_request_has_an_empty_context(db: Database) -> None:
    bind_context(principal="user_1")
    try:
        async with db.transaction() as session:
            event_id = await outbox.enqueue(session, "test.created", {})
    finally:
        clear_context()

    assert event_id is not None
    assert (await load(db, event_id)).context == {}

"""Recording a delivery and processing it, below HTTP."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import webhooks
from corridor.platform.clock import ManualClock
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.webhooks import (
    EventOutcome,
    MalformedEvent,
    Provider,
    UnknownProvider,
    WebhookEvent,
    WebhookRegistry,
)
from tests.webhooks.helpers import enqueued, envelope, stored_events


class Recorder:
    """A handler that succeeds and remembers what it was given."""

    def __init__(self) -> None:
        self.events: list[WebhookEvent] = []

    async def __call__(self, db: Database, event: WebhookEvent) -> None:
        self.events.append(event)


class Failing:
    async def __call__(self, db: Database, event: WebhookEvent) -> None:
        raise RuntimeError("the state machine is unavailable")


async def record(
    db: Database, provider: Provider = Provider.SIMBANK, raw: bytes | None = None
) -> uuid.UUID:
    parsed = webhooks.parse_envelope(raw if raw is not None else envelope())

    async def work(session: AsyncSession) -> uuid.UUID:
        recorded = await webhooks.record(session, provider, parsed)
        assert recorded.created
        return recorded.id

    return await db.run(work)


async def load(db: Database, event_id: uuid.UUID) -> WebhookEvent:
    async with db.transaction() as session:
        return await webhooks.get_event(session, event_id)


# --- the envelope ----------------------------------------------------------------------------


def test_an_envelope_keeps_the_event_as_it_was_sent() -> None:
    parsed = webhooks.parse_envelope(envelope("evt_1", "deposit.received", {"amount": "5.00"}))

    assert (parsed.event_id, parsed.type) == ("evt_1", "deposit.received")
    assert parsed.payload == {
        "id": "evt_1",
        "type": "deposit.received",
        "created_at": "2026-01-15T12:00:00Z",
        "data": {"amount": "5.00"},
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'{"id":"evt_1","type":"a.b","created_at":"2026-01-15T12:00:00Z"}',
        b'{"id":"evt_1","type":"a.b","data":{}}',
        b'{"id":"evt_1","created_at":"2026-01-15T12:00:00Z","data":{}}',
        b'{"type":"a.b","created_at":"2026-01-15T12:00:00Z","data":{}}',
        b'{"id":7,"type":"a.b","created_at":"2026-01-15T12:00:00Z","data":{}}',
        b'{"id":"","type":"a.b","created_at":"2026-01-15T12:00:00Z","data":{}}',
        b'{"id":"evt 1","type":"a.b","created_at":"2026-01-15T12:00:00Z","data":{}}',
        b'{"id":"evt_1","type":"","created_at":"2026-01-15T12:00:00Z","data":{}}',
        b'{"id":"evt_1","type":"a.b","created_at":"yesterday","data":{}}',
        b'{"id":"evt_1","type":"a.b","created_at":"2026-01-15T12:00:00","data":{}}',
        b'{"id":"evt_1","type":"a.b","created_at":"2026-01-15T12:00:00Z","data":[]}',
        b'{"id":"evt_1","type":"a.b","created_at":"2026-01-15T12:00:00Z","data":"x"}',
        b'{"id":"evt_1","type":"a.b","created_at":"2026-01-15T12:00:00Z","data":{},"x":1}',
        b'{"id":"evt_1","type":"a.b","created_at":"2026-01-15T12:00:00Z","data":{"a":"\\u0000"}}',
        b'{"id":"evt_1","type":"a.b","created_at":"2026-01-15T12:00:00Z","data":{"a":NaN}}',
        b'{"id":"evt_1","type":"a.b","created_at":"2026-01-15T12:00:00Z","data":{"a":"\xff"}}',
    ],
)
def test_a_malformed_envelope_is_refused_without_repeating_it(raw: bytes) -> None:
    with pytest.raises(MalformedEvent) as refused:
        webhooks.parse_envelope(raw)

    assert refused.value.status == 422
    assert "evt" not in str(refused.value.detail)


def test_only_the_two_providers_have_a_name() -> None:
    assert webhooks.provider_named("simbank") is Provider.SIMBANK
    assert webhooks.provider_named("simcustody") is Provider.SIMCUSTODY
    for name in ("bank", "SIMBANK", "simbank ", ""):
        with pytest.raises(UnknownProvider) as refused:
            webhooks.provider_named(name)
        assert refused.value.status == 404


# --- recording -------------------------------------------------------------------------------


async def test_a_recorded_delivery_is_stored_with_one_outbox_event(
    db: Database, clock: ManualClock
) -> None:
    event_id = await record(db, raw=envelope("evt_1", "deposit.received", {"amount": "5.00"}))

    (row,) = await stored_events(db)
    assert row == {
        "id": event_id,
        "provider": "simbank",
        "event_id": "evt_1",
        "type": "deposit.received",
        "payload": {
            "id": "evt_1",
            "type": "deposit.received",
            "created_at": "2026-01-15T12:00:00Z",
            "data": {"amount": "5.00"},
        },
        "received_at": clock.now(),
        "processed_at": None,
        "outcome": None,
    }
    assert await enqueued(db) == [event_id]


async def test_a_repeated_event_id_is_reported_and_writes_nothing(db: Database) -> None:
    first = await record(db)

    async with db.transaction() as session:
        again = await webhooks.record(
            session, Provider.SIMBANK, webhooks.parse_envelope(envelope())
        )

    assert (again.created, again.id) == (False, first)
    assert len(await stored_events(db)) == 1
    assert await enqueued(db) == [first]


async def test_the_same_event_id_from_the_other_provider_is_another_event(db: Database) -> None:
    await record(db, Provider.SIMBANK)
    await record(db, Provider.SIMCUSTODY)

    assert [row["provider"] for row in await stored_events(db)] == ["simbank", "simcustody"]
    assert len(await enqueued(db)) == 2


async def test_recording_commits_nothing_itself(db: Database) -> None:
    parsed = webhooks.parse_envelope(envelope())

    with pytest.raises(RuntimeError, match="after recording"):
        async with db.transaction() as session:
            await webhooks.record(session, Provider.SIMBANK, parsed)
            raise RuntimeError("after recording")

    assert await stored_events(db) == []
    assert await enqueued(db) == []


# --- processing ------------------------------------------------------------------------------


async def test_processing_runs_the_handler_registered_for_the_provider_and_type(
    db: Database, clock: ManualClock
) -> None:
    bank_payouts, custody_payouts, bank_deposits = Recorder(), Recorder(), Recorder()
    registry = WebhookRegistry()
    registry.register(Provider.SIMBANK, "payout.completed", bank_payouts)
    registry.register(Provider.SIMCUSTODY, "payout.completed", custody_payouts)
    registry.register(Provider.SIMBANK, "deposit.received", bank_deposits)
    event_id = await record(db, Provider.SIMBANK, envelope("evt_1", "payout.completed", {"a": 1}))
    clock.advance(seconds=7)

    await webhooks.process(db, event_id, registry)

    (handled,) = bank_payouts.events
    assert (handled.id, handled.provider, handled.event_id) == (event_id, Provider.SIMBANK, "evt_1")
    assert (handled.type, handled.data) == ("payout.completed", {"a": 1})
    assert custody_payouts.events == []
    assert bank_deposits.events == []
    processed = await load(db, event_id)
    assert (processed.processed_at, processed.outcome) == (clock.now(), EventOutcome.PROCESSED)


async def test_an_event_of_an_unknown_type_is_marked_ignored(
    db: Database, clock: ManualClock
) -> None:
    handler = Recorder()
    registry = WebhookRegistry()
    registry.register(Provider.SIMCUSTODY, "payout.completed", handler)
    event_id = await record(db, Provider.SIMBANK, envelope("evt_1", "payout.completed"))

    await webhooks.process(db, event_id, registry)

    assert handler.events == []
    ignored = await load(db, event_id)
    assert (ignored.processed_at, ignored.outcome) == (clock.now(), EventOutcome.IGNORED)


async def test_processing_an_event_twice_runs_its_handler_once(
    db: Database, clock: ManualClock
) -> None:
    handler = Recorder()
    registry = WebhookRegistry()
    registry.register(Provider.SIMBANK, "payout.completed", handler)
    event_id = await record(db)
    await webhooks.process(db, event_id, registry)
    first = await load(db, event_id)
    clock.advance(seconds=60)

    await webhooks.process(db, event_id, registry)

    assert len(handler.events) == 1
    assert await load(db, event_id) == first


async def test_an_ignored_event_is_not_handled_when_a_handler_arrives_later(db: Database) -> None:
    event_id = await record(db)
    await webhooks.process(db, event_id, WebhookRegistry())
    handler = Recorder()
    registry = WebhookRegistry()
    registry.register(Provider.SIMBANK, "payout.completed", handler)

    await webhooks.process(db, event_id, registry)

    assert handler.events == []
    assert (await load(db, event_id)).outcome is EventOutcome.IGNORED


async def test_a_handler_that_raises_leaves_the_event_unprocessed(db: Database) -> None:
    registry = WebhookRegistry()
    registry.register(Provider.SIMBANK, "payout.completed", Failing())
    event_id = await record(db)

    with pytest.raises(RuntimeError, match="state machine"):
        await webhooks.process(db, event_id, registry)

    failed = await load(db, event_id)
    assert (failed.processed_at, failed.outcome) == (None, None)


async def test_an_event_that_failed_is_handled_when_it_is_processed_again(db: Database) -> None:
    failing = WebhookRegistry()
    failing.register(Provider.SIMBANK, "payout.completed", Failing())
    event_id = await record(db)
    with pytest.raises(RuntimeError):
        await webhooks.process(db, event_id, failing)
    handler = Recorder()
    working = WebhookRegistry()
    working.register(Provider.SIMBANK, "payout.completed", handler)

    await webhooks.process(db, event_id, working)

    assert len(handler.events) == 1
    assert (await load(db, event_id)).outcome is EventOutcome.PROCESSED


async def test_processing_an_event_that_does_not_exist_is_an_error(db: Database) -> None:
    with pytest.raises(webhooks.EventNotFound):
        await webhooks.process(db, new_id(), WebhookRegistry())


def test_a_provider_and_type_has_one_handler() -> None:
    registry = WebhookRegistry()
    registry.register(Provider.SIMBANK, "payout.completed", Recorder())

    with pytest.raises(ValueError, match="already has a handler"):
        registry.register(Provider.SIMBANK, "payout.completed", Recorder())

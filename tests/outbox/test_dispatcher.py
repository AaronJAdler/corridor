"""The dispatcher: claiming due events, running their handlers, recording what happened.

Time is the test's to move: the dispatcher reads the application clock, so a backoff or a
claim is waited out by advancing the ``clock`` fixture, never by sleeping.
"""

import asyncio
import json
import random
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text

from corridor import outbox
from corridor.outbox import EventStatus, Handler, OutboxEvent, OutboxStats, Registry
from corridor.outbox import dispatcher as dispatcher_module
from corridor.outbox.retry import next_delay
from corridor.outbox.service import mark_done
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.errors import DomainError
from corridor.platform.ids import new_id
from corridor.platform.logging import REDACTED, bind_context, clear_context, current_context
from tests.outbox.helpers import (
    TOPIC,
    Failing,
    Gate,
    Longest,
    Recorder,
    Shortest,
    abandon,
    dispatcher_for,
    enqueue_event,
    enqueue_events,
    load,
    reap,
    status_counts,
)

# Shaped like a credential so the scrubber has something to find. It protects nothing.
NOT_A_TOKEN = "Bearer abcdefghijklmnop"  # pragma: allowlist secret
PAYLOAD_MARKER = "only-ever-in-the-payload"


def processed(topic: str, outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "corridor_outbox_processed_total", {"topic": topic, "outcome": outcome}
        )
        or 0.0
    )


class Teapot(DomainError):
    status = 418
    code = "teapot"
    title = "I'm a teapot"


class Indescribable(Exception):
    """An error that cannot even say what it is."""

    def __str__(self) -> str:
        raise RuntimeError("no description available")


# --- handling --------------------------------------------------------------------------------


async def test_a_due_event_is_handled_and_marked_done(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db, TOPIC, {"withdrawal_id": "wd_1"})
    recorder = Recorder()
    dispatcher = dispatcher_for(db, settings, {TOPIC: recorder})
    clock.advance(seconds=5)

    assert await dispatcher.run_once() == 1

    # The handler is given the event as it was claimed.
    [handled] = recorder.events
    assert (handled.id, handled.topic, handled.payload) == (
        event_id,
        TOPIC,
        {"withdrawal_id": "wd_1"},
    )
    assert (handled.status, handled.attempts) == (EventStatus.PROCESSING, 1)
    assert handled.locked_until == clock.now() + timedelta(seconds=settings.outbox_claim_seconds)

    event = await load(db, event_id)
    assert (event.status, event.attempts) == (EventStatus.DONE, 1)
    assert (event.finished_at, event.locked_until, event.last_error) == (clock.now(), None, None)


async def test_with_nothing_due_nothing_is_claimed(db: Database, settings: Settings) -> None:
    dispatcher = dispatcher_for(db, settings, {TOPIC: Recorder()})

    assert await dispatcher.run_once() == 0
    assert await dispatcher.drain() == 0


async def test_an_event_scheduled_for_later_waits_until_the_clock_reaches_it(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db, available_at=clock.now() + timedelta(seconds=60))
    recorder = Recorder()
    dispatcher = dispatcher_for(db, settings, {TOPIC: recorder})

    assert await dispatcher.run_once() == 0
    clock.advance(seconds=59)
    assert await dispatcher.run_once() == 0
    assert recorder.events == []

    clock.advance(seconds=1)
    assert await dispatcher.run_once() == 1
    assert recorder.ids == [event_id]


async def test_one_run_claims_no_more_than_a_batch(db: Database, settings: Settings) -> None:
    ids = await enqueue_events(db, 5)
    recorder = Recorder()
    dispatcher = dispatcher_for(db, settings, {TOPIC: recorder}, outbox_batch_size=2)

    assert await dispatcher.run_once() == 2

    assert sorted(recorder.ids) == ids[:2]
    assert await status_counts(db) == {"done": 2, "pending": 3}


async def test_a_batch_stays_a_batch_when_the_planner_believes_the_table_is_empty(
    db: Database, owner_db: Database, settings: Settings
) -> None:
    # A queue is often empty when its statistics are gathered and full a moment later.
    # With those statistics PostgreSQL may run a claim's inner query once per row instead
    # of once, and an inner LIMIT then stops limiting anything.
    async with owner_db.transaction() as session:
        await session.execute(text("ANALYZE outbox_events"))
    await enqueue_events(db, 40)
    dispatcher = dispatcher_for(db, settings, {TOPIC: Recorder()}, outbox_batch_size=3)

    assert await dispatcher.run_once() == 3
    assert await status_counts(db) == {"done": 3, "pending": 37}


async def test_drain_runs_batch_after_batch_until_nothing_is_due(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    ids = await enqueue_events(db, 7)
    later = await enqueue_event(db, available_at=clock.now() + timedelta(hours=1))
    recorder = Recorder()
    dispatcher = dispatcher_for(db, settings, {TOPIC: recorder}, outbox_batch_size=3)

    assert await dispatcher.drain() == 7

    assert sorted(recorder.ids) == ids
    assert await status_counts(db) == {"done": 7, "pending": 1}
    assert (await load(db, later)).status == EventStatus.PENDING


async def test_events_are_claimed_in_id_order_whatever_order_they_fell_due_in(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    start = clock.now()
    # The oldest event fails once. Its retry falls due two seconds from now, after the two
    # younger events, and its row has been rewritten behind theirs in the table. Neither
    # of those orders is the one that counts.
    oldest = await enqueue_event(db, "test.flaky")
    middle = await enqueue_event(db, TOPIC, available_at=start + timedelta(seconds=1))
    youngest = await enqueue_event(db, TOPIC, available_at=start + timedelta(seconds=1))
    assert oldest < middle < youngest

    handled: list[uuid.UUID] = []

    async def flaky(event: OutboxEvent) -> None:
        if event.attempts == 1:
            raise RuntimeError("not this time")
        handled.append(event.id)

    async def steady(event: OutboxEvent) -> None:
        handled.append(event.id)

    dispatcher = dispatcher_for(
        db,
        settings,
        {"test.flaky": flaky, TOPIC: steady},
        rng=Longest(),
        outbox_batch_size=2,
        outbox_concurrency=1,
    )
    assert await dispatcher.run_once() == 1
    assert (await load(db, oldest)).available_at == start + timedelta(seconds=2)

    clock.advance(seconds=10)
    assert await dispatcher.run_once() == 2
    assert handled == [oldest, middle]
    assert await dispatcher.run_once() == 1
    assert handled == [oldest, middle, youngest]


async def test_a_handler_runs_after_the_claim_has_committed_with_no_transaction_held_open(
    db: Database, settings: Settings
) -> None:
    event_id = await enqueue_event(db)
    seen: dict[str, object] = {}

    async def handler(event: OutboxEvent) -> None:
        # A real handler opens its own transaction, as this one does, and may call a
        # provider. Neither may happen inside a transaction of the dispatcher's.
        async with db.transaction() as session:
            stored = await outbox.get_event(session, event.id)
            assert stored is not None
            seen["status"], seen["attempts"] = stored.status, stored.attempts
            seen["sessions_idle_in_transaction"] = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND state = 'idle in transaction'"
                    )
                )
            ).scalar_one()

    assert await dispatcher_for(db, settings, {TOPIC: handler}).run_once() == 1

    assert seen == {
        "status": EventStatus.PROCESSING,
        "attempts": 1,
        "sessions_idle_in_transaction": 0,
    }
    assert (await load(db, event_id)).status == EventStatus.DONE


# --- failure and retry -----------------------------------------------------------------------


async def test_a_failing_handler_leaves_the_event_pending_with_its_error_and_a_backoff(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    failing = Failing(RuntimeError("the provider is down"))
    dispatcher = dispatcher_for(db, settings, {TOPIC: failing}, rng=random.Random(42))  # noqa: S311 - jitter, not a secret
    clock.advance(seconds=3)

    assert await dispatcher.run_once() == 1

    event = await load(db, event_id)
    assert (event.status, event.attempts) == (EventStatus.PENDING, 1)
    assert event.last_error == "RuntimeError: the provider is down"
    assert (event.locked_until, event.finished_at) == (None, None)
    # Within the bound for a first failure, and exactly what the policy drew.
    assert clock.now() <= event.available_at <= clock.now() + timedelta(seconds=2)
    drawn = next_delay(1, rng=random.Random(42))  # noqa: S311 - jitter, not a secret
    assert event.available_at == clock.now() + timedelta(seconds=drawn)
    assert failing.calls == 1


async def test_a_failed_event_is_not_retried_before_its_backoff_has_passed(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    failing = Failing()
    dispatcher = dispatcher_for(db, settings, {TOPIC: failing}, rng=Longest())

    assert await dispatcher.run_once() == 1
    assert await dispatcher.run_once() == 0
    clock.advance(seconds=1.999)
    assert await dispatcher.run_once() == 0
    assert failing.calls == 1

    clock.advance(seconds=0.001)
    assert await dispatcher.run_once() == 1
    assert failing.calls == 2
    assert (await load(db, event_id)).attempts == 2


async def test_a_failed_event_that_then_succeeds_is_done_and_keeps_its_last_error(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)

    async def second_time_lucky(event: OutboxEvent) -> None:
        if event.attempts == 1:
            raise TimeoutError("no answer from the provider")

    dispatcher = dispatcher_for(db, settings, {TOPIC: second_time_lucky}, rng=Shortest())
    assert await dispatcher.drain() == 2

    event = await load(db, event_id)
    assert (event.status, event.attempts, event.finished_at) == (EventStatus.DONE, 2, clock.now())
    # Kept as a trace of what the event went through on the way.
    assert event.last_error == "TimeoutError: no answer from the provider"


async def test_after_eight_failures_an_event_is_dead(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    failing = Failing(RuntimeError("still down"))
    dispatcher = dispatcher_for(db, settings, {TOPIC: failing}, rng=Longest())

    for attempt in range(1, 8):
        assert await dispatcher.run_once() == 1
        event = await load(db, event_id)
        assert (event.status, event.attempts) == (EventStatus.PENDING, attempt)
        # The longest wait allowed doubles each time: 2, 4, 8 ... 128 seconds.
        assert event.available_at == clock.now() + timedelta(seconds=2**attempt)
        clock.advance(seconds=2**attempt)

    assert await dispatcher.run_once() == 1

    event = await load(db, event_id)
    assert (event.status, event.attempts) == (EventStatus.DEAD, 8)
    assert (event.finished_at, event.locked_until) == (clock.now(), None)
    assert event.last_error == "RuntimeError: still down"
    assert failing.calls == 8

    # Dead is final until an operator says otherwise, however much time passes.
    clock.advance(hours=24)
    assert await dispatcher.run_once() == 0
    assert failing.calls == 8


async def test_the_number_of_attempts_comes_from_the_settings(
    db: Database, settings: Settings
) -> None:
    event_id = await enqueue_event(db)
    failing = Failing()
    dispatcher = dispatcher_for(
        db, settings, {TOPIC: failing}, rng=Shortest(), outbox_max_attempts=2
    )

    assert await dispatcher.drain() == 2

    event = await load(db, event_id)
    assert (event.status, event.attempts, failing.calls) == (EventStatus.DEAD, 2, 2)


async def test_an_event_with_no_handler_is_dead_at_once(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db, "test.unheard_of")
    dispatcher = dispatcher_for(db, settings, {TOPIC: Recorder()})

    assert await dispatcher.run_once() == 1

    event = await load(db, event_id)
    # Not retried: nothing will change until a deploy brings a handler.
    assert (event.status, event.attempts) == (EventStatus.DEAD, 1)
    assert event.last_error == "no handler for topic test.unheard_of"
    assert (event.finished_at, event.locked_until) == (clock.now(), None)
    assert await dispatcher.run_once() == 0


async def test_a_recorded_error_has_its_secrets_removed(db: Database, settings: Settings) -> None:
    event_id = await enqueue_event(db)
    failing = Failing(RuntimeError(f"provider refused {NOT_A_TOKEN} with 401"))

    await dispatcher_for(db, settings, {TOPIC: failing}).run_once()

    assert (await load(db, event_id)).last_error == (
        f"RuntimeError: provider refused {REDACTED} with 401"
    )


async def test_a_recorded_error_is_cut_to_500_characters(db: Database, settings: Settings) -> None:
    event_id = await enqueue_event(db)
    failing = Failing(ValueError("x" * 2_000))

    await dispatcher_for(db, settings, {TOPIC: failing}).run_once()

    assert (await load(db, event_id)).last_error == ("ValueError: " + "x" * 2_000)[:500]


async def test_a_secret_that_straddles_the_cut_is_removed_whole(
    db: Database, settings: Settings
) -> None:
    event_id = await enqueue_event(db)
    # Cut first and the 500th character would fall five characters into the credential,
    # leaving a stump that no longer looks like one and would be stored.
    padding = "x" * 473
    failing = Failing(RuntimeError(f"{padding} {NOT_A_TOKEN}"))
    assert len(f"RuntimeError: {padding} Bearer abcde") == 500

    await dispatcher_for(db, settings, {TOPIC: failing}).run_once()

    assert (await load(db, event_id)).last_error == f"RuntimeError: {padding} {REDACTED}"


async def test_a_nul_byte_in_a_handlers_error_is_dropped_and_the_failure_is_recorded(
    db: Database, settings: Settings
) -> None:
    event_id = await enqueue_event(db)
    # PostgreSQL refuses a NUL in text. Stored as it is, the failure could never be recorded.
    failing = Failing(RuntimeError("the provider said \x00 and hung up"))

    await dispatcher_for(db, settings, {TOPIC: failing}).run_once()

    event = await load(db, event_id)
    assert (event.status, event.attempts) == (EventStatus.PENDING, 1)
    assert event.last_error == "RuntimeError: the provider said  and hung up"


async def test_a_failure_that_cannot_be_recorded_as_described_is_dead_with_a_constant_message(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    # Half a surrogate pair cannot be sent to PostgreSQL as text at all.
    failing = Failing(RuntimeError("the provider said \ud800"))
    dispatcher = dispatcher_for(db, settings, {TOPIC: failing})
    before = processed(TOPIC, "dead")

    await dispatcher.run_once()

    event = await load(db, event_id)
    assert (event.status, event.attempts) == (EventStatus.DEAD, 1)
    assert event.last_error == "the failure could not be recorded"
    assert (event.finished_at, event.locked_until) == (clock.now(), None)
    assert processed(TOPIC, "dead") == before + 1
    # It is over: no claim is left to run out and bring the event round again.
    clock.advance(seconds=settings.outbox_claim_seconds + 1)
    assert await dispatcher.run_once() == 0
    assert failing.calls == 1


@pytest.mark.parametrize(
    ("error", "recorded"),
    [
        (RuntimeError("boom"), "RuntimeError: boom"),
        (KeyError("amount"), "KeyError: 'amount'"),
        (Teapot("Short and stout."), "Teapot: Short and stout."),
        (ValueError(), "ValueError"),
        (
            ExceptionGroup("two things", [ValueError("a"), KeyError("b")]),
            "ExceptionGroup: two things (2 sub-exceptions)",
        ),
        (Indescribable(), "Indescribable: (no printable message)"),
    ],
    ids=["runtime", "key", "domain", "no-message", "group", "indescribable"],
)
async def test_a_handlers_exception_never_escapes_and_never_stops_the_rest_of_the_batch(
    db: Database, settings: Settings, error: Exception, recorded: str
) -> None:
    failed_id = await enqueue_event(db, "test.fails")
    fine_ids = [await enqueue_event(db, "test.works") for _ in range(3)]
    recorder = Recorder()
    dispatcher = dispatcher_for(
        db, settings, {"test.fails": Failing(error), "test.works": recorder}
    )

    assert await dispatcher.run_once() == 4

    failed = await load(db, failed_id)
    assert (failed.status, failed.attempts) == (EventStatus.PENDING, 1)
    assert failed.last_error == recorded
    assert sorted(recorder.ids) == fine_ids
    assert await status_counts(db) == {"done": 3, "pending": 1}


async def test_a_handler_cancelled_by_shutdown_is_not_a_failure(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db, "test.cancelled_by_shutdown")
    gate = Gate()
    dispatcher = dispatcher_for(db, settings, {"test.cancelled_by_shutdown": gate})
    before = [processed("test.cancelled_by_shutdown", outcome) for outcome in ("retry", "dead")]

    working = asyncio.create_task(dispatcher.run_once())
    await gate.entered()
    working.cancel()
    with pytest.raises(asyncio.CancelledError):
        await working

    # Nothing is recorded: no attempt is used up beyond the claim, no error, no backoff.
    # The claim simply runs out, and the event is picked up again.
    event = await load(db, event_id)
    assert (event.status, event.attempts, event.last_error) == (EventStatus.PROCESSING, 1, None)
    assert event.locked_until == clock.now() + timedelta(seconds=settings.outbox_claim_seconds)
    assert [
        processed("test.cancelled_by_shutdown", outcome) for outcome in ("retry", "dead")
    ] == before


async def test_a_handler_that_raises_cancellation_itself_does_not_take_the_batch_with_it(
    db: Database, settings: Settings
) -> None:
    cancelled_id = await enqueue_event(db, "test.cancels_itself")
    fine_id = await enqueue_event(db)

    async def cancels_itself(event: OutboxEvent) -> None:
        raise asyncio.CancelledError

    dispatcher = dispatcher_for(
        db, settings, {"test.cancels_itself": cancels_itself, TOPIC: Recorder()}
    )

    assert await dispatcher.run_once() == 2

    cancelled = await load(db, cancelled_id)
    assert (cancelled.status, cancelled.last_error) == (EventStatus.PROCESSING, None)
    assert (await load(db, fine_id)).status == EventStatus.DONE


# --- more than one worker --------------------------------------------------------------------


async def test_two_dispatchers_running_at_once_over_200_events_handle_each_exactly_once(
    db: Database, settings: Settings
) -> None:
    ids = await enqueue_events(db, 200)
    handled: Counter[uuid.UUID] = Counter()
    handled_by: dict[str, set[uuid.UUID]] = {"first": set(), "second": set()}

    def handler_of(name: str) -> Handler:
        async def handle(event: OutboxEvent) -> None:
            handled[event.id] += 1
            handled_by[name].add(event.id)

        return handle

    first = dispatcher_for(db, settings, {TOPIC: handler_of("first")}, outbox_batch_size=10)
    second = dispatcher_for(db, settings, {TOPIC: handler_of("second")}, outbox_batch_size=10)

    # Round after round the two claim in the same instant, so their claims really overlap:
    # each reads the queue before the other has committed what it took.
    async with asyncio.timeout(60):
        while sum(await asyncio.gather(first.run_once(), second.run_once())):
            pass

    assert handled == Counter(ids)
    assert handled_by["first"].isdisjoint(handled_by["second"])
    assert handled_by["first"]
    assert handled_by["second"]
    assert await status_counts(db) == {"done": 200}
    async with db.transaction() as session:
        attempts = (
            await session.execute(text("SELECT DISTINCT attempts FROM outbox_events"))
        ).scalars()
        assert list(attempts) == [1]


async def test_a_claim_passes_over_events_another_worker_has_locked_instead_of_waiting(
    db: Database, settings: Settings
) -> None:
    ids = await enqueue_events(db, 5)
    recorder = Recorder()
    dispatcher = dispatcher_for(db, settings, {TOPIC: recorder})

    async with db.transaction() as other_worker:
        # Another worker is part-way through claiming the two oldest events.
        for event_id in ids[:2]:
            await other_worker.execute(
                text("SELECT 1 FROM outbox_events WHERE id = :id FOR UPDATE"), {"id": event_id}
            )

        # Well inside the five seconds after which a statement stuck behind those locks
        # would give up: a claim that waits at all fails here.
        async with asyncio.timeout(3):
            assert await dispatcher.run_once() == 3

    assert sorted(recorder.ids) == ids[2:]
    assert await status_counts(db) == {"done": 3, "pending": 2}


async def test_an_event_claimed_by_a_dispatcher_that_died_is_picked_up_when_its_claim_expires(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    await abandon(db, settings)
    assert (await load(db, event_id)).status == EventStatus.PROCESSING

    recorder = Recorder()
    survivor = dispatcher_for(db, settings, {TOPIC: recorder})

    # While the claim holds, the event is the dead worker's and nobody else's.
    assert await survivor.run_once() == 0
    clock.advance(seconds=settings.outbox_claim_seconds)
    assert await survivor.run_once() == 0
    assert recorder.events == []

    clock.advance(seconds=0.001)
    assert await survivor.run_once() == 1

    event = await load(db, event_id)
    assert (event.status, event.attempts) == (EventStatus.DONE, 2)
    assert [handled.attempts for handled in recorder.events] == [2]


async def test_a_claim_that_expires_after_the_last_attempt_leaves_the_event_dead(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    last = settings.model_copy(update={"outbox_max_attempts": 1})
    event_id = await enqueue_event(db)
    await abandon(db, last)
    recorder = Recorder()
    survivor = dispatcher_for(db, last, {TOPIC: recorder})

    clock.advance(seconds=last.outbox_claim_seconds + 1)
    assert await survivor.run_once() == 0

    event = await load(db, event_id)
    assert (event.status, event.attempts) == (EventStatus.DEAD, 1)
    assert event.last_error == "claim expired after the last attempt"
    assert (event.finished_at, event.locked_until) == (clock.now(), None)
    assert recorder.events == []


async def test_workers_that_keep_dying_do_not_take_an_event_past_its_attempts(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)

    for attempt in range(1, settings.outbox_max_attempts + 1):
        await abandon(db, settings)
        assert (await load(db, event_id)).attempts == attempt
        clock.advance(seconds=settings.outbox_claim_seconds + 1)

    recorder = Recorder()
    assert await dispatcher_for(db, settings, {TOPIC: recorder}).drain() == 0
    event = await load(db, event_id)
    assert (event.status, event.attempts) == (EventStatus.DEAD, settings.outbox_max_attempts)
    assert recorder.events == []


async def test_an_event_buried_by_an_expired_claim_does_not_hold_up_the_ones_behind_it(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    last = settings.model_copy(update={"outbox_max_attempts": 1})
    exhausted = await enqueue_event(db)
    await abandon(db, last)
    clock.advance(seconds=last.outbox_claim_seconds + 1)
    behind = await enqueue_event(db)
    recorder = Recorder()

    assert await dispatcher_for(db, last, {TOPIC: recorder}).run_once() == 1

    assert recorder.ids == [behind]
    assert (await load(db, exhausted)).status == EventStatus.DEAD


async def test_a_handler_that_outlasts_most_of_its_claim_is_stopped_and_has_failed(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hundredth of a second of a one-second claim, so the test does not wait for it.
    monkeypatch.setattr(dispatcher_module, "HANDLER_SHARE_OF_CLAIM", 0.01)
    event_id = await enqueue_event(db)
    hanging = Gate()
    dispatcher = dispatcher_for(db, settings, {TOPIC: hanging}, outbox_claim_seconds=1)

    async with asyncio.timeout(5):
        assert await dispatcher.run_once() == 1

    event = await load(db, event_id)
    assert (event.status, event.attempts) == (EventStatus.PENDING, 1)
    assert event.last_error == "TimeoutError"


def test_a_handler_is_given_nine_tenths_of_the_claim() -> None:
    assert dispatcher_module.HANDLER_SHARE_OF_CLAIM == 0.9


async def test_a_late_finish_cannot_overwrite_the_result_of_a_newer_claim(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    slow = Gate()
    first = dispatcher_for(db, settings, {TOPIC: slow})
    stuck = asyncio.create_task(first.run_once())
    try:
        await slow.entered()

        # The first worker's claim runs out while its handler is still busy. A second
        # worker takes the event over, and its handler fails.
        clock.advance(seconds=settings.outbox_claim_seconds + 1)
        second = dispatcher_for(
            db, settings, {TOPIC: Failing(RuntimeError("the second worker's verdict"))}
        )
        assert await second.run_once() == 1
        as_the_second_left_it = await load(db, event_id)
        assert (as_the_second_left_it.status, as_the_second_left_it.attempts) == (
            EventStatus.PENDING,
            2,
        )

        # Only now does the first worker's handler return, and successfully at that.
        slow.open()
        assert await stuck == 1
    finally:
        await reap(stuck)

    assert await load(db, event_id) == as_the_second_left_it


async def test_a_late_finish_cannot_end_a_newer_claim_that_is_still_in_flight(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    slow, current = Gate(), Gate()
    first = dispatcher_for(db, settings, {TOPIC: slow})
    second = dispatcher_for(db, settings, {TOPIC: current})
    stuck = asyncio.create_task(first.run_once())
    try:
        await slow.entered()
        clock.advance(seconds=settings.outbox_claim_seconds + 1)
        working = asyncio.create_task(second.run_once())
        try:
            await current.entered()
            under_the_second_claim = await load(db, event_id)

            # The event is `processing` again, as it was when the first worker claimed it.
            # Only the claim id tells the two claims apart.
            slow.open()
            assert await stuck == 1
            assert await load(db, event_id) == under_the_second_claim
            assert (under_the_second_claim.status, under_the_second_claim.attempts) == (
                EventStatus.PROCESSING,
                2,
            )

            current.open()
            assert await working == 1
        finally:
            await reap(working)
    finally:
        await reap(stuck)

    event = await load(db, event_id)
    assert (event.status, event.attempts, event.finished_at) == (EventStatus.DONE, 2, clock.now())


async def test_a_late_finish_cannot_end_a_claim_made_after_a_requeue_reset_the_attempts(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    last = settings.model_copy(update={"outbox_max_attempts": 1})
    event_id = await enqueue_event(db)
    slow, current = Gate(), Gate()
    stuck = asyncio.create_task(dispatcher_for(db, last, {TOPIC: slow}).run_once())
    try:
        await slow.entered()

        # While the first worker's handler hangs: its claim runs out on the last attempt,
        # the event is buried, an operator requeues it, and a new worker claims it. The
        # event is `processing` on attempt 1 again, exactly as the first worker claimed it.
        clock.advance(seconds=last.outbox_claim_seconds + 1)
        assert await dispatcher_for(db, last, {}).run_once() == 0
        async with db.transaction() as session:
            assert await outbox.requeue(session, event_id) is True
        working = asyncio.create_task(dispatcher_for(db, last, {TOPIC: current}).run_once())
        try:
            await current.entered()
            under_the_new_claim = await load(db, event_id)
            assert (under_the_new_claim.status, under_the_new_claim.attempts) == (
                EventStatus.PROCESSING,
                1,
            )
            [as_first_claimed] = slow.events
            assert as_first_claimed.attempts == 1
            assert as_first_claimed.claim_id != under_the_new_claim.claim_id

            slow.open()
            assert await stuck == 1
            assert await load(db, event_id) == under_the_new_claim

            current.open()
            assert await working == 1
        finally:
            await reap(working)
    finally:
        await reap(stuck)

    event = await load(db, event_id)
    assert (event.status, event.attempts, event.finished_at) == (EventStatus.DONE, 1, clock.now())


async def test_every_claim_has_an_id_of_its_own_which_is_cleared_when_the_claim_ends(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    assert (await load(db, event_id)).claim_id is None
    recorder = Recorder()
    failing = dispatcher_for(
        db, settings, {TOPIC: Failing()}, rng=Shortest(), outbox_max_attempts=2
    )

    # Abandoned, then retried, then dead: three claims.
    await abandon(db, settings)
    abandoned = (await load(db, event_id)).claim_id
    assert abandoned is not None
    clock.advance(seconds=settings.outbox_claim_seconds + 1)
    retrying = dispatcher_for(db, settings, {TOPIC: Failing()}, rng=Shortest())
    assert await retrying.run_once() == 1
    retried = await load(db, event_id)
    assert (retried.status, retried.claim_id) == (EventStatus.PENDING, None)
    assert await failing.run_once() == 1
    dead = await load(db, event_id)
    assert (dead.status, dead.claim_id) == (EventStatus.DEAD, None)

    async with db.transaction() as session:
        assert await outbox.requeue(session, event_id) is True
    assert (await load(db, event_id)).claim_id is None
    assert await dispatcher_for(db, settings, {TOPIC: recorder}).run_once() == 1
    [handled] = recorder.events
    assert handled.claim_id not in (None, abandoned)
    done = await load(db, event_id)
    assert (done.status, done.claim_id) == (EventStatus.DONE, None)


async def test_a_claim_that_expires_after_the_last_attempt_leaves_no_claim_id(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    last = settings.model_copy(update={"outbox_max_attempts": 1})
    event_id = await enqueue_event(db)
    await abandon(db, last)
    clock.advance(seconds=last.outbox_claim_seconds + 1)

    assert await dispatcher_for(db, last, {}).run_once() == 0

    event = await load(db, event_id)
    assert (event.status, event.claim_id) == (EventStatus.DEAD, None)


async def test_a_result_cannot_be_recorded_for_an_event_that_was_never_claimed(
    db: Database, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    unclaimed = await load(db, event_id)

    # Without a claim id there is nothing to tell this caller's row from any other's.
    async with db.transaction() as session:
        with pytest.raises(ValueError, match="was not claimed"):
            await mark_done(session, unclaimed, now=clock.now())

    assert await load(db, event_id) == unclaimed


async def test_a_late_finish_cannot_touch_an_event_that_is_no_longer_being_processed(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    slow = Gate()
    first = dispatcher_for(db, settings, {TOPIC: slow})
    stuck = asyncio.create_task(first.run_once())
    try:
        await slow.entered()

        # While the first worker's handler hangs: its claim runs out, a worker without a
        # handler for the topic buries the event, an operator requeues it, and a third
        # worker tries and fails. The event is now waiting for its retry on attempt 1,
        # the very attempt number the first worker claimed it with.
        clock.advance(seconds=settings.outbox_claim_seconds + 1)
        assert await dispatcher_for(db, settings, {}).run_once() == 1
        async with db.transaction() as session:
            assert await outbox.requeue(session, event_id) is True
        third = dispatcher_for(
            db, settings, {TOPIC: Failing(RuntimeError("the third worker's verdict"))}
        )
        assert await third.run_once() == 1
        as_the_third_left_it = await load(db, event_id)
        assert (as_the_third_left_it.status, as_the_third_left_it.attempts) == (
            EventStatus.PENDING,
            1,
        )

        slow.open()
        assert await stuck == 1
    finally:
        await reap(stuck)

    assert await load(db, event_id) == as_the_third_left_it


async def test_no_more_handlers_run_at_once_than_the_concurrency_allows(
    db: Database, settings: Settings
) -> None:
    await enqueue_events(db, 12)
    running = 0
    peak = 0

    async def handler(event: OutboxEvent) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        # Let every other handler that is allowed to start do so before this one ends.
        await asyncio.sleep(0)
        running -= 1

    dispatcher = dispatcher_for(
        db, settings, {TOPIC: handler}, outbox_batch_size=12, outbox_concurrency=3
    )

    assert await dispatcher.run_once() == 12
    # The limit is reached, which shows events are handled concurrently, and never passed.
    assert peak == 3
    assert await status_counts(db) == {"done": 12}


# --- context, metrics and logs ---------------------------------------------------------------


async def test_an_events_request_id_is_in_the_log_context_while_its_handler_runs(
    db: Database, settings: Settings
) -> None:
    clear_context()
    bind_context(request_id="req-from-the-api")
    from_a_request = await enqueue_event(db)
    clear_context()
    from_a_job = await enqueue_event(db)

    seen: dict[uuid.UUID, dict[str, Any]] = {}

    async def handler(event: OutboxEvent) -> None:
        seen[event.id] = dict(current_context())

    # Whatever the dispatcher's caller has bound is the caller's, not the event's.
    bind_context(request_id="req-of-the-caller", caller="worker")
    try:
        assert await dispatcher_for(db, settings, {TOPIC: handler}).run_once() == 2
        assert current_context() == {"request_id": "req-of-the-caller", "caller": "worker"}
    finally:
        clear_context()

    assert seen == {from_a_request: {"request_id": "req-from-the-api"}, from_a_job: {}}


async def test_each_outcome_is_counted_by_topic(db: Database, settings: Settings) -> None:
    topics = ("test.counted_done", "test.counted_retry", "test.counted_unknown")
    before = {
        (topic, outcome): processed(topic, outcome)
        for topic in topics
        for outcome in ("done", "retry", "dead")
    }
    for _ in range(3):
        await enqueue_event(db, "test.counted_done")
    await enqueue_event(db, "test.counted_retry")
    await enqueue_event(db, "test.counted_unknown")
    dispatcher = dispatcher_for(
        db,
        settings,
        {"test.counted_done": Recorder(), "test.counted_retry": Failing()},
        rng=Shortest(),
        outbox_max_attempts=3,
    )

    assert await dispatcher.drain() == 7

    moved = {
        key: processed(*key) - value for key, value in before.items() if processed(*key) != value
    }
    assert moved == {
        ("test.counted_done", "done"): 3,
        ("test.counted_retry", "retry"): 2,
        ("test.counted_retry", "dead"): 1,
        ("test.counted_unknown", "dead"): 1,
    }


async def test_each_outcome_is_logged_with_the_event_and_never_its_payload(
    db: Database, settings: Settings, logs: Callable[[], list[dict[str, Any]]]
) -> None:
    payload = {"note": PAYLOAD_MARKER}
    done = await enqueue_event(db, "test.works", payload)
    retried = await enqueue_event(db, "test.fails", payload)
    dead = await enqueue_event(db, "test.unheard_of", payload)
    dispatcher = dispatcher_for(
        db,
        settings,
        {"test.works": Recorder(), "test.fails": Failing(RuntimeError(f"401 for {NOT_A_TOKEN}"))},
    )

    assert await dispatcher.run_once() == 3

    lines = [line for line in logs() if str(line["event"]).startswith("outbox.")]
    assert {
        line["event"]: (line["event_id"], line["topic"], line["attempt"], line["level"])
        for line in lines
    } == {
        "outbox.event_done": (str(done), "test.works", 1, "info"),
        "outbox.event_retry": (str(retried), "test.fails", 1, "warning"),
        "outbox.event_dead": (str(dead), "test.unheard_of", 1, "error"),
    }
    written = json.dumps(lines)
    assert PAYLOAD_MARKER not in written
    assert "abcdefghijklmnop" not in written


# --- the registry ----------------------------------------------------------------------------


def test_a_topic_has_one_handler() -> None:
    registry = Registry()
    first, second = Recorder(), Recorder()
    registry.register(TOPIC, first)

    with pytest.raises(ValueError, match="already has a handler"):
        registry.register(TOPIC, second)

    assert registry.handler_for(TOPIC) is first
    assert registry.handler_for("test.unheard_of") is None


# --- looking after the queue -----------------------------------------------------------------


async def test_requeue_returns_a_dead_event_to_pending_and_it_is_then_handled(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    event_id = await enqueue_event(db)
    assert await dispatcher_for(db, settings, {}).run_once() == 1
    buried = await load(db, event_id)
    assert buried.status == EventStatus.DEAD

    clock.advance(hours=2)
    async with db.transaction() as session:
        assert await outbox.requeue(session, event_id) is True

    event = await load(db, event_id)
    assert (event.status, event.attempts, event.available_at) == (
        EventStatus.PENDING,
        0,
        clock.now(),
    )
    assert (event.finished_at, event.locked_until) == (None, None)
    # What went wrong stays on the event until something else does.
    assert event.last_error == buried.last_error == f"no handler for topic {TOPIC}"

    recorder = Recorder()
    assert await dispatcher_for(db, settings, {TOPIC: recorder}).run_once() == 1
    assert recorder.ids == [event_id]
    event = await load(db, event_id)
    assert (event.status, event.attempts) == (EventStatus.DONE, 1)


async def test_requeue_clears_a_claim_id_left_on_a_dead_event(
    db: Database, settings: Settings
) -> None:
    event_id = await enqueue_event(db)
    await dispatcher_for(db, settings, {}).run_once()
    # Nothing in the application leaves one there. A row repaired by hand might.
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE outbox_events SET claim_id = :claim WHERE id = :id"),
            {"claim": new_id(), "id": event_id},
        )

    async with db.transaction() as session:
        assert await outbox.requeue(session, event_id) is True

    assert (await load(db, event_id)).claim_id is None


async def test_requeue_can_be_told_the_time(db: Database, settings: Settings) -> None:
    event_id = await enqueue_event(db)
    await dispatcher_for(db, settings, {}).run_once()
    moment = (await load(db, event_id)).created_at + timedelta(days=3)

    async with db.transaction() as session:
        assert await outbox.requeue(session, event_id, now=moment) is True

    assert (await load(db, event_id)).available_at == moment


async def test_requeue_refuses_an_event_that_is_not_dead(db: Database, settings: Settings) -> None:
    done = await enqueue_event(db)
    await dispatcher_for(db, settings, {TOPIC: Recorder()}).run_once()
    processing = await enqueue_event(db)
    await abandon(db, settings)
    pending = await enqueue_event(db)
    untouched = [await load(db, event_id) for event_id in (pending, done, processing)]
    assert [event.status for event in untouched] == [
        EventStatus.PENDING,
        EventStatus.DONE,
        EventStatus.PROCESSING,
    ]

    async with db.transaction() as session:
        for event_id in (pending, done, processing, new_id()):
            assert await outbox.requeue(session, event_id) is False

    assert [await load(db, event_id) for event_id in (pending, done, processing)] == untouched


async def test_purge_finished_deletes_old_done_events_and_nothing_else(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    works = {"test.works": Recorder()}
    old_done = [await enqueue_event(db, "test.works") for _ in range(3)]
    old_dead = await enqueue_event(db, "test.unheard_of")
    old_pending = await enqueue_event(
        db, "test.works", available_at=clock.now() + timedelta(days=30)
    )
    assert await dispatcher_for(db, settings, works).run_once() == 4

    clock.advance(hours=1)
    on_the_cutoff = await enqueue_event(db, "test.works")
    assert await dispatcher_for(db, settings, works).run_once() == 1
    cutoff = clock.now()

    clock.advance(hours=1)
    recent_done = await enqueue_event(db, "test.works")
    assert await dispatcher_for(db, settings, works).run_once() == 1
    in_flight = await enqueue_event(db)
    await abandon(db, settings)

    async with db.transaction() as session:
        assert await outbox.purge_finished(session, older_than=cutoff) == 3

    async with db.transaction() as session:
        for event_id in old_done:
            assert await outbox.get_event(session, event_id) is None
    # Dead events wait for an operator however old they are, and unfinished ones are work.
    kept = [old_dead, old_pending, in_flight, on_the_cutoff, recent_done]
    assert [(await load(db, event_id)).status for event_id in kept] == [
        EventStatus.DEAD,
        EventStatus.PENDING,
        EventStatus.PROCESSING,
        EventStatus.DONE,
        EventStatus.DONE,
    ]

    async with db.transaction() as session:
        assert await outbox.purge_finished(session, older_than=cutoff) == 0


async def test_stats_count_what_is_waiting_how_long_and_what_is_dead(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    async with db.transaction() as session:
        assert await outbox.stats(session) == OutboxStats(
            pending=0, oldest_pending_seconds=0.0, dead=0
        )

    await enqueue_event(db, "test.unheard_of")
    await enqueue_event(db, "test.unheard_of")
    await enqueue_event(db, "test.works")
    assert await dispatcher_for(db, settings, {"test.works": Recorder()}).run_once() == 3

    await enqueue_event(db)
    clock.advance(seconds=30)
    await enqueue_event(db)
    await enqueue_event(db, available_at=clock.now() + timedelta(hours=1))
    clock.advance(seconds=12.5)

    async with db.transaction() as session:
        # Three are waiting. The one scheduled for later counts as pending but is not yet
        # late, so the age is that of the oldest event that is actually due.
        assert await outbox.stats(session) == OutboxStats(
            pending=3, oldest_pending_seconds=42.5, dead=2
        )


async def test_stats_show_no_age_when_nothing_pending_is_due_yet(
    db: Database, clock: ManualClock
) -> None:
    await enqueue_event(db, available_at=clock.now() + timedelta(minutes=5))

    async with db.transaction() as session:
        assert await outbox.stats(session) == OutboxStats(
            pending=1, oldest_pending_seconds=0.0, dead=0
        )


async def test_dead_events_are_listed_newest_first_a_page_at_a_time(
    db: Database, settings: Settings
) -> None:
    dead = [await enqueue_event(db, "test.unheard_of") for _ in range(5)]
    alive = await enqueue_event(db, "test.works")
    assert await dispatcher_for(db, settings, {"test.works": Recorder()}).run_once() == 6

    async with db.transaction() as session:
        first_page = await outbox.list_dead(session, limit=2)
        second_page = await outbox.list_dead(session, before=first_page[-1].id, limit=2)
        last_page = await outbox.list_dead(session, before=second_page[-1].id, limit=2)
        everything = await outbox.list_dead(session)

    newest_first = dead[::-1]
    assert [event.id for event in first_page] == newest_first[:2]
    assert [event.id for event in second_page] == newest_first[2:4]
    assert [event.id for event in last_page] == newest_first[4:]
    assert [event.id for event in everything] == newest_first
    assert alive not in {event.id for event in everything}
    assert {event.status for event in everything} == {EventStatus.DEAD}
    assert everything[0].last_error == "no handler for topic test.unheard_of"


async def test_a_page_of_dead_events_is_never_longer_than_200(
    db: Database, settings: Settings
) -> None:
    await enqueue_events(db, 205, "test.unheard_of")
    assert await dispatcher_for(db, settings, {}, outbox_batch_size=205).run_once() == 205

    async with db.transaction() as session:
        assert len(await outbox.list_dead(session)) == 50
        assert len(await outbox.list_dead(session, limit=200)) == 200
        assert len(await outbox.list_dead(session, limit=10_000)) == 200

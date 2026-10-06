"""Every statement against the outbox table.

Each function takes the caller's session and runs inside the caller's transaction. Nothing
here commits, and that is the point of an outbox: an event enqueued alongside a state
change exists if and only if that state change committed. See section 7.1 of the
architecture.
"""

import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    RowMapping,
    Table,
    and_,
    delete,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.outbox.models import OutboxEventRow
from corridor.outbox.types import EventStatus, OutboxEvent, OutboxStats
from corridor.platform.clock import utcnow
from corridor.platform.ids import new_id
from corridor.platform.logging import current_context

# A Core table. The outbox writes with explicit statements and never through the ORM's unit
# of work, so what reaches the database is exactly what is written here.
_events = cast(Table, OutboxEventRow.__table__)

# The rule the table's CHECK enforces: dotted lower-case words, "payments.withdrawal_requested".
_TOPIC: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")

MAX_PAGE_SIZE: Final = 200

# Statuses are written into the SQL as literals, not bound. The partial indexes are
# declared `WHERE status = '...'`, and PostgreSQL can use one in a plan it keeps for a
# prepared statement only if it can read the status in the statement itself. With a bound
# status it falls back, after a few executions, to walking the whole table on every claim.
_PENDING: Final = literal(EventStatus.PENDING.value, literal_execute=True)
_PROCESSING: Final = literal(EventStatus.PROCESSING.value, literal_execute=True)
_DONE: Final = literal(EventStatus.DONE.value, literal_execute=True)
_DEAD: Final = literal(EventStatus.DEAD.value, literal_execute=True)


# --- enqueueing ------------------------------------------------------------------------------


async def enqueue(
    session: AsyncSession,
    topic: str,
    payload: Mapping[str, Any],
    *,
    available_at: datetime | None = None,
    dedup_key: str | None = None,
) -> uuid.UUID | None:
    """Write an event in the caller's transaction and return its id.

    The event becomes visible to a worker when the caller commits, and the insert trigger
    wakes one then. With a ``dedup_key``, a second event for the same topic and key is not
    written and ``None`` is returned, so a caller that may run twice enqueues once.

    The payload must be JSON: turning ids and timestamps into strings is the caller's job.
    """
    if _TOPIC.fullmatch(topic) is None:
        raise ValueError(f"malformed outbox topic {topic!r}: expected dotted lower-case words")

    now = utcnow()
    inserted = await session.execute(
        pg_insert(_events)
        .values(
            id=new_id(),
            topic=topic,
            payload=dict(payload),
            status=_PENDING,
            attempts=0,
            available_at=available_at if available_at is not None else now,
            locked_until=None,
            dedup_key=dedup_key,
            last_error=None,
            context=_carried_context(),
            created_at=now,
            finished_at=None,
        )
        .on_conflict_do_nothing(
            index_elements=[_events.c.topic, _events.c.dedup_key],
            index_where=_events.c.dedup_key.is_not(None),
        )
        .returning(_events.c.id)
    )
    event_id: uuid.UUID | None = inserted.scalar_one_or_none()
    return event_id


def _carried_context() -> dict[str, Any]:
    """What of the current log context goes with an event: the request id, if there is one."""
    request_id = current_context().get("request_id")
    return {"request_id": str(request_id)} if request_id is not None else {}


# --- claiming and finishing: the dispatcher's statements -------------------------------------


async def claim(
    session: AsyncSession, *, now: datetime, limit: int, claim_seconds: int
) -> list[OutboxEvent]:
    """Claim up to ``limit`` events for ``claim_seconds`` and return them, oldest first.

    Two kinds of event can be claimed: one that is pending and due, and one whose earlier
    claim has run out, which means the worker that held it died or is too slow to count on.
    ``SKIP LOCKED`` is what lets several workers claim at once: each passes over the rows
    another is claiming at that moment instead of queueing behind them.
    """
    due = (
        select(_events.c.id)
        .where(
            or_(
                and_(_events.c.status == _PENDING, _events.c.available_at <= now),
                and_(_events.c.status == _PROCESSING, _events.c.locked_until < now),
            )
        )
        .order_by(_events.c.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        # A CTE, and not `WHERE id IN (SELECT ...)`: PostgreSQL may run an IN subquery once
        # per outer row, and each run locks the next rows along, so the limit stops being
        # one. It does so when its statistics say the table is empty, which is when a queue
        # tends to be analysed. A materialised CTE is evaluated exactly once.
        .cte("due")
        .prefix_with("MATERIALIZED")
    )
    claimed = await session.execute(
        update(_events)
        .where(_events.c.id == due.c.id)
        .values(
            status=_PROCESSING,
            attempts=_events.c.attempts + 1,
            locked_until=now + timedelta(seconds=claim_seconds),
        )
        .returning(_events)
    )
    # RETURNING promises no order.
    return sorted((_event(row) for row in claimed.mappings()), key=lambda event: event.id)


async def mark_done(session: AsyncSession, claimed: OutboxEvent, *, now: datetime) -> bool:
    """Record that the handler succeeded. False if the claim was lost in the meantime."""
    return await _finish(session, claimed, status=_DONE, finished_at=now)


async def mark_for_retry(
    session: AsyncSession, claimed: OutboxEvent, *, available_at: datetime, error: str
) -> bool:
    """Record a failure and put the event back in the queue for ``available_at``."""
    return await _finish(
        session, claimed, status=_PENDING, available_at=available_at, last_error=error
    )


async def mark_dead(
    session: AsyncSession, claimed: OutboxEvent, *, now: datetime, error: str
) -> bool:
    """Record a failure that will not be retried. The event waits for an operator."""
    return await _finish(session, claimed, status=_DEAD, finished_at=now, last_error=error)


async def _finish(session: AsyncSession, claimed: OutboxEvent, **values: Any) -> bool:
    finished = await session.execute(
        update(_events)
        .where(_still_held(claimed))
        .values(locked_until=None, **values)
        .returning(_events.c.id)
    )
    return finished.scalar_one_or_none() is not None


def _still_held(claimed: OutboxEvent) -> ColumnElement[bool]:
    """True for the event's row only while it is under the claim ``claimed`` was read in.

    A worker whose claim ran out before it finished may find that another worker has
    claimed the event again, or has already recorded a result. Its own result is then
    stale and must change nothing. The attempt number tells one claim from the next.
    """
    return and_(
        _events.c.id == claimed.id,
        _events.c.status == _PROCESSING,
        _events.c.attempts == claimed.attempts,
    )


# --- reading ---------------------------------------------------------------------------------


async def get_event(session: AsyncSession, event_id: uuid.UUID) -> OutboxEvent | None:
    rows = await session.execute(select(_events).where(_events.c.id == event_id))
    row = rows.mappings().one_or_none()
    return _event(row) if row is not None else None


async def list_dead(
    session: AsyncSession, *, before: uuid.UUID | None = None, limit: int = 50
) -> list[OutboxEvent]:
    """Dead events, newest first, starting below ``before``. At most 200 at a time."""
    query = (
        select(_events)
        .where(_events.c.status == _DEAD)
        .order_by(_events.c.id.desc())
        .limit(min(limit, MAX_PAGE_SIZE))
    )
    if before is not None:
        query = query.where(_events.c.id < before)
    rows = await session.execute(query)
    return [_event(row) for row in rows.mappings()]


async def stats(session: AsyncSession) -> OutboxStats:
    """How much is waiting, how long the oldest due event has waited, and how much is dead."""
    now = utcnow()
    waiting = (
        await session.execute(
            select(
                func.count(),
                # An event scheduled for later is pending but is not late.
                func.min(_events.c.available_at).filter(_events.c.available_at <= now),
            ).where(_events.c.status == _PENDING)
        )
    ).one()
    dead = (
        await session.execute(select(func.count()).where(_events.c.status == _DEAD))
    ).scalar_one()

    pending, oldest_due = waiting
    return OutboxStats(
        pending=pending,
        oldest_pending_seconds=(
            (now - oldest_due).total_seconds() if oldest_due is not None else 0.0
        ),
        dead=dead,
    )


def _event(row: RowMapping) -> OutboxEvent:
    return OutboxEvent(
        id=row["id"],
        topic=row["topic"],
        payload=row["payload"],
        status=EventStatus(row["status"]),
        attempts=row["attempts"],
        available_at=row["available_at"],
        locked_until=row["locked_until"],
        dedup_key=row["dedup_key"],
        last_error=row["last_error"],
        context=row["context"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


# --- looking after the queue -----------------------------------------------------------------


async def requeue(
    session: AsyncSession, event_id: uuid.UUID, *, now: datetime | None = None
) -> bool:
    """Give a dead event a full set of attempts again, due at once. False if it is not dead.

    Its last error is kept until a new attempt replaces it. No worker is notified: the
    event is found at the next poll.
    """
    requeued = await session.execute(
        update(_events)
        .where(_events.c.id == event_id, _events.c.status == _DEAD)
        .values(
            status=_PENDING,
            attempts=0,
            available_at=now if now is not None else utcnow(),
            finished_at=None,
        )
        .returning(_events.c.id)
    )
    return requeued.scalar_one_or_none() is not None


async def purge_finished(session: AsyncSession, *, older_than: datetime) -> int:
    """Delete the events that were done before ``older_than``, and say how many.

    Dead events are never deleted here, however old: each is waiting for an operator.
    """
    deleted = await session.execute(
        delete(_events).where(_events.c.status == _DONE, _events.c.finished_at < older_than)
    )
    return cast(CursorResult[Any], deleted).rowcount

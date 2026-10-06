"""The outbox's vocabulary: an event as a handler receives it, and the state of the queue."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

# The channel the insert trigger notifies. A worker listens on it to hear of new events at
# once instead of at its next poll.
NOTIFY_CHANNEL: Final = "corridor_outbox"


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    # Out of attempts, or no handler: it stays until an operator requeues it.
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """One event, as its row was when it was read."""

    id: uuid.UUID
    topic: str
    payload: Mapping[str, Any]
    status: EventStatus
    # How many times a worker has claimed it, this time included.
    attempts: int
    available_at: datetime
    locked_until: datetime | None
    dedup_key: str | None
    last_error: str | None
    # What the request that enqueued it wants carried across the queue: its request id.
    context: Mapping[str, Any]
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class OutboxStats:
    """What the queue's gauges show."""

    pending: int
    # How long the oldest event that is due has been waiting; zero when none is due.
    oldest_pending_seconds: float
    dead: int

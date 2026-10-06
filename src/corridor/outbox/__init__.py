"""The transactional outbox: events written with the state change that causes them.

A module enqueues an event inside its own transaction; a worker's dispatcher later claims
it, runs the handler registered for its topic and retries until that succeeds. Delivery is
at least once, so handlers are idempotent.

Other modules use the names exported here and nothing else from this package.
"""

from corridor.outbox.dispatcher import Dispatcher, Handler, Registry
from corridor.outbox.service import (
    enqueue,
    get_event,
    list_dead,
    purge_finished,
    requeue,
    stats,
)
from corridor.outbox.types import NOTIFY_CHANNEL, EventStatus, OutboxEvent, OutboxStats

__all__ = [
    "NOTIFY_CHANNEL",
    "Dispatcher",
    "EventStatus",
    "Handler",
    "OutboxEvent",
    "OutboxStats",
    "Registry",
    "enqueue",
    "get_event",
    "list_dead",
    "purge_finished",
    "requeue",
    "stats",
]

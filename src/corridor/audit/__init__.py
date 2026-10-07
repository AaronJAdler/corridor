"""The audit log: an append-only record of who did what, to what, for whom, and how it ended.

Other modules use the names exported here and nothing else from this package.
"""

from corridor.audit.service import MAX_PAGE_SIZE, list_events, record
from corridor.audit.types import Actor, ActorType, AuditEvent, Outcome

__all__ = [
    "MAX_PAGE_SIZE",
    "Actor",
    "ActorType",
    "AuditEvent",
    "Outcome",
    "list_events",
    "record",
]

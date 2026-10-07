"""Dead letters: the outbox events that ran out of attempts and wait for an operator.

Each function takes the caller's session and runs inside the caller's transaction. Each
needs an administrator and says so in the audit log.
"""

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, outbox
from corridor.identity import Principal
from corridor.ops.errors import DeadLetterNotFound
from corridor.outbox import EventStatus, OutboxEvent
from corridor.platform.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursor,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)

CURSOR_KIND: Final = "dead_letters"
_SCOPE: Final = "all"


async def list_dead_letters(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[OutboxEvent]:
    """One page of dead events, newest first.

    Pages are cut on the event id, which is a UUIDv7 and so in order of creation.
    """
    identity.require_admin(principal)
    limit = clamp_limit(limit)
    before = _position(cursor) if cursor is not None else None
    # One more than the page, to learn whether anything follows it. The outbox gives at
    # most the largest page at a time, so a page of that size is taken to have a successor.
    found = await outbox.list_dead(session, before=before, limit=limit + 1)
    shown = found[:limit]
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="outbox.dead_listed",
        resource_type="outbox_event",
        details={"returned": len(shown)},
    )
    more = len(found) > limit or len(shown) == MAX_LIMIT
    return Page(
        items=tuple(shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=_SCOPE, position=str(shown[-1].id))
            if more
            else None
        ),
    )


async def requeue_dead_letter(
    session: AsyncSession, principal: Principal, event_id: uuid.UUID
) -> OutboxEvent:
    """Give a dead event a full set of attempts again, due at once.

    Only a dead event is touched: one that is waiting, in hand or done is answered as one
    that does not exist, so requeueing twice cannot give an event two lives.
    """
    identity.require_admin(principal)
    if not await outbox.requeue(session, event_id):
        raise DeadLetterNotFound
    event = await outbox.get_event(session, event_id)
    if event is None or event.status is not EventStatus.PENDING:
        raise RuntimeError(f"outbox event {event_id} was requeued and is not pending")
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="outbox.dead_requeued",
        resource_type="outbox_event",
        resource_id=event_id,
        details={"topic": event.topic, "last_error": event.last_error},
    )
    return event


def _position(cursor: str) -> uuid.UUID:
    position = decode_cursor(cursor, kind=CURSOR_KIND, scope=_SCOPE)
    if not isinstance(position, str):
        raise InvalidCursor
    try:
        return uuid.UUID(position)
    except ValueError:
        raise InvalidCursor from None

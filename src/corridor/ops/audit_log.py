"""Reading the audit log: who did what, to what, for whom, for an operator who asks.

The function takes the caller's session and runs inside the caller's transaction. It
needs an administrator. The read is itself written to the log, once for each request and
not for each event it returned: who looked, and for what.
"""

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity
from corridor.audit import AuditEvent
from corridor.identity import Principal
from corridor.platform.pagination import (
    DEFAULT_LIMIT,
    InvalidCursor,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)

CURSOR_KIND: Final = "audit_events"
# A cursor is a place in the whole log, whatever is being looked for: the filters are
# applied again to what lies below it.
_SCOPE: Final = "all"


async def list_audit_events(
    session: AsyncSession,
    principal: Principal,
    *,
    actor: str | None = None,
    action_prefix: str | None = None,
    subject: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[AuditEvent]:
    """One page of the events that match every filter given, newest first.

    ``actor`` is the id of whoever acted, ``action_prefix`` what an action begins with,
    ``subject`` the id of what was acted on or of the user it was done for, and the events
    are those from ``since`` up to but not including ``until``.
    """
    identity.require_admin(principal)
    limit = clamp_limit(limit)
    before = _position(cursor) if cursor is not None else None
    # One more than the page, to learn whether anything follows it. The log gives at most
    # its largest page at a time, so a page of that size is taken to have a successor.
    found = await audit.list_events(
        session,
        actor_id=actor,
        action_prefix=action_prefix,
        subject=subject,
        since=since,
        until=until,
        before=before,
        limit=limit + 1,
    )
    shown = found[:limit]
    filters = {
        "actor": actor,
        "action": action_prefix,
        "subject": subject,
        "since": None if since is None else since.isoformat(),
        "until": None if until is None else until.isoformat(),
    }
    # Written after the page was read, so that a read is never in its own answer.
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="audit.listed",
        resource_type="audit_event",
        details={
            "returned": len(shown),
            "filters": {name: value for name, value in filters.items() if value is not None},
        },
    )
    more = len(found) > limit or len(shown) == audit.MAX_PAGE_SIZE
    return Page(
        items=tuple(shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=_SCOPE, position=str(shown[-1].id))
            if more
            else None
        ),
    )


def _position(cursor: str) -> uuid.UUID:
    position = decode_cursor(cursor, kind=CURSOR_KIND, scope=_SCOPE)
    if not isinstance(position, str):
        raise InvalidCursor
    try:
        return uuid.UUID(position)
    except ValueError:
        raise InvalidCursor from None

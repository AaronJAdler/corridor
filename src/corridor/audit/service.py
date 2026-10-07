"""Recording and reading audit events.

Every function takes the caller's session and runs inside the caller's transaction. Nothing
here commits, so an event exists if and only if the work it describes was committed.
"""

import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, cast

from sqlalchemy import RowMapping, Table, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.audit.models import AuditEventRow
from corridor.audit.types import Actor, AuditEvent, Outcome
from corridor.platform.clock import utcnow
from corridor.platform.ids import new_id
from corridor.platform.logging import current_context, scrub

# A Core table. Events are written with explicit statements and never through the ORM's
# unit of work, so what reaches the database is exactly what is written here.
_events = cast(Table, AuditEventRow.__table__)

# Dotted lower-case segments, at least two: "transfer.created". The table's check
# constraint states the same rule. It is matched with fullmatch, because "$" alone would
# let a trailing newline through.
_ACTION: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")

MAX_PAGE_SIZE: Final = 200


async def record(
    session: AsyncSession,
    *,
    actor: Actor,
    action: str,
    outcome: Outcome = "success",
    principal_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | uuid.UUID | None = None,
    details: Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> uuid.UUID:
    """Append an event to the audit log and return its id.

    The event is stamped with the application clock. ``principal_id`` is the user on whose
    behalf the actor acted. ``request_id`` defaults to the one the request middleware bound
    to the log context, so an event written while serving a request can be tied to it.

    A malformed ``action`` is a programming error and raises ``ValueError`` before any SQL
    runs, which leaves the caller's transaction usable.
    """
    if _ACTION.fullmatch(action) is None:
        raise ValueError(
            f"{action!r} is not an audit action: use dotted lower-case names"
            ' such as "transfer.created"'
        )

    event_id = new_id()
    await session.execute(
        insert(_events).values(
            id=event_id,
            occurred_at=utcnow(),
            actor_type=actor.type,
            actor_id=actor.id,
            principal_id=principal_id,
            action=action,
            resource_type=resource_type,
            resource_id=None if resource_id is None else str(resource_id),
            outcome=outcome,
            request_id=request_id if request_id is not None else _request_id_in_context(),
            # An audit log outlives every other record, so a secret that reaches it is a
            # secret kept forever. Whatever the caller passed, what is stored is scrubbed.
            details=scrub(details or {}),
        )
    )
    return event_id


def _request_id_in_context() -> str | None:
    bound = current_context().get("request_id")
    # The column is text, whatever was bound.
    return None if bound is None else str(bound)


async def list_events(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | uuid.UUID | None = None,
    action: str | None = None,
    action_prefix: str | None = None,
    actor_id: str | None = None,
    subject: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before: uuid.UUID | None = None,
    limit: int = 50,
) -> list[AuditEvent]:
    """Events that match every filter given, newest first, starting below ``before``.

    ``action_prefix`` matches the actions that begin with it, read as it is written and
    not as a pattern. ``subject`` is an id, and matches the events about it either way an
    event can be: as the resource acted on, or as the user it was done for. ``since`` is
    the first moment included and ``until`` the first that is not.

    Ids are UUIDv7, so id order is time order, and the id of the last event on one page is
    the cursor for the next. A page holds at most ``MAX_PAGE_SIZE`` events, however many
    were asked for.
    """
    if limit < 1:
        # An empty page would read as the end of the log, and a negative limit is an error
        # in PostgreSQL that would abort the caller's transaction.
        raise ValueError("a page holds at least one event")

    query = select(_events).order_by(_events.c.id.desc()).limit(min(limit, MAX_PAGE_SIZE))
    if principal_id is not None:
        query = query.where(_events.c.principal_id == principal_id)
    if resource_type is not None:
        query = query.where(_events.c.resource_type == resource_type)
    if resource_id is not None:
        query = query.where(_events.c.resource_id == str(resource_id))
    if action is not None:
        query = query.where(_events.c.action == action)
    if action_prefix is not None:
        query = query.where(_events.c.action.startswith(action_prefix, autoescape=True))
    if actor_id is not None:
        query = query.where(_events.c.actor_id == actor_id)
    if subject is not None:
        about = _events.c.resource_id == subject
        user_id = _as_uuid(subject)
        query = query.where(
            about if user_id is None else or_(about, _events.c.principal_id == user_id)
        )
    if since is not None:
        query = query.where(_events.c.occurred_at >= since)
    if until is not None:
        query = query.where(_events.c.occurred_at < until)
    if before is not None:
        query = query.where(_events.c.id < before)
    rows = await session.execute(query)
    return [_event(row) for row in rows.mappings()]


def _as_uuid(text: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _event(row: RowMapping) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        occurred_at=row["occurred_at"],
        actor_type=row["actor_type"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        action=row["action"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        outcome=row["outcome"],
        request_id=row["request_id"],
        details=row["details"],
    )

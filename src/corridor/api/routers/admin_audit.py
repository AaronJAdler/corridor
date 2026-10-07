"""The admin endpoint that reads the audit log.

It needs an administrator, and the service checks again. The read is itself written to
the log, in the transaction that serves it: once for the request, whatever it returned.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from fastapi import APIRouter, Query
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, ops
from corridor.api.deps import AdminPrincipal, Db
from corridor.platform.pagination import DEFAULT_LIMIT, Page

router = APIRouter(prefix="/v1/admin/audit", tags=["admin"])

# Far above any id or action there is. These bound what a request can make the server
# compare.
_MAX_ID_LENGTH = 200
_MAX_ACTION_LENGTH = 100
# What an action is made of, so that what is asked for could be the beginning of one.
_ACTION_PREFIX = r"^[a-z][a-z0-9_.]*$"


class AuditEventResponse(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    actor_type: audit.ActorType
    # The id of the user, admin or agent who acted, or the name of the job or provider.
    actor_id: str | None
    # The user it was done for or to, when there is one.
    principal_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: str | None
    outcome: audit.Outcome
    request_id: str | None
    details: dict[str, Any]

    @classmethod
    def of(cls, event: audit.AuditEvent) -> Self:
        return cls(
            id=event.id,
            occurred_at=event.occurred_at,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            principal_id=event.principal_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            outcome=event.outcome,
            request_id=event.request_id,
            details=dict(event.details),
        )


class AuditEventPageResponse(BaseModel):
    items: list[AuditEventResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


@router.get("", summary="The audit log, newest first")
async def list_audit_events(
    principal: AdminPrincipal,
    db: Db,
    actor: Annotated[
        str | None,
        Query(max_length=_MAX_ID_LENGTH, description="The id of whoever acted."),
    ] = None,
    action: Annotated[
        str | None,
        Query(
            max_length=_MAX_ACTION_LENGTH,
            pattern=_ACTION_PREFIX,
            description="What the action begins with: `user.` or `adjustment.approved`.",
        ),
    ] = None,
    subject: Annotated[
        str | None,
        Query(
            max_length=_MAX_ID_LENGTH,
            description="The id of what was acted on, or of the user it was done for.",
        ),
    ] = None,
    since: Annotated[
        AwareDatetime | None, Query(description="The first moment included, with its offset.")
    ] = None,
    until: Annotated[
        AwareDatetime | None, Query(description="The first moment not included.")
    ] = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> AuditEventPageResponse:
    async def work(session: AsyncSession) -> Page[audit.AuditEvent]:
        return await ops.list_audit_events(
            session,
            principal,
            actor=actor,
            action_prefix=action,
            subject=subject,
            since=since,
            until=until,
            cursor=cursor,
            limit=limit,
        )

    page = await db.run(work)
    return AuditEventPageResponse(
        items=[AuditEventResponse.of(event) for event in page.items],
        next_cursor=page.next_cursor,
    )

"""Admin endpoints for reconciliation: read the runs and the breaks, and resolve a break
with a note.

Every route here needs an administrator, and the service checks again. What an admin reads
or does is written to the audit log in the transaction that serves it.
"""

import uuid
from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import recon
from corridor.api.deps import AdminPrincipal, Db
from corridor.api.schemas import Text
from corridor.platform.logging import get_logger
from corridor.platform.money import format_amount
from corridor.platform.pagination import DEFAULT_LIMIT, Page

log = get_logger(__name__)

router = APIRouter(prefix="/v1/admin/recon", tags=["admin"])


class BreakResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    kind: recon.BreakKind
    provider: str
    # The provider's id for the deposit or the payout; the asset code for a balance.
    provider_ref: str
    asset: str
    # Decimal strings in major units: what Corridor recorded and what the provider reports.
    # Null on the side that has nothing.
    expected: str | None
    actual: str | None
    status: recon.BreakStatus
    note: str | None
    # "system" for a repair, or the id of the admin who resolved it.
    resolved_by: str | None
    created_at: datetime
    resolved_at: datetime | None

    @classmethod
    def of(cls, found: recon.Break) -> Self:
        return cls(
            id=found.id,
            run_id=found.run_id,
            kind=found.kind,
            provider=found.provider,
            provider_ref=found.provider_ref,
            asset=found.asset,
            expected=_amount(found.expected, found.asset),
            actual=_amount(found.actual, found.asset),
            status=found.status,
            note=found.note,
            resolved_by=found.resolved_by,
            created_at=found.created_at,
            resolved_at=found.resolved_at,
        )


class BreakPageResponse(BaseModel):
    items: list[BreakResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


class RunResponse(BaseModel):
    id: uuid.UUID
    # The half-open window ``[window_start, window_end)`` the run compared.
    window_start: datetime
    window_end: datetime
    # "incomplete" when a provider could not be read for all that was asked of it.
    status: recon.RunStatus
    # The disagreements the run saw, and how many of them had no open break before it.
    breaks_found: int
    breaks_opened: int
    started_at: datetime
    finished_at: datetime

    @classmethod
    def of(cls, run: recon.Run) -> Self:
        return cls(
            id=run.id,
            window_start=run.window_start,
            window_end=run.window_end,
            status=run.status,
            breaks_found=run.breaks_found,
            breaks_opened=run.breaks_opened,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )


class RunPageResponse(BaseModel):
    items: list[RunResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Why the break is settled. Its least length is the service's rule; this bounds what a
    # request can make the server read.
    note: Annotated[Text, Field(max_length=recon.MAX_NOTE_LENGTH)]


def _amount(minor: int | None, asset: str) -> str | None:
    return None if minor is None else format_amount(minor, asset)


@router.get("/runs", summary="Reconciliation runs, newest first")
async def list_runs(
    principal: AdminPrincipal,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> RunPageResponse:
    async def work(session: AsyncSession) -> Page[recon.Run]:
        return await recon.list_runs(session, principal, cursor=cursor, limit=limit)

    page = await db.run(work)
    return RunPageResponse(
        items=[RunResponse.of(run) for run in page.items], next_cursor=page.next_cursor
    )


@router.get("/breaks", summary="Reconciliation breaks, newest first")
async def list_breaks(
    principal: AdminPrincipal,
    db: Db,
    status: recon.BreakStatus | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> BreakPageResponse:
    async def work(session: AsyncSession) -> Page[recon.Break]:
        return await recon.list_breaks(
            session, principal, status=status, cursor=cursor, limit=limit
        )

    page = await db.run(work)
    return BreakPageResponse(
        items=[BreakResponse.of(found) for found in page.items], next_cursor=page.next_cursor
    )


@router.post("/breaks/{break_id}/resolve", summary="Resolve an open break, with a note")
async def resolve_break(
    break_id: uuid.UUID, body: ResolveRequest, principal: AdminPrincipal, db: Db
) -> BreakResponse:
    # No idempotency key: a second resolution finds the break resolved and is refused, so
    # repeating the request changes nothing.
    resolved = await db.run(
        lambda session: recon.resolve_break(session, principal, break_id, note=body.note)
    )
    log.info("recon.break_resolved", break_id=str(break_id), kind=resolved.kind)
    return BreakResponse.of(resolved)

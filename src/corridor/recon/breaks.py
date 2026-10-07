"""Every statement against the reconciliation tables, and what an admin does with a break.

Each function takes the caller's session and runs inside the caller's transaction.
"""

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Final, cast

from sqlalchemy import RowMapping, Table, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity
from corridor.identity import Principal
from corridor.platform.clock import utcnow
from corridor.platform.ids import new_id
from corridor.platform.pagination import (
    DEFAULT_LIMIT,
    InvalidCursor,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)
from corridor.recon.errors import BreakNotFound, BreakNotOpen, InvalidNote
from corridor.recon.models import ReconBreakRow, ReconRunRow
from corridor.recon.types import SYSTEM, Break, BreakKind, BreakStatus, Finding, Run, RunStatus

# Core tables: every statement against them is written out below.
_runs = cast(Table, ReconRunRow.__table__)
_breaks = cast(Table, ReconBreakRow.__table__)

CURSOR_KIND: Final = "recon_breaks"
RUNS_CURSOR_KIND: Final = "recon_runs"
MAX_NOTE_LENGTH: Final = 500


async def record_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
    status: RunStatus,
    breaks_found: int,
    breaks_opened: int,
    started_at: datetime,
) -> Run:
    inserted = await session.execute(
        insert(_runs)
        .values(
            id=run_id,
            window_start=window_start,
            window_end=window_end,
            status=status,
            breaks_found=breaks_found,
            breaks_opened=breaks_opened,
            started_at=started_at,
            finished_at=utcnow(),
        )
        .returning(_runs)
    )
    return _run(inserted.mappings().one())


async def open_break(
    session: AsyncSession, run_id: uuid.UUID, finding: Finding
) -> tuple[Break, bool]:
    """The open break for a disagreement, and whether this call opened it.

    One disagreement has one open break however many runs see it: the insert does nothing
    when there is one, and a concurrent run inserting the same break waits here for the
    first and then finds its row.
    """
    inserted = await session.execute(
        pg_insert(_breaks)
        .values(
            id=new_id(),
            run_id=run_id,
            kind=finding.kind,
            provider=finding.provider,
            provider_ref=finding.provider_ref,
            asset_code=finding.asset,
            expected=finding.expected,
            actual=finding.actual,
            status="open",
            note=None,
            resolved_by=None,
            created_at=utcnow(),
            resolved_at=None,
        )
        .on_conflict_do_nothing(
            index_elements=[_breaks.c.kind, _breaks.c.provider, _breaks.c.provider_ref],
            index_where=_breaks.c.status == "open",
        )
        .returning(_breaks)
    )
    row = inserted.mappings().one_or_none()
    if row is not None:
        return _break(row), True
    existing = await session.execute(
        select(_breaks).where(
            _breaks.c.kind == finding.kind,
            _breaks.c.provider == finding.provider,
            _breaks.c.provider_ref == finding.provider_ref,
            _breaks.c.status == "open",
        )
    )
    return _break(existing.mappings().one()), False


async def lock_open(session: AsyncSession, kinds: Collection[BreakKind]) -> list[Break]:
    """Every open break of these kinds, locked for the rest of the transaction, oldest
    first."""
    rows = await session.execute(
        select(_breaks)
        .where(_breaks.c.status == "open", _breaks.c.kind.in_(list(kinds)))
        .order_by(_breaks.c.id)
        .with_for_update()
    )
    return [_break(row) for row in rows.mappings()]


async def resolve_as_system(session: AsyncSession, break_id: uuid.UUID, note: str) -> None:
    """Close a break the repair put right. The caller holds its row."""
    await _resolve(session, break_id, resolved_by=SYSTEM, note=note)
    await audit.record(
        session,
        actor=audit.Actor.system("recon.repair"),
        action="recon.break_resolved",
        resource_type="recon_break",
        resource_id=break_id,
        details={"note": note},
    )


async def list_breaks(
    session: AsyncSession,
    principal: Principal,
    *,
    status: BreakStatus | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[Break]:
    """One page of breaks, newest first, for an admin. All of them, or those in one status.

    Pages are cut on the break id, which is a UUIDv7 and so in order of creation.
    """
    identity.require_admin(principal)
    limit = clamp_limit(limit)
    # The cursor is tied to the filter, so one from another list is refused.
    scope = status or "all"
    query = select(_breaks)
    if status is not None:
        query = query.where(_breaks.c.status == status)
    if cursor is not None:
        query = query.where(_breaks.c.id < _position(cursor, scope))
    # One more than the page, to learn whether anything follows it without a second query.
    rows = await session.execute(query.order_by(_breaks.c.id.desc()).limit(limit + 1))
    found = [_break(row) for row in rows.mappings()]
    shown = found[:limit]
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="recon.breaks_listed",
        resource_type="recon_break",
        details={"status": scope, "returned": len(shown)},
    )
    return Page(
        items=tuple(shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=scope, position=str(shown[-1].id))
            if len(found) > limit
            else None
        ),
    )


async def list_runs(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[Run]:
    """One page of reconciliation runs, newest first, for an admin.

    Pages are cut on the run id, which is a UUIDv7 and so in order of creation.
    """
    identity.require_admin(principal)
    limit = clamp_limit(limit)
    query = select(_runs)
    if cursor is not None:
        query = query.where(_runs.c.id < _position(cursor, "all", RUNS_CURSOR_KIND))
    # One more than the page, to learn whether anything follows it without a second query.
    rows = await session.execute(query.order_by(_runs.c.id.desc()).limit(limit + 1))
    found = [_run(row) for row in rows.mappings()]
    shown = found[:limit]
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="recon.runs_listed",
        resource_type="recon_run",
        details={"returned": len(shown)},
    )
    return Page(
        items=tuple(shown),
        next_cursor=(
            encode_cursor(kind=RUNS_CURSOR_KIND, scope="all", position=str(shown[-1].id))
            if len(found) > limit
            else None
        ),
    )


async def resolve_break(
    session: AsyncSession, principal: Principal, break_id: uuid.UUID, *, note: str
) -> Break:
    """Close an open break, with the admin's account of why it is settled.

    The row is locked and its status looked at first, so of two admins resolving the same
    break at once, one resolves it and the other is told it is no longer open.
    """
    identity.require_admin(principal)
    note = note.strip()
    if not 0 < len(note) <= MAX_NOTE_LENGTH:
        raise InvalidNote(f"A note of 1 to {MAX_NOTE_LENGTH} characters is required.", field="note")
    rows = await session.execute(select(_breaks).where(_breaks.c.id == break_id).with_for_update())
    row = rows.mappings().one_or_none()
    if row is None:
        raise BreakNotFound
    if row["status"] != "open":
        raise BreakNotOpen("This break has already been resolved.")
    resolved = await _resolve(session, break_id, resolved_by=str(principal.user_id), note=note)
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="recon.break_resolved",
        resource_type="recon_break",
        resource_id=break_id,
        details={"kind": resolved.kind, "provider": resolved.provider, "note": note},
    )
    return resolved


async def _resolve(
    session: AsyncSession, break_id: uuid.UUID, *, resolved_by: str, note: str
) -> Break:
    updated = await session.execute(
        update(_breaks)
        .where(_breaks.c.id == break_id, _breaks.c.status == "open")
        .values(status="resolved", resolved_by=resolved_by, note=note, resolved_at=utcnow())
        .returning(_breaks)
    )
    return _break(updated.mappings().one())


def _position(cursor: str, scope: str, kind: str = CURSOR_KIND) -> uuid.UUID:
    position = decode_cursor(cursor, kind=kind, scope=scope)
    if not isinstance(position, str):
        raise InvalidCursor
    try:
        return uuid.UUID(position)
    except ValueError:
        raise InvalidCursor from None


def _run(row: RowMapping) -> Run:
    return Run(
        id=row["id"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        status=row["status"],
        breaks_found=row["breaks_found"],
        breaks_opened=row["breaks_opened"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _break(row: RowMapping) -> Break:
    return Break(
        id=row["id"],
        run_id=row["run_id"],
        kind=row["kind"],
        provider=row["provider"],
        provider_ref=row["provider_ref"],
        asset=row["asset_code"],
        expected=row["expected"],
        actual=row["actual"],
        status=row["status"],
        note=row["note"],
        resolved_by=row["resolved_by"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )

"""Admin endpoints for operations: dead letters, the deposits in suspense, and adjustments
with dual approval.

Every route here needs an administrator, and the service checks again. An adjustment is
asked for and decided under an idempotency key, in the transaction that records the key,
so a request that is repeated asks, approves or rejects once.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from corridor import ops, outbox
from corridor.api.deps import AdminPrincipal, Db
from corridor.api.idempotency import IdempotencyKey, StoredResponse, run_idempotent, to_response
from corridor.api.middleware import route_template
from corridor.api.schemas import Text
from corridor.identity import Principal
from corridor.ledger import Direction
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.logging import get_logger
from corridor.platform.money import format_amount, parse_amount
from corridor.platform.pagination import DEFAULT_LIMIT, Page

log = get_logger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

# Far above any real value. These bound what a request can make the server read; what an
# amount or an asset may be is decided by the code that uses it.
_MAX_FIELD_LENGTH = 320

_DIRECTIONS = {"debit": Direction.DEBIT, "credit": Direction.CREDIT}
_DIRECTION_NAMES = {direction: name for name, direction in _DIRECTIONS.items()}


# --- dead letters ----------------------------------------------------------------------------


class DeadLetterResponse(BaseModel):
    id: uuid.UUID
    topic: str
    payload: dict[str, Any]
    status: outbox.EventStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    # When it was given up on. Null again once it has been requeued.
    finished_at: datetime | None

    @classmethod
    def of(cls, event: outbox.OutboxEvent) -> Self:
        return cls(
            id=event.id,
            topic=event.topic,
            payload=dict(event.payload),
            status=event.status,
            attempts=event.attempts,
            last_error=event.last_error,
            created_at=event.created_at,
            finished_at=event.finished_at,
        )


class DeadLetterPageResponse(BaseModel):
    items: list[DeadLetterResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


@router.get("/outbox/dead", summary="Outbox events that ran out of attempts, newest first")
async def list_dead_letters(
    principal: AdminPrincipal,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> DeadLetterPageResponse:
    async def work(session: AsyncSession) -> Page[outbox.OutboxEvent]:
        return await ops.list_dead_letters(session, principal, cursor=cursor, limit=limit)

    page = await db.run(work)
    return DeadLetterPageResponse(
        items=[DeadLetterResponse.of(event) for event in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/outbox/dead/{event_id}/requeue", summary="Give a dead event its attempts again")
async def requeue_dead_letter(
    event_id: uuid.UUID, principal: AdminPrincipal, db: Db
) -> DeadLetterResponse:
    # No idempotency key: a second requeue finds the event no longer dead and is refused,
    # so repeating the request cannot give it two lives.
    event = await db.run(lambda session: ops.requeue_dead_letter(session, principal, event_id))
    log.info("outbox.dead_requeued", event_id=str(event_id), topic=event.topic)
    return DeadLetterResponse.of(event)


# --- deposits in suspense --------------------------------------------------------------------


class SuspenseDepositResponse(BaseModel):
    id: uuid.UUID
    provider: str
    asset: str
    # A decimal string in major units, with exactly the asset's decimal places.
    amount: str
    # When Corridor recorded it.
    received_at: datetime
    # The review screening opened on it, open or decided. Null for a deposit that is in
    # suspense because it arrived at nobody's account.
    review_id: uuid.UUID | None

    @classmethod
    def of(cls, found: ops.SuspenseDeposit) -> Self:
        deposit = found.deposit
        return cls(
            id=deposit.id,
            provider=deposit.provider,
            asset=deposit.asset,
            amount=format_amount(deposit.amount, deposit.asset),
            received_at=deposit.created_at,
            review_id=found.review_id,
        )


class SuspenseDepositPageResponse(BaseModel):
    items: list[SuspenseDepositResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


@router.get("/deposits/suspense", summary="Deposits in suspense, newest first")
async def list_suspense_deposits(
    principal: AdminPrincipal,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> SuspenseDepositPageResponse:
    async def work(session: AsyncSession) -> Page[ops.SuspenseDeposit]:
        return await ops.list_suspense_deposits(session, principal, cursor=cursor, limit=limit)

    page = await db.run(work)
    return SuspenseDepositPageResponse(
        items=[SuspenseDepositResponse.of(found) for found in page.items],
        next_cursor=page.next_cursor,
    )


# --- adjustments -----------------------------------------------------------------------------


class _Request(BaseModel):
    # An unknown field is refused rather than dropped: a client that misspells one learns
    # of it before anything is recorded.
    model_config = ConfigDict(extra="forbid", frozen=True)


class LegRequest(_Request):
    account_id: uuid.UUID
    asset: Annotated[Text, Field(max_length=_MAX_FIELD_LENGTH)]
    direction: Literal["debit", "credit"]
    # A decimal string in major units of ``asset``. A JSON number is refused: it would
    # have been through a float before it got here.
    amount: Annotated[Text, Field(max_length=_MAX_FIELD_LENGTH)]


class AdjustmentRequest(_Request):
    reason: Annotated[Text, Field(max_length=ops.MAX_REASON_LENGTH)]
    legs: Annotated[list[LegRequest], Field(max_length=ops.MAX_LEGS)]


class SuspenseReleaseRequest(_Request):
    reason: Annotated[Text, Field(max_length=ops.MAX_REASON_LENGTH)]
    # The deposit to release, whole. Its asset and amount are the deposit's own.
    deposit_id: uuid.UUID
    # Whose available balance the money is credited to.
    user_id: uuid.UUID


class SuspenseReturnRequest(_Request):
    reason: Annotated[Text, Field(max_length=ops.MAX_REASON_LENGTH)]
    # The deposit to book as sent back, whole.
    deposit_id: uuid.UUID


class LegResponse(BaseModel):
    account_id: uuid.UUID
    asset: str
    direction: Literal["debit", "credit"]
    # A decimal string in major units, with exactly the asset's decimal places.
    amount: str


class AdjustmentResponse(BaseModel):
    id: uuid.UUID
    status: ops.AdjustmentStatus
    kind: ops.AdjustmentKind
    # The deposit a suspense adjustment takes out of suspense, and for a release the user
    # it is credited to. Null for an adjustment written by hand.
    deposit_id: uuid.UUID | None
    user_id: uuid.UUID | None
    reason: str
    legs: list[LegResponse]
    requested_by: uuid.UUID
    approved_by: uuid.UUID | None
    # The journal entry the approval posted.
    entry_id: uuid.UUID | None
    created_at: datetime
    decided_at: datetime | None

    @classmethod
    def of(cls, adjustment: ops.Adjustment) -> Self:
        return cls(
            id=adjustment.id,
            status=adjustment.status,
            kind=adjustment.kind,
            deposit_id=adjustment.deposit_id,
            user_id=adjustment.user_id,
            reason=adjustment.reason,
            legs=[
                LegResponse(
                    account_id=leg.account_id,
                    asset=leg.asset,
                    direction=_DIRECTION_NAMES[leg.direction],
                    amount=format_amount(leg.amount, leg.asset),
                )
                for leg in adjustment.legs
            ],
            requested_by=adjustment.requested_by,
            approved_by=adjustment.approved_by,
            entry_id=adjustment.entry_id,
            created_at=adjustment.created_at,
            decided_at=adjustment.decided_at,
        )


class AdjustmentPageResponse(BaseModel):
    items: list[AdjustmentResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


def _stored(status_code: int, adjustment: ops.Adjustment) -> StoredResponse:
    return StoredResponse(
        status_code, AdjustmentResponse.of(adjustment).model_dump(mode="json"), {}
    )


@router.post(
    "/adjustments",
    status_code=201,
    response_model=AdjustmentResponse,
    summary="Ask for a journal entry, for another admin to approve",
)
async def request_adjustment(
    request: Request,
    body: AdjustmentRequest,
    principal: AdminPrincipal,
    db: Db,
    key: IdempotencyKey,
) -> JSONResponse:
    # Before the key is read: a request that could never be performed does not use it up.
    legs = [
        ops.Leg(
            account_id=leg.account_id,
            asset=leg.asset,
            direction=_DIRECTIONS[leg.direction],
            amount=parse_amount(leg.amount, leg.asset),
        )
        for leg in body.legs
    ]

    async def work(session: AsyncSession) -> StoredResponse:
        adjustment = await ops.request_adjustment(
            session,
            principal,
            # Made here, inside the work: a replay returns the stored response and never
            # reaches this line, so one key names one adjustment.
            adjustment_id=new_id(),
            reason=body.reason,
            legs=legs,
        )
        return _stored(201, adjustment)

    return await _idempotent(request, principal, db, key, work, "adjustment.requested")


@router.post(
    "/adjustments/suspense-release",
    status_code=201,
    response_model=AdjustmentResponse,
    summary="Ask for a deposit in suspense to be credited to a user",
)
async def request_suspense_release(
    request: Request,
    body: SuspenseReleaseRequest,
    principal: AdminPrincipal,
    db: Db,
    key: IdempotencyKey,
) -> JSONResponse:
    async def work(session: AsyncSession) -> StoredResponse:
        adjustment = await ops.request_suspense_release(
            session,
            principal,
            adjustment_id=new_id(),
            reason=body.reason,
            deposit_id=body.deposit_id,
            user_id=body.user_id,
        )
        return _stored(201, adjustment)

    return await _idempotent(request, principal, db, key, work, "adjustment.requested")


@router.post(
    "/adjustments/suspense-return",
    status_code=201,
    response_model=AdjustmentResponse,
    summary="Ask for a deposit in suspense to be booked as sent back",
)
async def request_suspense_return(
    request: Request,
    body: SuspenseReturnRequest,
    principal: AdminPrincipal,
    db: Db,
    key: IdempotencyKey,
) -> JSONResponse:
    async def work(session: AsyncSession) -> StoredResponse:
        adjustment = await ops.request_suspense_return(
            session,
            principal,
            adjustment_id=new_id(),
            reason=body.reason,
            deposit_id=body.deposit_id,
        )
        return _stored(201, adjustment)

    return await _idempotent(request, principal, db, key, work, "adjustment.requested")


@router.post(
    "/adjustments/{adjustment_id}/approve",
    response_model=AdjustmentResponse,
    summary="Approve another admin's adjustment, which posts it",
)
async def approve_adjustment(
    request: Request,
    adjustment_id: uuid.UUID,
    principal: AdminPrincipal,
    db: Db,
    key: IdempotencyKey,
) -> JSONResponse:
    async def work(session: AsyncSession) -> StoredResponse:
        return _stored(200, await ops.approve_adjustment(session, principal, adjustment_id))

    return await _idempotent(request, principal, db, key, work, "adjustment.approved")


@router.post(
    "/adjustments/{adjustment_id}/reject",
    response_model=AdjustmentResponse,
    summary="Turn a pending adjustment down",
)
async def reject_adjustment(
    request: Request,
    adjustment_id: uuid.UUID,
    principal: AdminPrincipal,
    db: Db,
    key: IdempotencyKey,
) -> JSONResponse:
    async def work(session: AsyncSession) -> StoredResponse:
        return _stored(200, await ops.reject_adjustment(session, principal, adjustment_id))

    return await _idempotent(request, principal, db, key, work, "adjustment.rejected")


@router.get("/adjustments", summary="Adjustments, newest first")
async def list_adjustments(
    principal: AdminPrincipal,
    db: Db,
    status: ops.AdjustmentStatus | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> AdjustmentPageResponse:
    async def work(session: AsyncSession) -> Page[ops.Adjustment]:
        return await ops.list_adjustments(
            session, principal, status=status, cursor=cursor, limit=limit
        )

    page = await db.run(work)
    return AdjustmentPageResponse(
        items=[AdjustmentResponse.of(adjustment) for adjustment in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/adjustments/{adjustment_id}", summary="One adjustment")
async def get_adjustment(
    adjustment_id: uuid.UUID, principal: AdminPrincipal, db: Db
) -> AdjustmentResponse:
    adjustment = await db.run(lambda session: ops.get_adjustment(session, principal, adjustment_id))
    return AdjustmentResponse.of(adjustment)


async def _idempotent(
    request: Request,
    principal: Principal,
    db: Database,
    key: str,
    work: Callable[[AsyncSession], Awaitable[StoredResponse]],
    event: str,
) -> JSONResponse:
    stored, replayed = await run_idempotent(
        db,
        actor_id=principal.actor_id,
        key=key,
        method=request.method,
        route=route_template(request.scope),
        path=request.url.path,
        body=await request.body(),
        work=work,
    )
    log.info(event, status=stored.status_code, replayed=replayed)
    return to_response(stored, replayed, request)

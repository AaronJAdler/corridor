"""Approval endpoints: what a user's agents asked to move above their thresholds, and the
user's answer.

All of it is the owner's alone to do, from their own session. An agent's key is refused on
every route here, whatever its scopes, so no agent can approve what it asked for.
"""

import uuid
from datetime import datetime
from typing import Self

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import agents
from corridor.api.deps import CurrentPrincipal, Db, SettingsDep
from corridor.api.idempotency import StoredResponse
from corridor.api.ratelimit import money_rate_limit
from corridor.platform.logging import get_logger
from corridor.platform.money import format_amount
from corridor.platform.pagination import DEFAULT_LIMIT, Page

log = get_logger(__name__)

router = APIRouter(prefix="/v1/approvals", tags=["agents"])


class RequestedMovement(BaseModel):
    """What the agent asked for. A transfer names a recipient; a withdrawal names a
    beneficiary or an address."""

    asset: str
    # A decimal string in major units, with exactly the asset's decimal places.
    amount: str
    # The id of the user a transfer would pay.
    recipient_id: uuid.UUID | None = None
    memo: str | None = None
    beneficiary_id: uuid.UUID | None = None
    to_address: str | None = None

    @classmethod
    def of(cls, intent: agents.TransferIntent | agents.WithdrawalIntent) -> Self:
        amount = format_amount(intent.amount, intent.asset)
        if isinstance(intent, agents.TransferIntent):
            return cls(
                asset=intent.asset,
                amount=amount,
                recipient_id=uuid.UUID(intent.recipient),
                memo=intent.memo,
            )
        return cls(
            asset=intent.asset,
            amount=amount,
            beneficiary_id=intent.beneficiary_id,
            to_address=intent.to_address,
        )


class ApprovalResponse(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    kind: agents.ApprovalKind
    status: agents.ApprovalStatus
    request: RequestedMovement
    # The id of the transfer or withdrawal that was made. Null until one was.
    movement_id: uuid.UUID | None
    # Why an approved request was not carried out, as the code of the refusal it met.
    failure_code: str | None
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, approval: agents.ApprovalRequest) -> Self:
        return cls(
            id=approval.id,
            agent_id=approval.agent_id,
            kind=approval.kind,
            status=approval.status,
            request=RequestedMovement.of(approval.intent),
            movement_id=approval.movement_id if approval.status == "executed" else None,
            failure_code=approval.failure_code,
            expires_at=approval.expires_at,
            decided_at=approval.decided_at,
            created_at=approval.created_at,
        )


class ApprovalPageResponse(BaseModel):
    items: list[ApprovalResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


class AwaitingApprovalResponse(BaseModel):
    """What an agent is answered when what it asked for waits for its owner: nothing has
    moved, and this is the request the owner will see."""

    approval_request: ApprovalResponse


def awaiting_approval(approval: agents.ApprovalRequest) -> StoredResponse:
    """The ``202`` a money route answers in place of the movement it did not make. Stored
    with the idempotency key like any other answer, so a retry gets the same request."""
    body = AwaitingApprovalResponse(approval_request=ApprovalResponse.of(approval))
    return StoredResponse(202, body.model_dump(mode="json"), {})


@router.get("", summary="What the user's agents asked to be approved, newest first")
async def list_approvals(
    principal: CurrentPrincipal,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> ApprovalPageResponse:
    async def work(session: AsyncSession) -> Page[agents.ApprovalRequest]:
        return await agents.list_approvals(session, principal, cursor=cursor, limit=limit)

    page = await db.run(work)
    return ApprovalPageResponse(
        items=[ApprovalResponse.of(approval) for approval in page.items],
        next_cursor=page.next_cursor,
    )


@router.post(
    "/{approval_id}/approve",
    summary="Approve a request and make its movement",
    # Approving moves money, so it is counted with the routes that do, by who is acting.
    dependencies=[Depends(money_rate_limit)],
)
async def approve(
    approval_id: uuid.UUID, principal: CurrentPrincipal, db: Db, settings: SettingsDep
) -> ApprovalResponse:
    # No idempotency key: a request is decided once, and its movement has the id the
    # request gave it, so approving again finds it decided and moves nothing.
    outcome = await db.run(
        lambda session: agents.approve(session, principal, approval_id, settings=settings)
    )
    log.info(
        "agent.approval_approved",
        approval_id=str(approval_id),
        agent_id=str(outcome.request.agent_id),
        status=outcome.request.status,
    )
    if outcome.refusal is not None:
        # Raised here, after the commit: that the request expired or failed is on record.
        raise outcome.refusal
    return ApprovalResponse.of(outcome.request)


@router.post("/{approval_id}/reject", summary="Refuse a request, for good")
async def reject(approval_id: uuid.UUID, principal: CurrentPrincipal, db: Db) -> ApprovalResponse:
    approval = await db.run(lambda session: agents.reject(session, principal, approval_id))
    log.info(
        "agent.approval_rejected", approval_id=str(approval_id), agent_id=str(approval.agent_id)
    )
    return ApprovalResponse.of(approval)

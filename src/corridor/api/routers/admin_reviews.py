"""Admin endpoints for reviews: the movements screening held back, and deciding them.

Every route here needs an administrator, and the service checks again. What an admin reads
or decides is written to the audit log in the transaction that serves it.
"""

import uuid
from datetime import datetime
from typing import Literal, Self

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ops, risk
from corridor.api.deps import AdminPrincipal, Db
from corridor.platform.logging import get_logger
from corridor.platform.pagination import DEFAULT_LIMIT, Page

log = get_logger(__name__)

router = APIRouter(prefix="/v1/admin/reviews", tags=["admin"])


class ReviewResponse(BaseModel):
    id: uuid.UUID
    subject_type: risk.SubjectType
    # The id of the withdrawal or the deposit.
    subject_id: uuid.UUID
    # Whose movement it is. Null for a deposit that arrived at nobody's account.
    user_id: uuid.UUID | None
    # What screening answered: the reason the review exists.
    screening: Literal["deny", "review"]
    status: risk.ReviewStatus
    created_at: datetime
    resolved_at: datetime | None

    @classmethod
    def of(cls, review: risk.Review) -> Self:
        return cls(
            id=review.id,
            subject_type=review.subject_type,
            subject_id=review.subject_id,
            user_id=review.user_id,
            screening=review.outcome,
            status=review.status,
            created_at=review.created_at,
            resolved_at=review.resolved_at,
        )


class ReviewPageResponse(BaseModel):
    items: list[ReviewResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


@router.get("", summary="Reviews waiting for a decision, newest first")
async def list_open_reviews(
    principal: AdminPrincipal,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> ReviewPageResponse:
    async def work(session: AsyncSession) -> Page[risk.Review]:
        return await ops.list_open_reviews(session, principal, cursor=cursor, limit=limit)

    page = await db.run(work)
    return ReviewPageResponse(
        items=[ReviewResponse.of(review) for review in page.items], next_cursor=page.next_cursor
    )


@router.post("/{review_id}/clear", summary="Clear an open review: its movement goes ahead")
async def clear_review(review_id: uuid.UUID, principal: AdminPrincipal, db: Db) -> ReviewResponse:
    # No idempotency key: a second decision finds the review resolved and is refused, so
    # repeating the request sends or credits nothing twice.
    review = await db.run(lambda session: ops.clear_review(session, principal, review_id))
    log.info("review.cleared", review_id=str(review_id), subject_type=review.subject_type)
    return ReviewResponse.of(review)


@router.post("/{review_id}/reject", summary="Reject an open review: its movement is stopped")
async def reject_review(review_id: uuid.UUID, principal: AdminPrincipal, db: Db) -> ReviewResponse:
    review = await db.run(lambda session: ops.reject_review(session, principal, review_id))
    log.info("review.rejected", review_id=str(review_id), subject_type=review.subject_type)
    return ReviewResponse.of(review)

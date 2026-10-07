"""Reviews: what an operator decides about a movement screening would not let through.

A withdrawal to a party listed for review waits, held, with its funds reserved. A deposit
from a listed sender is on the books in suspense, as nobody's. An administrator clears the
review or rejects it, once, and what follows is done here in the same transaction: a
cleared withdrawal is sent and a cleared deposit goes to the user it arrived for; a
rejected withdrawal is given back, and a rejected deposit stays in suspense, to be sent
back by an adjustment.

Each function takes the caller's session and runs inside the caller's transaction. Each
needs an administrator and says so in the audit log.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, payments, risk
from corridor.identity import Principal
from corridor.ops.errors import ReviewHasNoUser
from corridor.platform.pagination import DEFAULT_LIMIT, Page
from corridor.risk import Review


async def list_open_reviews(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[Review]:
    """One page of the reviews that wait for a decision, newest first."""
    identity.require_admin(principal)
    page = await risk.list_reviews(session, status="open", cursor=cursor, limit=limit)
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="review.listed",
        resource_type="review",
        details={"returned": len(page.items)},
    )
    return page


async def clear_review(session: AsyncSession, principal: Principal, review_id: uuid.UUID) -> Review:
    """Clear an open review, and let its movement go ahead.

    The decision is one statement on the review's row, made before anything follows from
    it: of a clearance and a rejection at the same moment, one decides and the other is
    told the review is resolved, and nothing it would have done is done.

    A withdrawal is asked to be sent again, since the event written with it found the
    review open. A deposit is released from suspense to the user recorded on the review.
    One that has no such user, because it arrived at nobody's account, cannot be cleared:
    who it belongs to is not something this decides. Nor can one whose user has closed
    their account since, or one an adjustment has already taken out of suspense: payments
    refuses either, and the review stays open.
    """
    identity.require_admin(principal)
    actor = audit.Actor.admin(principal.user_id)
    review = await risk.get_review(session, review_id)
    if review.subject_type == "deposit" and review.user_id is None and review.status == "open":
        raise ReviewHasNoUser
    cleared = await risk.resolve_review(
        session, subject_type=review.subject_type, subject_id=review.subject_id, cleared=True
    )
    if cleared.subject_type == "withdrawal":
        await payments.send_cleared_withdrawal(session, cleared.subject_id)
    elif cleared.user_id is not None:
        await payments.release_from_suspense(
            session, cleared.subject_id, cleared.user_id, actor=actor
        )
    await _record(session, actor, "review.cleared", cleared)
    return cleared


async def reject_review(
    session: AsyncSession, principal: Principal, review_id: uuid.UUID
) -> Review:
    """Reject an open review, and stop its movement for good.

    A withdrawal that is still held is given back to its user and ended as failed, and
    what it used of the day's limit is given back with it. A deposit stays where it is, in
    suspense: the money is not the user's, and returning it is an adjustment.
    """
    identity.require_admin(principal)
    actor = audit.Actor.admin(principal.user_id)
    review = await risk.get_review(session, review_id)
    rejected = await risk.resolve_review(
        session, subject_type=review.subject_type, subject_id=review.subject_id, cleared=False
    )
    if rejected.subject_type == "withdrawal":
        await payments.reject_held_withdrawal(session, rejected.subject_id, actor=actor)
    await _record(session, actor, "review.rejected", rejected)
    return rejected


async def _record(session: AsyncSession, actor: audit.Actor, action: str, review: Review) -> None:
    await audit.record(
        session,
        actor=actor,
        action=action,
        principal_id=review.user_id,
        resource_type="review",
        resource_id=review.id,
        details={
            "subject_type": review.subject_type,
            "subject_id": str(review.subject_id),
            "screening": review.outcome,
        },
    )

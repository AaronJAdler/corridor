"""Deposits in suspense: money that is on the books and is nobody's, for an operator to
find before asking for it to be released or sent back.

The function takes the caller's session and runs inside the caller's transaction. It
needs an administrator and says so in the audit log.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, payments, risk
from corridor.identity import Principal
from corridor.ops.types import SuspenseDeposit
from corridor.platform.pagination import DEFAULT_LIMIT, Page


async def list_suspense_deposits(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[SuspenseDeposit]:
    """One page of the deposits in suspense, newest first, each with the review screening
    opened on it if it opened one.

    A deposit whose review was rejected is still here, with that review: rejecting leaves
    the money in suspense, to be sent back by an adjustment.
    """
    identity.require_admin(principal)
    page = await payments.list_suspense_deposits(session, cursor=cursor, limit=limit)
    reviews = await risk.find_reviews(session, "deposit", [deposit.id for deposit in page.items])
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="deposit.suspense_listed",
        resource_type="deposit",
        details={"returned": len(page.items)},
    )
    return Page(
        items=tuple(
            SuspenseDeposit(
                deposit=deposit,
                review_id=reviews[deposit.id].id if deposit.id in reviews else None,
            )
            for deposit in page.items
        ),
        next_cursor=page.next_cursor,
    )

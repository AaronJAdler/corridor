"""The authorisation hook: every movement of money out of a wallet asks here first.

It runs inside the caller's transaction, under the caller's per-user money-out lock, so
what it reads cannot be overtaken by another outgoing movement of the same user. That lock
is what makes the daily limit hold: balance rows are per asset and the limit is not, so
without it two movements in two assets would each see the day as the other left it.
"""

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import identity
from corridor.platform.db import advisory_xact_lock, lock_key
from corridor.risk import limits
from corridor.risk.errors import CounterpartyUnavailable, UserRestricted
from corridor.risk.types import Decision, MoneyMovement


async def authorize(session: AsyncSession, movement: MoneyMovement) -> Decision:
    """Allow the movement, or raise the ``Denied`` that says why not.

    The account's standing is checked first, then the limits. A movement that is allowed
    is counted against the limits here, in the caller's transaction, so a movement that
    does not commit uses up nothing. Nothing is written before a refusal, so the caller's
    transaction stays usable.
    """
    involved = {movement.user_id}
    if movement.counterparty_id is not None:
        involved.add(movement.counterparty_id)
    users = await identity.get_users(session, involved)

    # Restricted and closed accounts alike: only an active account moves money out.
    if users[movement.user_id].status != "active":
        raise UserRestricted

    if movement.counterparty_id is not None:
        counterparty = users.get(movement.counterparty_id)
        # A restricted account can still be paid. A closed one cannot.
        if counterparty is None or counterparty.status == "closed":
            raise CounterpartyUnavailable

    await limits.check_and_record(session, movement, users[movement.user_id])
    return Decision(outcome="allow")


# One name for the per-user lock that serialises a user's outgoing money movements, so
# transfers, withdrawals, conversions and returns all queue behind each other.
MONEY_OUT_LOCK: Final = "money_out"


async def restrict_user(session: AsyncSession, user_id: uuid.UUID, reason: str) -> identity.User:
    """Restrict a user, under that user's money-out lock.

    A movement of money out reads the account's standing under this lock and keeps the
    lock until it commits. Taking it here means the restriction waits for any movement
    that is under way, and that every movement after it sees the restriction: once this
    commits, nothing that read "active" is still on its way out.

    The lock is taken before the user's row is touched, which is where the lock order
    puts it. A caller that already holds it is not made to wait for itself.
    """
    await advisory_xact_lock(session, [lock_key(MONEY_OUT_LOCK, user_id)])
    return await identity.restrict_user(session, user_id, reason)

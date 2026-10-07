"""The authorisation hook: every movement of money out of a wallet asks here first.

It runs inside the caller's transaction, under the caller's per-user money-out lock, so
what it reads cannot be overtaken by another outgoing movement of the same user.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import identity
from corridor.risk.errors import CounterpartyUnavailable, UserRestricted
from corridor.risk.types import Decision, MoneyMovement


async def authorize(session: AsyncSession, movement: MoneyMovement) -> Decision:
    """Allow the movement, or raise the ``Denied`` that says why not.

    Nothing is written before a refusal, so the caller's transaction stays usable.
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

    return Decision(outcome="allow")

"""Accounts: what an administrator does to a user's standing. A role is given or taken
away, and an account is closed for good.

Each function takes the caller's session and runs inside the caller's transaction. Each
needs an administrator and says so in the audit log. Neither can be done to oneself: the
last administrator cannot be demoted or closed by a slip of their own hand, and a change
of office always has a second person in it.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, risk, wallets
from corridor.identity import Principal, Role, User
from corridor.ops.errors import AccountHoldsFunds, OwnAccount
from corridor.platform.db import advisory_xact_lock, lock_key

# The namespace of the per-user lock that puts changes of one user's role in a line.
_ROLE_LOCK = "user_role"


async def set_user_role(
    session: AsyncSession, principal: Principal, user_id: uuid.UUID, role: Role
) -> User:
    """Make a user an administrator, or stop them being one.

    The access tokens the user holds are ended by identity, so none goes on carrying the
    role they had.
    """
    identity.require_admin(principal)
    if user_id == principal.user_id:
        raise OwnAccount
    # Two administrators changing one user's role at once take turns here, so that each
    # change is audited with the role it really replaced.
    await advisory_xact_lock(session, [lock_key(_ROLE_LOCK, user_id)])
    before = await identity.get_user(session, user_id)
    after = await identity.set_role(session, user_id, role)
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="user.role_changed",
        principal_id=user_id,
        resource_type="user",
        resource_id=user_id,
        details={"old_role": before.role, "new_role": after.role},
    )
    return after


async def close_user(session: AsyncSession, principal: Principal, user_id: uuid.UUID) -> User:
    """Close a user's account, for good.

    Refused while the user has anything in a wallet, available or on hold: a closed
    account moves no money, so what was in it would be out of everybody's reach. The
    user's money-out lock is held while the balances are read, so nothing of theirs is
    being spent or given back at that moment. Closing a closed account changes nothing.
    """
    identity.require_admin(principal)
    if user_id == principal.user_id:
        raise OwnAccount
    await advisory_xact_lock(session, [lock_key(risk.MONEY_OUT_LOCK, user_id)])
    before = await identity.get_user(session, user_id)
    if any(
        wallet.available or wallet.held for wallet in await wallets.get_wallets(session, user_id)
    ):
        raise AccountHoldsFunds
    closed = await identity.close_user(session, user_id)
    await audit.record(
        session,
        actor=audit.Actor.admin(principal.user_id),
        action="user.closed",
        principal_id=user_id,
        resource_type="user",
        resource_id=user_id,
        details={"old_status": before.status},
    )
    return closed

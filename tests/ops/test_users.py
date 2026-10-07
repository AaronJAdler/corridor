"""A user's role and the closing of an account, as the service decides them: whoever
reaches these functions, and however, they need an administrator."""

import pytest

from corridor import ops
from corridor.identity import Principal, User
from corridor.ops import AccountHoldsFunds, OwnAccount
from corridor.platform.db import Database
from corridor.platform.errors import PermissionDenied
from tests.ops.support import audited
from tests.payments.support import acting_as, agent_of, deposit, user_status


async def test_only_an_administrator_changes_a_role_or_closes_an_account(
    db: Database, ana: Principal, maria: User
) -> None:
    for principal in (acting_as(maria), agent_of(maria, "*")):
        async with db.transaction() as session:
            with pytest.raises(PermissionDenied):
                await ops.set_user_role(session, principal, ana.user_id, "user")
            with pytest.raises(PermissionDenied):
                await ops.close_user(session, principal, ana.user_id)
            with pytest.raises(PermissionDenied):
                await ops.set_user_role(session, principal, maria.id, "admin")

    assert await user_status(db, maria) == "active"
    assert await audited(db, "user.role_changed") == []
    assert await audited(db, "user.closed") == []


async def test_an_administrator_changes_another_users_role_and_not_their_own(
    db: Database, ana: Principal, maria: User
) -> None:
    async with db.transaction() as session:
        promoted = await ops.set_user_role(session, ana, maria.id, "admin")
        with pytest.raises(OwnAccount):
            await ops.set_user_role(session, ana, ana.user_id, "user")

    assert promoted.role == "admin"
    (event,) = await audited(db, "user.role_changed")
    assert event["details"] == {"old_role": "user", "new_role": "admin"}


async def test_an_account_is_closed_once_it_is_empty(
    db: Database, ana: Principal, maria: User
) -> None:
    await deposit(db, maria, 1, "USDC")

    with pytest.raises(AccountHoldsFunds) as refusal:
        async with db.transaction() as session:
            await ops.close_user(session, ana, maria.id)
    assert (refusal.value.status, refusal.value.code) == (409, "account_holds_funds")
    assert await user_status(db, maria) == "active"

"""Admin endpoints for a user's standing: their role, restricting them and lifting it, and
closing their account.

Every route here needs an administrator, and the service checks again. What is changed is
written to the audit log in the transaction that changes it. No route takes an
idempotency key: giving a user the role they have, restricting a restricted user or
closing a closed account changes nothing but the reason on record.
"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from corridor import identity, ops
from corridor.api.deps import AdminPrincipal, Db
from corridor.api.schemas import Reason, UserResponse
from corridor.platform.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/v1/admin/users", tags=["admin"])


class RoleRequest(BaseModel):
    # An unknown field is refused rather than dropped: this changes what a user may do.
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: identity.Role


@router.post("/{user_id}/role", summary="Make a user an administrator, or stop them being one")
async def set_role(
    user_id: uuid.UUID, body: RoleRequest, principal: AdminPrincipal, db: Db
) -> UserResponse:
    user = await db.run(lambda session: ops.set_user_role(session, principal, user_id, body.role))
    log.info("admin.role_changed", user_id=str(user_id), role=user.role)
    return UserResponse.of(user)


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: Reason


@router.post("/{user_id}/restrict", summary="Stop a user moving money out")
async def restrict(
    user_id: uuid.UUID, body: ReasonRequest, principal: AdminPrincipal, db: Db
) -> UserResponse:
    user = await db.run(lambda session: ops.restrict_user(session, principal, user_id, body.reason))
    log.info("admin.user_restricted", user_id=str(user_id))
    return UserResponse.of(user)


@router.post("/{user_id}/lift-restriction", summary="Let a restricted user move money again")
async def lift_restriction(
    user_id: uuid.UUID, body: ReasonRequest, principal: AdminPrincipal, db: Db
) -> UserResponse:
    user = await db.run(
        lambda session: ops.lift_restriction(session, principal, user_id, body.reason)
    )
    log.info("admin.restriction_lifted", user_id=str(user_id))
    return UserResponse.of(user)


@router.post("/{user_id}/close", summary="Close a user's account, for good")
async def close(user_id: uuid.UUID, principal: AdminPrincipal, db: Db) -> UserResponse:
    user = await db.run(lambda session: ops.close_user(session, principal, user_id))
    log.info("admin.user_closed", user_id=str(user_id))
    return UserResponse.of(user)

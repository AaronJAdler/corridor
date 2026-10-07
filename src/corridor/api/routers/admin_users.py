"""Admin endpoints for a user's standing: their role, and closing their account.

Every route here needs an administrator, and the service checks again. What is changed is
written to the audit log in the transaction that changes it. Neither route takes an
idempotency key: giving a user the role they have, or closing a closed account, changes
nothing.
"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from corridor import identity, ops
from corridor.api.deps import AdminPrincipal, Db
from corridor.api.schemas import UserResponse
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


@router.post("/{user_id}/close", summary="Close a user's account, for good")
async def close(user_id: uuid.UUID, principal: AdminPrincipal, db: Db) -> UserResponse:
    user = await db.run(lambda session: ops.close_user(session, principal, user_id))
    log.info("admin.user_closed", user_id=str(user_id))
    return UserResponse.of(user)

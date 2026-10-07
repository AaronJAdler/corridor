"""The admin endpoint that changes a user's KYC tier.

Verifying who a user is happens outside Corridor. This is where its result is recorded: an
administrator sets the tier, the user's limits follow it, and the audit log keeps who
changed it, from what and to what.
"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, StrictInt
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity
from corridor.api.deps import AdminPrincipal, Db
from corridor.api.schemas import UserResponse
from corridor.platform.db import advisory_xact_lock, lock_key
from corridor.platform.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

# The namespace of the per-user lock that puts changes of one user's tier in a line.
_KYC_TIER_LOCK = "kyc_tier"


class KycTierRequest(BaseModel):
    # An unknown field is refused rather than dropped: this changes what a user may do.
    model_config = ConfigDict(extra="forbid", frozen=True)

    # A JSON integer and nothing that could be read as one. Which integers are tiers is
    # identity's to say.
    kyc_tier: StrictInt


@router.put("/users/{user_id}/kyc-tier", summary="Set a user's KYC tier")
async def set_kyc_tier(
    user_id: uuid.UUID, body: KycTierRequest, principal: AdminPrincipal, db: Db
) -> UserResponse:
    async def work(session: AsyncSession) -> tuple[identity.User, int]:
        # Two administrators changing one user's tier at once take turns here, so that
        # each change is audited with the tier it really replaced.
        await advisory_xact_lock(session, [lock_key(_KYC_TIER_LOCK, user_id)])
        before = await identity.get_user(session, user_id)
        after = await identity.set_kyc_tier(session, user_id, body.kyc_tier)
        await audit.record(
            session,
            actor=audit.Actor.admin(principal.actor_id),
            action="user.kyc_tier_changed",
            principal_id=user_id,
            resource_type="user",
            resource_id=user_id,
            details={"old_tier": before.kyc_tier, "new_tier": after.kyc_tier},
        )
        return after, before.kyc_tier

    user, old_tier = await db.run(work)
    log.info(
        "admin.kyc_tier_changed", user_id=str(user_id), old_tier=old_tier, new_tier=user.kyc_tier
    )
    return UserResponse.of(user)

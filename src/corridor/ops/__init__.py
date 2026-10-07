"""Operations: what an administrator does to the running system. Dead letters are looked at
and requeued, the books are adjusted by hand, with two people for each adjustment, and the
movements screening held back are cleared or rejected.

Other modules use the names exported here and nothing else from this package.
"""

from corridor.ops.adjustments import (
    MAX_LEGS,
    MAX_REASON_LENGTH,
    approve_adjustment,
    get_adjustment,
    list_adjustments,
    reject_adjustment,
    request_adjustment,
    request_suspense_release,
    request_suspense_return,
)
from corridor.ops.errors import (
    AccountHoldsFunds,
    AdjustmentNotFound,
    AdjustmentNotPending,
    DeadLetterNotFound,
    InvalidAdjustment,
    OwnAccount,
    ReviewHasNoUser,
    SelfApproval,
)
from corridor.ops.reviews import clear_review, list_open_reviews, reject_review
from corridor.ops.service import list_dead_letters, requeue_dead_letter
from corridor.ops.types import Adjustment, AdjustmentKind, AdjustmentStatus, Leg
from corridor.ops.users import close_user, set_user_role

__all__ = [
    "MAX_LEGS",
    "MAX_REASON_LENGTH",
    "AccountHoldsFunds",
    "Adjustment",
    "AdjustmentKind",
    "AdjustmentNotFound",
    "AdjustmentNotPending",
    "AdjustmentStatus",
    "DeadLetterNotFound",
    "InvalidAdjustment",
    "Leg",
    "OwnAccount",
    "ReviewHasNoUser",
    "SelfApproval",
    "approve_adjustment",
    "clear_review",
    "close_user",
    "get_adjustment",
    "list_adjustments",
    "list_dead_letters",
    "list_open_reviews",
    "reject_adjustment",
    "reject_review",
    "request_adjustment",
    "request_suspense_release",
    "request_suspense_return",
    "requeue_dead_letter",
    "set_user_role",
]

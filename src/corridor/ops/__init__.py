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
    AdjustmentNotFound,
    AdjustmentNotPending,
    DeadLetterNotFound,
    InvalidAdjustment,
    ReviewHasNoUser,
    SelfApproval,
)
from corridor.ops.reviews import clear_review, list_open_reviews, reject_review
from corridor.ops.service import list_dead_letters, requeue_dead_letter
from corridor.ops.types import Adjustment, AdjustmentStatus, Leg

__all__ = [
    "MAX_LEGS",
    "MAX_REASON_LENGTH",
    "Adjustment",
    "AdjustmentNotFound",
    "AdjustmentNotPending",
    "AdjustmentStatus",
    "DeadLetterNotFound",
    "InvalidAdjustment",
    "Leg",
    "ReviewHasNoUser",
    "SelfApproval",
    "approve_adjustment",
    "clear_review",
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
]

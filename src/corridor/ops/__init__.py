"""Operations: what an administrator does to the running system. Dead letters are looked at
and requeued, the books are adjusted by hand, with two people for each adjustment, and the
movements screening held back are cleared or rejected. Deposits in suspense and the audit
log are read, and a user is restricted or has the restriction lifted.

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
from corridor.ops.audit_log import list_audit_events
from corridor.ops.deposits import list_suspense_deposits
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
from corridor.ops.types import Adjustment, AdjustmentKind, AdjustmentStatus, Leg, SuspenseDeposit
from corridor.ops.users import close_user, lift_restriction, restrict_user, set_user_role

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
    "SuspenseDeposit",
    "approve_adjustment",
    "clear_review",
    "close_user",
    "get_adjustment",
    "lift_restriction",
    "list_adjustments",
    "list_audit_events",
    "list_dead_letters",
    "list_open_reviews",
    "list_suspense_deposits",
    "reject_adjustment",
    "reject_review",
    "request_adjustment",
    "request_suspense_release",
    "request_suspense_return",
    "requeue_dead_letter",
    "restrict_user",
    "set_user_role",
]

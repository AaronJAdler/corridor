"""Operations: what an administrator does to the running system. Dead letters are looked at
and requeued, and the books are adjusted by hand, with two people for each adjustment.

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
    SelfApproval,
)
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
    "SelfApproval",
    "approve_adjustment",
    "get_adjustment",
    "list_adjustments",
    "list_dead_letters",
    "reject_adjustment",
    "request_adjustment",
    "request_suspense_release",
    "request_suspense_return",
    "requeue_dead_letter",
]

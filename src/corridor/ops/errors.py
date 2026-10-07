"""What the operations module refuses, and how."""

from corridor.platform.errors import Conflict, InvalidRequest, NotFound, PermissionDenied


class DeadLetterNotFound(NotFound):
    """There is no such event, or there is and it is not dead. The two are not told apart:
    either way there is nothing here to requeue."""

    code = "dead_letter_not_found"
    title = "Dead letter not found"

    def __init__(self) -> None:
        super().__init__("There is no such dead event.")


class InvalidAdjustment(InvalidRequest):
    code = "invalid_adjustment"
    title = "Invalid adjustment"


class AdjustmentNotFound(NotFound):
    code = "adjustment_not_found"
    title = "Adjustment not found"

    def __init__(self) -> None:
        super().__init__("There is no such adjustment.")


class AdjustmentNotPending(Conflict):
    """The adjustment was approved or rejected already."""

    code = "adjustment_not_pending"
    title = "Adjustment is not pending"

    def __init__(self) -> None:
        super().__init__("This adjustment has already been decided.")


class SelfApproval(PermissionDenied):
    """The admin who asked for an adjustment tried to approve it."""

    code = "self_approval"
    title = "Self-approval is not allowed"

    def __init__(self) -> None:
        super().__init__("An adjustment is approved by a different administrator.")

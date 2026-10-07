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


class ReviewHasNoUser(Conflict):
    """An admin tried to clear the review of a deposit that arrived at nobody's account.
    There is no user to release it to, and clearing a review does not choose one."""

    code = "review_has_no_user"
    title = "Review has no user"

    def __init__(self) -> None:
        super().__init__(
            "This deposit was not attributed to a user. Release it with an adjustment."
        )


class OwnAccount(Conflict):
    """An administrator tried to change their own role or close their own account. Either
    is another administrator's to do."""

    code = "own_account"
    title = "Not on your own account"

    def __init__(self) -> None:
        super().__init__("An administrator's own role and account are changed by another.")


class AccountHoldsFunds(Conflict):
    """An administrator tried to close an account that still has money in it, available
    or on hold. It is paid out, or its withdrawals end, first."""

    code = "account_holds_funds"
    title = "Account holds funds"

    def __init__(self) -> None:
        super().__init__("An account is closed once nothing is in it and nothing is on hold.")

"""What risk refuses, and how."""

from corridor.platform.errors import DomainError


class Denied(DomainError):
    """A money movement was refused. Every refusal from this module is a subclass."""

    status = 403
    code = "risk_denied"
    title = "Not allowed"


class UserRestricted(Denied):
    """The user may not move money out. It never says why: the reason is an operator's."""

    code = "user_restricted"
    title = "Account restricted"

    def __init__(self) -> None:
        super().__init__("This account cannot send money at the moment.")


class CounterpartyUnavailable(Denied):
    """The other side of the movement cannot be paid. A closed account is answered exactly
    as an account that never existed, so that nothing confirms it was once there."""

    status = 404
    code = "recipient_not_found"
    title = "Recipient not found"

    def __init__(self) -> None:
        super().__init__("There is no such recipient.")

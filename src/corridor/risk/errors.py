"""What risk refuses, and how."""

from typing import Literal

from corridor.platform.errors import Conflict, DomainError, NotFound
from corridor.platform.money import format_amount


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


class LimitExceeded(Denied):
    """The movement is more than a limit allows, at once or within 24 hours.

    It names the limit and its size, which are the caller's own. It never says how much of
    a day's limit is used: for an agent that would be what its user spent elsewhere.
    """

    status = 422
    code = "limit_exceeded"
    title = "Limit exceeded"

    def __init__(
        self,
        *,
        limit: Literal["per_transaction", "daily"],
        scope: Literal["account", "agent"],
        usd_cents: int,
    ) -> None:
        size = f"{format_amount(usd_cents, 'USD')} USD"
        detail = (
            f"This is more than the limit of {size} for one movement."
            if limit == "per_transaction"
            else f"This would take the total past the limit of {size} in 24 hours."
        )
        super().__init__(detail, limit=limit, scope=scope)


class PartyDenied(Denied):
    """The other party to a movement is one Corridor does not deal with. It says no more
    than that: which list, and why, are an operator's to know."""

    code = "party_not_allowed"
    title = "Not allowed"

    def __init__(self) -> None:
        super().__init__("Money cannot be sent to this destination.")


class ReviewNotFound(NotFound):
    code = "review_not_found"
    title = "Review not found"

    def __init__(self) -> None:
        super().__init__("There is no such review.")


class ReviewAlreadyResolved(Conflict):
    code = "review_already_resolved"
    title = "Review already resolved"

    def __init__(self) -> None:
        super().__init__("This review has already been resolved.")

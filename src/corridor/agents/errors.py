"""What the agents module refuses, and how."""

from corridor.platform.errors import (
    Conflict,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    ServiceUnavailable,
)


class AgentNotFound(NotFound):
    """There is no such agent, or it is another user's: the two are not told apart."""

    code = "agent_not_found"
    title = "Agent not found"

    def __init__(self) -> None:
        super().__init__("There is no such agent.")


class AgentKeyNotFound(NotFound):
    code = "agent_key_not_found"
    title = "Agent key not found"

    def __init__(self) -> None:
        super().__init__("This agent has no such key.")


class AgentRevoked(Conflict):
    """Revoking an agent is final. What its owner wants after that is a new agent."""

    code = "agent_revoked"
    title = "Agent revoked"

    def __init__(self) -> None:
        super().__init__("This agent has been revoked, and that cannot be undone.")


class InvalidScopes(InvalidRequest):
    code = "invalid_scopes"
    title = "Invalid scopes"


class InvalidExpiry(InvalidRequest):
    code = "invalid_expiry"
    title = "Invalid expiry"


class AgentKeysUnavailable(ServiceUnavailable):
    """This deployment has no key to hash agent keys under, so it cannot issue one."""

    code = "agent_keys_unavailable"
    title = "Agent keys unavailable"

    def __init__(self) -> None:
        super().__init__("Agent keys cannot be issued at the moment.")


class InvalidPolicy(InvalidRequest):
    code = "invalid_policy"
    title = "Invalid policy"


class RecipientNotAllowed(PermissionDenied):
    """The agent's policy does not let it pay this destination.

    It says the same of a destination that does not exist and of one that is not on the
    list, so that an agent cannot use the answer to learn who is there.
    """

    code = "recipient_not_allowed"
    title = "Recipient not allowed"

    def __init__(self) -> None:
        super().__init__("This agent's policy does not allow it to pay this recipient.")


class ApprovalNotFound(NotFound):
    """There is no such request, or it is another user's: the two are not told apart."""

    code = "approval_not_found"
    title = "Approval request not found"

    def __init__(self) -> None:
        super().__init__("There is no such approval request.")


class ApprovalAlreadyDecided(Conflict):
    """A request is decided once. Approving it again can never move the money again."""

    code = "approval_already_decided"
    title = "Approval request already decided"

    def __init__(self) -> None:
        super().__init__("This approval request has already been decided.")


class ApprovalExpired(Conflict):
    code = "approval_expired"
    title = "Approval request expired"

    def __init__(self) -> None:
        super().__init__("This approval request expired before it was decided.")


class AgentNotActive(Conflict):
    """What a paused or revoked agent asked for is not carried out, even with approval."""

    code = "agent_not_active"
    title = "Agent not active"

    def __init__(self) -> None:
        super().__init__("This agent is paused or revoked, so its request was not carried out.")

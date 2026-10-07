"""What the agents module refuses, and how."""

from corridor.platform.errors import Conflict, InvalidRequest, NotFound, ServiceUnavailable


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

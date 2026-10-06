"""What identity refuses, and how."""

from corridor.platform.errors import (
    Conflict,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    Unauthenticated,
)


class InvalidHandle(InvalidRequest):
    code = "invalid_handle"
    title = "Invalid handle"


class EmailTaken(Conflict):
    code = "email_taken"
    title = "Email already registered"


class HandleTaken(Conflict):
    code = "handle_taken"
    title = "Handle already taken"


class UserNotFound(NotFound):
    code = "user_not_found"
    title = "User not found"


class WeakPassword(InvalidRequest):
    code = "weak_password"
    title = "Weak password"


class InvalidToken(Unauthenticated):
    """An access token failed verification. It never says which check failed: the
    difference between an expired token and a forged one is of use only to someone probing."""

    code = "invalid_token"
    title = "Invalid token"

    def __init__(self) -> None:
        super().__init__("The access token is not valid.")


class InsufficientScope(PermissionDenied):
    """The credential is genuine but was not given what this action needs."""

    code = "insufficient_scope"
    title = "Insufficient scope"


class ConfigurationError(RuntimeError):
    """The signing keys are configured wrongly. The process should not start."""

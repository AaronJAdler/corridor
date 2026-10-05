"""Domain errors.

A use case refuses a request by raising a ``DomainError`` subclass. Each subclass has a
stable ``code`` that clients can branch on. The API layer renders the error as an RFC 9457
problem document; no other layer deals in HTTP.
"""

from collections.abc import Mapping
from typing import ClassVar


class DomainError(Exception):
    status: ClassVar[int] = 400
    code: ClassVar[str] = "bad_request"
    title: ClassVar[str] = "Bad request"

    def __init__(
        self,
        detail: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        **extra: object,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.headers: dict[str, str] = dict(headers or {})
        # Extension members of the problem document, e.g. the field an error refers to.
        self.extra: dict[str, object] = extra


class InvalidRequest(DomainError):
    status = 422
    code = "invalid_request"
    title = "Invalid request"


class Unauthenticated(DomainError):
    status = 401
    code = "unauthenticated"
    title = "Authentication required"

    def __init__(self, detail: str | None = None, **extra: object) -> None:
        super().__init__(detail, headers={"WWW-Authenticate": "Bearer"}, **extra)


class PermissionDenied(DomainError):
    status = 403
    code = "permission_denied"
    title = "Permission denied"


class NotFound(DomainError):
    status = 404
    code = "not_found"
    title = "Not found"


class Conflict(DomainError):
    status = 409
    code = "conflict"
    title = "Conflict"


class RateLimited(DomainError):
    status = 429
    code = "rate_limited"
    title = "Too many requests"

    def __init__(self, retry_after_seconds: int, detail: str | None = None) -> None:
        super().__init__(detail, headers={"Retry-After": str(max(1, retry_after_seconds))})
        self.retry_after_seconds = retry_after_seconds


class ServiceUnavailable(DomainError):
    status = 503
    code = "service_unavailable"
    title = "Service unavailable"

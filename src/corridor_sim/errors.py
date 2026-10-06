"""Error responses, in the one shape the provider contract defines.

Every refusal and every failure leaves the simulator as
``{"error": {"code": ..., "message": ...}}``. The code is what a caller branches on.
"""

from collections.abc import Mapping


class ApiError(Exception):
    """A response that is not a success: a refusal (4xx) or a failure (5xx)."""

    def __init__(
        self, status: int, code: str, message: str, *, headers: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers: dict[str, str] = dict(headers or {})

    def body(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": self.message}}

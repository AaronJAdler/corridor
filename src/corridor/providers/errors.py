"""How a call to a provider can go wrong, as its callers need to tell the cases apart.

None of these is a ``DomainError``: a client of the API never causes one directly, and the
module that made the call decides what it means for the business operation.
"""


class ProviderError(Exception):
    """A call to a provider did not return a result."""

    def __init__(self, detail: str, *, provider: str, operation: str) -> None:
        super().__init__(f"{provider}.{operation}: {detail}")
        self.detail = detail
        self.provider = provider
        self.operation = operation


class ProviderRejected(ProviderError):
    """The provider refused the request with a ``4xx``: definitely nothing happened.

    ``code`` is the provider's own, from the contract, and is what a caller branches on.
    """

    def __init__(
        self, code: str, message: str, status: int, *, provider: str, operation: str
    ) -> None:
        super().__init__(f"refused with {status} {code}", provider=provider, operation=operation)
        self.code = code
        self.message = message
        self.status = status


class ProviderOutcomeUnknown(ProviderError):
    """Nothing can be said about whether the operation happened.

    A timeout, a broken connection, a ``5xx``, or an answer that cannot be believed. This
    is never a failure: a caller that moved money keeps it held, retries with the same
    idempotency key, and leaves what is still unsettled to the sweeper.
    """


class ProviderMisconfigured(ProviderError):
    """Corridor's own configuration is wrong: no address, no key, or a key the provider
    does not accept. Nothing happened, and no retry will help until it is put right."""

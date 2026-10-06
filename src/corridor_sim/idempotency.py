"""Idempotency keys, as the provider contract defines them.

A key is bound to the body it first arrived with and to the resource that request created.
The binding is made at the moment of the effect and at no other time. A request whose
response was lost has therefore still bound its key, and its retry finds the resource
instead of creating a second one; a request that was refused has bound nothing.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from corridor_sim.errors import ApiError


@dataclass(frozen=True, slots=True)
class IdempotentRequest:
    key: str
    # The body in canonical form, so that two bodies are the same body if and only if they
    # are the same JSON document, however each was laid out.
    body: str


def canonical(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class IdempotencyKeys:
    """The keys one operation has seen. Each operation keeps its own."""

    def __init__(self) -> None:
        self._bound: dict[str, tuple[str, str]] = {}

    def resource_for(self, request: IdempotentRequest | None) -> str | None:
        """The id of the resource this request already created, or ``None`` if its key is
        new. A known key with another body is refused."""
        if request is None or request.key not in self._bound:
            return None
        body, resource_id = self._bound[request.key]
        if body != request.body:
            raise ApiError(
                409,
                "idempotency_conflict",
                "This idempotency key was already used with a different request body.",
            )
        return resource_id

    def bind(self, request: IdempotentRequest | None, resource_id: str) -> None:
        if request is not None:
            self._bound[request.key] = (request.body, resource_id)

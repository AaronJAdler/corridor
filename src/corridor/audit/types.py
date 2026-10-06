"""The audit log's vocabulary: who acted, how it turned out, and an event as recorded."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Self

ActorType = Literal["user", "agent", "admin", "system", "provider"]
Outcome = Literal["success", "denied", "failed"]


@dataclass(frozen=True, slots=True)
class Actor:
    """Who performed an action.

    The id is text because not every actor is a row somewhere: a user or an agent has an
    id, a provider or a scheduled job has a name. It is ``None`` for someone the system
    could not identify, such as a login attempt for an unknown email.
    """

    type: ActorType
    id: str | None

    @classmethod
    def user(cls, user_id: uuid.UUID) -> Self:
        return cls("user", str(user_id))

    @classmethod
    def agent(cls, agent_id: uuid.UUID) -> Self:
        return cls("agent", str(agent_id))

    @classmethod
    def admin(cls, user_id: uuid.UUID) -> Self:
        """A user acting through the admin API, with an operator's powers."""
        return cls("admin", str(user_id))

    @classmethod
    def system(cls, name: str) -> Self:
        """Corridor itself: a scheduled job or an outbox handler, by name."""
        return cls("system", name)

    @classmethod
    def provider(cls, name: str) -> Self:
        """A bank-rail or custody provider, acting through a webhook."""
        return cls("provider", name)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One row of the audit log, field for field."""

    id: uuid.UUID
    occurred_at: datetime
    actor_type: ActorType
    actor_id: str | None
    # The user on whose behalf the action was taken, when there is one.
    principal_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: str | None
    outcome: Outcome
    request_id: str | None
    details: Mapping[str, Any]

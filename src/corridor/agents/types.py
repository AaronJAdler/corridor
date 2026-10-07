"""The agents module's vocabulary: what its service functions hand to other modules."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

AgentStatus = Literal["active", "paused", "revoked"]

# Why a key was not accepted. For the log and never for the client, who is told only that
# the credential is not accepted.
KeyRefusal = Literal[
    "unknown_key",
    "key_revoked",
    "key_expired",
    "agent_paused",
    "agent_revoked",
    "owner_not_active",
]


@dataclass(frozen=True, slots=True)
class AgentKey:
    """A key as its owner may see it again: what it is for, never what it is."""

    id: uuid.UUID
    agent_id: uuid.UUID
    # The public part of the key, by which its owner tells one key from another.
    prefix: str
    scopes: tuple[str, ...]
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Agent:
    """A named principal that acts on its owner's wallet with a key of its own."""

    id: uuid.UUID
    owner_user_id: uuid.UUID
    name: str
    status: AgentStatus
    created_at: datetime
    keys: tuple[AgentKey, ...]


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """A key at the one moment it exists in full: in the answer to the request that made it."""

    details: AgentKey
    key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PresentedKey:
    """What a request's key comes to before the database is asked about it: which key it
    claims to be, and the digest its secret has under the server's key."""

    prefix: str
    digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class KeyOutcome:
    """How presenting a key ended. ``reason`` is None if it was accepted.

    The ids are set whenever the key was the genuine one, accepted or not, so that a
    refusal can be traced to the agent it concerns.
    """

    reason: KeyRefusal | None
    agent_id: uuid.UUID | None = None
    key_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    scopes: frozenset[str] = frozenset()

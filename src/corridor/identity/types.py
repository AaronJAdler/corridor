"""Identity's vocabulary: what its service functions hand to other modules."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Role = Literal["user", "admin"]
UserStatus = Literal["active", "restricted", "closed"]
LoginRefusal = Literal["unknown_email", "bad_password", "locked", "closed"]


@dataclass(frozen=True, slots=True)
class User:
    """A user as other modules see one. It never carries the password hash."""

    id: uuid.UUID
    email: str
    handle: str
    display_name: str
    role: Role
    kyc_tier: int
    status: UserStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LoginCandidate:
    """Whose password a login has to match. Found before the password is checked."""

    user_id: uuid.UUID
    password_hash: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """How a login ended. ``user`` is set if it succeeded and ``reason`` if it did not."""

    user: User | None
    user_id: uuid.UUID | None
    reason: LoginRefusal | None
    locked_until: datetime | None


@dataclass(frozen=True, slots=True)
class AccessClaims:
    """What a verified access token says about its bearer."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    role: Role
    scope: str
    issued_at: datetime
    expires_at: datetime
    token_id: str


@dataclass(frozen=True, slots=True)
class TokenPair:
    """What a client holds for one session: a short-lived access token and the refresh
    token that renews it. Neither is ever printed."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """How a refresh ended. ``tokens`` is set if it succeeded and ``reason`` if it did not."""

    tokens: TokenPair | None
    user_id: uuid.UUID | None
    session_id: uuid.UUID | None
    reason: Literal["unknown", "revoked", "expired", "reuse_detected", "closed"] | None

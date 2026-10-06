"""The principal: who a request acts for, who is acting, and what they were allowed.

Authorisation is derived in one place. A request resolves to a ``Principal`` and every use
case receives it, so no use case works out for itself who is calling.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, Self

from corridor.identity.errors import InsufficientScope
from corridor.platform.errors import PermissionDenied

# The scope of a user's own session: it stands for every scope.
ALL_SCOPES: Final = "*"


class Scope(StrEnum):
    """What a credential narrower than a user session can be given."""

    WALLET_READ = "wallet:read"
    TRANSFERS_READ = "transfers:read"
    TRANSFERS_CREATE = "transfers:create"
    DEPOSITS_READ = "deposits:read"
    WITHDRAWALS_READ = "withdrawals:read"
    WITHDRAWALS_CREATE = "withdrawals:create"
    BENEFICIARIES_READ = "beneficiaries:read"
    BENEFICIARIES_WRITE = "beneficiaries:write"
    FX_READ = "fx:read"
    FX_CONVERT = "fx:convert"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID  # whose money this is: the owner
    actor_type: Literal["user", "agent"]
    actor_id: uuid.UUID  # the user, or the agent acting for them
    role: Literal["user", "admin"]
    scopes: frozenset[str]  # {"*"} for a user's own session
    session_id: uuid.UUID | None

    @classmethod
    def for_user(
        cls, user_id: uuid.UUID, role: Literal["user", "admin"], session_id: uuid.UUID
    ) -> Self:
        """The principal of a user acting for themselves, in their own session."""
        return cls(
            user_id=user_id,
            actor_type="user",
            actor_id=user_id,
            role=role,
            scopes=frozenset({ALL_SCOPES}),
            session_id=session_id,
        )

    def has_scope(self, scope: str) -> bool:
        return ALL_SCOPES in self.scopes or scope in self.scopes

    @property
    def is_agent(self) -> bool:
        return self.actor_type == "agent"

    @property
    def is_admin(self) -> bool:
        # An agent is never an admin, whatever its owner is: an admin who delegates their
        # wallet has not delegated their office.
        return self.actor_type == "user" and self.role == "admin"


def require_scope(principal: Principal, scope: str) -> None:
    if not principal.has_scope(scope):
        raise InsufficientScope(f"This credential does not have the {scope} scope.")


def require_user_session(principal: Principal) -> None:
    """For what is the owner's alone to do, such as approving what an agent asked for."""
    if principal.actor_type != "user":
        raise InsufficientScope("This action needs the account owner's own session.")


def require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise PermissionDenied("This action needs an administrator.")

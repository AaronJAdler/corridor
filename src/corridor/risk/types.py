"""Risk's vocabulary: what a money path asks to do, and what it is told."""

import uuid
from dataclasses import dataclass
from typing import Literal

from corridor.identity import Principal

MovementKind = Literal["transfer"]


@dataclass(frozen=True, slots=True)
class MoneyMovement:
    """Money about to leave a user's wallet, described before anything is posted."""

    kind: MovementKind
    # Whose money moves out.
    user_id: uuid.UUID
    # Who asked: the user, or an agent acting for them with limits of its own.
    principal: Principal
    asset: str
    amount: int
    # The user who receives it, when the movement has one.
    counterparty_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """What a movement that was not refused may do. A refusal is raised, not returned."""

    outcome: Literal["allow"]

"""Risk's vocabulary: what a money path asks to do, and what it is told."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from corridor.identity import Principal

MovementKind = Literal["transfer", "withdrawal", "conversion"]

# Whose limits a rule sets: everyone in a KYC tier, one user, or one agent.
LimitScope = Literal["tier", "user", "agent"]

# What screening is asked about, and what it answers.
PartyKind = Literal["name", "address", "account"]
ScreeningOutcome = Literal["clear", "review", "deny"]

# What a review is about, and how far it has got.
SubjectType = Literal["withdrawal", "deposit"]
ReviewStatus = Literal["open", "cleared", "rejected"]


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
    # The id of the transfer, withdrawal or conversion. With it, a movement that is
    # authorised twice is counted against the limits once.
    movement_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """What a movement that was not refused may do. A refusal is raised, not returned."""

    outcome: Literal["allow"]


@dataclass(frozen=True, slots=True)
class Limit:
    """One rule: the most a tier, a user or an agent may move at once and in 24 hours."""

    id: uuid.UUID
    scope: LimitScope
    tier: int | None
    user_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    # None for a rule that covers every kind of movement.
    kind: MovementKind | None
    # Whole US cents. None where the rule sets no limit of that sort.
    per_tx_usd: int | None
    daily_usd: int | None


@dataclass(frozen=True, slots=True)
class DenylistEntry:
    """One party on the deny list, in the form screening compares it in."""

    id: uuid.UUID
    kind: PartyKind
    # As normalised: the form a party is looked up in, not the form it was typed in.
    value: str
    outcome: Literal["deny", "review"]
    note: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Review:
    """A movement that screening would not let through unseen, and what became of it."""

    id: uuid.UUID
    subject_type: SubjectType
    subject_id: uuid.UUID
    user_id: uuid.UUID | None
    # What screening answered: the reason the review exists.
    outcome: Literal["deny", "review"]
    status: ReviewStatus
    created_at: datetime
    resolved_at: datetime | None

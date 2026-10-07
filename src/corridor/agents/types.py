"""The agents module's vocabulary: what its service functions hand to other modules."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from corridor.platform.errors import DomainError

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


# What a policy's list of recipients can name: a user, or one of the owner's beneficiaries.
RecipientKind = Literal["user", "beneficiary"]

# The movements a policy is asked about, and the ones that can wait for the owner.
PolicyKind = Literal["transfer", "withdrawal", "conversion"]
ApprovalKind = Literal["transfer", "withdrawal"]

ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "executed", "failed"]


@dataclass(frozen=True, slots=True)
class AllowedRecipient:
    """One destination an agent may pay."""

    kind: RecipientKind
    target_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Policy:
    """What an agent may spend and whom it may pay, as its owner set it.

    An agent whose owner has set nothing has this with every amount None, no recipients
    and ``any_recipient`` False: it can pay nobody.
    """

    agent_id: uuid.UUID
    # Whole US cents. None where the policy sets no limit of that sort.
    per_tx_usd: int | None
    daily_usd: int | None
    # A movement worth more than this waits for the owner's approval. None: none does.
    approval_threshold_usd: int | None
    # True lets the agent pay anyone. False lets it pay only ``allowed_recipients``.
    any_recipient: bool
    allowed_recipients: tuple[AllowedRecipient, ...]
    # None for an agent whose owner has never set a policy.
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class Recipient:
    """Where a movement would send money, as the request named it.

    A user is named by whatever the sender typed, a beneficiary by its id. An address is
    never on a list, so only a policy that allows any recipient lets an agent pay one.
    """

    kind: Literal["user", "beneficiary", "address"]
    value: str


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """What a movement the policy did not refuse may do. A refusal is raised, not returned."""

    outcome: Literal["allow", "requires_approval"]

    @property
    def requires_approval(self) -> bool:
        return self.outcome == "requires_approval"


@dataclass(frozen=True, slots=True)
class TransferIntent:
    """A transfer an agent asked for, kept until its owner decides."""

    # The recipient's user id, found when the agent asked: who is paid cannot change
    # between the request and its approval because a handle changed hands.
    recipient: str
    asset: str
    amount: int
    memo: str | None = None

    @property
    def kind(self) -> ApprovalKind:
        return "transfer"

    @property
    def destination(self) -> Recipient:
        return Recipient("user", self.recipient)


@dataclass(frozen=True, slots=True)
class WithdrawalIntent:
    """A withdrawal an agent asked for, kept until its owner decides."""

    asset: str
    amount: int
    beneficiary_id: uuid.UUID | None = None
    to_address: str | None = None

    @property
    def kind(self) -> ApprovalKind:
        return "withdrawal"

    @property
    def destination(self) -> Recipient:
        if self.beneficiary_id is not None:
            return Recipient("beneficiary", str(self.beneficiary_id))
        return Recipient("address", self.to_address or "")


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A movement above an agent's approval threshold, and what its owner made of it."""

    id: uuid.UUID
    agent_id: uuid.UUID
    owner_user_id: uuid.UUID
    intent: TransferIntent | WithdrawalIntent
    # ``expired`` as soon as the time has passed, whether or not anyone has looked since.
    status: ApprovalStatus
    # The id of the transfer or withdrawal that approving it makes.
    movement_id: uuid.UUID
    # The code of the refusal an approved request met, when it failed.
    failure_code: str | None
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime

    @property
    def kind(self) -> ApprovalKind:
        return self.intent.kind


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """How deciding a request ended.

    ``refusal`` is what stopped an approval that was recorded all the same, as expired or
    as failed. It is returned and not raised, because raising would undo the record: the
    entry point raises it once the transaction has committed.
    """

    request: ApprovalRequest
    refusal: DomainError | None = None

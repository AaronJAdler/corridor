"""Reconciliation's vocabulary: a run, a break, and what a run found."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from corridor.providers import ProviderTransaction

BreakKind = Literal[
    # The provider has a deposit that is not on Corridor's books.
    "missing_deposit",
    # Corridor's books have a deposit the provider's statement does not.
    "unknown_deposit",
    # Both sides have the deposit or the payout, with different amounts.
    "amount_mismatch",
    # The provider paid a withdrawal out, or could not, and it is still in flight here.
    "missing_payout_result",
    # A payout only one side has as paid: on the provider's statement and not a withdrawal
    # still waiting for it here, or settled here and not on the provider's statement.
    "unknown_payout",
    # The ledger's settlement account and the provider's balance differ by more than what
    # is known to be in transit.
    "settlement_balance",
]
BreakStatus = Literal["open", "resolved"]
RunStatus = Literal["completed", "incomplete"]

# Who resolved a break that the repair closed.
SYSTEM: Final = "system"


@dataclass(frozen=True, slots=True)
class Run:
    """One comparison of a window, as recorded."""

    id: uuid.UUID
    window_start: datetime
    window_end: datetime
    # ``incomplete`` when a provider could not be read: what it holds was not compared.
    status: RunStatus
    # How many disagreements the run saw, and how many of them had no open break yet.
    breaks_found: int
    breaks_opened: int
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class Break:
    """One thing a provider and Corridor disagree about, as recorded."""

    id: uuid.UUID
    # The run that first saw it, and the latest one that still did.
    run_id: uuid.UUID
    last_seen_run_id: uuid.UUID
    kind: BreakKind
    provider: str
    # The provider's id for the deposit or the payout; the asset code for a balance.
    provider_ref: str
    asset: str
    # What Corridor recorded and what the provider reports, in minor units, as the latest
    # run that saw the break found them. None on the side that has nothing.
    expected: int | None
    actual: int | None
    status: BreakStatus
    note: str | None
    # ``system`` for a repair, or the id of the admin who resolved it.
    resolved_by: str | None
    created_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunResult:
    run: Run
    # The open break for each disagreement the run saw, as it was before any repair.
    breaks: tuple[Break, ...]
    # How many breaks the repair closed afterwards.
    repaired: int


@dataclass(frozen=True, slots=True)
class Sent:
    """What a provider holds for one withdrawal, whichever kind of provider it is."""

    id: str
    status: Literal["pending", "completed", "failed"]
    asset: str
    amount: int
    # What the provider charges Corridor for it once it completes.
    fee: int
    # Corridor's own id for the withdrawal, as the provider was given it.
    reference: str
    settled_at: datetime | None
    failure_reason: str | None
    tx_hash: str | None


@dataclass(frozen=True, slots=True)
class Finding:
    """One disagreement a run saw, with the provider's record of it when the repair can
    use one."""

    kind: BreakKind
    provider: str
    provider_ref: str
    asset: str
    expected: int | None
    actual: int | None
    # The statement line of a deposit that is missing here.
    transaction: ProviderTransaction | None = None
    # True when the same statement shows that deposit as returned: there is nothing left
    # at the provider to credit.
    returned: bool = False
    # The provider's payout for a withdrawal that is still in flight here.
    sent: Sent | None = None
    withdrawal_id: uuid.UUID | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        """What makes two findings the same disagreement."""
        return (self.kind, self.provider, self.provider_ref)

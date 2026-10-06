"""The ports: what Corridor needs from each kind of provider.

One method per operation of the provider contract. A real provider is integrated by
writing a new adapter against these, and nothing above this module changes.

Every method raises ``ProviderRejected`` for a definite refusal, ``ProviderOutcomeUnknown``
when it cannot tell whether the operation happened, and ``ProviderMisconfigured`` when
Corridor's own configuration is at fault. Every method that changes something takes an
idempotency key and has no default for it: the caller derives it from its own id for the
operation, so that a retry cannot do the thing twice.
"""

from datetime import datetime
from typing import Protocol

from corridor.providers.types import (
    Beneficiary,
    DepositAddress,
    Payout,
    ProviderStatement,
    Rate,
    VirtualAccount,
    Withdrawal,
)


class BankRail(Protocol):
    @property
    def name(self) -> str:
        """The provider's name in Corridor, as it appears in webhook paths and ledger rows."""
        ...

    async def create_virtual_account(
        self, *, customer_reference: str, asset_code: str, idempotency_key: str
    ) -> VirtualAccount: ...

    async def create_beneficiary(
        self,
        *,
        customer_reference: str,
        asset_code: str,
        holder_name: str,
        account_number: str,
        routing_number: str | None = None,
        idempotency_key: str,
    ) -> Beneficiary: ...

    async def create_payout(
        self,
        *,
        beneficiary_id: str,
        asset_code: str,
        amount: int,
        reference: str,
        idempotency_key: str,
    ) -> Payout: ...

    async def get_payout(self, payout_id: str) -> Payout: ...

    async def find_payouts(self, reference: str) -> tuple[Payout, ...]: ...

    async def list_transactions(
        self, *, asset_code: str, start: datetime, end: datetime
    ) -> ProviderStatement: ...


class Custodian(Protocol):
    @property
    def name(self) -> str: ...

    async def create_address(
        self, *, customer_reference: str, asset_code: str, idempotency_key: str
    ) -> DepositAddress: ...

    async def create_withdrawal(
        self,
        *,
        asset_code: str,
        amount: int,
        to_address: str,
        reference: str,
        idempotency_key: str,
    ) -> Withdrawal: ...

    async def get_withdrawal(self, withdrawal_id: str) -> Withdrawal: ...

    async def find_withdrawals(self, reference: str) -> tuple[Withdrawal, ...]: ...

    async def list_transactions(
        self, *, asset_code: str, start: datetime, end: datetime
    ) -> ProviderStatement: ...


class RateSource(Protocol):
    @property
    def name(self) -> str: ...

    async def get_rate(self, base: str, quote: str) -> Rate: ...

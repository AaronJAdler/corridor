"""The ports: what Corridor needs from each kind of provider.

One method per operation of the provider contract. A real provider is integrated by
writing a new adapter against these, and nothing above this module changes.

Every method raises ``ProviderRejected`` for a definite refusal, ``ProviderOutcomeUnknown``
when it cannot tell whether the operation happened, and ``ProviderMisconfigured`` when
Corridor's own configuration is at fault. Every method that changes something takes an
idempotency key and has no default for it: the caller derives it from its own id for the
operation, so that a retry cannot do the thing twice.

Each port also has ``aclose``, which releases the adapter's connections, so that whoever
built an adapter through ``corridor.providers.factory`` can close it without knowing which
one it is.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from corridor.providers.types import (
    BankEvent,
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

    async def aclose(self) -> None: ...

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


@runtime_checkable
class BankEventSource(Protocol):
    """A bank rail whose webhooks say only that something changed, and not what.

    Such a provider's event names an object, and what happened is read from the object as
    the provider holds it now. That is a network call, made by the worker with no
    transaction open. Reading the present state is also what makes the events safe in any
    order: an old event read late finds what is true now.

    A bank rail whose webhooks carry the contract's ``data`` does not implement this.
    """

    @property
    def name(self) -> str: ...

    async def resolve_event(
        self, *, category: str, object_type: str, object_id: str
    ) -> tuple[BankEvent, ...]:
        """What an event of ``category`` about one object means, in the contract's bank
        events. Empty when it means nothing Corridor acts on yet."""
        ...


class Custodian(Protocol):
    @property
    def name(self) -> str: ...

    async def aclose(self) -> None: ...

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

    async def aclose(self) -> None: ...

    async def get_rate(self, base: str, quote: str) -> Rate: ...

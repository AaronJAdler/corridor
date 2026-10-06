"""The custodian adapter for the simulated custodian, ``/custody/v1`` of the provider
contract."""

from datetime import datetime
from typing import Final, Literal

import httpx
from pydantic import AwareDatetime, Field

from corridor.platform.config import Settings
from corridor.providers.addresses import is_valid_address
from corridor.providers.http import (
    Document,
    ProviderClient,
    ResponseMismatch,
    StatementDocument,
    in_utc,
    major_units,
    minor_units,
    optional_utc,
    require_echo,
    searchable,
    segment,
    statement,
    statement_params,
)
from corridor.providers.types import DepositAddress, ProviderStatement, Withdrawal


class _AddressDocument(Document):
    id: str = Field(min_length=1)
    customer_reference: str
    asset: str
    network: str
    address: str


class _WithdrawalDocument(Document):
    id: str = Field(min_length=1)
    status: Literal["pending", "broadcast", "completed", "failed"]
    asset: str
    amount: str
    network_fee: str
    to_address: str
    reference: str
    tx_hash: str | None
    confirmations: int = Field(ge=0)
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    failure_reason: str | None


class _WithdrawalsDocument(Document):
    withdrawals: list[_WithdrawalDocument]


def _withdrawal(document: _WithdrawalDocument) -> Withdrawal:
    return Withdrawal(
        id=document.id,
        status=document.status,
        asset_code=document.asset,
        amount=minor_units(document.amount, document.asset),
        network_fee=minor_units(document.network_fee, document.asset, allow_zero=True),
        to_address=document.to_address,
        reference=document.reference,
        tx_hash=document.tx_hash,
        confirmations=document.confirmations,
        created_at=in_utc(document.created_at),
        completed_at=optional_utc(document.completed_at),
        failure_reason=document.failure_reason,
    )


class SimCustody:
    """Implements ``Custodian``."""

    name: Final = "simcustody"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._http = ProviderClient(
            provider=self.name,
            base_url=settings.custody_url,
            api_key=settings.custody_api_key,
            timeout_seconds=settings.provider_timeout_seconds,
            client=client,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create_address(
        self, *, customer_reference: str, asset_code: str, idempotency_key: str
    ) -> DepositAddress:
        def convert(document: _AddressDocument) -> DepositAddress:
            require_echo("customer_reference", customer_reference, document.customer_reference)
            require_echo("asset", asset_code, document.asset)
            # A customer will be told to send money here. An address that fails its own
            # checksum is one nobody can be sure the custodian watches.
            if not is_valid_address(document.address):
                raise ResponseMismatch("'address' is not a valid address")
            return DepositAddress(
                id=document.id,
                customer_reference=document.customer_reference,
                asset_code=document.asset,
                network=document.network,
                address=document.address,
            )

        return await self._http.post(
            "create_address",
            "/custody/v1/addresses",
            _AddressDocument,
            convert,
            body={"customer_reference": customer_reference, "asset": asset_code},
            idempotency_key=idempotency_key,
        )

    async def create_withdrawal(
        self,
        *,
        asset_code: str,
        amount: int,
        to_address: str,
        reference: str,
        idempotency_key: str,
    ) -> Withdrawal:
        def convert(document: _WithdrawalDocument) -> Withdrawal:
            require_echo("asset", asset_code, document.asset)
            require_echo("to_address", to_address, document.to_address)
            require_echo("reference", reference, document.reference)
            withdrawal = _withdrawal(document)
            # Compared as integers, so "25.0" and "25.000000" are the same amount.
            require_echo("amount", amount, withdrawal.amount)
            return withdrawal

        return await self._http.post(
            "create_withdrawal",
            "/custody/v1/withdrawals",
            _WithdrawalDocument,
            convert,
            body={
                "asset": asset_code,
                "amount": major_units(amount, asset_code),
                "to_address": to_address,
                "reference": reference,
            },
            idempotency_key=idempotency_key,
        )

    async def get_withdrawal(self, withdrawal_id: str) -> Withdrawal:
        def convert(document: _WithdrawalDocument) -> Withdrawal:
            require_echo("id", withdrawal_id, document.id)
            return _withdrawal(document)

        return await self._http.get(
            "get_withdrawal",
            f"/custody/v1/withdrawals/{segment(withdrawal_id)}",
            _WithdrawalDocument,
            convert,
        )

    async def find_withdrawals(self, reference: str) -> tuple[Withdrawal, ...]:
        def convert(document: _WithdrawalsDocument) -> tuple[Withdrawal, ...]:
            for withdrawal in document.withdrawals:
                require_echo("withdrawals.reference", reference, withdrawal.reference)
            return tuple(_withdrawal(withdrawal) for withdrawal in document.withdrawals)

        return await self._http.get(
            "find_withdrawals",
            "/custody/v1/withdrawals",
            _WithdrawalsDocument,
            convert,
            params={"reference": searchable(reference)},
        )

    async def list_transactions(
        self, *, asset_code: str, start: datetime, end: datetime
    ) -> ProviderStatement:
        return await self._http.get(
            "list_transactions",
            "/custody/v1/transactions",
            StatementDocument,
            lambda document: statement(document, asset_code=asset_code, start=start, end=end),
            params=statement_params(asset_code, start, end),
        )

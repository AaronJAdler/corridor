"""The bank rail adapter for the simulated bank, ``/bank/v1`` of the provider contract."""

from datetime import datetime
from typing import Final, Literal

import httpx
from pydantic import AwareDatetime, Field

from corridor.platform.config import Settings
from corridor.providers.http import (
    Document,
    ProviderClient,
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
from corridor.providers.types import Beneficiary, Payout, ProviderStatement, VirtualAccount


class _VirtualAccountDocument(Document):
    id: str = Field(min_length=1)
    customer_reference: str
    asset: str
    rail: str
    bank_name: str
    account_number: str = Field(min_length=1)
    # Present for USD only.
    routing_number: str | None = None


class _BeneficiaryDocument(Document):
    id: str = Field(min_length=1)
    asset: str
    rail: str
    holder_name: str
    account_mask: str


class _PayoutDocument(Document):
    id: str = Field(min_length=1)
    status: Literal["pending", "completed", "failed"]
    beneficiary_id: str
    asset: str
    amount: str
    fee: str
    reference: str
    created_at: AwareDatetime
    settled_at: AwareDatetime | None
    failure_reason: str | None


class _PayoutsDocument(Document):
    payouts: list[_PayoutDocument]


def _payout(document: _PayoutDocument) -> Payout:
    return Payout(
        id=document.id,
        status=document.status,
        beneficiary_id=document.beneficiary_id,
        asset_code=document.asset,
        amount=minor_units(document.amount, document.asset),
        fee=minor_units(document.fee, document.asset, allow_zero=True),
        reference=document.reference,
        created_at=in_utc(document.created_at),
        settled_at=optional_utc(document.settled_at),
        failure_reason=document.failure_reason,
    )


class SimBank:
    """Implements ``BankRail``."""

    name: Final = "simbank"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._http = ProviderClient(
            provider=self.name,
            base_url=settings.bank_rail_url,
            api_key=settings.bank_rail_api_key,
            timeout_seconds=settings.provider_timeout_seconds,
            client=client,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create_virtual_account(
        self, *, customer_reference: str, asset_code: str, idempotency_key: str
    ) -> VirtualAccount:
        def convert(document: _VirtualAccountDocument) -> VirtualAccount:
            require_echo("customer_reference", customer_reference, document.customer_reference)
            require_echo("asset", asset_code, document.asset)
            return VirtualAccount(
                id=document.id,
                customer_reference=document.customer_reference,
                asset_code=document.asset,
                rail=document.rail,
                bank_name=document.bank_name,
                account_number=document.account_number,
                routing_number=document.routing_number,
            )

        return await self._http.post(
            "create_virtual_account",
            "/bank/v1/virtual-accounts",
            _VirtualAccountDocument,
            convert,
            body={"customer_reference": customer_reference, "asset": asset_code},
            idempotency_key=idempotency_key,
        )

    async def create_beneficiary(
        self,
        *,
        customer_reference: str,
        asset_code: str,
        holder_name: str,
        account_number: str,
        routing_number: str | None = None,
        idempotency_key: str,
    ) -> Beneficiary:
        def convert(document: _BeneficiaryDocument) -> Beneficiary:
            require_echo("asset", asset_code, document.asset)
            require_echo("holder_name", holder_name, document.holder_name)
            return Beneficiary(
                id=document.id,
                asset_code=document.asset,
                rail=document.rail,
                holder_name=document.holder_name,
                account_mask=document.account_mask,
            )

        body: dict[str, object] = {
            "customer_reference": customer_reference,
            "asset": asset_code,
            "holder_name": holder_name,
            "account_number": account_number,
        }
        if routing_number is not None:
            body["routing_number"] = routing_number
        return await self._http.post(
            "create_beneficiary",
            "/bank/v1/beneficiaries",
            _BeneficiaryDocument,
            convert,
            body=body,
            idempotency_key=idempotency_key,
        )

    async def create_payout(
        self,
        *,
        beneficiary_id: str,
        asset_code: str,
        amount: int,
        reference: str,
        idempotency_key: str,
    ) -> Payout:
        def convert(document: _PayoutDocument) -> Payout:
            require_echo("beneficiary_id", beneficiary_id, document.beneficiary_id)
            require_echo("asset", asset_code, document.asset)
            require_echo("reference", reference, document.reference)
            payout = _payout(document)
            # Compared as integers, so "100.0" and "100.00" are the same amount.
            require_echo("amount", amount, payout.amount)
            return payout

        return await self._http.post(
            "create_payout",
            "/bank/v1/payouts",
            _PayoutDocument,
            convert,
            body={
                "beneficiary_id": beneficiary_id,
                "asset": asset_code,
                "amount": major_units(amount, asset_code),
                "reference": reference,
            },
            idempotency_key=idempotency_key,
        )

    async def get_payout(self, payout_id: str) -> Payout:
        def convert(document: _PayoutDocument) -> Payout:
            require_echo("id", payout_id, document.id)
            return _payout(document)

        return await self._http.get(
            "get_payout", f"/bank/v1/payouts/{segment(payout_id)}", _PayoutDocument, convert
        )

    async def find_payouts(self, reference: str) -> tuple[Payout, ...]:
        def convert(document: _PayoutsDocument) -> tuple[Payout, ...]:
            for payout in document.payouts:
                require_echo("payouts.reference", reference, payout.reference)
            return tuple(_payout(payout) for payout in document.payouts)

        return await self._http.get(
            "find_payouts",
            "/bank/v1/payouts",
            _PayoutsDocument,
            convert,
            params={"reference": searchable(reference)},
        )

    async def list_transactions(
        self, *, asset_code: str, start: datetime, end: datetime
    ) -> ProviderStatement:
        return await self._http.get(
            "list_transactions",
            "/bank/v1/transactions",
            StatementDocument,
            lambda document: statement(document, asset_code=asset_code, start=start, end=end),
            params=statement_params(asset_code, start, end),
        )

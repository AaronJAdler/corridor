"""The bank rail's books: virtual accounts, beneficiaries, deposits and payouts."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

from corridor_sim import money
from corridor_sim.books import Account, Statement, Transaction
from corridor_sim.clock import SimClock, format_time
from corridor_sim.errors import ApiError
from corridor_sim.idempotency import IdempotencyKeys, IdempotentRequest
from corridor_sim.ids import IdFactory
from corridor_sim.settings import SimSettings
from corridor_sim.webhooks import WebhookQueue


@dataclass(frozen=True, slots=True)
class Rail:
    name: str
    # Charged to Corridor, in the payout's asset, when a payout completes.
    fee: Decimal


# Each asset moves on one rail: the contract's table.
RAILS: Final[Mapping[str, Rail]] = MappingProxyType(
    {
        "USD": Rail("ach", Decimal("0.25")),
        "MXN": Rail("spei", Decimal("5.00")),
        "BRL": Rail("pix", Decimal("0.10")),
    }
)

BANK_NAME: Final = "Sim Bank"
ROUTING_NUMBER: Final = "021000021"
# A deposit that names no virtual account the bank knows cannot take its asset from one.
UNATTRIBUTED_ASSET: Final = "USD"
# An account number that ends like this belongs to a closed account: a payout to it fails.
CLOSED_ACCOUNT_SUFFIX: Final = "0000"

_ACH_ACCOUNT: Final = re.compile(r"[0-9]{4,17}")
_ACH_ROUTING: Final = re.compile(r"[0-9]{9}")
_CLABE: Final = re.compile(r"[0-9]{18}")
_PIX_KEY: Final = re.compile(r"\S{1,77}")

_MASK: Final = "\u2022" * 4


@dataclass(frozen=True, slots=True)
class VirtualAccount:
    id: str
    customer_reference: str
    asset: str
    rail: str
    account_number: str
    routing_number: str | None


@dataclass(frozen=True, slots=True)
class Beneficiary:
    id: str
    customer_reference: str
    asset: str
    rail: str
    holder_name: str
    # Kept so that a payout can be judged by where it is going. No provider endpoint
    # returns it.
    account_number: str
    routing_number: str | None

    @property
    def account_mask(self) -> str:
        """Four bullets and the last four characters, but never the whole identifier: one
        shorter than eight characters shows at most half of itself."""
        visible = min(4, len(self.account_number) // 2)
        return _MASK + self.account_number[len(self.account_number) - visible :]


@dataclass(slots=True)
class Deposit:
    id: str
    virtual_account_id: str
    # None when the virtual account is not one the bank issued.
    customer_reference: str | None
    asset: str
    amount: Decimal
    sender_name: str
    reference: str
    received_at: datetime
    returned_at: datetime | None = None
    return_reason: str | None = None


@dataclass(slots=True)
class Payout:
    id: str
    beneficiary_id: str
    asset: str
    amount: Decimal
    fee: Decimal
    reference: str
    idempotency_key: str
    created_at: datetime
    due_at: datetime
    status: Literal["pending", "completed", "failed"] = "pending"
    settled_at: datetime | None = None
    failure_reason: str | None = None


class BankBooks:
    def __init__(
        self, settings: SimSettings, clock: SimClock, ids: IdFactory, webhooks: WebhookQueue
    ) -> None:
        self._clock = clock
        self._ids = ids
        self._webhooks = webhooks
        self._settle_after = {
            "USD": timedelta(seconds=settings.ach_settle_seconds),
            "MXN": timedelta(seconds=settings.spei_settle_seconds),
            "BRL": timedelta(seconds=settings.pix_settle_seconds),
        }
        self._accounts = {asset: Account(asset) for asset in RAILS}
        self._virtual_accounts: dict[str, VirtualAccount] = {}
        self._virtual_account_of: dict[tuple[str, str], str] = {}
        self._account_numbers: set[str] = set()
        self._beneficiaries: dict[str, Beneficiary] = {}
        self._deposits: dict[str, Deposit] = {}
        self._payouts: dict[str, Payout] = {}
        self._pending: list[str] = []
        self._virtual_account_keys = IdempotencyKeys()
        self._beneficiary_keys = IdempotencyKeys()
        self._payout_keys = IdempotencyKeys()

    # --- what the provider API does ----------------------------------------------------------

    def create_virtual_account(
        self, customer_reference: str, asset: str, idempotent: IdempotentRequest | None = None
    ) -> tuple[VirtualAccount, bool]:
        """The account a customer deposits into. There is one per customer and asset, so
        asking again returns it; the flag says whether this call created it."""
        replayed = self._virtual_account_keys.resource_for(idempotent)
        if replayed is not None:
            return self._virtual_accounts[replayed], False

        rail = _rail_of(asset)
        existing = self._virtual_account_of.get((customer_reference, asset))
        if existing is None:
            account = VirtualAccount(
                id=self._ids.new("va_"),
                customer_reference=customer_reference,
                asset=asset,
                rail=rail.name,
                account_number=self._new_account_number(asset),
                routing_number=ROUTING_NUMBER if asset == "USD" else None,
            )
            self._virtual_accounts[account.id] = account
            self._virtual_account_of[customer_reference, asset] = account.id
        else:
            account = self._virtual_accounts[existing]
        self._virtual_account_keys.bind(idempotent, account.id)
        return account, existing is None

    def virtual_accounts(self, customer_reference: str) -> list[VirtualAccount]:
        """The accounts one customer deposits into, in every asset, in asset order. Empty
        for a customer the bank has never issued one to."""
        return sorted(
            (
                account
                for account in self._virtual_accounts.values()
                if account.customer_reference == customer_reference
            ),
            key=lambda account: account.asset,
        )

    def create_beneficiary(
        self,
        *,
        customer_reference: str,
        asset: str,
        holder_name: str,
        account_number: object,
        routing_number: object,
        idempotent: IdempotentRequest | None = None,
    ) -> tuple[Beneficiary, bool]:
        replayed = self._beneficiary_keys.resource_for(idempotent)
        if replayed is not None:
            return self._beneficiaries[replayed], False

        rail = _rail_of(asset)
        number, routing = _account_identifiers(rail, account_number, routing_number)
        beneficiary = Beneficiary(
            id=self._ids.new("ben_"),
            customer_reference=customer_reference,
            asset=asset,
            rail=rail.name,
            holder_name=holder_name,
            account_number=number,
            routing_number=routing,
        )
        self._beneficiaries[beneficiary.id] = beneficiary
        self._beneficiary_keys.bind(idempotent, beneficiary.id)
        return beneficiary, True

    def create_payout(
        self,
        *,
        beneficiary_id: str,
        asset: str,
        amount: object,
        reference: str,
        idempotent: IdempotentRequest,
    ) -> tuple[Payout, bool]:
        """Accept a payout, or return the one this idempotency key already created.

        A repeated key is recognised before anything about the request is judged, and the
        key is bound in the same step that records the payout. The payout is never refused
        for lack of funds: the balance may go negative.
        """
        replayed = self._payout_keys.resource_for(idempotent)
        if replayed is not None:
            return self._payouts[replayed], False

        beneficiary = self._beneficiaries.get(beneficiary_id)
        if beneficiary is None:
            raise ApiError(
                404, "beneficiary_not_found", f"There is no beneficiary {beneficiary_id}."
            )
        if beneficiary.asset != asset:
            raise ApiError(
                422, "asset_mismatch", f"Beneficiary {beneficiary_id} is in {beneficiary.asset}."
            )
        value = money.parse_amount(amount, asset)

        now = self._clock.now()
        payout = Payout(
            id=self._ids.new("po_"),
            beneficiary_id=beneficiary.id,
            asset=asset,
            amount=value,
            fee=RAILS[asset].fee,
            reference=reference,
            idempotency_key=idempotent.key,
            created_at=now,
            due_at=now + self._settle_after[asset],
        )
        self._payouts[payout.id] = payout
        self._pending.append(payout.id)
        self._payout_keys.bind(idempotent, payout.id)
        return payout, True

    def get_payout(self, payout_id: str) -> Payout:
        payout = self._payouts.get(payout_id)
        if payout is None:
            raise ApiError(404, "payout_not_found", f"There is no payout {payout_id}.")
        return payout

    def payouts_by_reference(self, reference: str) -> list[Payout]:
        return [payout for payout in self._payouts.values() if payout.reference == reference]

    def statement(self, asset: str, start: datetime, end: datetime) -> Statement:
        _rail_of(asset)
        return self._accounts[asset].statement(start, end)

    # --- what the outside world does ---------------------------------------------------------

    def receive_deposit(
        self,
        *,
        virtual_account_id: str,
        amount: object,
        sender_name: str,
        reference: str,
        asset: str | None = None,
    ) -> Deposit:
        """A customer's bank transfer arrives. One that names a virtual account the bank
        never issued is accepted all the same: attributing it is Corridor's problem."""
        if asset is not None:
            _rail_of(asset)
        account = self._virtual_accounts.get(virtual_account_id)
        if account is None:
            asset = asset or UNATTRIBUTED_ASSET
        elif asset not in (None, account.asset):
            raise ApiError(
                422,
                "asset_mismatch",
                f"Virtual account {virtual_account_id} is in {account.asset}.",
            )
        else:
            asset = account.asset
        value = money.parse_amount(amount, asset)

        deposit = Deposit(
            id=self._ids.new("dep_"),
            virtual_account_id=virtual_account_id,
            customer_reference=account.customer_reference if account is not None else None,
            asset=asset,
            amount=value,
            sender_name=sender_name,
            reference=reference,
            received_at=self._clock.now(),
        )
        self._deposits[deposit.id] = deposit
        self._accounts[asset].post(
            Transaction(
                id=deposit.id,
                type="deposit",
                direction="credit",
                asset=asset,
                amount=value,
                reference=reference,
                related_id=virtual_account_id,
                occurred_at=deposit.received_at,
            )
        )
        self._webhooks.emit(
            "bank",
            "deposit.received",
            {
                "deposit_id": deposit.id,
                "virtual_account_id": deposit.virtual_account_id,
                "customer_reference": deposit.customer_reference,
                "asset": asset,
                "amount": money.format_amount(value, asset),
                "sender_name": sender_name,
                "reference": reference,
            },
        )
        return deposit

    def return_deposit(self, deposit_id: str, reason: str) -> Deposit:
        """The sending bank recalls a deposit, whatever has become of the money since."""
        deposit = self._deposits.get(deposit_id)
        if deposit is None:
            raise ApiError(404, "deposit_not_found", f"There is no deposit {deposit_id}.")
        if deposit.returned_at is not None:
            raise ApiError(
                409, "deposit_already_returned", f"Deposit {deposit_id} was already returned."
            )

        deposit.returned_at = self._clock.now()
        deposit.return_reason = reason
        self._accounts[deposit.asset].post(
            Transaction(
                id=f"{deposit.id}:return",
                type="deposit_return",
                direction="debit",
                asset=deposit.asset,
                amount=deposit.amount,
                reference=deposit.reference,
                related_id=deposit.id,
                occurred_at=deposit.returned_at,
            )
        )
        self._webhooks.emit(
            "bank",
            "deposit.returned",
            {
                "deposit_id": deposit.id,
                "asset": deposit.asset,
                "amount": money.format_amount(deposit.amount, deposit.asset),
                "reason": reason,
            },
        )
        return deposit

    # --- what time does ----------------------------------------------------------------------

    def settle_due(self, now: datetime) -> None:
        """Settle every pending payout whose rail's delay has passed."""
        still_pending: list[str] = []
        for payout_id in self._pending:
            payout = self._payouts[payout_id]
            if payout.due_at > now:
                still_pending.append(payout_id)
            elif self._beneficiaries[payout.beneficiary_id].account_number.endswith(
                CLOSED_ACCOUNT_SUFFIX
            ):
                self._fail(payout)
            else:
                self._complete(payout, now)
        self._pending = still_pending

    def _complete(self, payout: Payout, now: datetime) -> None:
        payout.status = "completed"
        payout.settled_at = now
        account = self._accounts[payout.asset]
        account.post(
            Transaction(
                id=payout.id,
                type="payout",
                direction="debit",
                asset=payout.asset,
                amount=payout.amount,
                reference=payout.reference,
                related_id=payout.beneficiary_id,
                occurred_at=now,
            )
        )
        account.post(
            Transaction(
                id=f"{payout.id}:fee",
                type="payout_fee",
                direction="debit",
                asset=payout.asset,
                amount=payout.fee,
                reference=payout.reference,
                related_id=payout.id,
                occurred_at=now,
            )
        )
        self._webhooks.emit(
            "bank",
            "payout.completed",
            {
                "payout_id": payout.id,
                "reference": payout.reference,
                "asset": payout.asset,
                "amount": money.format_amount(payout.amount, payout.asset),
                "fee": money.format_amount(payout.fee, payout.asset),
                "settled_at": format_time(now),
            },
        )

    def _fail(self, payout: Payout) -> None:
        # Nothing left the account, so there is no transaction and no fee.
        payout.status = "failed"
        payout.failure_reason = "account_closed"
        self._webhooks.emit(
            "bank",
            "payout.failed",
            {
                "payout_id": payout.id,
                "reference": payout.reference,
                "asset": payout.asset,
                "amount": money.format_amount(payout.amount, payout.asset),
                "failure_reason": payout.failure_reason,
            },
        )

    # --- inspection --------------------------------------------------------------------------

    def balances(self) -> dict[str, Decimal]:
        return {asset: account.balance for asset, account in self._accounts.items()}

    def payouts(self) -> list[Payout]:
        return list(self._payouts.values())

    def deposits(self) -> list[Deposit]:
        return list(self._deposits.values())

    def beneficiary(self, beneficiary_id: str) -> Beneficiary:
        return self._beneficiaries[beneficiary_id]

    def _new_account_number(self, asset: str) -> str:
        while True:
            if asset == "USD":
                number = "9000" + self._ids.digits(8)
            elif asset == "MXN":
                number = "646180" + self._ids.digits(12)
            else:
                # A PIX key can be an e-mail address, a phone number, a tax id or, as here,
                # a random key in the form of a UUID.
                key = self._ids.hex(32)
                number = f"{key[:8]}-{key[8:12]}-{key[12:16]}-{key[16:20]}-{key[20:]}"
            if number not in self._account_numbers:
                self._account_numbers.add(number)
                return number


def _rail_of(asset: str) -> Rail:
    rail = RAILS.get(asset)
    if rail is None:
        raise ApiError(422, "unsupported_asset", "The bank carries USD, MXN and BRL.")
    return rail


def _account_identifiers(
    rail: Rail, account_number: object, routing_number: object
) -> tuple[str, str | None]:
    """Check an account's identifiers against the shape its rail requires."""
    if rail.name == "ach":
        if not _matches(_ACH_ACCOUNT, account_number) or not _matches(_ACH_ROUTING, routing_number):
            raise _invalid_account(
                "An ACH account is 4 to 17 digits with a 9-digit routing number."
            )
        return str(account_number), str(routing_number)
    if rail.name == "spei":
        if not _matches(_CLABE, account_number):
            raise _invalid_account("A SPEI account is an 18-digit CLABE.")
    elif not _matches(_PIX_KEY, account_number):
        raise _invalid_account("A PIX key is 1 to 77 characters with no whitespace.")
    return str(account_number), None


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _invalid_account(message: str) -> ApiError:
    return ApiError(422, "invalid_account", message)


# --- documents -------------------------------------------------------------------------------


def virtual_account_document(account: VirtualAccount) -> dict[str, object]:
    document: dict[str, object] = {
        "id": account.id,
        "customer_reference": account.customer_reference,
        "asset": account.asset,
        "rail": account.rail,
        "bank_name": BANK_NAME,
        "account_number": account.account_number,
    }
    if account.routing_number is not None:
        document["routing_number"] = account.routing_number
    return document


def beneficiary_document(beneficiary: Beneficiary) -> dict[str, object]:
    return {
        "id": beneficiary.id,
        "asset": beneficiary.asset,
        "rail": beneficiary.rail,
        "holder_name": beneficiary.holder_name,
        "account_mask": beneficiary.account_mask,
    }


def payout_document(payout: Payout) -> dict[str, object]:
    return {
        "id": payout.id,
        "status": payout.status,
        "beneficiary_id": payout.beneficiary_id,
        "asset": payout.asset,
        "amount": money.format_amount(payout.amount, payout.asset),
        "fee": money.format_amount(payout.fee, payout.asset),
        "reference": payout.reference,
        "created_at": format_time(payout.created_at),
        "settled_at": format_time(payout.settled_at) if payout.settled_at is not None else None,
        "failure_reason": payout.failure_reason,
    }


def payout_inspection(payout: Payout, beneficiary: Beneficiary) -> dict[str, object]:
    """A payout as a test sees it: with its key, and with where it is really going."""
    return {
        **payout_document(payout),
        "idempotency_key": payout.idempotency_key,
        "due_at": format_time(payout.due_at),
        "beneficiary": {
            "id": beneficiary.id,
            "customer_reference": beneficiary.customer_reference,
            "asset": beneficiary.asset,
            "rail": beneficiary.rail,
            "holder_name": beneficiary.holder_name,
            "account_number": beneficiary.account_number,
            "routing_number": beneficiary.routing_number,
        },
    }


def deposit_document(deposit: Deposit) -> dict[str, object]:
    return {
        "id": deposit.id,
        "virtual_account_id": deposit.virtual_account_id,
        "customer_reference": deposit.customer_reference,
        "asset": deposit.asset,
        "amount": money.format_amount(deposit.amount, deposit.asset),
        "sender_name": deposit.sender_name,
        "reference": deposit.reference,
        "status": "received" if deposit.returned_at is None else "returned",
        "received_at": format_time(deposit.received_at),
        "returned_at": format_time(deposit.returned_at)
        if deposit.returned_at is not None
        else None,
        "return_reason": deposit.return_reason,
    }

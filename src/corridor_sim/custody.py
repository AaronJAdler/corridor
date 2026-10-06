"""The custodian's books: deposit addresses, a chain of blocks, deposits and withdrawals.

One network, ``simchain``, carrying one asset. Nothing on it is final at once: a deposit is
credited and a withdrawal is charged only when enough blocks have passed.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from corridor_sim import money
from corridor_sim.books import Account, Statement, Transaction
from corridor_sim.clock import SimClock, format_time
from corridor_sim.errors import ApiError
from corridor_sim.idempotency import IdempotencyKeys, IdempotentRequest
from corridor_sim.ids import IdFactory
from corridor_sim.settings import SimSettings
from corridor_sim.webhooks import WebhookQueue

NETWORK: Final = "simchain"
ASSET: Final = "USDC"
# Fixed, paid from Corridor's balance at the custodian, charged when a withdrawal completes.
NETWORK_FEE: Final = Decimal("0.150000")
# The network rejects a withdrawal to an address whose body begins like this.
REJECTED_BODY_PREFIX: Final = "dead"

_ADDRESS_PREFIX: Final = "sim1"
_BODY_LENGTH: Final = 32
_ADDRESS: Final = re.compile(r"sim1(?P<body>[a-z2-7]{32})(?P<checksum>[0-9a-f]{8})")


def address_for(body: str) -> str:
    """The address with this body: the prefix, the body, and the body's checksum."""
    return _ADDRESS_PREFIX + body + _checksum(body)


def is_valid_address(text: object) -> bool:
    """Whether ``text`` is a simchain address, by the rule either side can check alone:
    ``sim1``, 32 characters of lower-case base32, then 8 lower-case hex characters that are
    the first four bytes of the SHA-256 of the body."""
    match = _ADDRESS.fullmatch(text) if isinstance(text, str) else None
    return match is not None and match["checksum"] == _checksum(match["body"])


def _checksum(body: str) -> str:
    return hashlib.sha256(body.encode("ascii")).digest()[:4].hex()


@dataclass(frozen=True, slots=True)
class Address:
    id: str
    customer_reference: str
    asset: str
    address: str


@dataclass(slots=True)
class ChainDeposit:
    id: str
    address_id: str
    address: str
    customer_reference: str
    asset: str
    amount: Decimal
    tx_hash: str
    from_address: str
    # The height of the chain when the transaction was first seen. It has one confirmation
    # for every block since.
    detected_height: int
    detected_at: datetime
    status: Literal["detected", "confirmed", "failed"] = "detected"
    confirmed_at: datetime | None = None
    failure_reason: str | None = None


@dataclass(slots=True)
class Withdrawal:
    id: str
    asset: str
    amount: Decimal
    network_fee: Decimal
    to_address: str
    reference: str
    idempotency_key: str
    created_height: int
    created_at: datetime
    status: Literal["pending", "broadcast", "completed", "failed"] = "pending"
    tx_hash: str | None = None
    # The block that carried the transaction: the first one after the withdrawal was made.
    broadcast_height: int | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None


class CustodyBooks:
    def __init__(
        self, settings: SimSettings, clock: SimClock, ids: IdFactory, webhooks: WebhookQueue
    ) -> None:
        self._clock = clock
        self._ids = ids
        self._webhooks = webhooks
        self._final_after = settings.confirmations
        self._block_time = timedelta(seconds=settings.block_seconds)
        # Blocks are counted from the moment these books were opened.
        self._genesis = clock.now()
        self._mined = 0
        self.height = 0
        self._account = Account(ASSET)
        self._addresses: dict[str, Address] = {}
        self._address_of: dict[tuple[str, str], str] = {}
        self._address_ids: dict[str, str] = {}
        self._deposits: dict[str, ChainDeposit] = {}
        self._withdrawals: dict[str, Withdrawal] = {}
        self._unconfirmed: list[str] = []
        self._in_flight: list[str] = []
        self._address_keys = IdempotencyKeys()
        self._withdrawal_keys = IdempotencyKeys()

    # --- what the provider API does ----------------------------------------------------------

    def create_address(
        self, customer_reference: str, asset: str, idempotent: IdempotentRequest | None = None
    ) -> tuple[Address, bool]:
        """A customer's deposit address. There is one per customer and asset, so asking
        again returns it; the flag says whether this call created it."""
        replayed = self._address_keys.resource_for(idempotent)
        if replayed is not None:
            return self._addresses[replayed], False

        _check_asset(asset)
        existing = self._address_of.get((customer_reference, asset))
        if existing is None:
            address = Address(
                id=self._ids.new("addr_"),
                customer_reference=customer_reference,
                asset=asset,
                address=self._new_address(),
            )
            self._addresses[address.id] = address
            self._address_of[customer_reference, asset] = address.id
            self._address_ids[address.address] = address.id
        else:
            address = self._addresses[existing]
        self._address_keys.bind(idempotent, address.id)
        return address, existing is None

    def create_withdrawal(
        self,
        *,
        asset: str,
        amount: object,
        to_address: object,
        reference: str,
        idempotent: IdempotentRequest,
    ) -> tuple[Withdrawal, bool]:
        """Accept a withdrawal, or return the one this idempotency key already created.

        As with a payout, a repeated key is recognised before the request is judged, and
        the key is bound in the same step that records the withdrawal.
        """
        replayed = self._withdrawal_keys.resource_for(idempotent)
        if replayed is not None:
            return self._withdrawals[replayed], False

        _check_asset(asset)
        value = money.parse_amount(amount, asset)
        if not isinstance(to_address, str) or not is_valid_address(to_address):
            raise _invalid_address("'to_address' is not a simchain address.")

        withdrawal = Withdrawal(
            id=self._ids.new("wd_"),
            asset=asset,
            amount=value,
            network_fee=NETWORK_FEE,
            to_address=to_address,
            reference=reference,
            idempotency_key=idempotent.key,
            created_height=self.height,
            created_at=self._clock.now(),
        )
        self._withdrawals[withdrawal.id] = withdrawal
        self._in_flight.append(withdrawal.id)
        self._withdrawal_keys.bind(idempotent, withdrawal.id)
        return withdrawal, True

    def get_withdrawal(self, withdrawal_id: str) -> Withdrawal:
        withdrawal = self._withdrawals.get(withdrawal_id)
        if withdrawal is None:
            raise ApiError(404, "withdrawal_not_found", f"There is no withdrawal {withdrawal_id}.")
        return withdrawal

    def withdrawals_by_reference(self, reference: str) -> list[Withdrawal]:
        return [w for w in self._withdrawals.values() if w.reference == reference]

    def statement(self, asset: str, start: datetime, end: datetime) -> Statement:
        _check_asset(asset)
        return self._account.statement(start, end)

    # --- what the outside world does ---------------------------------------------------------

    def detect_deposit(self, *, address: str, amount: object, from_address: object) -> ChainDeposit:
        """A transaction to one of the custodian's addresses appears, unconfirmed."""
        address_id = self._address_ids.get(address)
        if address_id is None:
            raise ApiError(
                404, "address_not_found", "That is not a deposit address this custodian issued."
            )
        target = self._addresses[address_id]
        value = money.parse_amount(amount, target.asset)
        if not isinstance(from_address, str) or not is_valid_address(from_address):
            raise _invalid_address("'from_address' is not a simchain address.")

        deposit = ChainDeposit(
            id=self._ids.new("dep_"),
            address_id=target.id,
            address=target.address,
            customer_reference=target.customer_reference,
            asset=target.asset,
            amount=value,
            tx_hash=self._ids.hex(64),
            from_address=from_address,
            detected_height=self.height,
            detected_at=self._clock.now(),
        )
        self._deposits[deposit.id] = deposit
        self._unconfirmed.append(deposit.id)
        # Information only: the funds are not final, and nothing is credited.
        self._webhooks.emit("custody", "deposit.detected", self._deposit_event(deposit))
        return deposit

    def drop_deposit(self, deposit_id: str) -> ChainDeposit:
        """The transaction is dropped before it is final. It never credits anything."""
        deposit = self._deposits.get(deposit_id)
        if deposit is None:
            raise ApiError(404, "deposit_not_found", f"There is no deposit {deposit_id}.")
        if deposit.status != "detected":
            raise ApiError(
                409, "deposit_already_final", f"Deposit {deposit_id} is already {deposit.status}."
            )

        deposit.status = "failed"
        deposit.failure_reason = "dropped"
        self._unconfirmed.remove(deposit.id)
        self._webhooks.emit(
            "custody",
            "deposit.failed",
            {
                "deposit_id": deposit.id,
                "asset": deposit.asset,
                "amount": money.format_amount(deposit.amount, deposit.asset),
                "tx_hash": deposit.tx_hash,
                "reason": deposit.failure_reason,
            },
        )
        return deposit

    def mine(self, blocks: int) -> None:
        """Produce blocks now, on top of those the clock produces. They take effect at the
        next ``advance_chain``."""
        if blocks < 1:
            raise ValueError("a block cannot be unmined")
        self._mined += blocks

    # --- what time does ----------------------------------------------------------------------

    def advance_chain(self, now: datetime) -> None:
        """Bring the chain up to ``now`` and finalise what its new blocks make final."""
        height = (now - self._genesis) // self._block_time + self._mined
        if height == self.height:
            return
        self.height = height

        still_unconfirmed: list[str] = []
        for deposit_id in self._unconfirmed:
            deposit = self._deposits[deposit_id]
            if self.height - deposit.detected_height >= self._final_after:
                self._confirm(deposit, now)
            else:
                still_unconfirmed.append(deposit_id)
        self._unconfirmed = still_unconfirmed

        still_in_flight: list[str] = []
        for withdrawal_id in self._in_flight:
            withdrawal = self._withdrawals[withdrawal_id]
            if withdrawal.status == "pending" and self.height > withdrawal.created_height:
                self._broadcast(withdrawal)
            if (
                withdrawal.broadcast_height is not None
                and self.height - withdrawal.broadcast_height >= self._final_after
            ):
                self._complete(withdrawal, now)
            if withdrawal.status in ("pending", "broadcast"):
                still_in_flight.append(withdrawal_id)
        self._in_flight = still_in_flight

    def _confirm(self, deposit: ChainDeposit, now: datetime) -> None:
        deposit.status = "confirmed"
        deposit.confirmed_at = now
        self._account.post(
            Transaction(
                id=deposit.id,
                type="deposit",
                direction="credit",
                asset=deposit.asset,
                amount=deposit.amount,
                reference=None,
                related_id=deposit.address_id,
                occurred_at=now,
                tx_hash=deposit.tx_hash,
            )
        )
        self._webhooks.emit("custody", "deposit.confirmed", self._deposit_event(deposit))

    def _broadcast(self, withdrawal: Withdrawal) -> None:
        """The first block after a withdrawal was made either carries it or rejects it."""
        if withdrawal.to_address.startswith(_ADDRESS_PREFIX + REJECTED_BODY_PREFIX):
            # Nothing reached the chain, so there is no hash, no transaction and no fee.
            withdrawal.status = "failed"
            withdrawal.failure_reason = "rejected_by_network"
            self._webhooks.emit(
                "custody",
                "withdrawal.failed",
                {
                    "withdrawal_id": withdrawal.id,
                    "reference": withdrawal.reference,
                    "asset": withdrawal.asset,
                    "amount": money.format_amount(withdrawal.amount, withdrawal.asset),
                    "failure_reason": withdrawal.failure_reason,
                },
            )
            return
        withdrawal.status = "broadcast"
        withdrawal.tx_hash = self._ids.hex(64)
        withdrawal.broadcast_height = withdrawal.created_height + 1

    def _complete(self, withdrawal: Withdrawal, now: datetime) -> None:
        withdrawal.status = "completed"
        withdrawal.completed_at = now
        self._account.post(
            Transaction(
                id=withdrawal.id,
                type="withdrawal",
                direction="debit",
                asset=withdrawal.asset,
                amount=withdrawal.amount,
                reference=withdrawal.reference,
                related_id=withdrawal.to_address,
                occurred_at=now,
                tx_hash=withdrawal.tx_hash,
            )
        )
        self._account.post(
            Transaction(
                id=f"{withdrawal.id}:fee",
                type="network_fee",
                direction="debit",
                asset=withdrawal.asset,
                amount=withdrawal.network_fee,
                reference=withdrawal.reference,
                related_id=withdrawal.id,
                occurred_at=now,
                tx_hash=withdrawal.tx_hash,
            )
        )
        self._webhooks.emit(
            "custody",
            "withdrawal.completed",
            {
                "withdrawal_id": withdrawal.id,
                "reference": withdrawal.reference,
                "asset": withdrawal.asset,
                "amount": money.format_amount(withdrawal.amount, withdrawal.asset),
                "network_fee": money.format_amount(withdrawal.network_fee, withdrawal.asset),
                "tx_hash": withdrawal.tx_hash,
            },
        )

    # --- inspection and documents ------------------------------------------------------------

    def balances(self) -> dict[str, Decimal]:
        return {ASSET: self._account.balance}

    def deposits(self) -> list[ChainDeposit]:
        return list(self._deposits.values())

    def withdrawals(self) -> list[Withdrawal]:
        return list(self._withdrawals.values())

    def deposit_confirmations(self, deposit: ChainDeposit) -> int:
        # A dropped transaction is not on the chain, so it has none.
        return 0 if deposit.status == "failed" else self.height - deposit.detected_height

    def withdrawal_confirmations(self, withdrawal: Withdrawal) -> int:
        if withdrawal.broadcast_height is None:
            return 0
        return self.height - withdrawal.broadcast_height

    def deposit_document(self, deposit: ChainDeposit) -> dict[str, object]:
        return {
            "id": deposit.id,
            "address_id": deposit.address_id,
            "address": deposit.address,
            "customer_reference": deposit.customer_reference,
            "asset": deposit.asset,
            "amount": money.format_amount(deposit.amount, deposit.asset),
            "tx_hash": deposit.tx_hash,
            "from_address": deposit.from_address,
            "status": deposit.status,
            "confirmations": self.deposit_confirmations(deposit),
            "detected_at": format_time(deposit.detected_at),
            "confirmed_at": _optional_time(deposit.confirmed_at),
            "failure_reason": deposit.failure_reason,
        }

    def withdrawal_document(self, withdrawal: Withdrawal) -> dict[str, object]:
        return {
            "id": withdrawal.id,
            "status": withdrawal.status,
            "asset": withdrawal.asset,
            "amount": money.format_amount(withdrawal.amount, withdrawal.asset),
            "network_fee": money.format_amount(withdrawal.network_fee, withdrawal.asset),
            "to_address": withdrawal.to_address,
            "reference": withdrawal.reference,
            "tx_hash": withdrawal.tx_hash,
            "confirmations": self.withdrawal_confirmations(withdrawal),
            "created_at": format_time(withdrawal.created_at),
            "completed_at": _optional_time(withdrawal.completed_at),
            "failure_reason": withdrawal.failure_reason,
        }

    def withdrawal_inspection(self, withdrawal: Withdrawal) -> dict[str, object]:
        """A withdrawal as a test sees it: with its key, and its place on the chain."""
        return {
            **self.withdrawal_document(withdrawal),
            "idempotency_key": withdrawal.idempotency_key,
            "created_height": withdrawal.created_height,
            "broadcast_height": withdrawal.broadcast_height,
        }

    def _deposit_event(self, deposit: ChainDeposit) -> dict[str, object]:
        return {
            "deposit_id": deposit.id,
            "address_id": deposit.address_id,
            "address": deposit.address,
            "customer_reference": deposit.customer_reference,
            "asset": deposit.asset,
            "amount": money.format_amount(deposit.amount, deposit.asset),
            "tx_hash": deposit.tx_hash,
            "from_address": deposit.from_address,
            "confirmations": self.deposit_confirmations(deposit),
        }

    def _new_address(self) -> str:
        while True:
            address = address_for(self._ids.base32(_BODY_LENGTH))
            if address not in self._address_ids:
                return address


def address_document(address: Address) -> dict[str, object]:
    return {
        "id": address.id,
        "customer_reference": address.customer_reference,
        "asset": address.asset,
        "network": NETWORK,
        "address": address.address,
    }


def _check_asset(asset: str) -> None:
    if asset != ASSET:
        raise ApiError(422, "unsupported_asset", f"The custodian carries {ASSET} on {NETWORK}.")


def _invalid_address(message: str) -> ApiError:
    return ApiError(422, "invalid_address", message)


def _optional_time(moment: datetime | None) -> str | None:
    return format_time(moment) if moment is not None else None

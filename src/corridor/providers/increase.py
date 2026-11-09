"""The bank rail adapter for Increase's sandbox: USD over ACH.

Every fact about Increase used here is from its public documentation, and
``docs/adr/0020-increase-bank-rail-adapter.md`` cites each one. Where that documentation and
this module meet, the ADR has the mapping: which Increase object stands for which part of the
bank rail port, and how each answer is read.

The sandbox only. The settings refuse to load with any other address, the factory refuses to
build this adapter with one, and so does the adapter itself, because it is where the address
is used. Nothing reads the API key to decide which environment it is for.

Increase's answers are read as the shared client reads every provider's: a body that cannot be
believed is an unknown outcome, never a refusal. Its errors are RFC 9457 documents with a
``type``, and only the types that say the request was refused are read as refusals.
"""

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal

import httpx
from pydantic import AwareDatetime, Field, ValidationError

from corridor.platform.config import INCREASE_SANDBOX_URL, Settings
from corridor.platform.logging import get_logger
from corridor.providers.errors import (
    ProviderMisconfigured,
    ProviderOutcomeUnknown,
    ProviderRejected,
)
from corridor.providers.http import (
    Document,
    ErrorReading,
    ProviderClient,
    ResponseMismatch,
    in_utc,
    major_units,
    require_echo,
    searchable,
    segment,
    timestamp,
)
from corridor.providers.names import INCREASE
from corridor.providers.types import (
    BankEvent,
    Beneficiary,
    ListedEvent,
    Payout,
    PayoutStatus,
    ProviderStatement,
    ProviderTransaction,
    VirtualAccount,
)

log = get_logger(__name__)

_WHICH_ACCOUNT: Final = (
    "the key reaches {count} open USD accounts; set CORRIDOR_INCREASE_ACCOUNT_ID to the one to use"
)

# ACH moves US dollars and nothing else: Increase gives an ACH transfer's currency as USD.
USD: Final = "USD"
_RAIL: Final = "ach"
_BANK_NAME: Final = "Increase"

# What Corridor writes on the ACH transfers it sends. A fixed text: nothing about a user.
STATEMENT_DESCRIPTOR: Final = "Corridor withdrawal"

# Increase takes keys of up to 200 ASCII characters. A longer key Corridor derives is sent as
# its digest, which is the same for the same key and so still makes a retry find its object.
MAX_IDEMPOTENCY_KEY: Final = 200
_DIGEST_PREFIX: Final = "sha256:"

# The largest page Increase serves, and how many pages one statement may take. A statement
# that goes on past that is not read as a statement.
PAGE_LIMIT: Final = 100
MAX_STATEMENT_PAGES: Final = 100

# The event categories this adapter reads, by the type of object each names.
ACH_TRANSFER_EVENTS: Final = frozenset({"ach_transfer.created", "ach_transfer.updated"})
INBOUND_ACH_TRANSFER_EVENTS: Final = frozenset(
    {"inbound_ach_transfer.created", "inbound_ach_transfer.updated"}
)
EVENT_CATEGORIES: Final = ACH_TRANSFER_EVENTS | INBOUND_ACH_TRANSFER_EVENTS
# How many pages of events one listing reads. The caller asks again for the rest.
MAX_EVENT_PAGES: Final = 10

# Increase's error types, as the ADR reads them. A type that says the request was refused
# is a refusal, under a code a caller can branch on. One that says Corridor's credentials or
# address are wrong is a fault of configuration. Every other type, and rate limiting among
# them, is unknown: it does not say the request was refused, and a refusal releases funds.
_REFUSALS: Final[Mapping[str, str]] = {
    "invalid_parameters_error": "invalid_parameters",
    "malformed_request_error": "malformed_request",
    "object_not_found_error": "object_not_found",
    "invalid_operation_error": "invalid_operation",
    # The key belongs to another request. The saga reads this code as unknown, never as a
    # refusal of the payout: the other request may be this payout, sent before.
    "idempotency_key_already_used_error": "idempotency_conflict",
}
_MISCONFIGURATIONS: Final = frozenset(
    {
        "invalid_api_key_error",
        "insufficient_permissions_error",
        "private_feature_error",
        "environment_mismatch_error",
        "api_method_not_found_error",
    }
)

# An ACH transfer's status, as the port's three. ``submitted`` is pending until the
# transfer's settlement is recorded: Increase keeps the status and fills ``settlement`` in.
_PENDING: Final = frozenset(
    {
        "pending_approval",
        "pending_transfer_session_confirmation",
        "pending_submission",
        "pending_reviewing",
        "requires_attention",
    }
)
_FAILED: Final = frozenset({"canceled", "rejected", "returned"})

ACHTransferStatus = Literal[
    "pending_approval",
    "pending_transfer_session_confirmation",
    "canceled",
    "pending_submission",
    "pending_reviewing",
    "requires_attention",
    "rejected",
    "submitted",
    "returned",
]

# The statement's words for each kind of line, which reconciliation matches on.
_DEPOSIT: Final = "deposit"
_DEPOSIT_RETURN: Final = "deposit_return"
_PAYOUT: Final = "payout"
_PAYOUT_RETURN: Final = "payout_return"
# The transaction sources that take an ACH transfer's money back: both offset its intention.
_OFFSETS: Final = ("ach_transfer_return", "ach_transfer_rejection")


# --- errors -------------------------------------------------------------------------------------


class _ErrorDocument(Document):
    status: int
    type: str = Field(min_length=1)
    title: str
    detail: str | None = None


def read_error(status: int, content: bytes) -> ErrorReading:
    """An Increase error, as a refusal, a fault of configuration, or unknown.

    Only a body that is Increase's own document, whose ``status`` is the response's, is read
    at all: anything else came from something in between. The title is kept as the message;
    the detail is not, since it can repeat what was sent.
    """
    try:
        error = _ErrorDocument.model_validate_json(content)
    except ValidationError:
        return ErrorReading("unknown", "", f"a {status} without Increase's error document")
    if error.status != status:
        return ErrorReading("unknown", "", f"a {status} whose document says {error.status}")
    code = _REFUSALS.get(error.type)
    if code is not None:
        return ErrorReading("rejected", code, error.title)
    if error.type in _MISCONFIGURATIONS:
        return ErrorReading("misconfigured", "", f"Increase answered {error.type}")
    return ErrorReading("unknown", "", f"Increase answered {status} {error.type}")


# --- what Increase answers ----------------------------------------------------------------------


class _AccountNumberDocument(Document):
    id: str = Field(min_length=1)
    type: Literal["account_number"]
    account_id: str
    account_number: str = Field(min_length=1)
    routing_number: str = Field(min_length=1)
    name: str
    status: str
    inbound_ach_debits: str


class _ExternalAccountDocument(Document):
    id: str = Field(min_length=1)
    type: Literal["external_account"]
    account_number: str
    routing_number: str
    description: str
    status: str


class _Settlement(Document):
    settled_at: AwareDatetime


class _Return(Document):
    return_reason_code: str


class _ACHTransferDocument(Document):
    id: str = Field(min_length=1)
    type: Literal["ach_transfer"]
    account_id: str
    amount: int
    currency: Literal["USD"]
    external_account_id: str | None
    idempotency_key: str | None
    status: ACHTransferStatus
    settlement: _Settlement | None = None
    returned: _Return | None = Field(default=None, alias="return")
    created_at: AwareDatetime


class _ACHTransfersDocument(Document):
    data: list[_ACHTransferDocument]
    next_cursor: str | None


class _TransferReturn(Document):
    reason: str


class _InboundACHTransferDocument(Document):
    id: str = Field(min_length=1)
    type: Literal["inbound_ach_transfer"]
    account_id: str
    account_number_id: str = Field(min_length=1)
    amount: int
    direction: Literal["credit", "debit"]
    status: Literal["pending", "declined", "accepted", "returned"]
    originator_company_name: str
    originator_company_entry_description: str
    transfer_return: _TransferReturn | None = None


class _TransferSource(Document):
    transfer_id: str = Field(min_length=1)


class _InboundReturnSource(Document):
    inbound_ach_transfer_id: str = Field(min_length=1)


class _Source(Document):
    category: str = Field(min_length=1)
    ach_transfer_intention: _TransferSource | None = None
    ach_transfer_rejection: _TransferSource | None = None
    ach_transfer_return: _TransferSource | None = None
    inbound_ach_transfer: _TransferSource | None = None
    inbound_ach_transfer_return_intention: _InboundReturnSource | None = None


class _TransactionDocument(Document):
    id: str = Field(min_length=1)
    type: Literal["transaction"]
    account_id: str
    amount: int
    currency: str
    created_at: AwareDatetime
    route_id: str | None = None
    source: _Source


class _TransactionsDocument(Document):
    data: list[_TransactionDocument]
    next_cursor: str | None


_EVENT_IDENTIFIER: Final = r"^[A-Za-z0-9_.-]{1,255}$"


class _EventDocument(Document):
    id: str = Field(pattern=_EVENT_IDENTIFIER)
    category: str = Field(pattern=_EVENT_IDENTIFIER)
    associated_object_type: str = Field(pattern=_EVENT_IDENTIFIER)
    associated_object_id: str = Field(pattern=_EVENT_IDENTIFIER)
    created_at: AwareDatetime
    type: Literal["event"]


class _EventsDocument(Document):
    data: list[_EventDocument]
    next_cursor: str | None


class _AccountDocument(Document):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_]{1,200}$")
    type: Literal["account"]
    status: Literal["open", "closed"]
    currency: str


class _AccountsDocument(Document):
    data: list[_AccountDocument]
    next_cursor: str | None


class _BalanceDocument(Document):
    type: Literal["balance_lookup"]
    account_id: str
    current_balance: int


# --- reading them in Corridor's terms -----------------------------------------------------------


def _status(document: _ACHTransferDocument) -> tuple[PayoutStatus, str | None]:
    """The port's status for an ACH transfer, and why it failed if it did."""
    if document.status == "submitted":
        return ("completed" if document.settlement is not None else "pending"), None
    if document.status in _PENDING:
        return "pending", None
    if document.status == "returned":
        reason = document.returned.return_reason_code if document.returned else "returned"
        return "failed", reason
    if document.status in _FAILED:
        return "failed", document.status
    # The literal admits nothing else; this is for the type checker and for a future edit.
    raise ResponseMismatch("'status' is not one this adapter reads")


def _payout(document: _ACHTransferDocument) -> Payout:
    """An ACH transfer Corridor sent, as a payout. One it cannot have sent is refused.

    Corridor sends credits, under a key, to an external account. The key is the reference
    the payout was sent with, which Increase keeps and Corridor finds it by.
    """
    if document.amount <= 0:
        raise ResponseMismatch("'amount' is not a credit Corridor sent")
    if document.idempotency_key is None:
        raise ResponseMismatch("'idempotency_key' is missing")
    if document.external_account_id is None:
        raise ResponseMismatch("'external_account_id' is missing")
    status, reason = _status(document)
    settled_at = (
        in_utc(document.settlement.settled_at)
        if status == "completed" and document.settlement is not None
        else None
    )
    return Payout(
        id=document.id,
        status=status,
        beneficiary_id=document.external_account_id,
        asset_code=USD,
        amount=document.amount,
        # Increase does not charge a fee on the transfer itself.
        fee=0,
        reference=document.idempotency_key,
        created_at=in_utc(document.created_at),
        settled_at=settled_at,
        failure_reason=reason,
    )


def mask(account_number: str) -> str:
    """Four bullets and the end of an account number: never more than half of it."""
    shown = min(4, len(account_number) // 2)
    return "••••" + (account_number[-shown:] if shown else "")


def idempotency_key_for(key: str) -> str:
    """A key as Increase takes it: as it is when it fits, and its digest when it does not.

    Corridor's keys are visible ASCII; one that is not is a broken contract, not something
    to send.
    """
    if not key or not key.isascii() or not key.isprintable() or key.strip() != key:
        raise ValueError("an idempotency key is visible ASCII")
    if len(key) <= MAX_IDEMPOTENCY_KEY:
        return key
    return _DIGEST_PREFIX + hashlib.sha256(key.encode()).hexdigest()


def _is_digits(text: str | None, low: int, high: int) -> bool:
    return text is not None and text.isascii() and text.isdigit() and low <= len(text) <= high


class IncreaseBank:
    """Implements ``BankRail`` and ``BankEventSource`` against Increase's sandbox."""

    name: Final = INCREASE

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        # At the point of use, as well as where the settings load and where the factory
        # builds: whatever made this settings object, this adapter calls only the sandbox.
        if settings.increase_base_url != INCREASE_SANDBOX_URL:
            raise self._misconfigured("increase_base_url is not the Increase sandbox address")
        # The account money moves from. Set, it is used as it is; unset, it is the one open
        # account the key reaches, found on first use (``_ready``).
        self._account_id = settings.increase_account_id or ""
        self._finding = asyncio.Lock()
        self._http = ProviderClient(
            provider=self.name,
            base_url=settings.increase_base_url,
            api_key=settings.increase_api_key,
            timeout_seconds=settings.provider_timeout_seconds,
            client=client,
            errors=read_error,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- the bank rail port ----------------------------------------------------------------

    async def create_virtual_account(
        self, *, customer_reference: str, asset_code: str, idempotency_key: str
    ) -> VirtualAccount:
        """An Account Number on Corridor's account, named for the customer.

        It refuses ACH debits: a deposit account is for money coming in, and a debit would
        let whoever has its number take money out.
        """
        operation = "create_virtual_account"
        if asset_code != USD:
            raise self._unsupported(operation)
        await self._ready()

        def convert(document: _AccountNumberDocument) -> VirtualAccount:
            require_echo("account_id", self._account_id, document.account_id)
            require_echo("name", customer_reference, document.name)
            require_echo("status", "active", document.status)
            require_echo("inbound_ach_debits", "blocked", document.inbound_ach_debits)
            return VirtualAccount(
                id=document.id,
                customer_reference=customer_reference,
                asset_code=USD,
                rail=_RAIL,
                bank_name=_BANK_NAME,
                account_number=document.account_number,
                routing_number=document.routing_number,
            )

        return await self._http.post(
            operation,
            "/account_numbers",
            _AccountNumberDocument,
            convert,
            body={
                "account_id": self._account_id,
                "name": customer_reference,
                "inbound_ach_debits": "blocked",
            },
            idempotency_key=idempotency_key_for(idempotency_key),
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
        """An External Account, described by its holder's name.

        The numbers are checked against the lengths Increase documents before anything is
        sent, and a refusal of the parameters is the account's: the numbers and the name are
        all that the request carries of the user's.
        """
        operation = "create_beneficiary"
        if asset_code != USD:
            raise self._unsupported(operation)
        if not _is_digits(account_number, 1, 17) or not _is_digits(routing_number, 9, 9):
            raise self._rejected(operation, "invalid_account", 422)

        def convert(document: _ExternalAccountDocument) -> Beneficiary:
            require_echo("account_number", account_number, document.account_number)
            require_echo("routing_number", routing_number, document.routing_number)
            require_echo("description", holder_name, document.description)
            require_echo("status", "active", document.status)
            return Beneficiary(
                id=document.id,
                asset_code=USD,
                rail=_RAIL,
                holder_name=document.description,
                account_mask=mask(document.account_number),
            )

        try:
            return await self._http.post(
                operation,
                "/external_accounts",
                _ExternalAccountDocument,
                convert,
                body={
                    "account_number": account_number,
                    "routing_number": routing_number,
                    "description": holder_name,
                    "funding": "checking",
                },
                idempotency_key=idempotency_key_for(idempotency_key),
            )
        except ProviderRejected as refusal:
            if refusal.code == "invalid_parameters":
                raise self._rejected(operation, "invalid_account", refusal.status) from None
            raise

    async def create_payout(
        self,
        *,
        beneficiary_id: str,
        asset_code: str,
        amount: int,
        reference: str,
        idempotency_key: str,
    ) -> Payout:
        """An ACH credit from Corridor's account to the external account.

        Increase keeps a transfer's idempotency key and finds a transfer by it; it has no
        other field for Corridor's reference. So the reference is the key, and a call that
        asks for two different ones is refused before anything is sent.
        """
        operation = "create_payout"
        if reference != idempotency_key:
            raise ValueError("an Increase payout's reference is its idempotency key")
        if len(reference) > MAX_IDEMPOTENCY_KEY:
            raise ValueError("an Increase payout's reference fits an idempotency key")
        if asset_code != USD:
            raise self._unsupported(operation)
        # Refuses anything but a positive int. A dollar's minor unit is Increase's cent.
        major_units(amount, USD)
        await self._ready()

        def convert(document: _ACHTransferDocument) -> Payout:
            require_echo("account_id", self._account_id, document.account_id)
            require_echo("external_account_id", beneficiary_id, document.external_account_id)
            require_echo("idempotency_key", reference, document.idempotency_key)
            payout = _payout(document)
            require_echo("amount", amount, payout.amount)
            return payout

        return await self._http.post(
            operation,
            "/ach_transfers",
            _ACHTransferDocument,
            convert,
            body={
                "account_id": self._account_id,
                "amount": amount,
                "external_account_id": beneficiary_id,
                "statement_descriptor": STATEMENT_DESCRIPTOR,
                "require_approval": False,
            },
            idempotency_key=idempotency_key_for(idempotency_key),
        )

    async def get_payout(self, payout_id: str) -> Payout:
        await self._ready()

        def convert(document: _ACHTransferDocument) -> Payout:
            require_echo("id", payout_id, document.id)
            require_echo("account_id", self._account_id, document.account_id)
            return _payout(document)

        return await self._http.get(
            "get_payout", f"/ach_transfers/{segment(payout_id)}", _ACHTransferDocument, convert
        )

    async def find_payouts(self, reference: str) -> tuple[Payout, ...]:
        """The transfers under the key the reference is. Increase documents that a used key
        lists exactly one; more than a page is not believed."""
        await self._ready()

        def convert(document: _ACHTransfersDocument) -> tuple[Payout, ...]:
            if document.next_cursor is not None:
                raise ResponseMismatch("'next_cursor' says one key has more than a page")
            for transfer in document.data:
                require_echo("data.idempotency_key", reference, transfer.idempotency_key)
                require_echo("data.account_id", self._account_id, transfer.account_id)
            return tuple(_payout(transfer) for transfer in document.data)

        return await self._http.get(
            "find_payouts",
            "/ach_transfers",
            _ACHTransfersDocument,
            convert,
            params={"account_id": self._account_id, "idempotency_key": searchable(reference)},
        )

    async def list_transactions(
        self, *, asset_code: str, start: datetime, end: datetime
    ) -> ProviderStatement:
        """Corridor's account at Increase over ``[start, end)``, and its balance at ``end``.

        Increase holds dollars only, so its statement of any other asset is empty and its
        balance nothing. Each line is named in the statement's words, so that reconciliation
        reads it as it reads the simulator's (the ADR has the table). An ACH transfer whose
        money came back in the same window is left out with its return: as far as Corridor's
        books go, it never left.
        """
        if start > end:
            raise ValueError("a statement window ends after it starts")
        if asset_code != USD:
            return ProviderStatement(asset_code, in_utc(start), in_utc(end), (), 0)
        await self._ready()
        transactions = await self._transactions(start, end)
        references = await self._references(transactions)
        balance = await self._http.get(
            "list_transactions",
            f"/accounts/{segment(self._account_id)}/balance",
            _BalanceDocument,
            self._balance,
            params={"at_time": timestamp(end)},
        )
        return ProviderStatement(
            asset_code=USD,
            start=in_utc(start),
            end=in_utc(end),
            transactions=_lines(transactions, references),
            closing_balance=balance,
        )

    # --- the event source ------------------------------------------------------------------

    async def resolve_event(
        self, *, category: str, object_type: str, object_id: str
    ) -> tuple[BankEvent, ...]:
        """What one Increase event means, read from the object it names as it is now.

        An event says only that an object changed. Reading the object gives its present
        state, so an event that is repeated, late or out of order comes to the same thing as
        the latest one, and the payment functions, which are idempotent, do nothing twice.
        """
        if category in EVENT_CATEGORIES:
            await self._ready()
        if category in ACH_TRANSFER_EVENTS:
            self._require_type(object_type, "ach_transfer")

            def payout_events(document: _ACHTransferDocument) -> tuple[BankEvent, ...]:
                # Inside the read, so that a transfer that cannot be believed is unknown.
                return _payout_events(self._own(document, object_id))

            return await self._http.get(
                "resolve_event",
                f"/ach_transfers/{segment(object_id)}",
                _ACHTransferDocument,
                payout_events,
            )
        if category in INBOUND_ACH_TRANSFER_EVENTS:
            self._require_type(object_type, "inbound_ach_transfer")

            def deposit_events(document: _InboundACHTransferDocument) -> tuple[BankEvent, ...]:
                return _deposit_events(self._own(document, object_id))

            return await self._http.get(
                "resolve_event",
                f"/inbound_ach_transfers/{segment(object_id)}",
                _InboundACHTransferDocument,
                deposit_events,
            )
        return ()

    async def list_events(self, *, since: datetime) -> tuple[ListedEvent, ...]:
        """The events of the categories this adapter reads, oldest first, from ``since``.

        Increase keeps its events listable for 30 days. A category that was not asked for is
        passed over, whatever the listing says.
        """
        params = {
            "category.in": ",".join(sorted(EVENT_CATEGORIES)),
            "created_at.on_or_after": timestamp(since),
            "order_by.field": "created_at",
            "order_by.direction": "ascending",
            "limit": str(PAGE_LIMIT),
        }
        found: list[ListedEvent] = []
        cursor: str | None = None
        for _page in range(MAX_EVENT_PAGES):
            page = await self._http.get(
                "list_events",
                "/events",
                _EventsDocument,
                lambda document: document,
                params=params if cursor is None else {**params, "cursor": cursor},
            )
            found.extend(
                _listed(event) for event in page.data if event.category in EVENT_CATEGORIES
            )
            cursor = page.next_cursor
            if cursor is None:
                break
        return tuple(found)

    # --- helpers ---------------------------------------------------------------------------

    async def _ready(self) -> None:
        """Find the account, once, if the settings did not name it.

        Only an account that is unmistakably the one is used: a key that reaches no open
        account, or more than one, is a configuration that has to say which, and nothing is
        sent until it does.
        """
        if self._account_id:
            return
        async with self._finding:
            # Another call may have found it while this one waited for the lock.
            await self._find_account()

    async def _find_account(self) -> None:
        if not self._account_id:

            def the_one(document: _AccountsDocument) -> str:
                if document.next_cursor is not None:
                    log.warning("increase.account_not_chosen", open_accounts="more than a page")
                    raise self._misconfigured(_WHICH_ACCOUNT.format(count="more than a page of"))
                open_accounts = [
                    account
                    for account in document.data
                    if account.status == "open" and account.currency == USD
                ]
                if len(open_accounts) != 1:
                    # The count, so that the log says why: a key that reaches no account is
                    # a new sandbox, and one that reaches several needs to be told which.
                    log.warning("increase.account_not_chosen", open_accounts=len(open_accounts))
                    raise self._misconfigured(_WHICH_ACCOUNT.format(count=len(open_accounts)))
                return open_accounts[0].id

            self._account_id = await self._http.get(
                "find_account",
                "/accounts",
                _AccountsDocument,
                the_one,
                params={"status.in": "open", "limit": str(PAGE_LIMIT)},
            )
            log.info("increase.account_found")

    def _own[D: _ACHTransferDocument | _InboundACHTransferDocument](
        self, document: D, object_id: str
    ) -> D:
        require_echo("id", object_id, document.id)
        require_echo("account_id", self._account_id, document.account_id)
        return document

    def _require_type(self, object_type: str, expected: str) -> None:
        if object_type != expected:
            raise ProviderOutcomeUnknown(
                "the event names an object of another type than its category",
                provider=self.name,
                operation="resolve_event",
            )

    def _balance(self, document: _BalanceDocument) -> int:
        require_echo("account_id", self._account_id, document.account_id)
        return document.current_balance

    async def _transactions(self, start: datetime, end: datetime) -> list[_TransactionDocument]:
        params = {
            "account_id": self._account_id,
            "created_at.on_or_after": timestamp(start),
            "created_at.before": timestamp(end),
            "limit": str(PAGE_LIMIT),
        }
        found: list[_TransactionDocument] = []
        cursor: str | None = None
        for _page in range(MAX_STATEMENT_PAGES):
            page = await self._http.get(
                "list_transactions",
                "/transactions",
                _TransactionsDocument,
                lambda document: document,
                params=params if cursor is None else {**params, "cursor": cursor},
            )
            for line in page.data:
                if line.account_id != self._account_id or line.currency != USD:
                    raise self._unbelievable("a transaction of another account or currency")
                if not start <= line.created_at < end:
                    raise self._unbelievable("a transaction outside the window")
            found.extend(page.data)
            cursor = page.next_cursor
            if cursor is None:
                return found
        raise self._unbelievable("a statement longer than this adapter reads")

    async def _references(
        self, transactions: list[_TransactionDocument]
    ) -> Mapping[str, _ACHTransferDocument]:
        """Each ACH transfer a line pays out, read once: its key is Corridor's reference,
        which the transaction does not carry."""
        found: dict[str, _ACHTransferDocument] = {}
        for line in transactions:
            intention = line.source.ach_transfer_intention
            if intention is None or intention.transfer_id in found:
                continue
            found[intention.transfer_id] = await self._transfer(
                "list_transactions", intention.transfer_id
            )
        return found

    async def _transfer(self, operation: str, transfer_id: str) -> _ACHTransferDocument:
        def convert(document: _ACHTransferDocument) -> _ACHTransferDocument:
            return self._own(document, transfer_id)

        return await self._http.get(
            operation, f"/ach_transfers/{segment(transfer_id)}", _ACHTransferDocument, convert
        )

    def _rejected(self, operation: str, code: str, status: int) -> ProviderRejected:
        return ProviderRejected(
            code, "refused before it was sent", status, provider=self.name, operation=operation
        )

    def _unsupported(self, operation: str) -> ProviderRejected:
        # Nothing is sent: Increase's ACH moves dollars only.
        return self._rejected(operation, "unsupported_asset", 422)

    def _unbelievable(self, detail: str) -> ProviderOutcomeUnknown:
        return ProviderOutcomeUnknown(detail, provider=self.name, operation="list_transactions")

    def _misconfigured(self, detail: str) -> ProviderMisconfigured:
        return ProviderMisconfigured(detail, provider=self.name, operation="build")


def _listed(event: _EventDocument) -> ListedEvent:
    """A listed event, with the payload a webhook would have carried for it."""
    created_at = in_utc(event.created_at)
    return ListedEvent(
        id=event.id,
        category=event.category,
        created_at=created_at,
        payload={
            "id": event.id,
            "created_at": timestamp(created_at),
            "category": event.category,
            "associated_object_type": event.associated_object_type,
            "associated_object_id": event.associated_object_id,
            "type": "event",
        },
    )


def _payout_events(transfer: _ACHTransferDocument) -> tuple[BankEvent, ...]:
    """An ACH transfer's present state as the contract's payout events: one when it has
    settled or failed, none while it is on its way."""
    if transfer.amount <= 0 or transfer.idempotency_key is None:
        # Not one Corridor sent: someone moved money from the account by other means.
        log.warning("increase.foreign_transfer", transfer_id=transfer.id)
        return ()
    payout = _payout(transfer)
    common = {
        "payout_id": payout.id,
        "reference": payout.reference,
        "asset": USD,
        "amount": major_units(payout.amount, USD),
    }
    if payout.status == "completed" and payout.settled_at is not None:
        return (
            BankEvent(
                "payout.completed",
                {**common, "fee": "0.00", "settled_at": timestamp(payout.settled_at)},
            ),
        )
    if payout.status == "failed":
        return (
            BankEvent("payout.failed", {**common, "failure_reason": payout.failure_reason or ""}),
        )
    return ()


def _deposit_events(inbound: _InboundACHTransferDocument) -> tuple[BankEvent, ...]:
    """An inbound ACH transfer's present state as the contract's deposit events.

    Accepted is received. Returned is returned, whether or not it was received first: a
    return that arrives before its deposit is recorded so, and credits nothing. Pending and
    declined have moved nothing.
    """
    if inbound.direction != "credit" or inbound.amount <= 0:
        # The account numbers refuse debits. One that was taken anyway is money gone that no
        # deposit stands for, and reconciliation shows it in the balance.
        log.error("increase.inbound_debit", inbound_ach_transfer_id=inbound.id)
        return ()
    amount = major_units(inbound.amount, USD)
    if inbound.status == "accepted":
        return (
            BankEvent(
                "deposit.received",
                {
                    "deposit_id": inbound.id,
                    "virtual_account_id": inbound.account_number_id,
                    "asset": USD,
                    "amount": amount,
                    "sender_name": inbound.originator_company_name,
                    "reference": inbound.originator_company_entry_description,
                },
            ),
        )
    if inbound.status == "returned":
        reason = inbound.transfer_return.reason if inbound.transfer_return else "returned"
        return (
            BankEvent(
                "deposit.returned",
                {"deposit_id": inbound.id, "asset": USD, "amount": amount, "reason": reason},
            ),
        )
    return ()


def _lines(
    transactions: list[_TransactionDocument], references: Mapping[str, _ACHTransferDocument]
) -> tuple[ProviderTransaction, ...]:
    """Increase's transactions as statement lines (the ADR has the table)."""
    intended = {
        line.source.ach_transfer_intention.transfer_id
        for line in transactions
        if line.source.ach_transfer_intention is not None
    }
    offset: set[str] = set()
    for line in transactions:
        for name in _OFFSETS:
            offsetting: _TransferSource | None = getattr(line.source, name)
            if offsetting is not None and offsetting.transfer_id in intended:
                offset.add(offsetting.transfer_id)

    lines: list[ProviderTransaction] = []
    for line in transactions:
        if line.amount == 0:
            # Nothing moved.
            continue
        source = line.source
        kind, identifier = source.category, line.id
        related: str | None = line.route_id
        reference: str | None = None
        if source.inbound_ach_transfer is not None:
            kind, identifier = _DEPOSIT, source.inbound_ach_transfer.transfer_id
        elif source.inbound_ach_transfer_return_intention is not None:
            inbound_id = source.inbound_ach_transfer_return_intention.inbound_ach_transfer_id
            kind, identifier, related = _DEPOSIT_RETURN, f"{inbound_id}:return", inbound_id
        elif source.ach_transfer_intention is not None:
            transfer_id = source.ach_transfer_intention.transfer_id
            if transfer_id in offset:
                continue
            transfer = references[transfer_id]
            kind, identifier = _PAYOUT, transfer_id
            related, reference = transfer.external_account_id, transfer.idempotency_key
        else:
            returned = source.ach_transfer_return or source.ach_transfer_rejection
            if returned is not None:
                if returned.transfer_id in offset:
                    continue
                kind, related = _PAYOUT_RETURN, returned.transfer_id
                identifier = f"{returned.transfer_id}:{source.category}"
        lines.append(
            ProviderTransaction(
                id=identifier,
                type=kind,
                direction="credit" if line.amount > 0 else "debit",
                asset_code=USD,
                amount=abs(line.amount),
                reference=reference,
                related_id=related,
                occurred_at=in_utc(line.created_at),
                tx_hash=None,
            )
        )
    return tuple(lines)

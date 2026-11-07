"""The Increase adapter, against hand-written answers shaped from Increase's documentation.

Nothing here reaches Increase. Each answer is a document of the shape the API reference gives
the object (``docs/adr/0020-increase-bank-rail-adapter.md`` cites each one), written out by
hand with synthetic identifiers. What the sandbox really answers is for the manual sandbox
run to show; what is tested here is how the adapter reads each shape and each error.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from corridor.platform.config import INCREASE_SANDBOX_URL, Settings
from corridor.providers import (
    INCREASE,
    BankEvent,
    BankEventSource,
    IncreaseBank,
    ProviderMisconfigured,
    ProviderOutcomeUnknown,
    ProviderRejected,
)
from corridor.providers.increase import (
    MAX_STATEMENT_PAGES,
    STATEMENT_DESCRIPTOR,
    idempotency_key_for,
    mask,
)

# Made up: the shapes of the identifiers in Increase's examples, and nobody's.
KEY = SecretStr("placeholder-increase-key-" + "0" * 16)  # pragma: allowlist secret
ACCOUNT = "account_in71c4amph0vgo2qllky"
OTHER_ACCOUNT = "account_0000000000000000000"
ACCOUNT_NUMBER_ID = "account_number_v18nkfqm6afpsrvy82b2"
EXTERNAL_ACCOUNT = "external_account_ukk55lr923a3ac0pp7iv"
TRANSFER = "ach_transfer_uoxatyh3lt5evrsdvo7q"
INBOUND = "inbound_ach_transfer_tdrwqr3fq9gnnq49odev"
CUSTOMER = "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10"
REFERENCE = "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f"
ROUTING = "101050001"
NUMBER = "987654321"
CREATED = "2026-01-15T12:00:00Z"
SETTLED = "2026-01-16T15:30:00Z"
START = datetime(2026, 1, 15, tzinfo=UTC)
END = datetime(2026, 1, 16, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "bank_rail_provider": "increase_sandbox",
        "increase_base_url": INCREASE_SANDBOX_URL,
        "increase_api_key": KEY,
        "increase_account_id": ACCOUNT,
        "provider_timeout_seconds": 5.0,
        **overrides,
    }
    return Settings.model_construct(**values)  # type: ignore[arg-type]


# --- Increase, as its documentation shows it ---------------------------------------------------


def account_number(**changes: object) -> dict[str, Any]:
    return {
        "id": ACCOUNT_NUMBER_ID,
        "account_id": ACCOUNT,
        "account_number": "1234567890",
        "routing_number": ROUTING,
        "name": CUSTOMER,
        "status": "active",
        "inbound_ach_debits": "blocked",
        "unregistered_inbound_checks": "blocked",
        "idempotency_key": None,
        "created_at": CREATED,
        "type": "account_number",
        **changes,
    }


def external_account(**changes: object) -> dict[str, Any]:
    return {
        "id": EXTERNAL_ACCOUNT,
        "account_number": NUMBER,
        "routing_number": ROUTING,
        "description": "Maria Silva",
        "account_holder": "unknown",
        "funding": "checking",
        "status": "active",
        "idempotency_key": None,
        "created_at": CREATED,
        "type": "external_account",
        **changes,
    }


def ach_transfer(**changes: object) -> dict[str, Any]:
    return {
        "id": TRANSFER,
        "account_id": ACCOUNT,
        "account_number": NUMBER,
        "routing_number": ROUTING,
        "amount": 100_00,
        "currency": "USD",
        "external_account_id": EXTERNAL_ACCOUNT,
        "idempotency_key": REFERENCE,
        "status": "pending_submission",
        "statement_descriptor": STATEMENT_DESCRIPTOR,
        "settlement": None,
        "return": None,
        "submission": None,
        "acknowledgement": None,
        "notifications_of_change": [],
        "created_at": CREATED,
        "type": "ach_transfer",
        **changes,
    }


def inbound(**changes: object) -> dict[str, Any]:
    return {
        "id": INBOUND,
        "account_id": ACCOUNT,
        "account_number_id": ACCOUNT_NUMBER_ID,
        "amount": 250_00,
        "direction": "credit",
        "status": "accepted",
        "originator_company_name": "BIG BANK",
        "originator_company_entry_description": "PAYROLL",
        "originator_company_id": "0987654321",
        "originator_routing_number": ROUTING,
        "trace_number": "021000038461022",
        "acceptance": {"accepted_at": CREATED, "transaction_id": "transaction_1"},
        "decline": None,
        "transfer_return": None,
        "created_at": CREATED,
        "type": "inbound_ach_transfer",
        **changes,
    }


def transaction(
    identifier: str, amount: int, category: str, detail: dict[str, Any], **changes: object
) -> dict[str, Any]:
    return {
        "id": identifier,
        "account_id": ACCOUNT,
        "amount": amount,
        "currency": "USD",
        "created_at": "2026-01-15T09:30:00Z",
        "description": "synthetic",
        "route_id": ACCOUNT_NUMBER_ID,
        "route_type": "account_number",
        "source": {"category": category, category: detail},
        "type": "transaction",
        **changes,
    }


def error(status: int, kind: str) -> httpx.Response:
    # RFC 9457, as Increase's API overview shows its errors.
    body = {"status": status, "type": kind, "title": "A title.", "detail": "A detail."}
    return httpx.Response(status, json=body)


Route = Callable[[httpx.Request], httpx.Response]


@dataclass
class FakeIncrease:
    """Answers by method and path, and keeps every request it was sent."""

    routes: dict[tuple[str, str], Route | list[Route]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def on(self, method: str, path: str, *answers: httpx.Response | Route) -> None:
        handlers = [
            answer if callable(answer) else (lambda _request, given=answer: given)
            for answer in answers
        ]
        self.routes[(method, path)] = handlers if len(handlers) > 1 else handlers[0]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return error(404, "api_method_not_found_error")
        if isinstance(route, list):
            return route.pop(0)(request)
        return route(request)

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)


@pytest.fixture
def increase() -> FakeIncrease:
    return FakeIncrease()


@asynccontextmanager
async def adapter(fake: FakeIncrease, **overrides: object) -> AsyncIterator[IncreaseBank]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    bank = IncreaseBank(settings(**overrides), client=client)
    try:
        yield bank
    finally:
        await bank.aclose()
        await client.aclose()


def ok(document: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=document)


# --- building ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address", ["https://api.increase.com", "https://sandbox.increase.com/", "http://localhost"]
)
def test_the_adapter_itself_refuses_any_address_but_the_sandbox(address: str) -> None:
    with pytest.raises(ProviderMisconfigured) as failure:
        IncreaseBank(settings(increase_base_url=address))

    assert "not the Increase sandbox address" in str(failure.value)


def test_the_adapter_refuses_to_be_built_without_an_account() -> None:
    with pytest.raises(ProviderMisconfigured):
        IncreaseBank(settings(increase_account_id=None))


async def test_it_is_a_bank_rail_whose_events_are_read_from_their_objects(
    increase: FakeIncrease,
) -> None:
    async with adapter(increase) as bank:
        assert bank.name == INCREASE
        assert isinstance(bank, BankEventSource)


async def test_every_request_carries_the_key_as_a_bearer_and_goes_to_the_sandbox(
    increase: FakeIncrease,
) -> None:
    increase.on("GET", f"/ach_transfers/{TRANSFER}", ok(ach_transfer()))

    async with adapter(increase) as bank:
        await bank.get_payout(TRANSFER)

    (request,) = increase.requests
    assert str(request.url).startswith(INCREASE_SANDBOX_URL + "/")
    assert request.headers["authorization"] == f"Bearer {KEY.get_secret_value()}"


# --- errors --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind", "code"),
    [
        (400, "invalid_parameters_error", "invalid_parameters"),
        (400, "malformed_request_error", "malformed_request"),
        (404, "object_not_found_error", "object_not_found"),
        (409, "invalid_operation_error", "invalid_operation"),
        (409, "idempotency_key_already_used_error", "idempotency_conflict"),
    ],
)
async def test_an_error_that_says_the_request_was_refused_is_a_refusal(
    increase: FakeIncrease, status: int, kind: str, code: str
) -> None:
    increase.on("GET", f"/ach_transfers/{TRANSFER}", error(status, kind))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderRejected) as refusal:
            await bank.get_payout(TRANSFER)

    assert (refusal.value.code, refusal.value.status) == (code, status)
    # The detail can repeat what was sent; only the title is kept.
    assert refusal.value.message == "A title."


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (403, "insufficient_permissions_error"),
        (403, "private_feature_error"),
        (403, "environment_mismatch_error"),
        (404, "api_method_not_found_error"),
    ],
)
async def test_an_error_that_says_corridor_is_set_up_wrong_is_a_misconfiguration(
    increase: FakeIncrease, status: int, kind: str
) -> None:
    increase.on("GET", f"/ach_transfers/{TRANSFER}", error(status, kind))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderMisconfigured):
            await bank.get_payout(TRANSFER)


async def test_a_401_is_a_misconfiguration_whatever_its_body(increase: FakeIncrease) -> None:
    increase.on("GET", f"/ach_transfers/{TRANSFER}", error(401, "invalid_api_key_error"))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderMisconfigured):
            await bank.get_payout(TRANSFER)


@pytest.mark.parametrize(
    "answer",
    [
        # Rate limited: nothing was done, and nothing says the payout is refused.
        error(429, "rate_limited_error"),
        error(500, "internal_server_error"),
        error(503, "internal_server_error"),
        # A type this adapter does not know.
        error(409, "some_future_error"),
        # Not Increase's document: something in between answered.
        httpx.Response(400, text="Bad Request"),
        httpx.Response(404, json={"error": {"code": "not_found", "message": ""}}),
        # A document whose status is not the response's.
        httpx.Response(400, json={"status": 500, "type": "invalid_parameters_error", "title": "x"}),
    ],
)
async def test_anything_that_is_not_clearly_a_refusal_is_unknown(
    increase: FakeIncrease, answer: httpx.Response
) -> None:
    increase.on("GET", f"/ach_transfers/{TRANSFER}", answer)

    async with adapter(increase) as bank:
        with pytest.raises(ProviderOutcomeUnknown):
            await bank.get_payout(TRANSFER)


# --- account numbers -----------------------------------------------------------------------


async def test_a_virtual_account_is_an_account_number_that_refuses_debits(
    increase: FakeIncrease,
) -> None:
    increase.on("POST", "/account_numbers", ok(account_number()))

    async with adapter(increase) as bank:
        account = await bank.create_virtual_account(
            customer_reference=CUSTOMER, asset_code="USD", idempotency_key=f"instr:{CUSTOMER}:USD"
        )

    assert increase.body() == {
        "account_id": ACCOUNT,
        "name": CUSTOMER,
        "inbound_ach_debits": "blocked",
    }
    assert increase.requests[0].headers["idempotency-key"] == f"instr:{CUSTOMER}:USD"
    assert (account.id, account.rail, account.asset_code) == (ACCOUNT_NUMBER_ID, "ach", "USD")
    assert (account.account_number, account.routing_number) == ("1234567890", ROUTING)
    assert "1234567890" not in repr(account)


@pytest.mark.parametrize(
    "changes",
    [
        {"inbound_ach_debits": "allowed"},
        {"status": "disabled"},
        {"name": "someone else"},
        {"account_id": OTHER_ACCOUNT},
    ],
)
async def test_an_account_number_that_is_not_what_was_asked_for_is_not_believed(
    increase: FakeIncrease, changes: dict[str, object]
) -> None:
    increase.on("POST", "/account_numbers", ok(account_number(**changes)))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderOutcomeUnknown):
            await bank.create_virtual_account(
                customer_reference=CUSTOMER, asset_code="USD", idempotency_key="k"
            )


@pytest.mark.parametrize("asset", ["MXN", "BRL", "USDC"])
async def test_an_asset_ach_does_not_carry_is_refused_without_asking(
    increase: FakeIncrease, asset: str
) -> None:
    async with adapter(increase) as bank:
        with pytest.raises(ProviderRejected) as refusal:
            await bank.create_virtual_account(
                customer_reference=CUSTOMER, asset_code=asset, idempotency_key="k"
            )

    assert refusal.value.code == "unsupported_asset"
    assert increase.requests == []


# --- external accounts ---------------------------------------------------------------------


async def test_a_beneficiary_is_an_external_account_and_comes_back_masked(
    increase: FakeIncrease,
) -> None:
    increase.on("POST", "/external_accounts", ok(external_account()))

    async with adapter(increase) as bank:
        beneficiary = await bank.create_beneficiary(
            customer_reference=CUSTOMER,
            asset_code="USD",
            holder_name="Maria Silva",
            account_number=NUMBER,
            routing_number=ROUTING,
            idempotency_key="ben:actor:1",
        )

    assert increase.body() == {
        "account_number": NUMBER,
        "routing_number": ROUTING,
        "description": "Maria Silva",
        "funding": "checking",
    }
    assert (beneficiary.id, beneficiary.account_mask) == (EXTERNAL_ACCOUNT, "••••4321")
    assert NUMBER not in repr(beneficiary)


@pytest.mark.parametrize(
    ("number", "routing"),
    [
        ("", ROUTING),
        ("1" * 18, ROUTING),
        ("12345x", ROUTING),
        ("\uff11\uff12\uff13\uff14", ROUTING),  # digits, but not ASCII ones
        (NUMBER, None),
        (NUMBER, "12345678"),
        (NUMBER, "1234567890"),
    ],
)
async def test_numbers_of_the_wrong_shape_are_an_invalid_account_and_are_not_sent(
    increase: FakeIncrease, number: str, routing: str | None
) -> None:
    async with adapter(increase) as bank:
        with pytest.raises(ProviderRejected) as refusal:
            await bank.create_beneficiary(
                customer_reference=CUSTOMER,
                asset_code="USD",
                holder_name="Maria Silva",
                account_number=number,
                routing_number=routing,
                idempotency_key="ben:actor:1",
            )

    assert refusal.value.code == "invalid_account"
    assert increase.requests == []


async def test_increase_refusing_the_parameters_of_an_account_is_an_invalid_account(
    increase: FakeIncrease,
) -> None:
    increase.on("POST", "/external_accounts", error(400, "invalid_parameters_error"))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderRejected) as refusal:
            await bank.create_beneficiary(
                customer_reference=CUSTOMER,
                asset_code="USD",
                holder_name="Maria Silva",
                account_number=NUMBER,
                routing_number=ROUTING,
                idempotency_key="ben:actor:1",
            )

    assert refusal.value.code == "invalid_account"


@pytest.mark.parametrize(
    "changes",
    [{"account_number": "987654320"}, {"routing_number": "021000021"}, {"status": "archived"}],
)
async def test_an_external_account_for_other_numbers_is_not_believed(
    increase: FakeIncrease, changes: dict[str, object]
) -> None:
    increase.on("POST", "/external_accounts", ok(external_account(**changes)))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderOutcomeUnknown) as unknown:
            await bank.create_beneficiary(
                customer_reference=CUSTOMER,
                asset_code="USD",
                holder_name="Maria Silva",
                account_number=NUMBER,
                routing_number=ROUTING,
                idempotency_key="ben:actor:1",
            )

    # Which field, and never the numbers.
    assert NUMBER not in str(unknown.value)


@pytest.mark.parametrize(
    ("number", "shown"), [("987654321", "••••4321"), ("1234567", "••••567"), ("1", "••••")]
)
def test_a_mask_never_shows_more_than_half_of_a_number(number: str, shown: str) -> None:
    assert mask(number) == shown


# --- idempotency keys ----------------------------------------------------------------------


def test_a_key_that_fits_is_sent_as_it_is_and_a_longer_one_as_its_digest() -> None:
    assert idempotency_key_for("a" * 200) == "a" * 200
    long = "ben:" + "b" * 250
    digest = idempotency_key_for(long)
    assert digest.startswith("sha256:")
    assert len(digest) <= 200
    # The same key is the same digest, so a retry finds what the first attempt made.
    assert idempotency_key_for(long) == digest
    assert idempotency_key_for(long + "c") != digest


@pytest.mark.parametrize("key", ["", " k", "k ", "kéy", "k\ny"])
def test_a_key_that_is_not_visible_ascii_is_a_broken_contract(key: str) -> None:
    with pytest.raises(ValueError, match="visible ASCII"):
        idempotency_key_for(key)


async def test_a_long_beneficiary_key_reaches_increase_as_its_digest(
    increase: FakeIncrease,
) -> None:
    increase.on("POST", "/external_accounts", ok(external_account()))
    key = "ben:" + "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10:" + "x" * 255

    async with adapter(increase) as bank:
        await bank.create_beneficiary(
            customer_reference=CUSTOMER,
            asset_code="USD",
            holder_name="Maria Silva",
            account_number=NUMBER,
            routing_number=ROUTING,
            idempotency_key=key,
        )

    assert increase.requests[0].headers["idempotency-key"] == idempotency_key_for(key)


# --- payouts -------------------------------------------------------------------------------


async def test_a_payout_is_an_ach_credit_in_cents_under_the_reference_as_its_key(
    increase: FakeIncrease,
) -> None:
    increase.on("POST", "/ach_transfers", ok(ach_transfer()))

    async with adapter(increase) as bank:
        payout = await bank.create_payout(
            beneficiary_id=EXTERNAL_ACCOUNT,
            asset_code="USD",
            amount=100_00,
            reference=REFERENCE,
            idempotency_key=REFERENCE,
        )

    assert increase.body() == {
        "account_id": ACCOUNT,
        "amount": 100_00,
        "external_account_id": EXTERNAL_ACCOUNT,
        "statement_descriptor": STATEMENT_DESCRIPTOR,
        "require_approval": False,
    }
    assert increase.requests[0].headers["idempotency-key"] == REFERENCE
    assert (payout.id, payout.status, payout.amount, payout.fee) == (TRANSFER, "pending", 100_00, 0)
    assert (payout.reference, payout.beneficiary_id) == (REFERENCE, EXTERNAL_ACCOUNT)


async def test_a_payout_whose_reference_is_not_its_key_is_refused_before_it_is_sent(
    increase: FakeIncrease,
) -> None:
    """Increase keeps no reference but the key. A payout sent under another key could not
    be found by its reference, and the saga asks for it by its reference before it gives
    funds back."""
    async with adapter(increase) as bank:
        with pytest.raises(ValueError, match="idempotency key"):
            await bank.create_payout(
                beneficiary_id=EXTERNAL_ACCOUNT,
                asset_code="USD",
                amount=100_00,
                reference=REFERENCE,
                idempotency_key="another",
            )

    assert increase.requests == []


@pytest.mark.parametrize(
    ("changes", "status", "reason", "settled"),
    [
        ({"status": "pending_approval"}, "pending", None, None),
        ({"status": "pending_transfer_session_confirmation"}, "pending", None, None),
        ({"status": "pending_submission"}, "pending", None, None),
        ({"status": "pending_reviewing"}, "pending", None, None),
        ({"status": "requires_attention"}, "pending", None, None),
        ({"status": "submitted"}, "pending", None, None),
        (
            {"status": "submitted", "settlement": {"settled_at": SETTLED}},
            "completed",
            None,
            datetime(2026, 1, 16, 15, 30, tzinfo=UTC),
        ),
        ({"status": "canceled"}, "failed", "canceled", None),
        ({"status": "rejected"}, "failed", "rejected", None),
        (
            {"status": "returned", "return": {"return_reason_code": "no_account"}},
            "failed",
            "no_account",
            None,
        ),
        ({"status": "returned"}, "failed", "returned", None),
    ],
)
async def test_each_status_of_an_ach_transfer_is_one_of_the_ports_three(
    increase: FakeIncrease,
    changes: dict[str, object],
    status: str,
    reason: str | None,
    settled: datetime | None,
) -> None:
    increase.on("GET", f"/ach_transfers/{TRANSFER}", ok(ach_transfer(**changes)))

    async with adapter(increase) as bank:
        payout = await bank.get_payout(TRANSFER)

    assert (payout.status, payout.failure_reason, payout.settled_at) == (status, reason, settled)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "settled"},  # not a status Increase documents
        {"amount": "10000"},  # a string where Increase writes an integer
        {"amount": 10000.0},  # a number that has been through a float
        {"amount": -100_00},  # a debit, which Corridor never sends
        {"currency": "EUR"},
        {"idempotency_key": None},
        {"external_account_id": None},
        {"id": "ach_transfer_another"},
        {"account_id": OTHER_ACCOUNT},
        {"type": "wire_transfer"},
    ],
)
async def test_an_ach_transfer_that_cannot_be_believed_is_unknown(
    increase: FakeIncrease, changes: dict[str, object]
) -> None:
    increase.on("GET", f"/ach_transfers/{TRANSFER}", ok(ach_transfer(**changes)))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderOutcomeUnknown):
            await bank.get_payout(TRANSFER)


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": 99_99},
        {"external_account_id": "external_account_another"},
        {"idempotency_key": "another"},
    ],
)
async def test_a_created_transfer_that_is_not_the_one_asked_for_is_unknown(
    increase: FakeIncrease, changes: dict[str, object]
) -> None:
    increase.on("POST", "/ach_transfers", ok(ach_transfer(**changes)))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderOutcomeUnknown):
            await bank.create_payout(
                beneficiary_id=EXTERNAL_ACCOUNT,
                asset_code="USD",
                amount=100_00,
                reference=REFERENCE,
                idempotency_key=REFERENCE,
            )


async def test_payouts_are_found_by_the_key_their_reference_is(increase: FakeIncrease) -> None:
    increase.on("GET", "/ach_transfers", ok({"data": [ach_transfer()], "next_cursor": None}))

    async with adapter(increase) as bank:
        found = await bank.find_payouts(REFERENCE)

    assert [payout.id for payout in found] == [TRANSFER]
    params = increase.requests[0].url.params
    assert (params["idempotency_key"], params["account_id"]) == (REFERENCE, ACCOUNT)


async def test_a_key_nobody_used_finds_nothing(increase: FakeIncrease) -> None:
    increase.on("GET", "/ach_transfers", ok({"data": [], "next_cursor": None}))

    async with adapter(increase) as bank:
        assert await bank.find_payouts(REFERENCE) == ()


@pytest.mark.parametrize(
    "page",
    [
        {"data": [ach_transfer()], "next_cursor": "more"},
        {"data": [ach_transfer(idempotency_key="another")], "next_cursor": None},
        {"data": [ach_transfer(account_id=OTHER_ACCOUNT)], "next_cursor": None},
    ],
)
async def test_a_search_that_answers_for_another_key_or_more_than_a_page_is_unknown(
    increase: FakeIncrease, page: dict[str, Any]
) -> None:
    increase.on("GET", "/ach_transfers", ok(page))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderOutcomeUnknown):
            await bank.find_payouts(REFERENCE)


# --- the statement -------------------------------------------------------------------------


def balance(amount: int, account: str = ACCOUNT) -> httpx.Response:
    return ok(
        {
            "account_id": account,
            "current_balance": amount,
            "available_balance": amount,
            "loan": None,
            "type": "balance_lookup",
        }
    )


async def test_the_statement_names_each_line_as_reconciliation_reads_them(
    increase: FakeIncrease,
) -> None:
    lines = [
        transaction("transaction_1", 250_00, "inbound_ach_transfer", {"transfer_id": INBOUND}),
        transaction(
            "transaction_2",
            -250_00,
            "inbound_ach_transfer_return_intention",
            {"inbound_ach_transfer_id": INBOUND},
        ),
        transaction("transaction_3", -100_00, "ach_transfer_intention", {"transfer_id": TRANSFER}),
        transaction("transaction_4", -5_00, "fee_payment", {"fee_period_start": "2026-01-01"}),
        transaction("transaction_5", 0, "interest_payment", {}),
    ]
    increase.on("GET", "/transactions", ok({"data": lines, "next_cursor": None}))
    increase.on("GET", f"/ach_transfers/{TRANSFER}", ok(ach_transfer(status="submitted")))
    increase.on("GET", f"/accounts/{ACCOUNT}/balance", balance(-105_00))

    async with adapter(increase) as bank:
        statement = await bank.list_transactions(asset_code="USD", start=START, end=END)

    assert [(line.type, line.direction, line.amount) for line in statement.transactions] == [
        ("deposit", "credit", 250_00),
        ("deposit_return", "debit", 250_00),
        ("payout", "debit", 100_00),
        ("fee_payment", "debit", 5_00),
    ]
    deposit, recall, paid, fee = statement.transactions
    assert (deposit.id, deposit.related_id) == (INBOUND, ACCOUNT_NUMBER_ID)
    assert (recall.id, recall.related_id) == (f"{INBOUND}:return", INBOUND)
    assert (paid.id, paid.reference, paid.related_id) == (TRANSFER, REFERENCE, EXTERNAL_ACCOUNT)
    assert fee.id == "transaction_4"
    assert (statement.asset_code, statement.start, statement.end) == ("USD", START, END)
    assert statement.closing_balance == -105_00

    listed = increase.requests[0].url.params
    assert listed["account_id"] == ACCOUNT
    assert (listed["created_at.on_or_after"], listed["created_at.before"]) == (
        "2026-01-15T00:00:00Z",
        "2026-01-16T00:00:00Z",
    )
    assert increase.requests[-1].url.params["at_time"] == "2026-01-16T00:00:00Z"


@pytest.mark.parametrize("offset", ["ach_transfer_return", "ach_transfer_rejection"])
async def test_a_transfer_whose_money_came_back_in_the_window_is_left_out_with_its_return(
    increase: FakeIncrease, offset: str
) -> None:
    lines = [
        transaction("transaction_3", -100_00, "ach_transfer_intention", {"transfer_id": TRANSFER}),
        transaction("transaction_6", 100_00, offset, {"transfer_id": TRANSFER}),
    ]
    increase.on("GET", "/transactions", ok({"data": lines, "next_cursor": None}))
    increase.on("GET", f"/ach_transfers/{TRANSFER}", ok(ach_transfer(status="returned")))
    increase.on("GET", f"/accounts/{ACCOUNT}/balance", balance(0))

    async with adapter(increase) as bank:
        statement = await bank.list_transactions(asset_code="USD", start=START, end=END)

    assert statement.transactions == ()


async def test_a_return_of_a_transfer_from_before_the_window_is_a_payout_return(
    increase: FakeIncrease,
) -> None:
    lines = [transaction("transaction_6", 100_00, "ach_transfer_return", {"transfer_id": TRANSFER})]
    increase.on("GET", "/transactions", ok({"data": lines, "next_cursor": None}))
    increase.on("GET", f"/accounts/{ACCOUNT}/balance", balance(100_00))

    async with adapter(increase) as bank:
        statement = await bank.list_transactions(asset_code="USD", start=START, end=END)

    (line,) = statement.transactions
    assert (line.type, line.direction, line.related_id) == ("payout_return", "credit", TRANSFER)
    assert line.id == f"{TRANSFER}:ach_transfer_return"


async def test_every_page_of_the_statement_is_read_with_the_same_filters(
    increase: FakeIncrease,
) -> None:
    first = transaction("transaction_1", 1_00, "inbound_ach_transfer", {"transfer_id": INBOUND})
    second = transaction(
        "transaction_2", 2_00, "inbound_ach_transfer", {"transfer_id": INBOUND + "b"}
    )
    increase.on(
        "GET",
        "/transactions",
        ok({"data": [first], "next_cursor": "page_2"}),
        ok({"data": [second], "next_cursor": None}),
    )
    increase.on("GET", f"/accounts/{ACCOUNT}/balance", balance(3_00))

    async with adapter(increase) as bank:
        statement = await bank.list_transactions(asset_code="USD", start=START, end=END)

    assert [line.amount for line in statement.transactions] == [1_00, 2_00]
    one, two = increase.requests[0].url.params, increase.requests[1].url.params
    assert "cursor" not in one
    assert two["cursor"] == "page_2"
    assert {k: v for k, v in two.items() if k != "cursor"} == dict(one)


async def test_a_statement_that_never_ends_is_not_read_as_one(increase: FakeIncrease) -> None:
    line = transaction("transaction_1", 1_00, "inbound_ach_transfer", {"transfer_id": INBOUND})
    increase.on(
        "GET",
        "/transactions",
        *[ok({"data": [line], "next_cursor": "again"})] * (MAX_STATEMENT_PAGES + 1),
    )

    async with adapter(increase) as bank:
        with pytest.raises(ProviderOutcomeUnknown):
            await bank.list_transactions(asset_code="USD", start=START, end=END)

    assert len(increase.requests) == MAX_STATEMENT_PAGES


@pytest.mark.parametrize(
    "changes",
    [
        {"created_at": "2026-01-14T23:59:59Z"},
        {"created_at": "2026-01-16T00:00:00Z"},  # the window is half-open
        {"account_id": OTHER_ACCOUNT},
        {"currency": "EUR"},
    ],
)
async def test_a_line_of_another_account_or_outside_the_window_is_unknown(
    increase: FakeIncrease, changes: dict[str, object]
) -> None:
    line = transaction(
        "transaction_1", 1_00, "inbound_ach_transfer", {"transfer_id": INBOUND}, **changes
    )
    increase.on("GET", "/transactions", ok({"data": [line], "next_cursor": None}))
    increase.on("GET", f"/accounts/{ACCOUNT}/balance", balance(1_00))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderOutcomeUnknown):
            await bank.list_transactions(asset_code="USD", start=START, end=END)


async def test_the_balance_of_another_account_is_unknown(increase: FakeIncrease) -> None:
    increase.on("GET", "/transactions", ok({"data": [], "next_cursor": None}))
    increase.on("GET", f"/accounts/{ACCOUNT}/balance", balance(0, account=OTHER_ACCOUNT))

    async with adapter(increase) as bank:
        with pytest.raises(ProviderOutcomeUnknown):
            await bank.list_transactions(asset_code="USD", start=START, end=END)


@pytest.mark.parametrize("asset", ["MXN", "BRL"])
async def test_increase_holds_nothing_in_another_currency_and_is_not_asked(
    increase: FakeIncrease, asset: str
) -> None:
    async with adapter(increase) as bank:
        statement = await bank.list_transactions(asset_code=asset, start=START, end=END)

    assert (statement.transactions, statement.closing_balance) == ((), 0)
    assert increase.requests == []


# --- events --------------------------------------------------------------------------------


async def resolve(
    increase: FakeIncrease, category: str, object_type: str, object_id: str
) -> tuple[BankEvent, ...]:
    async with adapter(increase) as bank:
        return await bank.resolve_event(
            category=category, object_type=object_type, object_id=object_id
        )


@pytest.mark.parametrize("category", ["ach_transfer.created", "ach_transfer.updated"])
async def test_a_settled_transfer_is_a_completed_payout(
    increase: FakeIncrease, category: str
) -> None:
    settled = ach_transfer(status="submitted", settlement={"settled_at": SETTLED})
    increase.on("GET", f"/ach_transfers/{TRANSFER}", ok(settled))

    (event,) = await resolve(increase, category, "ach_transfer", TRANSFER)

    assert event == BankEvent(
        "payout.completed",
        {
            "payout_id": TRANSFER,
            "reference": REFERENCE,
            "asset": "USD",
            "amount": "100.00",
            "fee": "0.00",
            "settled_at": "2026-01-16T15:30:00Z",
        },
    )


async def test_a_returned_transfer_is_a_failed_payout_with_the_return_code(
    increase: FakeIncrease,
) -> None:
    returned = ach_transfer(status="returned", **{"return": {"return_reason_code": "no_account"}})
    increase.on("GET", f"/ach_transfers/{TRANSFER}", ok(returned))

    (event,) = await resolve(increase, "ach_transfer.updated", "ach_transfer", TRANSFER)

    assert event.type == "payout.failed"
    assert event.data == {
        "payout_id": TRANSFER,
        "reference": REFERENCE,
        "asset": "USD",
        "amount": "100.00",
        "failure_reason": "no_account",
    }


@pytest.mark.parametrize("status", ["pending_submission", "submitted", "requires_attention"])
async def test_a_transfer_on_its_way_means_nothing_yet(increase: FakeIncrease, status: str) -> None:
    increase.on("GET", f"/ach_transfers/{TRANSFER}", ok(ach_transfer(status=status)))

    assert await resolve(increase, "ach_transfer.updated", "ach_transfer", TRANSFER) == ()


@pytest.mark.parametrize("changes", [{"idempotency_key": None}, {"amount": -100_00}])
async def test_a_transfer_corridor_did_not_send_means_nothing_to_it(
    increase: FakeIncrease, changes: dict[str, object]
) -> None:
    settled = ach_transfer(status="submitted", settlement={"settled_at": SETTLED}, **changes)
    increase.on("GET", f"/ach_transfers/{TRANSFER}", ok(settled))

    assert await resolve(increase, "ach_transfer.updated", "ach_transfer", TRANSFER) == ()


@pytest.mark.parametrize(
    "category", ["inbound_ach_transfer.created", "inbound_ach_transfer.updated"]
)
async def test_an_accepted_inbound_credit_is_a_deposit_received(
    increase: FakeIncrease, category: str
) -> None:
    increase.on("GET", f"/inbound_ach_transfers/{INBOUND}", ok(inbound()))

    (event,) = await resolve(increase, category, "inbound_ach_transfer", INBOUND)

    assert event.type == "deposit.received"
    assert event.data == {
        "deposit_id": INBOUND,
        "virtual_account_id": ACCOUNT_NUMBER_ID,
        "asset": "USD",
        "amount": "250.00",
        "sender_name": "BIG BANK",
        "reference": "PAYROLL",
    }
    # Who sent it is the data's, and not printed with the event.
    assert "BIG BANK" not in repr(event)


async def test_a_returned_inbound_credit_is_a_deposit_returned(increase: FakeIncrease) -> None:
    returned = inbound(
        status="returned",
        transfer_return={
            "reason": "credit_entry_refused_by_receiver",
            "returned_at": CREATED,
            "transaction_id": "transaction_9",
        },
    )
    increase.on("GET", f"/inbound_ach_transfers/{INBOUND}", ok(returned))

    (event,) = await resolve(
        increase, "inbound_ach_transfer.updated", "inbound_ach_transfer", INBOUND
    )

    assert event == BankEvent(
        "deposit.returned",
        {
            "deposit_id": INBOUND,
            "asset": "USD",
            "amount": "250.00",
            "reason": "credit_entry_refused_by_receiver",
        },
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "pending"},
        {"status": "declined"},
        # A debit pulls money out. The account numbers refuse them; one taken anyway is
        # never a deposit.
        {"direction": "debit", "amount": -250_00},
        {"direction": "debit"},
    ],
)
async def test_an_inbound_transfer_that_credited_nothing_is_no_deposit(
    increase: FakeIncrease, changes: dict[str, object]
) -> None:
    increase.on("GET", f"/inbound_ach_transfers/{INBOUND}", ok(inbound(**changes)))

    assert (
        await resolve(increase, "inbound_ach_transfer.created", "inbound_ach_transfer", INBOUND)
        == ()
    )


@pytest.mark.parametrize(
    ("path", "category", "object_type", "object_id", "document"),
    [
        (f"/inbound_ach_transfers/{INBOUND}", "inbound_ach_transfer.created",
         "inbound_ach_transfer", INBOUND, inbound(account_id=OTHER_ACCOUNT)),
        (f"/inbound_ach_transfers/{INBOUND}", "inbound_ach_transfer.created",
         "inbound_ach_transfer", INBOUND, inbound(id=INBOUND + "x")),
        (f"/ach_transfers/{TRANSFER}", "ach_transfer.updated", "ach_transfer", TRANSFER,
         ach_transfer(account_id=OTHER_ACCOUNT)),
    ],
)  # fmt: skip
async def test_an_object_of_another_account_or_id_is_unknown(
    increase: FakeIncrease,
    path: str,
    category: str,
    object_type: str,
    object_id: str,
    document: dict[str, Any],
) -> None:
    increase.on("GET", path, ok(document))

    with pytest.raises(ProviderOutcomeUnknown):
        await resolve(increase, category, object_type, object_id)


async def test_a_transfer_that_cannot_be_believed_makes_its_event_unknown(
    increase: FakeIncrease,
) -> None:
    settled = ach_transfer(
        status="submitted", settlement={"settled_at": SETTLED}, external_account_id=None
    )
    increase.on("GET", f"/ach_transfers/{TRANSFER}", ok(settled))

    with pytest.raises(ProviderOutcomeUnknown):
        await resolve(increase, "ach_transfer.updated", "ach_transfer", TRANSFER)


async def test_an_event_whose_object_is_not_of_its_categorys_type_is_unknown(
    increase: FakeIncrease,
) -> None:
    with pytest.raises(ProviderOutcomeUnknown):
        await resolve(increase, "ach_transfer.updated", "inbound_ach_transfer", INBOUND)

    assert increase.requests == []


@pytest.mark.parametrize("category", ["transaction.created", "account_number.created", "x.y"])
async def test_an_event_of_another_category_means_nothing_and_asks_nothing(
    increase: FakeIncrease, category: str
) -> None:
    assert await resolve(increase, category, "transaction", "transaction_1") == ()
    assert increase.requests == []


async def test_an_object_that_cannot_be_read_is_raised_for_the_event_to_be_tried_again(
    increase: FakeIncrease,
) -> None:
    increase.on("GET", f"/ach_transfers/{TRANSFER}", error(500, "internal_server_error"))

    with pytest.raises(ProviderOutcomeUnknown):
        await resolve(increase, "ach_transfer.updated", "ach_transfer", TRANSFER)

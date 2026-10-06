"""What the adapters send, and what they refuse to believe.

The first half watches the requests that reach the real simulator. The second half puts a
scripted provider in its place, to answer in ways the simulator never does.
"""

import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from corridor.platform.config import Settings
from corridor.providers import (
    ProviderMisconfigured,
    ProviderOutcomeUnknown,
    ProviderRejected,
    SimBank,
    SimCustody,
    SimRates,
)
from corridor.providers.http import ProviderClient
from tests.providers.conftest import (
    ACCOUNT_NUMBER,
    API_KEY,
    CUSTOMER,
    EXTERNAL_ADDRESS,
    REFERENCE,
    ROUTING_NUMBER,
    Handler,
    Sim,
    answer,
    captured_logs,
    log_events,
)

Stub = Callable[[Handler], httpx.AsyncClient]

PAYOUT: dict[str, object] = {
    "id": "po_2h5j8n",
    "status": "pending",
    "beneficiary_id": "ben_4t7w1x",
    "asset": "USD",
    "amount": "100.00",
    "fee": "0.25",
    "reference": REFERENCE,
    "created_at": "2026-01-15T12:00:00Z",
    "settled_at": None,
    "failure_reason": None,
}
WITHDRAWAL: dict[str, object] = {
    "id": "wd_9q4s6v",
    "status": "pending",
    "asset": "USDC",
    "amount": "25.000000",
    "network_fee": "0.150000",
    "to_address": EXTERNAL_ADDRESS,
    "reference": REFERENCE,
    "tx_hash": None,
    "confirmations": 0,
    "created_at": "2026-01-15T12:00:00Z",
    "completed_at": None,
    "failure_reason": None,
}


async def pay(bank: SimBank) -> Any:
    return await bank.create_payout(
        beneficiary_id="ben_4t7w1x",
        asset_code="USD",
        amount=100_00,
        reference=REFERENCE,
        idempotency_key=REFERENCE,
    )


async def withdraw(custody: SimCustody) -> Any:
    return await custody.create_withdrawal(
        asset_code="USDC",
        amount=25_000_000,
        to_address=EXTERNAL_ADDRESS,
        reference=REFERENCE,
        idempotency_key=REFERENCE,
    )


# --- what is sent ----------------------------------------------------------------------------


async def test_every_mutating_call_carries_its_idempotency_key(
    bank: SimBank, custody: SimCustody, sim: Sim
) -> None:
    await bank.create_virtual_account(
        customer_reference=CUSTOMER, asset_code="USD", idempotency_key="key-va"
    )
    beneficiary = await bank.create_beneficiary(
        customer_reference=CUSTOMER,
        asset_code="USD",
        holder_name="Maria Silva",
        account_number=ACCOUNT_NUMBER,
        routing_number=ROUTING_NUMBER,
        idempotency_key="key-ben",
    )
    await bank.create_payout(
        beneficiary_id=beneficiary.id,
        asset_code="USD",
        amount=100_00,
        reference=REFERENCE,
        idempotency_key="key-po",
    )
    await custody.create_address(
        customer_reference=CUSTOMER, asset_code="USDC", idempotency_key="key-addr"
    )
    await custody.create_withdrawal(
        asset_code="USDC",
        amount=25_000_000,
        to_address=EXTERNAL_ADDRESS,
        reference=REFERENCE,
        idempotency_key="key-wd",
    )

    assert [
        (request.url.path, request.headers.get("idempotency-key"))
        for request in sim.recorder.sent("POST")
    ] == [
        ("/bank/v1/virtual-accounts", "key-va"),
        ("/bank/v1/beneficiaries", "key-ben"),
        ("/bank/v1/payouts", "key-po"),
        ("/custody/v1/addresses", "key-addr"),
        ("/custody/v1/withdrawals", "key-wd"),
    ]
    assert (await sim.payouts())[0]["idempotency_key"] == "key-po"
    assert (await sim.withdrawals())[0]["idempotency_key"] == "key-wd"


async def test_a_read_carries_no_idempotency_key(bank: SimBank, rates: SimRates, sim: Sim) -> None:
    await bank.find_payouts(REFERENCE)
    await rates.get_rate("USD", "MXN")

    assert [request.headers.get("idempotency-key") for request in sim.recorder.sent("GET")] == [
        None,
        None,
    ]


@pytest.mark.parametrize(
    ("adapter", "method"),
    [
        (SimBank, "create_virtual_account"),
        (SimBank, "create_beneficiary"),
        (SimBank, "create_payout"),
        (SimCustody, "create_address"),
        (SimCustody, "create_withdrawal"),
        (ProviderClient, "post"),
    ],
)
def test_the_idempotency_key_is_an_argument_nobody_can_leave_out(
    adapter: type, method: str
) -> None:
    parameter = inspect.signature(getattr(adapter, method)).parameters["idempotency_key"]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is str


async def test_a_call_without_a_key_does_not_run(bank: SimBank, sim: Sim) -> None:
    with pytest.raises(TypeError, match="idempotency_key"):
        await bank.create_payout(  # type: ignore[call-arg]
            beneficiary_id="ben_4t7w1x", asset_code="USD", amount=100_00, reference=REFERENCE
        )

    assert sim.recorder.requests == []


@pytest.mark.parametrize("key", ["", "   "])
async def test_a_blank_key_is_never_sent(bank: SimBank, sim: Sim, key: str) -> None:
    with pytest.raises(ValueError, match="idempotency key"):
        await bank.create_payout(
            beneficiary_id="ben_4t7w1x",
            asset_code="USD",
            amount=100_00,
            reference=REFERENCE,
            idempotency_key=key,
        )

    assert sim.recorder.requests == []


async def test_every_call_presents_the_api_key_as_a_bearer_credential(
    bank: SimBank, sim: Sim
) -> None:
    await bank.find_payouts(REFERENCE)

    assert sim.recorder.requests[-1].headers["authorization"] == f"Bearer {API_KEY}"


async def test_amounts_are_sent_as_decimal_strings_never_numbers(
    provider_settings: Settings, stub: Stub
) -> None:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json=PAYOUT)

    await pay(SimBank(provider_settings, client=stub(handler)))

    assert seen[0]["amount"] == "100.00"


# --- what is not believed --------------------------------------------------------------------


async def test_a_payout_that_matches_the_request_is_accepted(
    provider_settings: Settings, stub: Stub
) -> None:
    payout = await pay(SimBank(provider_settings, client=stub(answer(201, PAYOUT))))

    assert (payout.id, payout.amount, payout.fee) == ("po_2h5j8n", 100_00, 25)


@pytest.mark.parametrize(
    "changed",
    [
        {"amount": "100.01"},
        {"amount": "1000.00"},
        {"asset": "MXN"},
        {"reference": "0199b7c3-0000-7c3d-8e4f-5a6b7c8d9e0f"},
        {"beneficiary_id": "ben_another"},
    ],
    ids=lambda changed: next(iter(changed)),
)
async def test_a_payout_that_does_not_echo_the_request_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub, changed: dict[str, object]
) -> None:
    bank = SimBank(provider_settings, client=stub(answer(201, PAYOUT | changed)))

    with pytest.raises(ProviderOutcomeUnknown):
        await pay(bank)


@pytest.mark.parametrize(
    "changed",
    [
        {"amount": "25.000001"},
        {"asset": "USD", "amount": "25.00", "network_fee": "0.15"},
        {"reference": "another"},
        {"to_address": "sim1" + "b" * 32 + "00000000"},
        {"confirmations": "0"},
    ],
    ids=lambda changed: next(iter(changed)),
)
async def test_a_withdrawal_that_does_not_echo_the_request_or_is_mistyped_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub, changed: dict[str, object]
) -> None:
    custody = SimCustody(provider_settings, client=stub(answer(201, WITHDRAWAL | changed)))

    with pytest.raises(ProviderOutcomeUnknown):
        await withdraw(custody)


@pytest.mark.parametrize(
    "document",
    [
        {"id": "po_2h5j8n"},
        {key: value for key, value in PAYOUT.items() if key != "status"},
        {key: value for key, value in PAYOUT.items() if key != "settled_at"},
        PAYOUT | {"amount": 100.0},
        PAYOUT | {"amount": "100.001"},
        PAYOUT | {"amount": "1e2"},
        PAYOUT | {"fee": "-0.25"},
        PAYOUT | {"fee": None},
        PAYOUT | {"status": "sent"},
        PAYOUT | {"id": ""},
        PAYOUT | {"created_at": "2026-01-15T12:00:00"},
        PAYOUT | {"created_at": "yesterday"},
        PAYOUT | {"created_at": 1768478400},
        [PAYOUT],
        None,
    ],
)
async def test_a_malformed_or_short_payout_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub, document: object
) -> None:
    bank = SimBank(provider_settings, client=stub(answer(201, document)))

    with pytest.raises(ProviderOutcomeUnknown):
        await pay(bank)


@pytest.mark.parametrize("body", [b"", b"<html>ok</html>", b'{"id": "po_2h5j8n", "status": "pen'])
async def test_a_response_that_is_not_json_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub, body: bytes
) -> None:
    bank = SimBank(provider_settings, client=stub(lambda _: httpx.Response(201, content=body)))

    with pytest.raises(ProviderOutcomeUnknown):
        await pay(bank)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
async def test_a_server_error_is_an_unknown_outcome_whatever_its_body_says(
    provider_settings: Settings, stub: Stub, status: int
) -> None:
    refusal = {"error": {"code": "invalid_amount", "message": "No."}}
    bank = SimBank(provider_settings, client=stub(answer(status, refusal)))

    with pytest.raises(ProviderOutcomeUnknown):
        await pay(bank)


@pytest.mark.parametrize("status", [500, 503])
async def test_a_server_error_is_an_unknown_outcome_even_with_a_complete_payout_in_its_body(
    provider_settings: Settings, stub: Stub, status: int
) -> None:
    bank = SimBank(provider_settings, client=stub(answer(status, PAYOUT)))

    with pytest.raises(ProviderOutcomeUnknown):
        await pay(bank)


@pytest.mark.parametrize("status", [204, 301, 302])
async def test_an_answer_that_is_neither_success_nor_refusal_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub, status: int
) -> None:
    bank = SimBank(provider_settings, client=stub(answer(status, PAYOUT)))

    with pytest.raises(ProviderOutcomeUnknown):
        await pay(bank)


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadError("reset"), httpx.ReadTimeout("slow")],
    ids=lambda error: type(error).__name__,
)
async def test_a_connection_that_fails_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub, error: Exception
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(ProviderOutcomeUnknown):
        await pay(SimBank(provider_settings, client=stub(handler)))


async def test_a_refusal_carries_the_providers_code_and_status(
    provider_settings: Settings, stub: Stub
) -> None:
    refusal = {"error": {"code": "invalid_amount", "message": "An amount is greater than zero."}}
    bank = SimBank(provider_settings, client=stub(answer(422, refusal)))

    with pytest.raises(ProviderRejected) as refused:
        await pay(bank)

    assert (refused.value.status, refused.value.code) == (422, "invalid_amount")
    assert refused.value.message == "An amount is greater than zero."


@pytest.mark.parametrize(
    "document", ["Not Found", {"error": "nope"}, {"error": {"code": 7, "message": "x"}}, {}]
)
async def test_a_4xx_without_the_contracts_error_body_is_not_taken_as_a_refusal(
    provider_settings: Settings, stub: Stub, document: object
) -> None:
    bank = SimBank(provider_settings, client=stub(answer(404, document)))

    with pytest.raises(ProviderOutcomeUnknown):
        await pay(bank)


async def test_a_401_is_a_misconfiguration_whatever_the_operation(
    provider_settings: Settings, stub: Stub
) -> None:
    refusal = {"error": {"code": "unauthorized", "message": "Send the API key."}}
    custody = SimCustody(provider_settings, client=stub(answer(401, refusal)))

    with pytest.raises(ProviderMisconfigured):
        await withdraw(custody)


async def test_a_payout_read_back_under_another_id_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub
) -> None:
    bank = SimBank(provider_settings, client=stub(answer(200, PAYOUT)))

    with pytest.raises(ProviderOutcomeUnknown):
        await bank.get_payout("po_another")


async def test_a_search_that_returns_another_reference_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub
) -> None:
    bank = SimBank(provider_settings, client=stub(answer(200, {"payouts": [PAYOUT]})))

    with pytest.raises(ProviderOutcomeUnknown):
        await bank.find_payouts("another")


async def test_an_issued_address_that_fails_its_checksum_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub
) -> None:
    document = {
        "id": "addr_5m2p9r",
        "customer_reference": CUSTOMER,
        "asset": "USDC",
        "network": "simchain",
        "address": EXTERNAL_ADDRESS[:-1] + ("0" if EXTERNAL_ADDRESS[-1] != "0" else "1"),
    }
    custody = SimCustody(provider_settings, client=stub(answer(201, document)))

    with pytest.raises(ProviderOutcomeUnknown):
        await custody.create_address(
            customer_reference=CUSTOMER, asset_code="USDC", idempotency_key="addr-1"
        )


async def test_an_address_issued_to_another_customer_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub
) -> None:
    document = {
        "id": "addr_5m2p9r",
        "customer_reference": "someone-else",
        "asset": "USDC",
        "network": "simchain",
        "address": EXTERNAL_ADDRESS,
    }
    custody = SimCustody(provider_settings, client=stub(answer(201, document)))

    with pytest.raises(ProviderOutcomeUnknown):
        await custody.create_address(
            customer_reference=CUSTOMER, asset_code="USDC", idempotency_key="addr-1"
        )


@pytest.mark.parametrize(
    "changed",
    [{"base": "MXN"}, {"quote": "BRL"}, {"mid": "0.000000"}, {"mid": 17.2534}, {"mid": "-1.0"}],
    ids=str,
)
async def test_a_rate_for_another_pair_or_of_nothing_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub, changed: dict[str, object]
) -> None:
    document = {"base": "USD", "quote": "MXN", "mid": "17.253400", "as_of": "2026-01-15T12:00:00Z"}
    rates = SimRates(provider_settings, client=stub(answer(200, document | changed)))

    with pytest.raises(ProviderOutcomeUnknown):
        await rates.get_rate("USD", "MXN")


async def test_a_statement_for_another_asset_is_an_unknown_outcome(
    provider_settings: Settings, stub: Stub
) -> None:
    document = {
        "asset": "MXN",
        "from": "2026-01-15T00:00:00Z",
        "to": "2026-01-16T00:00:00Z",
        "transactions": [],
        "closing_balance": "0.00",
    }
    bank = SimBank(provider_settings, client=stub(answer(200, document)))

    with pytest.raises(ProviderOutcomeUnknown):
        await bank.list_transactions(
            asset_code="USD",
            start=datetime(2026, 1, 15, tzinfo=UTC),
            end=datetime(2026, 1, 16, tzinfo=UTC),
        )


# --- what is logged --------------------------------------------------------------------------


async def test_a_call_is_logged_by_operation_status_and_duration(
    bank: SimBank, capsys: pytest.CaptureFixture[str]
) -> None:
    with captured_logs(capsys) as read:
        await bank.find_payouts(REFERENCE)
        with pytest.raises(ProviderRejected):
            await bank.get_payout("po_missing")
        events = [event for event in log_events(read()) if event["event"] == "provider.call"]

    assert [
        (event["provider"], event["operation"], event["status"], event["outcome"])
        for event in events
    ] == [("simbank", "find_payouts", 200, "ok"), ("simbank", "get_payout", 404, "rejected")]
    assert all(event["duration_ms"] >= 0 for event in events)
    assert events[1]["code"] == "payout_not_found"


async def test_no_account_number_or_key_reaches_the_logs(
    bank: SimBank, provider_settings: Settings, stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    with captured_logs(capsys) as read:
        account = await bank.create_virtual_account(
            customer_reference=CUSTOMER, asset_code="USD", idempotency_key="va-1"
        )
        await bank.create_beneficiary(
            customer_reference=CUSTOMER,
            asset_code="USD",
            holder_name="Maria Silva",
            account_number=ACCOUNT_NUMBER,
            routing_number=ROUTING_NUMBER,
            idempotency_key="ben-1",
        )
        with pytest.raises(ProviderRejected):
            await bank.create_beneficiary(
                customer_reference=CUSTOMER,
                asset_code="USD",
                holder_name="Maria Silva",
                account_number="77",
                routing_number=ROUTING_NUMBER,
                idempotency_key="ben-2",
            )
        # A provider that sends the account number back where it does not belong.
        leaky = {
            "id": "ben_1",
            "asset": "USD",
            "rail": "ach",
            "holder_name": ACCOUNT_NUMBER,
            "account_mask": ACCOUNT_NUMBER,
        }
        with pytest.raises(ProviderOutcomeUnknown) as unknown:
            await SimBank(provider_settings, client=stub(answer(201, leaky))).create_beneficiary(
                customer_reference=CUSTOMER,
                asset_code="USD",
                holder_name="Maria Silva",
                account_number=ACCOUNT_NUMBER,
                routing_number=ROUTING_NUMBER,
                idempotency_key="ben-3",
            )
        logged = read()

    calls = [event for event in log_events(logged) if event["event"] == "provider.call"]
    assert [event["outcome"] for event in calls] == ["ok", "ok", "rejected", "unknown"]
    for secret in (ACCOUNT_NUMBER, ROUTING_NUMBER, account.account_number, API_KEY):
        assert secret not in logged
        assert secret not in str(unknown.value)

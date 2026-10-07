"""The bank-rail simulator: virtual accounts, beneficiaries, payouts and the statement."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from corridor_sim.clock import format_time
from tests.simulators.conftest import ACCOUNTS, API_KEY, CUSTOMER, START, Sim, running_sim

Launch = Callable[..., Awaitable[Sim]]

# Asset, rail, seconds until a payout settles, and the provider's fee: the contract's table.
SCHEDULE = [("USD", "ach", 30, "0.25"), ("MXN", "spei", 5, "5.00"), ("BRL", "pix", 1, "0.10")]

MASK = "\u2022" * 4

WINDOW = {"from": "2026-01-15T00:00:00Z", "to": "2026-01-16T00:00:00Z"}

BANK_ROUTES = [
    ("POST", "/bank/v1/virtual-accounts"),
    ("POST", "/bank/v1/beneficiaries"),
    ("POST", "/bank/v1/payouts"),
    ("GET", "/bank/v1/payouts/po_000000000000"),
    ("GET", "/bank/v1/payouts?reference=wd-1"),
    ("GET", "/bank/v1/transactions?asset=USD&from=2026-01-15T00:00:00Z&to=2026-01-16T00:00:00Z"),
]


def code_of(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def after(seconds: float) -> str:
    return format_time(START + timedelta(seconds=seconds))


# --- authentication ------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), BANK_ROUTES)
async def test_a_bank_route_refuses_a_request_without_the_api_key(
    sim: Sim, method: str, path: str
) -> None:
    response = await sim.anonymous.request(method, path, json={})

    assert code_of(response) == (401, "unauthorized")


@pytest.mark.parametrize(("method", "path"), BANK_ROUTES)
async def test_a_bank_route_refuses_a_wrong_api_key(sim: Sim, method: str, path: str) -> None:
    response = await sim.anonymous.request(
        method, path, json={}, headers={"Authorization": f"Bearer {API_KEY}-not"}
    )

    assert code_of(response) == (401, "unauthorized")


async def test_a_request_refused_for_its_credentials_does_nothing(sim: Sim) -> None:
    body = {"customer_reference": CUSTOMER, "asset": "USD"}

    refused = await sim.anonymous.post("/bank/v1/virtual-accounts", json=body)
    accepted = await sim.api.post("/bank/v1/virtual-accounts", json=body)

    assert refused.status_code == 401
    # 201, not 200: the refused request had not already created the account.
    assert accepted.status_code == 201


@pytest.mark.parametrize("path", ["/bank/payouts", "/bank/deposits", "/bank/balances"])
async def test_the_control_endpoints_need_no_credentials(sim: Sim, path: str) -> None:
    response = await sim.anonymous.get(f"/_control{path}")

    assert response.status_code == 200


# --- virtual accounts ----------------------------------------------------------------------


async def test_a_virtual_account_is_created_once_and_returned_again(sim: Sim) -> None:
    body = {"customer_reference": CUSTOMER, "asset": "USD"}

    first = await sim.api.post("/bank/v1/virtual-accounts", json=body)
    again = await sim.api.post("/bank/v1/virtual-accounts", json=body)

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert first.json() == {
        "id": first.json()["id"],
        "customer_reference": CUSTOMER,
        "asset": "USD",
        "rail": "ach",
        "bank_name": "Sim Bank",
        "account_number": first.json()["account_number"],
        "routing_number": "021000021",
    }
    assert re.fullmatch(r"va_[0-9a-f]{12}", first.json()["id"])


async def test_there_is_one_virtual_account_per_customer_and_asset(sim: Sim) -> None:
    usd = await sim.virtual_account("USD")
    mxn = await sim.virtual_account("MXN")
    someone_else = await sim.virtual_account("USD", customer="another-customer")

    ids = {usd["id"], mxn["id"], someone_else["id"]}
    numbers = {usd["account_number"], mxn["account_number"], someone_else["account_number"]}
    assert len(ids) == 3
    assert len(numbers) == 3
    assert (await sim.virtual_account("MXN")) == mxn


async def test_the_control_endpoints_say_which_accounts_a_customer_was_issued(sim: Sim) -> None:
    usd = await sim.virtual_account("USD")
    mxn = await sim.virtual_account("MXN")
    await sim.virtual_account("USD", customer="another-customer")

    # With no credential: the provider API key is not needed to find an account.
    issued = await sim.control(
        "GET", "/bank/virtual-accounts?customer_reference=" + usd["customer_reference"]
    )
    nobody = await sim.control("GET", "/bank/virtual-accounts?customer_reference=nobody")
    unnamed = await sim.anonymous.get("/_control/bank/virtual-accounts")

    assert issued == {"virtual_accounts": [mxn, usd]}
    assert nobody == {"virtual_accounts": []}
    assert unnamed.status_code == 422


@pytest.mark.parametrize(("asset", "rail", "_delay", "_fee"), SCHEDULE)
async def test_a_virtual_account_is_on_its_assets_rail_with_identifiers_of_that_rails_shape(
    sim: Sim, asset: str, rail: str, _delay: int, _fee: str
) -> None:
    account = await sim.virtual_account(asset)

    assert account["rail"] == rail
    if asset == "USD":
        assert re.fullmatch(r"[0-9]{12}", account["account_number"])
        assert re.fullmatch(r"[0-9]{9}", account["routing_number"])
    elif asset == "MXN":
        assert re.fullmatch(r"[0-9]{18}", account["account_number"])
        assert "routing_number" not in account
    else:
        assert re.fullmatch(r"\S{1,77}", account["account_number"])
        assert "routing_number" not in account
    # What the bank hands out as an account, it also accepts as one to pay out to.
    identifiers = {k: account[k] for k in ("account_number", "routing_number") if k in account}
    assert (await sim.beneficiary(asset, **identifiers))["rail"] == rail


@pytest.mark.parametrize("asset", ["USDC", "EUR", "usd", ""])
async def test_a_virtual_account_in_an_asset_the_bank_does_not_carry_is_refused(
    sim: Sim, asset: str
) -> None:
    response = await sim.api.post(
        "/bank/v1/virtual-accounts", json={"customer_reference": CUSTOMER, "asset": asset}
    )

    assert code_of(response) == (422, "unsupported_asset")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"asset": "USD"},
        {"customer_reference": CUSTOMER},
        {"customer_reference": "", "asset": "USD"},
        {"customer_reference": 7, "asset": "USD"},
        {"customer_reference": CUSTOMER, "asset": None},
    ],
)
async def test_a_malformed_virtual_account_request_is_refused(sim: Sim, body: object) -> None:
    response = await sim.api.post("/bank/v1/virtual-accounts", json=body)

    assert code_of(response) == (422, "invalid_request")


async def test_a_virtual_account_request_may_carry_an_idempotency_key(sim: Sim) -> None:
    body = {"customer_reference": CUSTOMER, "asset": "USD"}
    key = {"Idempotency-Key": "va-1"}

    first = await sim.api.post("/bank/v1/virtual-accounts", json=body, headers=key)
    again = await sim.api.post("/bank/v1/virtual-accounts", json=body, headers=key)
    other = await sim.api.post(
        "/bank/v1/virtual-accounts", json={**body, "asset": "MXN"}, headers=key
    )

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert code_of(other) == (409, "idempotency_conflict")


# --- beneficiaries -------------------------------------------------------------------------


async def test_a_beneficiary_comes_back_as_a_token_and_a_mask(sim: Sim) -> None:
    response = await sim.api.post(
        "/bank/v1/beneficiaries",
        json={
            "customer_reference": CUSTOMER,
            "asset": "USD",
            "holder_name": "Maria Silva",
            "account_number": "000123456789",
            "routing_number": "021000021",
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "id": response.json()["id"],
        "asset": "USD",
        "rail": "ach",
        "holder_name": "Maria Silva",
        "account_mask": MASK + "6789",
    }
    assert re.fullmatch(r"ben_[0-9a-f]{12}", response.json()["id"])


@pytest.mark.parametrize(("asset", "rail", "_delay", "_fee"), SCHEDULE)
async def test_no_provider_endpoint_ever_returns_the_full_account_number(
    sim: Sim, asset: str, rail: str, _delay: int, _fee: str
) -> None:
    number = ACCOUNTS[asset]["account_number"]
    created = await sim.api.post(
        "/bank/v1/beneficiaries",
        json={
            "customer_reference": CUSTOMER,
            "asset": asset,
            "holder_name": "Maria Silva",
            **ACCOUNTS[asset],
        },
    )
    beneficiary = created.json()
    payout = await sim.post_payout(beneficiary["id"], "10.00", asset, key="wd-1")
    await sim.advance(60)
    seen = [
        created,
        payout,
        await sim.post_payout(beneficiary["id"], "10.00", asset, key="wd-1"),
        await sim.api.get(f"/bank/v1/payouts/{payout.json()['id']}"),
        await sim.api.get("/bank/v1/payouts", params={"reference": "wd-1"}),
        await sim.api.get("/bank/v1/transactions", params={"asset": asset, **WINDOW}),
    ]

    assert beneficiary["rail"] == rail
    assert beneficiary["account_mask"] == MASK + number[-4:]
    for response in seen:
        assert response.status_code in (200, 201)
        assert number not in response.text


@pytest.mark.parametrize(
    ("number", "mask"),
    [
        ("1234", MASK + "34"),
        ("12345", MASK + "45"),
        ("1234567", MASK + "567"),
        ("12345678", MASK + "5678"),
    ],
)
async def test_a_short_account_number_is_masked_without_giving_it_all_away(
    sim: Sim, number: str, mask: str
) -> None:
    beneficiary = await sim.beneficiary("USD", account_number=number)

    assert beneficiary["account_mask"] == mask


@pytest.mark.parametrize(("key", "mask"), [("k", MASK), ("ab", MASK + "b"), ("abc", MASK + "c")])
async def test_a_very_short_pix_key_is_masked_without_giving_it_all_away(
    sim: Sim, key: str, mask: str
) -> None:
    beneficiary = await sim.beneficiary("BRL", account_number=key)

    assert beneficiary["account_mask"] == mask


async def test_the_control_endpoint_shows_where_a_payout_is_really_going(sim: Sim) -> None:
    beneficiary = await sim.beneficiary("USD")
    payout = await sim.payout(beneficiary["id"], key="wd-1")

    (inspected,) = await sim.inspect("bank", "payouts")

    assert {key: inspected[key] for key in payout} == payout
    assert inspected["idempotency_key"] == "wd-1"
    assert inspected["beneficiary"] == {
        "id": beneficiary["id"],
        "customer_reference": CUSTOMER,
        "asset": "USD",
        "rail": "ach",
        "holder_name": "Maria Silva",
        "account_number": "000123456789",
        "routing_number": "021000021",
    }


@pytest.mark.parametrize(
    ("asset", "fields"),
    [
        # ACH: 4 to 17 digits, and a 9-digit routing number.
        ("USD", {"account_number": "123"}),
        ("USD", {"account_number": "1" * 18}),
        ("USD", {"account_number": "12345678a"}),  # pragma: allowlist secret
        ("USD", {"account_number": "1234 5678"}),
        ("USD", {"account_number": "١٢٣٤٥٦"}),
        ("USD", {"account_number": ""}),
        ("USD", {"account_number": None}),
        ("USD", {"account_number": 123456789}),
        ("USD", {"routing_number": "02100002"}),
        ("USD", {"routing_number": "0210000211"}),
        ("USD", {"routing_number": "02100002x"}),
        ("USD", {"routing_number": None}),
        ("USD", {"routing_number": 21000021}),
        # SPEI: exactly 18 digits.
        ("MXN", {"account_number": "03218000011835971"}),
        ("MXN", {"account_number": "0321800001183597190"}),
        ("MXN", {"account_number": "03218000011835971x"}),
        # PIX: 1 to 77 characters, no whitespace.
        ("BRL", {"account_number": ""}),
        ("BRL", {"account_number": "k" * 78}),
        ("BRL", {"account_number": "maria silva"}),
        ("BRL", {"account_number": "maria\tsilva"}),
        ("BRL", {"account_number": "maria\u00a0silva"}),
    ],
)
async def test_an_account_of_the_wrong_shape_for_its_rail_is_refused(
    sim: Sim, asset: str, fields: dict[str, object]
) -> None:
    response = await sim.api.post(
        "/bank/v1/beneficiaries",
        json={
            "customer_reference": CUSTOMER,
            "asset": asset,
            "holder_name": "Maria Silva",
            **ACCOUNTS[asset],
            **fields,
        },
    )

    assert code_of(response) == (422, "invalid_account")


@pytest.mark.parametrize(
    ("asset", "number"),
    [
        ("USD", "1234"),
        ("USD", "1" * 17),
        ("MXN", "0" * 18),
        ("BRL", "k"),
        ("BRL", "k" * 77),
        ("BRL", "+5511999990000"),
    ],
)
async def test_an_account_at_the_edge_of_its_rails_shape_is_accepted(
    sim: Sim, asset: str, number: str
) -> None:
    assert (await sim.beneficiary(asset, account_number=number))["asset"] == asset


async def test_a_routing_number_is_needed_only_for_ach(sim: Sim) -> None:
    without = {"customer_reference": CUSTOMER, "holder_name": "Maria Silva"}

    spei = await sim.api.post(
        "/bank/v1/beneficiaries", json={**without, "asset": "MXN", **ACCOUNTS["MXN"]}
    )
    ach = await sim.api.post(
        "/bank/v1/beneficiaries",
        json={**without, "asset": "USD", "account_number": "000123456789"},
    )

    assert spei.status_code == 201
    assert code_of(ach) == (422, "invalid_account")


@pytest.mark.parametrize("asset", ["USDC", "EUR", "usd"])
async def test_a_beneficiary_in_an_asset_the_bank_does_not_carry_is_refused(
    sim: Sim, asset: str
) -> None:
    response = await sim.api.post(
        "/bank/v1/beneficiaries",
        json={
            "customer_reference": CUSTOMER,
            "asset": asset,
            "holder_name": "Maria Silva",
            **ACCOUNTS["USD"],
        },
    )

    assert code_of(response) == (422, "unsupported_asset")


@pytest.mark.parametrize(
    "fields",
    [{"holder_name": ""}, {"holder_name": None}, {"customer_reference": 5}, {"asset": ["USD"]}],
)
async def test_a_malformed_beneficiary_request_is_refused(
    sim: Sim, fields: dict[str, object]
) -> None:
    response = await sim.api.post(
        "/bank/v1/beneficiaries",
        json={
            "customer_reference": CUSTOMER,
            "asset": "USD",
            "holder_name": "Maria Silva",
            **ACCOUNTS["USD"],
            **fields,
        },
    )

    assert code_of(response) == (422, "invalid_request")


async def test_registering_the_same_account_twice_gives_two_tokens(sim: Sim) -> None:
    first = await sim.beneficiary("USD")
    second = await sim.beneficiary("USD")

    assert first["id"] != second["id"]


async def test_a_beneficiary_request_may_carry_an_idempotency_key(sim: Sim) -> None:
    body = {
        "customer_reference": CUSTOMER,
        "asset": "USD",
        "holder_name": "Maria Silva",
        **ACCOUNTS["USD"],
    }
    key = {"Idempotency-Key": "ben-1"}

    first = await sim.api.post("/bank/v1/beneficiaries", json=body, headers=key)
    again = await sim.api.post("/bank/v1/beneficiaries", json=body, headers=key)
    other = await sim.api.post(
        "/bank/v1/beneficiaries", json={**body, "holder_name": "M. Silva"}, headers=key
    )

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert code_of(other) == (409, "idempotency_conflict")


# --- payouts -------------------------------------------------------------------------------


@pytest.mark.parametrize(("asset", "_rail", "_delay", "fee"), SCHEDULE)
async def test_a_new_payout_is_pending_and_carries_its_rails_fee(
    sim: Sim, asset: str, _rail: str, _delay: int, fee: str
) -> None:
    beneficiary = await sim.beneficiary(asset)

    response = await sim.post_payout(beneficiary["id"], "100.00", asset, reference="wd-7")

    assert response.status_code == 201
    assert response.json() == {
        "id": response.json()["id"],
        "status": "pending",
        "beneficiary_id": beneficiary["id"],
        "asset": asset,
        "amount": "100.00",
        "fee": fee,
        "reference": "wd-7",
        "created_at": "2026-01-15T12:00:00Z",
        "settled_at": None,
        "failure_reason": None,
    }
    assert re.fullmatch(r"po_[0-9a-f]{12}", response.json()["id"])


async def test_a_new_payout_has_moved_no_money_yet(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()

    await sim.payout(beneficiary["id"])

    assert await sim.balance("bank", "USD") == "0.00"
    assert (await sim.statement("bank", "USD"))["transactions"] == []


async def test_the_same_idempotency_key_returns_the_first_payout_and_creates_no_second(
    sim: Sim,
) -> None:
    beneficiary = await sim.beneficiary()

    first = await sim.post_payout(beneficiary["id"], key="wd-1")
    again = await sim.post_payout(beneficiary["id"], key="wd-1")

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert [payout["id"] for payout in await sim.inspect("bank", "payouts")] == [first.json()["id"]]


async def test_a_replay_shows_the_payout_as_it_is_now(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    first = await sim.payout(beneficiary["id"], key="wd-1")
    await sim.advance(30)

    again = await sim.post_payout(beneficiary["id"], key="wd-1")

    assert again.status_code == 200
    assert again.json() == {**first, "status": "completed", "settled_at": after(30)}
    assert len(await sim.inspect("bank", "payouts")) == 1


async def test_a_replay_is_recognised_whatever_the_layout_of_its_body(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    fields = {
        "beneficiary_id": beneficiary["id"],
        "asset": "USD",
        "amount": "100.00",
        "reference": "wd-1",
    }
    headers = {"Idempotency-Key": "wd-1", "Content-Type": "application/json"}

    first = await sim.api.post("/bank/v1/payouts", content=json.dumps(fields), headers=headers)
    again = await sim.api.post(
        "/bank/v1/payouts",
        content=json.dumps(dict(reversed(fields.items())), indent=4),
        headers=headers,
    )

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "100.01"},
        {"amount": "100.0"},
        {"reference": "wd-2"},
        {"asset": "MXN"},
        {"beneficiary_id": "ben_000000000000"},
        {"amount": 100},
        {"note": "an extra field"},
    ],
)
async def test_the_same_key_with_a_different_body_is_a_conflict(
    sim: Sim, change: dict[str, object]
) -> None:
    beneficiary = await sim.beneficiary()
    body = {
        "beneficiary_id": beneficiary["id"],
        "asset": "USD",
        "amount": "100.00",
        "reference": "wd-1",
    }
    key = {"Idempotency-Key": "wd-1"}
    first = await sim.api.post("/bank/v1/payouts", json=body, headers=key)

    conflicting = await sim.api.post("/bank/v1/payouts", json={**body, **change}, headers=key)

    assert code_of(conflicting) == (409, "idempotency_conflict")
    (only,) = await sim.inspect("bank", "payouts")
    assert {field: only[field] for field in first.json()} == first.json()


async def test_another_key_with_the_same_body_is_another_payout(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()

    first = await sim.payout(beneficiary["id"], key="wd-1")
    second = await sim.payout(beneficiary["id"], key="wd-2")

    assert first["id"] != second["id"]
    assert len(await sim.inspect("bank", "payouts")) == 2


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": ""}, {"Idempotency-Key": "   "}])
async def test_a_payout_without_an_idempotency_key_is_refused(
    sim: Sim, headers: dict[str, str]
) -> None:
    beneficiary = await sim.beneficiary()

    response = await sim.api.post(
        "/bank/v1/payouts",
        json={
            "beneficiary_id": beneficiary["id"],
            "asset": "USD",
            "amount": "100.00",
            "reference": "wd-1",
        },
        headers=headers,
    )

    assert code_of(response) == (400, "idempotency_key_required")
    assert await sim.inspect("bank", "payouts") == []


async def test_a_refused_payout_does_not_use_up_its_key(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()

    refused = await sim.post_payout(beneficiary["id"], "100.001", key="wd-1")
    corrected = await sim.post_payout(beneficiary["id"], "100.00", key="wd-1")

    assert code_of(refused) == (422, "invalid_amount")
    assert corrected.status_code == 201


async def test_a_key_used_for_a_beneficiary_is_free_for_a_payout(sim: Sim) -> None:
    created = await sim.api.post(
        "/bank/v1/beneficiaries",
        json={
            "customer_reference": CUSTOMER,
            "asset": "USD",
            "holder_name": "Maria Silva",
            **ACCOUNTS["USD"],
        },
        headers={"Idempotency-Key": "shared"},
    )

    payout = await sim.post_payout(created.json()["id"], key="shared")

    assert (created.status_code, payout.status_code) == (201, 201)


async def test_a_payout_to_an_unknown_beneficiary_is_refused(sim: Sim) -> None:
    response = await sim.post_payout("ben_000000000000")

    assert code_of(response) == (404, "beneficiary_not_found")
    assert await sim.inspect("bank", "payouts") == []


@pytest.mark.parametrize("asset", ["MXN", "USDC", "EUR"])
async def test_a_payout_in_another_asset_than_the_beneficiarys_is_refused(
    sim: Sim, asset: str
) -> None:
    beneficiary = await sim.beneficiary("USD")

    response = await sim.post_payout(beneficiary["id"], "100.00", asset)

    assert code_of(response) == (422, "asset_mismatch")
    assert await sim.inspect("bank", "payouts") == []


@pytest.mark.parametrize(
    ("asset", "amount"),
    [
        ("USD", 100),
        ("USD", 100.5),
        ("USD", None),
        ("USD", "-100.00"),
        ("USD", "0"),
        ("USD", "0.00"),
        ("USD", "1e2"),
        ("USD", "100.001"),
        ("USD", "100.000"),
        ("USD", "1,000.00"),
        ("USD", " 100.00"),
        ("USD", ""),
        ("MXN", "5.123"),
        ("BRL", "0.001"),
    ],
)
async def test_a_payout_with_a_malformed_amount_is_refused(
    sim: Sim, asset: str, amount: object
) -> None:
    beneficiary = await sim.beneficiary(asset)

    response = await sim.api.post(
        "/bank/v1/payouts",
        json={
            "beneficiary_id": beneficiary["id"],
            "asset": asset,
            "amount": amount,
            "reference": "wd-1",
        },
        headers={"Idempotency-Key": "wd-1"},
    )

    assert code_of(response) == (422, "invalid_amount")
    assert await sim.inspect("bank", "payouts") == []


async def test_a_payout_without_an_amount_is_refused_as_an_invalid_amount(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()

    response = await sim.api.post(
        "/bank/v1/payouts",
        json={"beneficiary_id": beneficiary["id"], "asset": "USD", "reference": "wd-1"},
        headers={"Idempotency-Key": "wd-1"},
    )

    assert code_of(response) == (422, "invalid_amount")


@pytest.mark.parametrize(
    "change", [{"reference": ""}, {"reference": None}, {"beneficiary_id": 7}, {"asset": None}]
)
async def test_a_malformed_payout_request_is_refused(sim: Sim, change: dict[str, object]) -> None:
    beneficiary = await sim.beneficiary()

    response = await sim.api.post(
        "/bank/v1/payouts",
        json={
            "beneficiary_id": beneficiary["id"],
            "asset": "USD",
            "amount": "100.00",
            "reference": "wd-1",
            **change,
        },
        headers={"Idempotency-Key": "wd-1"},
    )

    assert code_of(response) == (422, "invalid_request")
    assert await sim.inspect("bank", "payouts") == []


@pytest.mark.parametrize(
    ("asset", "amount", "shown"),
    [("USD", "100", "100.00"), ("USD", "100.5", "100.50"), ("MXN", "0250.1", "250.10")],
)
async def test_an_amount_is_shown_with_exactly_its_assets_places(
    sim: Sim, asset: str, amount: str, shown: str
) -> None:
    beneficiary = await sim.beneficiary(asset)

    payout = await sim.payout(beneficiary["id"], amount, asset)

    assert payout["amount"] == shown


# --- settlement ----------------------------------------------------------------------------


@pytest.mark.parametrize(("asset", "_rail", "delay", "fee"), SCHEDULE)
async def test_a_payout_settles_when_its_rails_delay_has_passed_and_not_before(
    sim: Sim, asset: str, _rail: str, delay: int, fee: str
) -> None:
    beneficiary = await sim.beneficiary(asset)
    payout = await sim.payout(beneficiary["id"], "100.00", asset)

    await sim.advance(delay - 0.001)
    just_before = await sim.get_payout(payout["id"])
    balance_just_before = await sim.balance("bank", asset)
    await sim.advance(0.001)
    on_time = await sim.get_payout(payout["id"])

    assert just_before == payout
    assert balance_just_before == "0.00"
    assert on_time == {**payout, "status": "completed", "settled_at": after(delay)}
    # The provider does not refuse for lack of funds: the balance goes below zero.
    assert await sim.balance("bank", asset) == str(-(Decimal("100.00") + Decimal(fee)))


async def test_a_settled_payout_is_two_statement_lines_the_amount_and_the_fee(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    payout = await sim.payout(beneficiary["id"], "100.00", reference="wd-9")

    await sim.advance(30)

    assert (await sim.statement("bank", "USD"))["transactions"] == [
        {
            "id": payout["id"],
            "type": "payout",
            "direction": "debit",
            "asset": "USD",
            "amount": "100.00",
            "reference": "wd-9",
            "related_id": beneficiary["id"],
            "occurred_at": "2026-01-15T12:00:30Z",
        },
        {
            "id": f"{payout['id']}:fee",
            "type": "payout_fee",
            "direction": "debit",
            "asset": "USD",
            "amount": "0.25",
            "reference": "wd-9",
            "related_id": payout["id"],
            "occurred_at": "2026-01-15T12:00:30Z",
        },
    ]


async def test_a_payout_noticed_late_is_settled_when_it_is_noticed(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    payout = await sim.payout(beneficiary["id"])

    await sim.advance(45)

    assert (await sim.get_payout(payout["id"]))["settled_at"] == after(45)
    assert [
        line["occurred_at"] for line in (await sim.statement("bank", "USD"))["transactions"]
    ] == [after(45), after(45)]


async def test_each_payout_settles_on_its_own_schedule(sim: Sim) -> None:
    usd = await sim.payout((await sim.beneficiary("USD"))["id"], "1.00", "USD")
    mxn = await sim.payout((await sim.beneficiary("MXN"))["id"], "1.00", "MXN")
    brl = await sim.payout((await sim.beneficiary("BRL"))["id"], "1.00", "BRL")
    await sim.advance(10)
    late = await sim.payout((await sim.beneficiary("BRL"))["id"], "1.00", "BRL")

    await sim.advance(0.5)

    statuses = {
        name: (await sim.get_payout(payout["id"]))["status"]
        for name, payout in {"usd": usd, "mxn": mxn, "brl": brl, "late": late}.items()
    }
    assert statuses == {"usd": "pending", "mxn": "completed", "brl": "completed", "late": "pending"}


async def test_the_settlement_delays_are_configurable(launch: Launch) -> None:
    sim = await launch(ach_settle_seconds=2, pix_settle_seconds=0)
    usd = await sim.payout((await sim.beneficiary("USD"))["id"], "1.00", "USD")
    brl = await sim.payout((await sim.beneficiary("BRL"))["id"], "1.00", "BRL")

    await sim.advance(0)
    at_once = [(await sim.get_payout(payout["id"]))["status"] for payout in (usd, brl)]
    await sim.advance(2)

    assert at_once == ["pending", "completed"]
    assert (await sim.get_payout(usd["id"]))["status"] == "completed"


@pytest.mark.parametrize(("asset", "_rail", "delay", "_fee"), SCHEDULE)
async def test_a_payout_to_an_account_ending_0000_fails_at_settlement_and_costs_nothing(
    sim: Sim, asset: str, _rail: str, delay: int, _fee: str
) -> None:
    closed = {"USD": "000123450000", "MXN": "032180000118350000", "BRL": "key-0000"}[asset]
    beneficiary = await sim.beneficiary(asset, account_number=closed)
    payout = await sim.payout(beneficiary["id"], "100.00", asset)

    await sim.advance(delay - 0.001)
    just_before = await sim.get_payout(payout["id"])
    await sim.advance(0.001)

    assert just_before["status"] == "pending"
    assert await sim.get_payout(payout["id"]) == {
        **payout,
        "status": "failed",
        "failure_reason": "account_closed",
    }
    assert await sim.balance("bank", asset) == "0.00"
    assert (await sim.statement("bank", asset)) == {
        "asset": asset,
        "from": "2026-01-01T00:00:00Z",
        "to": "2027-01-01T00:00:00Z",
        "transactions": [],
        "closing_balance": "0.00",
    }


@pytest.mark.parametrize("number", ["000123456000", "000012345678", "000000001234"])
async def test_only_an_account_number_that_ends_in_0000_is_closed(sim: Sim, number: str) -> None:
    beneficiary = await sim.beneficiary("USD", account_number=number)
    payout = await sim.payout(beneficiary["id"])

    await sim.advance(30)

    assert (await sim.get_payout(payout["id"]))["status"] == "completed"


async def test_a_settled_payout_stays_settled(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    payout = await sim.payout(beneficiary["id"])
    await sim.advance(30)
    settled = await sim.get_payout(payout["id"])

    await sim.advance(300)

    assert await sim.get_payout(payout["id"]) == settled
    assert len((await sim.statement("bank", "USD"))["transactions"]) == 2
    assert await sim.balance("bank", "USD") == "-100.25"


# --- reading payouts -----------------------------------------------------------------------


async def test_a_payout_is_read_by_id(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    payout = await sim.payout(beneficiary["id"])

    response = await sim.api.get(f"/bank/v1/payouts/{payout['id']}")

    assert (response.status_code, response.json()) == (200, payout)


async def test_an_unknown_payout_is_not_found(sim: Sim) -> None:
    response = await sim.api.get("/bank/v1/payouts/po_000000000000")

    assert code_of(response) == (404, "payout_not_found")


async def test_payouts_are_listed_by_reference(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    first = await sim.payout(beneficiary["id"], reference="wd-1", key="a")
    await sim.payout(beneficiary["id"], reference="wd-2", key="b")
    resubmitted = await sim.payout(beneficiary["id"], reference="wd-1", key="c")

    matching = await sim.api.get("/bank/v1/payouts", params={"reference": "wd-1"})
    nothing = await sim.api.get("/bank/v1/payouts", params={"reference": "wd-3"})

    assert (matching.status_code, matching.json()) == (200, {"payouts": [first, resubmitted]})
    assert (nothing.status_code, nothing.json()) == (200, {"payouts": []})


@pytest.mark.parametrize("query", ["", "?reference="])
async def test_listing_payouts_needs_a_reference(sim: Sim, query: str) -> None:
    response = await sim.api.get(f"/bank/v1/payouts{query}")

    assert code_of(response) == (422, "invalid_request")


# --- deposits ------------------------------------------------------------------------------


async def test_a_deposit_credits_the_balance_and_appears_in_the_statement(sim: Sim) -> None:
    account = await sim.virtual_account("USD")

    deposit = await sim.bank_deposit(
        account["id"], "250.00", sender_name="Joao Souza", reference="INV-2041"
    )

    assert deposit == {
        "id": deposit["id"],
        "virtual_account_id": account["id"],
        "customer_reference": CUSTOMER,
        "asset": "USD",
        "amount": "250.00",
        "sender_name": "Joao Souza",
        "reference": "INV-2041",
        "status": "received",
        "received_at": "2026-01-15T12:00:00Z",
        "returned_at": None,
        "return_reason": None,
    }
    assert re.fullmatch(r"dep_[0-9a-f]{12}", deposit["id"])
    assert await sim.balance("bank", "USD") == "250.00"
    assert await sim.statement("bank", "USD", WINDOW["from"], WINDOW["to"]) == {
        "asset": "USD",
        **WINDOW,
        "transactions": [
            {
                "id": deposit["id"],
                "type": "deposit",
                "direction": "credit",
                "asset": "USD",
                "amount": "250.00",
                "reference": "INV-2041",
                "related_id": account["id"],
                "occurred_at": "2026-01-15T12:00:00Z",
            }
        ],
        "closing_balance": "250.00",
    }
    assert await sim.inspect("bank", "deposits") == [deposit]


async def test_a_deposit_is_in_the_asset_of_its_virtual_account(sim: Sim) -> None:
    account = await sim.virtual_account("MXN")

    deposit = await sim.bank_deposit(account["id"], "1500")

    assert (deposit["asset"], deposit["amount"]) == ("MXN", "1500.00")
    assert await sim.control("GET", "/bank/balances") == {
        "balances": {"USD": "0.00", "MXN": "1500.00", "BRL": "0.00"}
    }


async def test_a_deposit_to_an_unknown_virtual_account_is_accepted(sim: Sim) -> None:
    deposit = await sim.bank_deposit("va_000000000000", "75.00")

    assert deposit["virtual_account_id"] == "va_000000000000"
    assert deposit["customer_reference"] is None
    assert deposit["asset"] == "USD"
    assert await sim.balance("bank", "USD") == "75.00"
    (line,) = (await sim.statement("bank", "USD"))["transactions"]
    assert (line["type"], line["related_id"]) == ("deposit", "va_000000000000")


async def test_an_unattributable_deposit_can_name_its_asset(sim: Sim) -> None:
    deposit = await sim.bank_deposit("va_000000000000", "75.00", asset="BRL")

    assert deposit["asset"] == "BRL"
    assert await sim.balance("bank", "BRL") == "75.00"


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ({"amount": 250}, (422, "invalid_amount")),
        ({"amount": "250.001"}, (422, "invalid_amount")),
        ({"amount": "-1.00"}, (422, "invalid_amount")),
        ({"amount": "0.00"}, (422, "invalid_amount")),
        ({"asset": "MXN"}, (422, "asset_mismatch")),
        ({"asset": "USDC"}, (422, "unsupported_asset")),
        ({"sender_name": ""}, (422, "invalid_request")),
        ({"sender_name": None}, (422, "invalid_request")),
        ({"reference": 7}, (422, "invalid_request")),
        ({"virtual_account_id": None}, (422, "invalid_request")),
    ],
)
async def test_a_malformed_deposit_is_refused_and_credits_nothing(
    sim: Sim, change: dict[str, object], refusal: tuple[int, str]
) -> None:
    account = await sim.virtual_account("USD")

    response = await sim.anonymous.post(
        "/_control/bank/deposits",
        json={
            "virtual_account_id": account["id"],
            "amount": "250.00",
            "sender_name": "Joao Souza",
            "reference": "INV-2041",
            **change,
        },
    )

    assert code_of(response) == refusal
    assert await sim.balance("bank", "USD") == "0.00"
    assert await sim.inspect("bank", "deposits") == []


async def test_an_unattributable_deposit_in_an_unsupported_asset_is_refused(sim: Sim) -> None:
    response = await sim.anonymous.post(
        "/_control/bank/deposits",
        json={
            "virtual_account_id": "va_000000000000",
            "amount": "1.00",
            "sender_name": "Joao Souza",
            "reference": "INV-2041",
            "asset": "USDC",
        },
    )

    assert code_of(response) == (422, "unsupported_asset")


async def test_a_returned_deposit_is_debited_again(sim: Sim) -> None:
    account = await sim.virtual_account("USD")
    deposit = await sim.bank_deposit(account["id"], "250.00")
    await sim.advance(3600)

    returned = await sim.control(
        "POST", f"/bank/deposits/{deposit['id']}/return", {"reason": "sender_recall"}
    )

    assert returned == {
        **deposit,
        "status": "returned",
        "returned_at": "2026-01-15T13:00:00Z",
        "return_reason": "sender_recall",
    }
    assert await sim.balance("bank", "USD") == "0.00"
    statement = await sim.statement("bank", "USD")
    assert statement["closing_balance"] == "0.00"
    assert statement["transactions"][1] == {
        "id": f"{deposit['id']}:return",
        "type": "deposit_return",
        "direction": "debit",
        "asset": "USD",
        "amount": "250.00",
        "reference": "INV-2041",
        "related_id": deposit["id"],
        "occurred_at": "2026-01-15T13:00:00Z",
    }
    assert await sim.inspect("bank", "deposits") == [returned]


async def test_a_deposit_cannot_be_returned_twice(sim: Sim) -> None:
    deposit = await sim.bank_deposit((await sim.virtual_account())["id"], "250.00")
    path = f"/_control/bank/deposits/{deposit['id']}/return"
    await sim.anonymous.post(path, json={"reason": "sender_recall"})

    again = await sim.anonymous.post(path, json={"reason": "sender_recall"})

    assert code_of(again) == (409, "deposit_already_returned")
    assert await sim.balance("bank", "USD") == "0.00"
    assert len((await sim.statement("bank", "USD"))["transactions"]) == 2


async def test_a_return_can_take_the_balance_below_zero(sim: Sim) -> None:
    deposit = await sim.bank_deposit((await sim.virtual_account())["id"], "100.00")
    await sim.payout((await sim.beneficiary())["id"], "80.00")
    await sim.advance(30)

    await sim.control("POST", f"/bank/deposits/{deposit['id']}/return", {"reason": "fraud"})

    assert await sim.balance("bank", "USD") == "-80.25"


async def test_returning_an_unknown_deposit_is_not_found(sim: Sim) -> None:
    response = await sim.anonymous.post(
        "/_control/bank/deposits/dep_000000000000/return", json={"reason": "sender_recall"}
    )

    assert code_of(response) == (404, "deposit_not_found")


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": None}, {"reason": 3}])
async def test_a_return_needs_a_reason(sim: Sim, body: object) -> None:
    deposit = await sim.bank_deposit((await sim.virtual_account())["id"], "250.00")

    response = await sim.anonymous.post(
        f"/_control/bank/deposits/{deposit['id']}/return", json=body
    )

    assert code_of(response) == (422, "invalid_request")
    assert await sim.balance("bank", "USD") == "250.00"


# --- the statement -------------------------------------------------------------------------


async def test_the_statement_matches_the_contracts_example(launch: Launch) -> None:
    sim = await launch(start_time="2026-01-15T09:30:00Z")
    account = await sim.virtual_account("USD")
    deposit = await sim.bank_deposit(account["id"], "250.00", reference="INV-2041")
    await sim.advance(9000)
    beneficiary = await sim.beneficiary("USD")
    payout = await sim.payout(
        beneficiary["id"], "100.00", reference="0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f"
    )
    await sim.advance(30)

    response = await sim.api.get("/bank/v1/transactions", params={"asset": "USD", **WINDOW})

    assert response.status_code == 200
    assert response.json() == {
        "asset": "USD",
        "from": "2026-01-15T00:00:00Z",
        "to": "2026-01-16T00:00:00Z",
        "transactions": [
            {
                "id": deposit["id"],
                "type": "deposit",
                "direction": "credit",
                "asset": "USD",
                "amount": "250.00",
                "reference": "INV-2041",
                "related_id": account["id"],
                "occurred_at": "2026-01-15T09:30:00Z",
            },
            {
                "id": payout["id"],
                "type": "payout",
                "direction": "debit",
                "asset": "USD",
                "amount": "100.00",
                "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f",
                "related_id": beneficiary["id"],
                "occurred_at": "2026-01-15T12:00:30Z",
            },
            {
                "id": f"{payout['id']}:fee",
                "type": "payout_fee",
                "direction": "debit",
                "asset": "USD",
                "amount": "0.25",
                "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f",
                "related_id": payout["id"],
                "occurred_at": "2026-01-15T12:00:30Z",
            },
        ],
        "closing_balance": "149.75",
    }


async def test_the_statement_window_includes_its_start_and_excludes_its_end(sim: Sim) -> None:
    account = await sim.virtual_account("USD")
    for amount in ("1.00", "2.00", "4.00"):
        await sim.bank_deposit(account["id"], amount)
        await sim.advance(10)

    async def amounts(start: float, end: float) -> tuple[list[str], str]:
        statement = await sim.statement("bank", "USD", after(start), after(end))
        return [line["amount"] for line in statement["transactions"]], statement["closing_balance"]

    assert await amounts(0, 30) == (["1.00", "2.00", "4.00"], "7.00")
    assert await amounts(0, 20) == (["1.00", "2.00"], "3.00")
    assert await amounts(0, 20.000001) == (["1.00", "2.00", "4.00"], "7.00")
    assert await amounts(10, 20) == (["2.00"], "3.00")
    assert await amounts(0.000001, 10) == ([], "1.00")
    assert await amounts(10, 10) == ([], "1.00")
    assert await amounts(-60, 0) == ([], "0.00")
    assert await amounts(20, 3600) == (["4.00"], "7.00")


async def test_the_closing_balance_is_the_balance_at_the_end_of_the_window_even_if_negative(
    sim: Sim,
) -> None:
    await sim.payout((await sim.beneficiary())["id"], "100.00")
    await sim.advance(30)
    await sim.advance(10)
    await sim.bank_deposit((await sim.virtual_account())["id"], "500.00")

    overdrawn = await sim.statement("bank", "USD", after(0), after(40))
    later = await sim.statement("bank", "USD", after(41), after(60))

    assert [line["type"] for line in overdrawn["transactions"]] == ["payout", "payout_fee"]
    assert overdrawn["closing_balance"] == "-100.25"
    # A window with nothing in it still closes on everything that came before it.
    assert later["transactions"] == []
    assert later["closing_balance"] == "399.75"


async def test_each_asset_has_a_statement_of_its_own(sim: Sim) -> None:
    await sim.bank_deposit((await sim.virtual_account("USD"))["id"], "10.00")
    await sim.bank_deposit((await sim.virtual_account("BRL"))["id"], "20.00")

    brl = await sim.statement("bank", "BRL")

    assert [line["amount"] for line in brl["transactions"]] == ["20.00"]
    assert brl["closing_balance"] == "20.00"
    assert (await sim.statement("bank", "MXN"))["closing_balance"] == "0.00"


async def test_the_statement_echoes_its_window_in_utc(sim: Sim) -> None:
    response = await sim.api.get(
        "/bank/v1/transactions",
        params={
            "asset": "USD",
            "from": "2026-01-15T02:00:00+02:00",
            "to": "2026-01-15T19:00:00-05:00",
        },
    )

    assert response.status_code == 200
    assert (response.json()["from"], response.json()["to"]) == (
        "2026-01-15T00:00:00Z",
        "2026-01-16T00:00:00Z",
    )


@pytest.mark.parametrize(
    ("params", "refusal"),
    [
        ({"asset": "USDC", **WINDOW}, (422, "unsupported_asset")),
        ({"asset": "EUR", **WINDOW}, (422, "unsupported_asset")),
        (WINDOW, (422, "invalid_request")),
        ({"asset": "USD", "from": WINDOW["from"]}, (422, "invalid_request")),
        ({"asset": "USD", "to": WINDOW["to"]}, (422, "invalid_request")),
        ({"asset": "USD", "from": "yesterday", "to": WINDOW["to"]}, (422, "invalid_request")),
        (
            {"asset": "USD", "from": "2026-01-15T00:00:00", "to": WINDOW["to"]},
            (422, "invalid_request"),
        ),
        ({"asset": "USD", "from": WINDOW["from"], "to": "2026-01-16"}, (422, "invalid_request")),
        ({"asset": "USD", "from": WINDOW["to"], "to": WINDOW["from"]}, (422, "invalid_request")),
    ],
)
async def test_a_malformed_statement_request_is_refused(
    sim: Sim, params: dict[str, str], refusal: tuple[int, str]
) -> None:
    response = await sim.api.get("/bank/v1/transactions", params=params)

    assert code_of(response) == refusal


# --- reproducibility -----------------------------------------------------------------------


async def test_the_same_seed_invents_the_same_identifiers_and_another_seed_does_not(
    launch: Launch,
) -> None:
    async def invented(sim: Sim) -> list[str]:
        account = await sim.virtual_account("USD")
        beneficiary = await sim.beneficiary("USD")
        payout = await sim.payout(beneficiary["id"])
        return [account["id"], account["account_number"], beneficiary["id"], payout["id"]]

    first = await invented(await launch(seed=7))
    second = await invented(await launch(seed=7))
    third = await invented(await launch(seed=8))

    assert first == second
    assert set(first).isdisjoint(third)


# --- the books as a whole ------------------------------------------------------------------

ASSETS = ("USD", "MXN", "BRL")
DELAY_MS = {"USD": 30_000, "MXN": 5_000, "BRL": 1_000}
FEE_MINOR = {"USD": 25, "MXN": 500, "BRL": 10}


@dataclass(frozen=True)
class Step:
    kind: Literal["deposit", "payout", "return", "advance"]
    asset: str
    minor: int
    closed: bool
    pick: int
    milliseconds: int


steps = st.lists(
    st.builds(
        Step,
        kind=st.sampled_from(["deposit", "payout", "return", "advance"]),
        asset=st.sampled_from(ASSETS),
        minor=st.integers(1, 5_000_00),
        closed=st.booleans(),
        pick=st.integers(0, 50),
        milliseconds=st.sampled_from([0, 500, 1_000, 4_999, 5_000, 29_999, 30_000, 31_000]),
    ),
    max_size=30,
)


def money(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100}.{abs(minor) % 100:02d}"


def minor_of(text: str) -> int:
    whole, _, cents = text.lstrip("-").partition(".")
    assert len(cents) == 2
    return (-1 if text.startswith("-") else 1) * (int(whole) * 100 + int(cents))


async def run_bank(sequence: list[Step]) -> None:
    """Drive the bank and a plain model side by side, then compare every view of the books."""
    async with running_sim(bank_webhook_url=None, custody_webhook_url=None) as sim:
        accounts = {asset: (await sim.virtual_account(asset))["id"] for asset in ASSETS}
        open_account = {asset: (await sim.beneficiary(asset))["id"] for asset in ASSETS}
        closed_number = {"USD": "000123450000", "MXN": "032180000118350000", "BRL": "key-0000"}
        closed_account = {
            asset: (await sim.beneficiary(asset, account_number=closed_number[asset]))["id"]
            for asset in ASSETS
        }

        now_ms = 0
        deposits: list[tuple[str, str, int]] = []  # id, asset, minor
        returned: set[str] = set()
        payouts: list[tuple[str, str, int, bool, int]] = []  # id, asset, minor, closed, created

        for step in sequence:
            if step.kind == "deposit":
                deposit = await sim.bank_deposit(accounts[step.asset], money(step.minor))
                deposits.append((deposit["id"], step.asset, step.minor))
            elif step.kind == "payout":
                target = (closed_account if step.closed else open_account)[step.asset]
                payout = await sim.payout(target, money(step.minor), step.asset)
                payouts.append((payout["id"], step.asset, step.minor, step.closed, now_ms))
            elif step.kind == "return":
                candidates = [d for d in deposits if d[0] not in returned]
                if not candidates:
                    continue
                deposit_id = candidates[step.pick % len(candidates)][0]
                await sim.control("POST", f"/bank/deposits/{deposit_id}/return", {"reason": "x"})
                returned.add(deposit_id)
            else:
                await sim.advance(step.milliseconds / 1000)
                now_ms += step.milliseconds

        expected = dict.fromkeys(ASSETS, 0)
        for deposit_id, asset, minor in deposits:
            expected[asset] += 0 if deposit_id in returned else minor
        expected_status = {}
        for payout_id, asset, minor, closed, created_ms in payouts:
            # Time moves only through advance, and every advance settles what is due.
            due = now_ms - created_ms >= DELAY_MS[asset]
            expected_status[payout_id] = ("failed" if closed else "completed") if due else "pending"
            if expected_status[payout_id] == "completed":
                expected[asset] -= minor + FEE_MINOR[asset]

        balances = (await sim.control("GET", "/bank/balances"))["balances"]
        listed = await sim.inspect("bank", "payouts")
        assert {payout["id"]: payout["status"] for payout in listed} == expected_status
        for asset in ASSETS:
            statement = await sim.statement("bank", asset)
            lines = statement["transactions"]
            signed = [
                minor_of(line["amount"]) * (1 if line["direction"] == "credit" else -1)
                for line in lines
            ]
            # Three views of one number: the model, the running balance, the statement.
            assert balances[asset] == money(expected[asset])
            assert statement["closing_balance"] == money(expected[asset])
            assert sum(signed) == expected[asset]
            assert all(minor_of(line["amount"]) > 0 for line in lines)
            times = [datetime.fromisoformat(line["occurred_at"]) for line in lines]
            assert times == sorted(times)
            # A pending or failed payout is not a transaction; a completed one is two.
            completed = {p for p, status in expected_status.items() if status == "completed"}
            assert {line["id"] for line in lines if line["type"] == "payout"} == {
                payout_id for payout_id, a, *_ in payouts if a == asset and payout_id in completed
            }
            assert len([line for line in lines if line["type"] == "payout_fee"]) == len(
                [line for line in lines if line["type"] == "payout"]
            )
            # The balance at any earlier instant is the sum of what had happened by then.
            for cut in sorted(set(times)):
                earlier = await sim.statement(
                    "bank", asset, "2026-01-01T00:00:00Z", format_time(cut)
                )
                assert minor_of(earlier["closing_balance"]) == sum(
                    amount for amount, time in zip(signed, times, strict=True) if time < cut
                )


@hypothesis_settings(max_examples=40, deadline=None)
@given(sequence=steps)
def test_the_balance_always_equals_the_sum_of_the_statement(sequence: list[Step]) -> None:
    asyncio.run(run_bank(sequence))

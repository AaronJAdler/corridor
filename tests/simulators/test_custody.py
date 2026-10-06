"""The custody simulator: deposit addresses, a chain of blocks, deposits and withdrawals."""

import asyncio
import re
import string
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import pytest
from hypothesis import assume, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from corridor_sim.clock import format_time
from corridor_sim.custody import is_valid_address
from tests.simulators.conftest import (
    API_KEY,
    CUSTOMER,
    START,
    Sim,
    address_with_body,
    running_sim,
)

Launch = Callable[..., Awaitable[Sim]]

BASE32 = string.ascii_lowercase + "234567"
# The address the contract gives as an example, and says is valid.
CONTRACT_EXAMPLE = "sim1corridorexampledepositaddress234f24b41da"
SOMEWHERE = address_with_body("destination".ljust(32, "a"))
SENDER = address_with_body("sender".ljust(32, "a"))
# An address whose body begins with "dead" is rejected by the network.
DEAD = address_with_body("dead".ljust(32, "b"))

WINDOW = {"from": "2026-01-15T00:00:00Z", "to": "2026-01-16T00:00:00Z"}

CUSTODY_ROUTES = [
    ("POST", "/custody/v1/addresses"),
    ("POST", "/custody/v1/withdrawals"),
    ("GET", "/custody/v1/withdrawals/wd_000000000000"),
    ("GET", "/custody/v1/withdrawals?reference=wd-1"),
    (
        "GET",
        "/custody/v1/transactions?asset=USDC&from=2026-01-15T00:00:00Z&to=2026-01-16T00:00:00Z",
    ),
]


def code_of(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def after(seconds: float) -> str:
    return format_time(START + timedelta(seconds=seconds))


async def deposit_status(sim: Sim, deposit_id: str) -> dict[str, Any]:
    (deposit,) = [d for d in await sim.inspect("custody", "deposits") if d["id"] == deposit_id]
    return deposit


# --- authentication ------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), CUSTODY_ROUTES)
async def test_a_custody_route_refuses_a_request_without_the_api_key(
    sim: Sim, method: str, path: str
) -> None:
    response = await sim.anonymous.request(method, path, json={})

    assert code_of(response) == (401, "unauthorized")


@pytest.mark.parametrize(("method", "path"), CUSTODY_ROUTES)
async def test_a_custody_route_refuses_a_wrong_api_key(sim: Sim, method: str, path: str) -> None:
    response = await sim.anonymous.request(
        method, path, json={}, headers={"Authorization": f"Bearer {API_KEY}-not"}
    )

    assert code_of(response) == (401, "unauthorized")


@pytest.mark.parametrize("path", ["/custody/withdrawals", "/custody/deposits", "/custody/balances"])
async def test_the_control_endpoints_need_no_credentials(sim: Sim, path: str) -> None:
    response = await sim.anonymous.get(f"/_control{path}")

    assert response.status_code == 200


# --- the address format --------------------------------------------------------------------


def test_the_contracts_example_address_is_valid() -> None:
    assert is_valid_address(CONTRACT_EXAMPLE)


@given(body=st.text(alphabet=BASE32, min_size=32, max_size=32))
def test_any_body_with_its_checksum_is_a_valid_address(body: str) -> None:
    assert is_valid_address(address_with_body(body))


@given(
    body=st.text(alphabet=BASE32, min_size=32, max_size=32),
    position=st.integers(0, 43),
    replacement=st.sampled_from(BASE32 + "0189" + "ABCDEFSIM" + " -_.") | st.characters(),
)
def test_changing_any_one_character_of_an_address_makes_it_invalid(
    body: str, position: int, replacement: str
) -> None:
    address = address_with_body(body)
    assume(replacement != address[position])

    tampered = address[:position] + replacement + address[position + 1 :]

    assert not is_valid_address(tampered)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "sim1",
        CONTRACT_EXAMPLE[:-1],
        CONTRACT_EXAMPLE + "a",
        " " + CONTRACT_EXAMPLE,
        CONTRACT_EXAMPLE + "\n",
        CONTRACT_EXAMPLE.upper(),
        "SIM1" + CONTRACT_EXAMPLE[4:],
        "sim2" + CONTRACT_EXAMPLE[4:],
        CONTRACT_EXAMPLE[:36] + CONTRACT_EXAMPLE[36:].upper(),
        # Bodies outside the base32 alphabet, each with the checksum that body would have.
        address_with_body("0" * 32),
        address_with_body("A" * 32),
        address_with_body("corridor-example-deposit-address"),
    ],
)
def test_a_malformed_address_is_invalid(text: str) -> None:
    assert not is_valid_address(text)


@pytest.mark.parametrize("value", [None, 42, CONTRACT_EXAMPLE.encode(), [CONTRACT_EXAMPLE]])
def test_only_a_string_can_be_an_address(value: object) -> None:
    assert not is_valid_address(value)


# --- deposit addresses ---------------------------------------------------------------------


async def test_an_address_is_created_once_and_returned_again(sim: Sim) -> None:
    body = {"customer_reference": CUSTOMER, "asset": "USDC"}

    first = await sim.api.post("/custody/v1/addresses", json=body)
    again = await sim.api.post("/custody/v1/addresses", json=body)

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert first.json() == {
        "id": first.json()["id"],
        "customer_reference": CUSTOMER,
        "asset": "USDC",
        "network": "simchain",
        "address": first.json()["address"],
    }
    assert re.fullmatch(r"addr_[0-9a-f]{12}", first.json()["id"])


async def test_a_generated_address_is_valid_by_the_contracts_checksum_rule(sim: Sim) -> None:
    address = (await sim.address())["address"]

    assert re.fullmatch(r"sim1[a-z2-7]{32}[0-9a-f]{8}", address)
    assert address == address_with_body(address[4:36])
    assert is_valid_address(address)


async def test_each_customer_has_an_address_of_their_own(sim: Sim) -> None:
    first = await sim.address("customer-1")
    second = await sim.address("customer-2")

    assert first["id"] != second["id"]
    assert first["address"] != second["address"]
    assert await sim.address("customer-1") == first


@pytest.mark.parametrize("asset", ["USD", "MXN", "EUR", "usdc", ""])
async def test_an_address_in_an_asset_the_custodian_does_not_carry_is_refused(
    sim: Sim, asset: str
) -> None:
    response = await sim.api.post(
        "/custody/v1/addresses", json={"customer_reference": CUSTOMER, "asset": asset}
    )

    assert code_of(response) == (422, "unsupported_asset")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"asset": "USDC"},
        {"customer_reference": "", "asset": "USDC"},
        {"customer_reference": CUSTOMER},
    ],
)
async def test_a_malformed_address_request_is_refused(sim: Sim, body: object) -> None:
    response = await sim.api.post("/custody/v1/addresses", json=body)

    assert code_of(response) == (422, "invalid_request")


async def test_an_address_request_may_carry_an_idempotency_key(sim: Sim) -> None:
    body = {"customer_reference": CUSTOMER, "asset": "USDC"}
    key = {"Idempotency-Key": "addr-1"}

    first = await sim.api.post("/custody/v1/addresses", json=body, headers=key)
    again = await sim.api.post("/custody/v1/addresses", json=body, headers=key)
    other = await sim.api.post(
        "/custody/v1/addresses", json={**body, "customer_reference": "someone-else"}, headers=key
    )

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert code_of(other) == (409, "idempotency_conflict")


# --- deposits ------------------------------------------------------------------------------


async def test_a_deposit_is_detected_at_once_and_credits_nothing_yet(sim: Sim) -> None:
    address = await sim.address()

    deposit = await sim.chain_deposit(address["address"], "40.5", from_address=SENDER)

    assert deposit == {
        "id": deposit["id"],
        "address_id": address["id"],
        "address": address["address"],
        "customer_reference": CUSTOMER,
        "asset": "USDC",
        "amount": "40.500000",
        "tx_hash": deposit["tx_hash"],
        "from_address": SENDER,
        "status": "detected",
        "confirmations": 0,
        "detected_at": "2026-01-15T12:00:00Z",
        "confirmed_at": None,
        "failure_reason": None,
    }
    assert re.fullmatch(r"dep_[0-9a-f]{12}", deposit["id"])
    assert re.fullmatch(r"[0-9a-f]{64}", deposit["tx_hash"])
    assert await sim.balance("custody", "USDC") == "0.000000"
    assert (await sim.statement("custody", "USDC"))["transactions"] == []
    assert await sim.inspect("custody", "deposits") == [deposit]


async def test_a_deposit_is_confirmed_when_three_blocks_have_passed_and_not_before(
    sim: Sim,
) -> None:
    address = await sim.address()
    deposit = await sim.chain_deposit(address["address"], "40.500000")

    # A block every two seconds: the third arrives at six.
    await sim.advance(5.999)
    just_before = await deposit_status(sim, deposit["id"])
    balance_just_before = await sim.balance("custody", "USDC")
    await sim.advance(0.001)

    assert just_before == {**deposit, "confirmations": 2}
    assert balance_just_before == "0.000000"
    assert await deposit_status(sim, deposit["id"]) == {
        **deposit,
        "status": "confirmed",
        "confirmations": 3,
        "confirmed_at": "2026-01-15T12:00:06Z",
    }
    assert await sim.balance("custody", "USDC") == "40.500000"
    assert await sim.statement("custody", "USDC", WINDOW["from"], WINDOW["to"]) == {
        "asset": "USDC",
        **WINDOW,
        "transactions": [
            {
                "id": deposit["id"],
                "type": "deposit",
                "direction": "credit",
                "asset": "USDC",
                "amount": "40.500000",
                "reference": None,
                "related_id": address["id"],
                "occurred_at": "2026-01-15T12:00:06Z",
                "tx_hash": deposit["tx_hash"],
            }
        ],
        "closing_balance": "40.500000",
    }


async def test_a_deposit_is_confirmed_by_mining_three_blocks(sim: Sim) -> None:
    deposit = await sim.chain_deposit((await sim.address())["address"], "1.000000")

    height_after_two = await sim.mine(2)
    after_two = await deposit_status(sim, deposit["id"])
    height_after_three = await sim.mine(1)

    assert (height_after_two, height_after_three) == (2, 3)
    assert (after_two["status"], after_two["confirmations"]) == ("detected", 2)
    assert (await deposit_status(sim, deposit["id"]))["status"] == "confirmed"
    assert await sim.balance("custody", "USDC") == "1.000000"
    # Mining moved the chain, not the clock.
    assert (await sim.control("GET", "/clock"))["now"] == "2026-01-15T12:00:00Z"


async def test_blocks_from_the_clock_and_mined_blocks_add_up(sim: Sim) -> None:
    deposit = await sim.chain_deposit((await sim.address())["address"], "1.000000")

    await sim.advance(4)
    after_the_clock = await deposit_status(sim, deposit["id"])
    height = await sim.mine(1)

    assert after_the_clock["confirmations"] == 2
    assert height == 3
    assert (await deposit_status(sim, deposit["id"]))["status"] == "confirmed"


async def test_confirmations_count_blocks_not_seconds(sim: Sim) -> None:
    await sim.advance(3)
    # Detected at three seconds, a second before the block at four.
    deposit = await sim.chain_deposit((await sim.address())["address"], "1.000000")

    await sim.advance(4.999)
    just_before = await deposit_status(sim, deposit["id"])
    await sim.advance(0.001)

    assert (just_before["status"], just_before["confirmations"]) == ("detected", 2)
    assert await deposit_status(sim, deposit["id"]) == {
        **deposit,
        "status": "confirmed",
        "confirmations": 3,
        "confirmed_at": after(8),
    }


async def test_a_confirmed_deposit_keeps_gaining_confirmations_and_is_credited_once(
    sim: Sim,
) -> None:
    deposit = await sim.chain_deposit((await sim.address())["address"], "1.000000")

    await sim.advance(60)

    confirmed = await deposit_status(sim, deposit["id"])
    assert (confirmed["status"], confirmed["confirmations"]) == ("confirmed", 30)
    assert confirmed["confirmed_at"] == after(60)
    assert await sim.balance("custody", "USDC") == "1.000000"
    assert len((await sim.statement("custody", "USDC"))["transactions"]) == 1


async def test_the_block_time_and_the_confirmations_needed_are_configurable(
    launch: Launch,
) -> None:
    sim = await launch(block_seconds=0.5, confirmations=2)
    deposit = await sim.chain_deposit((await sim.address())["address"], "1.000000")

    await sim.advance(0.999)
    just_before = await deposit_status(sim, deposit["id"])
    await sim.advance(0.001)

    assert (just_before["status"], just_before["confirmations"]) == ("detected", 1)
    assert (await deposit_status(sim, deposit["id"]))["status"] == "confirmed"


async def test_a_deposit_to_an_address_the_custodian_never_issued_is_not_found(sim: Sim) -> None:
    response = await sim.anonymous.post(
        "/_control/custody/deposits",
        json={"address": SOMEWHERE, "amount": "1.000000", "from_address": SENDER},
    )

    assert code_of(response) == (404, "address_not_found")
    assert await sim.inspect("custody", "deposits") == []


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ({"amount": 1}, (422, "invalid_amount")),
        ({"amount": "1.0000001"}, (422, "invalid_amount")),
        ({"amount": "0.000000"}, (422, "invalid_amount")),
        ({"amount": "-1.000000"}, (422, "invalid_amount")),
        ({"amount": None}, (422, "invalid_amount")),
        ({"from_address": "not-an-address"}, (422, "invalid_address")),
        (
            {"from_address": SENDER[:-1] + ("0" if SENDER[-1] != "0" else "1")},
            (422, "invalid_address"),
        ),
        ({"from_address": None}, (422, "invalid_address")),
        ({"address": None}, (422, "invalid_request")),
        ({"address": 7}, (422, "invalid_request")),
    ],
)
async def test_a_malformed_chain_deposit_is_refused(
    sim: Sim, change: dict[str, object], refusal: tuple[int, str]
) -> None:
    address = await sim.address()

    response = await sim.anonymous.post(
        "/_control/custody/deposits",
        json={
            "address": address["address"],
            "amount": "1.000000",
            "from_address": SENDER,
            **change,
        },
    )

    assert code_of(response) == refusal
    assert await sim.inspect("custody", "deposits") == []


async def test_a_dropped_deposit_never_credits_anything(sim: Sim) -> None:
    deposit = await sim.chain_deposit((await sim.address())["address"], "40.500000")
    await sim.mine(2)

    dropped = await sim.control("POST", f"/custody/deposits/{deposit['id']}/drop")
    await sim.mine(10)
    await sim.advance(60)

    assert dropped == {
        **deposit,
        "status": "failed",
        "confirmations": 0,
        "failure_reason": "dropped",
    }
    assert await deposit_status(sim, deposit["id"]) == dropped
    assert await sim.balance("custody", "USDC") == "0.000000"
    assert (await sim.statement("custody", "USDC")) == {
        "asset": "USDC",
        "from": "2026-01-01T00:00:00Z",
        "to": "2027-01-01T00:00:00Z",
        "transactions": [],
        "closing_balance": "0.000000",
    }


async def test_a_deposit_cannot_be_dropped_after_it_is_confirmed(sim: Sim) -> None:
    deposit = await sim.chain_deposit((await sim.address())["address"], "40.500000")
    await sim.mine(3)

    response = await sim.anonymous.post(f"/_control/custody/deposits/{deposit['id']}/drop")

    assert code_of(response) == (409, "deposit_already_final")
    assert (await deposit_status(sim, deposit["id"]))["status"] == "confirmed"
    assert await sim.balance("custody", "USDC") == "40.500000"


async def test_a_deposit_cannot_be_dropped_twice(sim: Sim) -> None:
    deposit = await sim.chain_deposit((await sim.address())["address"], "40.500000")
    path = f"/_control/custody/deposits/{deposit['id']}/drop"
    await sim.anonymous.post(path)

    again = await sim.anonymous.post(path)

    assert code_of(again) == (409, "deposit_already_final")


async def test_dropping_an_unknown_deposit_is_not_found(sim: Sim) -> None:
    response = await sim.anonymous.post("/_control/custody/deposits/dep_000000000000/drop")

    assert code_of(response) == (404, "deposit_not_found")


# --- withdrawals ---------------------------------------------------------------------------


async def test_a_new_withdrawal_is_pending_and_carries_the_network_fee(sim: Sim) -> None:
    response = await sim.post_withdrawal(SOMEWHERE, "25", reference="wd-7")

    assert response.status_code == 201
    assert response.json() == {
        "id": response.json()["id"],
        "status": "pending",
        "asset": "USDC",
        "amount": "25.000000",
        "network_fee": "0.150000",
        "to_address": SOMEWHERE,
        "reference": "wd-7",
        "tx_hash": None,
        "confirmations": 0,
        "created_at": "2026-01-15T12:00:00Z",
        "completed_at": None,
        "failure_reason": None,
    }
    assert re.fullmatch(r"wd_[0-9a-f]{12}", response.json()["id"])
    assert await sim.balance("custody", "USDC") == "0.000000"


async def test_a_withdrawal_is_broadcast_at_the_next_block_and_completed_three_blocks_later(
    sim: Sim,
) -> None:
    withdrawal = await sim.withdrawal(SOMEWHERE, "25.000000", reference="wd-9")

    await sim.advance(1.999)
    before_the_block = await sim.get_withdrawal(withdrawal["id"])
    await sim.advance(0.001)
    broadcast = await sim.get_withdrawal(withdrawal["id"])
    await sim.advance(5.999)
    just_before = await sim.get_withdrawal(withdrawal["id"])
    balance_just_before = await sim.balance("custody", "USDC")
    await sim.advance(0.001)
    completed = await sim.get_withdrawal(withdrawal["id"])

    assert before_the_block == withdrawal
    assert broadcast == {**withdrawal, "status": "broadcast", "tx_hash": broadcast["tx_hash"]}
    assert re.fullmatch(r"[0-9a-f]{64}", broadcast["tx_hash"])
    assert just_before == {**broadcast, "confirmations": 2}
    assert balance_just_before == "0.000000"
    assert completed == {
        **broadcast,
        "status": "completed",
        "confirmations": 3,
        "completed_at": "2026-01-15T12:00:08Z",
    }
    # The amount and the network fee leave the balance together, when it completes.
    assert await sim.balance("custody", "USDC") == "-25.150000"
    assert (await sim.statement("custody", "USDC"))["transactions"] == [
        {
            "id": withdrawal["id"],
            "type": "withdrawal",
            "direction": "debit",
            "asset": "USDC",
            "amount": "25.000000",
            "reference": "wd-9",
            "related_id": SOMEWHERE,
            "occurred_at": "2026-01-15T12:00:08Z",
            "tx_hash": broadcast["tx_hash"],
        },
        {
            "id": f"{withdrawal['id']}:fee",
            "type": "network_fee",
            "direction": "debit",
            "asset": "USDC",
            "amount": "0.150000",
            "reference": "wd-9",
            "related_id": withdrawal["id"],
            "occurred_at": "2026-01-15T12:00:08Z",
            "tx_hash": broadcast["tx_hash"],
        },
    ]


async def test_a_withdrawal_moves_through_its_states_by_mining_too(sim: Sim) -> None:
    withdrawal = await sim.withdrawal(SOMEWHERE)

    await sim.mine(1)
    one = await sim.get_withdrawal(withdrawal["id"])
    await sim.mine(2)
    three = await sim.get_withdrawal(withdrawal["id"])
    await sim.mine(1)
    four = await sim.get_withdrawal(withdrawal["id"])

    assert (one["status"], one["confirmations"]) == ("broadcast", 0)
    assert (three["status"], three["confirmations"]) == ("broadcast", 2)
    assert (four["status"], four["confirmations"]) == ("completed", 3)


async def test_a_withdrawal_noticed_late_goes_through_both_steps_at_once(sim: Sim) -> None:
    withdrawal = await sim.withdrawal(SOMEWHERE)

    await sim.advance(60)

    completed = await sim.get_withdrawal(withdrawal["id"])
    assert (completed["status"], completed["completed_at"]) == ("completed", after(60))
    # Broadcast in block 1 of 30.
    assert completed["confirmations"] == 29
    assert re.fullmatch(r"[0-9a-f]{64}", completed["tx_hash"])
    assert await sim.balance("custody", "USDC") == "-25.150000"


async def test_a_completed_withdrawal_stays_completed_and_is_charged_once(sim: Sim) -> None:
    withdrawal = await sim.withdrawal(SOMEWHERE)
    await sim.advance(8)

    await sim.advance(600)

    assert (await sim.get_withdrawal(withdrawal["id"]))["status"] == "completed"
    assert len((await sim.statement("custody", "USDC"))["transactions"]) == 2
    assert await sim.balance("custody", "USDC") == "-25.150000"


async def test_each_withdrawal_gets_a_transaction_hash_of_its_own(sim: Sim) -> None:
    first = await sim.withdrawal(SOMEWHERE, key="a")
    second = await sim.withdrawal(SOMEWHERE, key="b")

    await sim.mine(1)

    hashes = {(await sim.get_withdrawal(w["id"]))["tx_hash"] for w in (first, second)}
    assert len(hashes) == 2
    assert None not in hashes


async def test_a_withdrawal_to_a_dead_address_fails_at_broadcast_and_charges_nothing(
    sim: Sim,
) -> None:
    withdrawal = await sim.withdrawal(DEAD, "25.000000")

    await sim.advance(1.999)
    before_the_block = await sim.get_withdrawal(withdrawal["id"])
    await sim.advance(0.001)
    failed = await sim.get_withdrawal(withdrawal["id"])
    await sim.advance(60)

    assert before_the_block["status"] == "pending"
    assert failed == {**withdrawal, "status": "failed", "failure_reason": "rejected_by_network"}
    assert await sim.get_withdrawal(withdrawal["id"]) == failed
    assert await sim.balance("custody", "USDC") == "0.000000"
    assert (await sim.statement("custody", "USDC"))["transactions"] == []


async def test_only_a_body_that_begins_with_dead_is_rejected_by_the_network(sim: Sim) -> None:
    elsewhere = address_with_body("adead".ljust(32, "b"))
    withdrawal = await sim.withdrawal(elsewhere)

    await sim.mine(4)

    assert (await sim.get_withdrawal(withdrawal["id"]))["status"] == "completed"


async def test_the_same_idempotency_key_returns_the_first_withdrawal_and_creates_no_second(
    sim: Sim,
) -> None:
    first = await sim.post_withdrawal(SOMEWHERE, key="wd-1")
    again = await sim.post_withdrawal(SOMEWHERE, key="wd-1")

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert [w["id"] for w in await sim.inspect("custody", "withdrawals")] == [first.json()["id"]]


async def test_a_replayed_withdrawal_is_shown_as_it_is_now(sim: Sim) -> None:
    first = await sim.withdrawal(SOMEWHERE, key="wd-1")
    await sim.mine(4)

    again = await sim.post_withdrawal(SOMEWHERE, key="wd-1")

    assert again.status_code == 200
    assert again.json() == await sim.get_withdrawal(first["id"])
    assert again.json()["status"] == "completed"
    assert len(await sim.inspect("custody", "withdrawals")) == 1
    assert await sim.balance("custody", "USDC") == "-25.150000"


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "25.000001"},
        {"amount": "25"},
        {"reference": "wd-2"},
        {"to_address": DEAD},
        {"to_address": "not-an-address"},
        {"asset": "USD"},
    ],
)
async def test_the_same_withdrawal_key_with_a_different_body_is_a_conflict(
    sim: Sim, change: dict[str, object]
) -> None:
    body = {"asset": "USDC", "amount": "25.000000", "to_address": SOMEWHERE, "reference": "wd-1"}
    key = {"Idempotency-Key": "wd-1"}
    first = await sim.api.post("/custody/v1/withdrawals", json=body, headers=key)

    conflicting = await sim.api.post(
        "/custody/v1/withdrawals", json={**body, **change}, headers=key
    )

    assert code_of(conflicting) == (409, "idempotency_conflict")
    (only,) = await sim.inspect("custody", "withdrawals")
    assert {field: only[field] for field in first.json()} == first.json()


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": ""}, {"Idempotency-Key": "  "}])
async def test_a_withdrawal_without_an_idempotency_key_is_refused(
    sim: Sim, headers: dict[str, str]
) -> None:
    response = await sim.api.post(
        "/custody/v1/withdrawals",
        json={"asset": "USDC", "amount": "25.000000", "to_address": SOMEWHERE, "reference": "wd-1"},
        headers=headers,
    )

    assert code_of(response) == (400, "idempotency_key_required")
    assert await sim.inspect("custody", "withdrawals") == []


async def test_a_refused_withdrawal_does_not_use_up_its_key(sim: Sim) -> None:
    refused = await sim.post_withdrawal("not-an-address", key="wd-1")
    corrected = await sim.post_withdrawal(SOMEWHERE, key="wd-1")

    assert code_of(refused) == (422, "invalid_address")
    assert corrected.status_code == 201


@pytest.mark.parametrize(
    "to_address",
    [
        "",
        "not-an-address",
        SOMEWHERE[:-1],
        SOMEWHERE + "a",
        SOMEWHERE[:-1] + ("0" if SOMEWHERE[-1] != "0" else "1"),
        SOMEWHERE[:10] + ("b" if SOMEWHERE[10] != "b" else "c") + SOMEWHERE[11:],
        SOMEWHERE.upper(),
        "0x52908400098527886E0F7030069857D2E4169EE7",
        None,
        42,
    ],
)
async def test_a_withdrawal_to_an_invalid_address_is_refused(sim: Sim, to_address: object) -> None:
    response = await sim.api.post(
        "/custody/v1/withdrawals",
        json={
            "asset": "USDC",
            "amount": "25.000000",
            "to_address": to_address,
            "reference": "wd-1",
        },
        headers={"Idempotency-Key": "wd-1"},
    )

    assert code_of(response) == (422, "invalid_address")
    assert await sim.inspect("custody", "withdrawals") == []


@pytest.mark.parametrize(
    "amount",
    [25, 25.5, None, "-25.000000", "0", "0.000000", "2.5e1", "25.0000001", "25,000000", " 25", ""],
)
async def test_a_withdrawal_with_a_malformed_amount_is_refused(sim: Sim, amount: object) -> None:
    response = await sim.api.post(
        "/custody/v1/withdrawals",
        json={"asset": "USDC", "amount": amount, "to_address": SOMEWHERE, "reference": "wd-1"},
        headers={"Idempotency-Key": "wd-1"},
    )

    assert code_of(response) == (422, "invalid_amount")
    assert await sim.inspect("custody", "withdrawals") == []


@pytest.mark.parametrize("asset", ["USD", "BRL", "EUR", "usdc"])
async def test_a_withdrawal_in_an_asset_the_custodian_does_not_carry_is_refused(
    sim: Sim, asset: str
) -> None:
    response = await sim.post_withdrawal(SOMEWHERE, "25.00", asset=asset)

    assert code_of(response) == (422, "unsupported_asset")
    assert await sim.inspect("custody", "withdrawals") == []


@pytest.mark.parametrize("change", [{"reference": ""}, {"reference": None}, {"asset": None}])
async def test_a_malformed_withdrawal_request_is_refused(
    sim: Sim, change: dict[str, object]
) -> None:
    response = await sim.api.post(
        "/custody/v1/withdrawals",
        json={
            "asset": "USDC",
            "amount": "25.000000",
            "to_address": SOMEWHERE,
            "reference": "wd-1",
            **change,
        },
        headers={"Idempotency-Key": "wd-1"},
    )

    assert code_of(response) == (422, "invalid_request")


async def test_a_withdrawal_is_read_by_id(sim: Sim) -> None:
    withdrawal = await sim.withdrawal(SOMEWHERE)

    response = await sim.api.get(f"/custody/v1/withdrawals/{withdrawal['id']}")

    assert (response.status_code, response.json()) == (200, withdrawal)


async def test_an_unknown_withdrawal_is_not_found(sim: Sim) -> None:
    response = await sim.api.get("/custody/v1/withdrawals/wd_000000000000")

    assert code_of(response) == (404, "withdrawal_not_found")


async def test_withdrawals_are_listed_by_reference(sim: Sim) -> None:
    first = await sim.withdrawal(SOMEWHERE, reference="wd-1", key="a")
    await sim.withdrawal(SOMEWHERE, reference="wd-2", key="b")
    resubmitted = await sim.withdrawal(SOMEWHERE, reference="wd-1", key="c")

    matching = await sim.api.get("/custody/v1/withdrawals", params={"reference": "wd-1"})
    nothing = await sim.api.get("/custody/v1/withdrawals", params={"reference": "wd-3"})

    assert (matching.status_code, matching.json()) == (200, {"withdrawals": [first, resubmitted]})
    assert (nothing.status_code, nothing.json()) == (200, {"withdrawals": []})


@pytest.mark.parametrize("query", ["", "?reference="])
async def test_listing_withdrawals_needs_a_reference(sim: Sim, query: str) -> None:
    response = await sim.api.get(f"/custody/v1/withdrawals{query}")

    assert code_of(response) == (422, "invalid_request")


async def test_the_control_endpoint_shows_a_withdrawals_key_and_blocks(sim: Sim) -> None:
    await sim.mine(5)
    withdrawal = await sim.withdrawal(SOMEWHERE, key="wd-1")
    await sim.mine(1)

    (inspected,) = await sim.inspect("custody", "withdrawals")

    assert {key: inspected[key] for key in withdrawal} == await sim.get_withdrawal(withdrawal["id"])
    assert inspected["idempotency_key"] == "wd-1"
    assert (inspected["created_height"], inspected["broadcast_height"]) == (5, 6)


# --- the statement -------------------------------------------------------------------------


async def test_the_statement_lists_only_final_movements(sim: Sim) -> None:
    address = (await sim.address())["address"]
    confirmed = await sim.chain_deposit(address, "100.000000")
    completed = await sim.withdrawal(SOMEWHERE, "25.000000", key="a")
    await sim.mine(4)
    # None of these is final: a deposit still waiting, one that was dropped, a withdrawal
    # only just broadcast, one not yet broadcast, and one the network rejected.
    await sim.chain_deposit(address, "1.000000")
    dropped = await sim.chain_deposit(address, "2.000000")
    await sim.control("POST", f"/custody/deposits/{dropped['id']}/drop")
    await sim.withdrawal(DEAD, "4.000000", key="b")
    await sim.withdrawal(SOMEWHERE, "8.000000", key="c")
    await sim.mine(1)
    await sim.withdrawal(SOMEWHERE, "16.000000", key="d")

    statement = await sim.statement("custody", "USDC")

    assert [
        (line["id"], line["type"], line["direction"], line["amount"])
        for line in statement["transactions"]
    ] == [
        (confirmed["id"], "deposit", "credit", "100.000000"),
        (completed["id"], "withdrawal", "debit", "25.000000"),
        (f"{completed['id']}:fee", "network_fee", "debit", "0.150000"),
    ]
    assert all(re.fullmatch(r"[0-9a-f]{64}", line["tx_hash"]) for line in statement["transactions"])
    assert statement["closing_balance"] == "74.850000"
    assert await sim.control("GET", "/custody/balances") == {"balances": {"USDC": "74.850000"}}


async def test_the_custody_statement_window_includes_its_start_and_excludes_its_end(
    sim: Sim,
) -> None:
    address = (await sim.address())["address"]
    await sim.chain_deposit(address, "1.000000")
    await sim.advance(6)
    await sim.chain_deposit(address, "2.000000")
    await sim.advance(6)

    async def amounts(start: float, end: float) -> tuple[list[str], str]:
        statement = await sim.statement("custody", "USDC", after(start), after(end))
        return [line["amount"] for line in statement["transactions"]], statement["closing_balance"]

    assert await amounts(0, 12) == (["1.000000"], "1.000000")
    assert await amounts(0, 12.000001) == (["1.000000", "2.000000"], "3.000000")
    assert await amounts(6, 12) == (["1.000000"], "1.000000")
    assert await amounts(6.000001, 12.000001) == (["2.000000"], "3.000000")
    assert await amounts(0, 6) == ([], "0.000000")


@pytest.mark.parametrize(
    ("params", "refusal"),
    [
        ({"asset": "USD", **WINDOW}, (422, "unsupported_asset")),
        ({"asset": "EUR", **WINDOW}, (422, "unsupported_asset")),
        (WINDOW, (422, "invalid_request")),
        ({"asset": "USDC", "from": WINDOW["from"]}, (422, "invalid_request")),
        ({"asset": "USDC", "from": "yesterday", "to": WINDOW["to"]}, (422, "invalid_request")),
        ({"asset": "USDC", "from": WINDOW["to"], "to": WINDOW["from"]}, (422, "invalid_request")),
    ],
)
async def test_a_malformed_custody_statement_request_is_refused(
    sim: Sim, params: dict[str, str], refusal: tuple[int, str]
) -> None:
    response = await sim.api.get("/custody/v1/transactions", params=params)

    assert code_of(response) == refusal


# --- the chain -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body", [{}, {"blocks": 0}, {"blocks": -1}, {"blocks": 1.5}, {"blocks": "3"}]
)
async def test_mining_needs_a_positive_whole_number_of_blocks(sim: Sim, body: object) -> None:
    response = await sim.anonymous.post("/_control/chain/mine", json=body)

    assert code_of(response) == (422, "invalid_request")


async def test_the_chain_starts_over_from_the_moment_the_simulator_does(launch: Launch) -> None:
    sim = await launch(start_time="2026-03-01T00:00:01Z")

    await sim.advance(1.999)
    before = await sim.mine(1)
    await sim.advance(0.001)

    # Blocks are counted from the simulator's own start, not from the calendar.
    assert before == 1
    assert await sim.mine(1) == 3


# --- the books as a whole ------------------------------------------------------------------

BLOCK_MS = 2_000
CONFIRMATIONS = 3
FEE_MICRO = 150_000


@dataclass(frozen=True)
class Step:
    kind: Literal["deposit", "drop", "withdraw", "mine", "advance"]
    micro: int
    dead: bool
    pick: int
    blocks: int
    milliseconds: int


steps = st.lists(
    st.builds(
        Step,
        kind=st.sampled_from(["deposit", "drop", "withdraw", "mine", "advance"]),
        micro=st.integers(1, 5_000_000_000),
        dead=st.booleans(),
        pick=st.integers(0, 50),
        blocks=st.integers(1, 3),
        milliseconds=st.sampled_from([0, 500, 1_999, 2_000, 2_001, 6_000, 8_000]),
    ),
    max_size=30,
)


def usdc(micro: int) -> str:
    sign = "-" if micro < 0 else ""
    return f"{sign}{abs(micro) // 10**6}.{abs(micro) % 10**6:06d}"


def micro_of(text: str) -> int:
    whole, _, fraction = text.lstrip("-").partition(".")
    assert len(fraction) == 6
    return (-1 if text.startswith("-") else 1) * (int(whole) * 10**6 + int(fraction))


async def run_custody(sequence: list[Step]) -> None:
    """Drive the custodian and a plain model of the chain side by side, then compare."""
    async with running_sim(bank_webhook_url=None, custody_webhook_url=None) as sim:
        address = (await sim.address())["address"]
        now_ms, mined = 0, 0
        deposits: dict[str, tuple[int, int]] = {}  # id -> micro, height detected at
        dropped: set[str] = set()
        withdrawals: dict[str, tuple[int, bool, int]] = {}  # id -> micro, dead, height created at

        def height() -> int:
            return now_ms // BLOCK_MS + mined

        def deposit_state(deposit_id: str) -> str:
            if deposit_id in dropped:
                return "failed"
            confirmed = height() - deposits[deposit_id][1] >= CONFIRMATIONS
            return "confirmed" if confirmed else "detected"

        def withdrawal_state(withdrawal_id: str) -> str:
            _, dead, created = withdrawals[withdrawal_id]
            if height() <= created:
                return "pending"
            if dead:
                return "failed"
            return "completed" if height() - (created + 1) >= CONFIRMATIONS else "broadcast"

        for step in sequence:
            if step.kind == "deposit":
                deposit = await sim.chain_deposit(address, usdc(step.micro))
                deposits[deposit["id"]] = (step.micro, height())
            elif step.kind == "drop":
                if not deposits:
                    continue
                deposit_id = sorted(deposits)[step.pick % len(deposits)]
                response = await sim.anonymous.post(f"/_control/custody/deposits/{deposit_id}/drop")
                droppable = deposit_state(deposit_id) == "detected"
                assert response.status_code == (200 if droppable else 409), response.text
                if droppable:
                    dropped.add(deposit_id)
            elif step.kind == "withdraw":
                to_address = DEAD if step.dead else SOMEWHERE
                withdrawal = await sim.withdrawal(to_address, usdc(step.micro))
                withdrawals[withdrawal["id"]] = (step.micro, step.dead, height())
            elif step.kind == "mine":
                mined += step.blocks
                assert await sim.mine(step.blocks) == height()
            else:
                await sim.advance(step.milliseconds / 1000)
                now_ms += step.milliseconds

        expected = sum(
            micro
            for deposit_id, (micro, _) in deposits.items()
            if deposit_state(deposit_id) == "confirmed"
        ) - sum(
            micro + FEE_MICRO
            for withdrawal_id, (micro, _, _) in withdrawals.items()
            if withdrawal_state(withdrawal_id) == "completed"
        )

        listed_deposits = await sim.inspect("custody", "deposits")
        listed_withdrawals = await sim.inspect("custody", "withdrawals")
        statement = await sim.statement("custody", "USDC")
        lines = statement["transactions"]
        signed = [
            micro_of(line["amount"]) * (1 if line["direction"] == "credit" else -1)
            for line in lines
        ]
        assert {d["id"]: d["status"] for d in listed_deposits} == {
            deposit_id: deposit_state(deposit_id) for deposit_id in deposits
        }
        assert {w["id"]: w["status"] for w in listed_withdrawals} == {
            withdrawal_id: withdrawal_state(withdrawal_id) for withdrawal_id in withdrawals
        }
        # Three views of one number: the model, the running balance, the statement.
        assert await sim.balance("custody", "USDC") == usdc(expected)
        assert statement["closing_balance"] == usdc(expected)
        assert sum(signed) == expected
        # Only what is final is on the statement, each movement once.
        assert sorted(line["id"] for line in lines if line["type"] == "deposit") == sorted(
            deposit_id for deposit_id in deposits if deposit_state(deposit_id) == "confirmed"
        )
        completed = sorted(w for w in withdrawals if withdrawal_state(w) == "completed")
        assert sorted(line["id"] for line in lines if line["type"] == "withdrawal") == completed
        assert sorted(line["id"] for line in lines if line["type"] == "network_fee") == [
            f"{withdrawal_id}:fee" for withdrawal_id in completed
        ]
        # A transaction hash exists exactly for what reached the chain.
        for withdrawal in listed_withdrawals:
            on_chain = withdrawal["status"] in ("broadcast", "completed")
            assert (withdrawal["tx_hash"] is not None) == on_chain


@hypothesis_settings(max_examples=40, deadline=None)
@given(sequence=steps)
def test_the_custody_balance_always_equals_the_sum_of_the_statement(sequence: list[Step]) -> None:
    asyncio.run(run_custody(sequence))

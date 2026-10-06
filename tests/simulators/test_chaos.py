"""Injected faults, forgetting everything, and the simulator running on the wall clock."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from tests.simulators.conftest import BANK_WEBHOOK_SECRET, Sim, address_with_body, signed_at

Launch = Callable[..., Awaitable[Sim]]

RECIPIENT = address_with_body("receiver".ljust(32, "a"))
WINDOW = {"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"}


@dataclass(frozen=True)
class MoneyOut:
    """One of the two operations that move money out, driven the same way."""

    operation: str
    provider: str
    listing: str
    amount_with_fee: str

    async def prepare(self, sim: Sim) -> str:
        """Whatever the request needs to exist first; returns where the money goes."""
        if self.provider == "bank":
            return str((await sim.beneficiary())["id"])
        return RECIPIENT

    async def post(self, sim: Sim, target: str, key: str) -> httpx.Response:
        if self.provider == "bank":
            return await sim.post_payout(target, key=key)
        return await sim.post_withdrawal(target, key=key)

    async def recorded(self, sim: Sim) -> list[dict[str, Any]]:
        return await sim.inspect(self.provider, self.listing)

    async def finish(self, sim: Sim) -> None:
        """Let enough time pass for the payout or the withdrawal to complete."""
        await sim.advance(30)


PAYOUT = MoneyOut("bank.create_payout", "bank", "payouts", "-100.25")
WITHDRAWAL = MoneyOut("custody.create_withdrawal", "custody", "withdrawals", "-25.150000")
BOTH = pytest.mark.parametrize("out", [PAYOUT, WITHDRAWAL], ids=["payout", "withdrawal"])


async def inject(sim: Sim, operation: str, mode: str, **fields: object) -> Any:
    return await sim.control(
        "POST", "/faults", {"operation": operation, "mode": mode, **fields}, expect=201
    )


def code_of(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def books_balance(sim: Sim, out: MoneyOut, expected: str) -> None:
    asset = "USD" if out.provider == "bank" else "USDC"
    statement = await sim.statement(out.provider, asset)
    assert await sim.balance(out.provider, asset) == expected
    assert statement["closing_balance"] == expected


# --- error -----------------------------------------------------------------------------------


@BOTH
async def test_an_error_fault_answers_with_its_status_and_nothing_happens(
    sim: Sim, out: MoneyOut
) -> None:
    target = await out.prepare(sim)
    await inject(sim, out.operation, "error", status=500)

    failed = await out.post(sim, target, "key-a")
    recorded_after_the_fault = await out.recorded(sim)
    retried = await out.post(sim, target, "key-a")

    assert code_of(failed) == (500, "injected_fault")
    assert recorded_after_the_fault == []
    # The key was never bound, so the retry is the first request the provider acted on.
    assert retried.status_code == 201
    assert [item["id"] for item in await out.recorded(sim)] == [retried.json()["id"]]


async def test_an_error_fault_answers_503_unless_told_otherwise(sim: Sim) -> None:
    await inject(sim, "bank.create_payout", "error")

    response = await sim.post_payout("ben_unknown")

    assert code_of(response) == (503, "injected_fault")


# --- error after the effect ------------------------------------------------------------------


@BOTH
async def test_an_error_after_the_effect_leaves_exactly_one_and_the_retry_returns_it(
    sim: Sim, out: MoneyOut
) -> None:
    target = await out.prepare(sim)
    await inject(sim, out.operation, "error_after_effect", status=500)

    failed = await out.post(sim, target, "key-a")
    (recorded,) = await out.recorded(sim)
    retried = await out.post(sim, target, "key-a")

    assert code_of(failed) == (500, "injected_fault")
    assert recorded["idempotency_key"] == "key-a"
    assert (retried.status_code, retried.json()["id"]) == (200, recorded["id"])
    assert len(await out.recorded(sim)) == 1
    await out.finish(sim)
    await books_balance(sim, out, out.amount_with_fee)


@BOTH
async def test_a_request_that_is_refused_does_not_use_up_a_fault_after_the_effect(
    sim: Sim, out: MoneyOut
) -> None:
    target = await out.prepare(sim)
    await inject(sim, out.operation, "error_after_effect", status=500)

    no_key = await sim.api.post(f"/{out.provider}/v1/{out.listing}", json={})
    failed = await out.post(sim, target, "key-a")

    assert code_of(no_key) == (400, "idempotency_key_required")
    assert code_of(failed) == (500, "injected_fault")
    assert len(await out.recorded(sim)) == 1


# --- timeouts --------------------------------------------------------------------------------


@BOTH
async def test_a_timeout_fault_outlasts_the_callers_deadline_and_nothing_happens(
    sim: Sim, out: MoneyOut
) -> None:
    target = await out.prepare(sim)
    await inject(sim, out.operation, "timeout", hang_seconds=5)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await out.post(sim, target, "key-a")
    recorded_after_the_fault = await out.recorded(sim)
    retried = await out.post(sim, target, "key-a")

    assert recorded_after_the_fault == []
    assert retried.status_code == 201
    assert len(await out.recorded(sim)) == 1
    await out.finish(sim)
    await books_balance(sim, out, out.amount_with_fee)


@BOTH
async def test_a_timeout_after_the_effect_leaves_exactly_one_and_the_retry_returns_it(
    sim: Sim, out: MoneyOut
) -> None:
    target = await out.prepare(sim)
    await inject(sim, out.operation, "timeout_after_effect", hang_seconds=5)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await out.post(sim, target, "key-a")
    (recorded,) = await out.recorded(sim)
    retried = await out.post(sim, target, "key-a")

    assert recorded["idempotency_key"] == "key-a"
    assert (retried.status_code, retried.json()["id"]) == (200, recorded["id"])
    assert len(await out.recorded(sim)) == 1
    # The handler that was cancelled left nothing half done: time still passes, the one
    # payout or withdrawal completes, and the books agree with the statement.
    await out.finish(sim)
    assert [item["status"] for item in await out.recorded(sim)] == ["completed"]
    await books_balance(sim, out, out.amount_with_fee)


@BOTH
@pytest.mark.parametrize(("mode", "happened"), [("timeout", 0), ("timeout_after_effect", 1)])
async def test_a_caller_who_waits_out_the_hang_gets_a_504(
    sim: Sim, out: MoneyOut, mode: str, happened: int
) -> None:
    target = await out.prepare(sim)
    await inject(sim, out.operation, mode, hang_seconds=0.01)

    response = await out.post(sim, target, "key-a")

    assert code_of(response) == (504, "injected_timeout")
    assert len(await out.recorded(sim)) == happened


# --- the fault table -------------------------------------------------------------------------


async def test_a_fault_spoils_as_many_calls_as_its_times_and_no_more(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    queued = await inject(sim, "bank.create_payout", "error", times=2)

    statuses = [(await sim.post_payout(beneficiary["id"])).status_code for _ in range(3)]

    assert queued == {
        "faults": [
            {
                "operation": "bank.create_payout",
                "mode": "error",
                "times": 2,
                "status": 503,
                "hang_seconds": 30,
            }
        ]
    }
    assert statuses == [503, 503, 201]


async def test_faults_on_one_operation_are_met_in_the_order_they_were_queued(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    await inject(sim, "bank.create_payout", "error", status=502)
    await inject(sim, "bank.create_payout", "error_after_effect", status=500)
    await inject(sim, "bank.create_payout", "error", status=429)

    statuses = [(await sim.post_payout(beneficiary["id"])).status_code for _ in range(4)]

    assert statuses == [502, 500, 429, 201]
    assert len(await sim.inspect("bank", "payouts")) == 2


async def test_a_fault_reaches_only_its_own_operation(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    await inject(sim, "custody.create_withdrawal", "error")

    payout = await sim.post_payout(beneficiary["id"])
    withdrawal = await sim.post_withdrawal(RECIPIENT)

    assert (payout.status_code, withdrawal.status_code) == (201, 503)


async def test_clearing_removes_every_fault(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    await inject(sim, "bank.create_payout", "error", times=5)
    await inject(sim, "fx.get_rate", "error")

    cleared = await sim.control("DELETE", "/faults")

    assert cleared == {"faults": []}
    assert (await sim.post_payout(beneficiary["id"])).status_code == 201
    assert (await sim.api.get("/fx/v1/rates/USD/MXN")).status_code == 200


async def test_an_unknown_operation_is_refused(sim: Sim) -> None:
    response = await sim.anonymous.post(
        "/_control/faults", json={"operation": "bank.create_payouts", "mode": "error"}
    )

    assert code_of(response) == (422, "unknown_operation")
    assert await sim.control("DELETE", "/faults") == {"faults": []}


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "bank.create_payout"},
        {"mode": "error"},
        {"operation": "bank.create_payout", "mode": "explode"},
        {"operation": "bank.create_payout", "mode": "error", "times": 0},
        {"operation": "bank.create_payout", "mode": "error", "times": "1"},
        {"operation": "bank.create_payout", "mode": "error", "status": 200},
        {"operation": "bank.create_payout", "mode": "error", "status": 600},
        {"operation": "bank.create_payout", "mode": "timeout", "hang_seconds": -1},
    ],
)
async def test_a_malformed_fault_is_refused_and_queues_nothing(sim: Sim, body: object) -> None:
    beneficiary = await sim.beneficiary()

    response = await sim.anonymous.post("/_control/faults", json=body)

    assert code_of(response) == (422, "invalid_request")
    assert (await sim.post_payout(beneficiary["id"])).status_code == 201


def _post(path: str, body: dict[str, object]) -> Callable[[Sim], Awaitable[httpx.Response]]:
    return lambda sim: sim.api.post(path, json=body, headers={"Idempotency-Key": "key-a"})


def _get(path: str, **params: str) -> Callable[[Sim], Awaitable[httpx.Response]]:
    return lambda sim: sim.api.get(path, params=params)


# Every operation the contract lists, with a request that reaches it.
CALLS: dict[str, Callable[[Sim], Awaitable[httpx.Response]]] = {
    "bank.create_virtual_account": _post(
        "/bank/v1/virtual-accounts", {"customer_reference": "c-1", "asset": "USD"}
    ),
    "bank.create_beneficiary": _post(
        "/bank/v1/beneficiaries",
        {
            "customer_reference": "c-1",
            "asset": "MXN",
            "holder_name": "Maria Silva",
            "account_number": "032180000118359719",
        },
    ),
    "bank.create_payout": _post(
        "/bank/v1/payouts",
        {"beneficiary_id": "ben_none", "asset": "USD", "amount": "1.00", "reference": "wd-1"},
    ),
    "bank.get_payout": _get("/bank/v1/payouts/po_none"),
    "bank.list_transactions": _get("/bank/v1/transactions", asset="USD", **WINDOW),
    "custody.create_address": _post(
        "/custody/v1/addresses", {"customer_reference": "c-1", "asset": "USDC"}
    ),
    "custody.create_withdrawal": _post(
        "/custody/v1/withdrawals",
        {"asset": "USDC", "amount": "1.000000", "to_address": RECIPIENT, "reference": "wd-1"},
    ),
    "custody.get_withdrawal": _get("/custody/v1/withdrawals/wd_none"),
    "custody.list_transactions": _get("/custody/v1/transactions", asset="USDC", **WINDOW),
    "fx.get_rate": _get("/fx/v1/rates/USD/MXN"),
}
# What each of those requests gets when nothing is wrong.
HEALTHY = {
    "bank.create_payout": 404,
    "bank.get_payout": 404,
    "custody.get_withdrawal": 404,
    "bank.list_transactions": 200,
    "custody.list_transactions": 200,
    "fx.get_rate": 200,
}


@pytest.mark.parametrize("operation", sorted(CALLS))
async def test_every_operation_in_the_contract_can_be_made_to_fail(
    sim: Sim, operation: str
) -> None:
    await inject(sim, operation, "error", status=502)

    spoiled = await CALLS[operation](sim)
    healthy = await CALLS[operation](sim)

    assert code_of(spoiled) == (502, "injected_fault")
    assert healthy.status_code == HEALTHY.get(operation, 201)


@pytest.mark.parametrize(
    "operation",
    ["bank.create_virtual_account", "bank.create_beneficiary", "custody.create_address"],
)
async def test_a_create_that_failed_after_the_effect_is_found_by_its_key(
    sim: Sim, operation: str
) -> None:
    await inject(sim, operation, "error_after_effect", status=500)

    failed = await CALLS[operation](sim)
    retried = await CALLS[operation](sim)

    assert code_of(failed) == (500, "injected_fault")
    assert retried.status_code == 200


async def test_a_read_can_fail_and_then_answer(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    payout = await sim.payout(beneficiary["id"], reference="wd-9")
    await inject(sim, "bank.get_payout", "error", times=2)
    await inject(sim, "fx.get_rate", "error_after_effect", status=500)

    by_id = await sim.api.get(f"/bank/v1/payouts/{payout['id']}")
    by_reference = await sim.api.get("/bank/v1/payouts", params={"reference": "wd-9"})
    rate = await sim.api.get("/fx/v1/rates/USD/MXN")

    assert (code_of(by_id), code_of(by_reference)) == ((503, "injected_fault"),) * 2
    assert code_of(rate) == (500, "injected_fault")
    assert (await sim.get_payout(payout["id"]))["id"] == payout["id"]
    assert (await sim.api.get("/fx/v1/rates/USD/MXN")).json()["mid"] == "17.250000"


async def test_a_read_can_hang_past_the_callers_deadline(sim: Sim) -> None:
    await inject(sim, "fx.get_rate", "timeout", hang_seconds=5)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await sim.api.get("/fx/v1/rates/USD/MXN")

    assert (await sim.api.get("/fx/v1/rates/USD/MXN")).status_code == 200


async def test_a_fault_is_not_met_by_a_caller_without_the_api_key(sim: Sim) -> None:
    await inject(sim, "fx.get_rate", "error")

    refused = await sim.anonymous.get("/fx/v1/rates/USD/MXN")
    spoiled = await sim.api.get("/fx/v1/rates/USD/MXN")

    assert (refused.status_code, spoiled.status_code) == (401, 503)


# --- reset -----------------------------------------------------------------------------------


async def test_reset_forgets_everything(sim: Sim) -> None:
    account = await sim.virtual_account()
    await sim.bank_deposit(account["id"], "250.00")
    beneficiary = await sim.beneficiary()
    first_payout = await sim.payout(beneficiary["id"], key="key-a")
    address = await sim.address()
    await sim.chain_deposit(address["address"], "10.000000")
    await sim.withdrawal(RECIPIENT, key="key-a")
    await sim.control("POST", "/fx/rates", {"base": "USD", "quote": "MXN", "mid": "18.5"})
    await sim.control("POST", "/fx/freeze", {"frozen": True})
    await sim.control("POST", "/webhooks/behaviour", {"hold": True, "duplicates": 3})
    await inject(sim, "fx.get_rate", "error", times=100)
    await sim.advance(60)

    reset = await sim.control("POST", "/reset")

    assert reset == {"now": "2026-01-15T12:00:00Z", "mode": "manual"}
    for provider, listing in [
        ("bank", "payouts"),
        ("bank", "deposits"),
        ("custody", "withdrawals"),
        ("custody", "deposits"),
    ]:
        assert await sim.inspect(provider, listing) == []
    assert (await sim.control("GET", "/bank/balances"))["balances"] == {
        "USD": "0.00",
        "MXN": "0.00",
        "BRL": "0.00",
    }
    assert (await sim.control("GET", "/custody/balances"))["balances"] == {"USDC": "0.000000"}
    assert await sim.events() == []
    assert (await sim.api.get("/fx/v1/rates/USD/MXN")).json()["mid"] == "17.250000"
    assert await sim.control("POST", "/webhooks/behaviour", {}) == {
        "duplicates": 0,
        "drop_types": [],
        "hold": False,
        "reverse": False,
    }
    assert (await sim.api.get(f"/bank/v1/payouts/{first_payout['id']}")).status_code == 404
    assert (await sim.post_payout(beneficiary["id"], key="key-a")).status_code == 404
    # The same seed from the start: the simulator invents the same values again.
    assert await sim.virtual_account() == account
    await sim.advance(5)
    assert (await sim.api.get("/fx/v1/rates/USD/MXN")).json()["as_of"] == "2026-01-15T12:00:05Z"


async def test_a_reset_simulator_still_delivers_webhooks(sim: Sim) -> None:
    await sim.control("POST", "/reset")
    account = await sim.virtual_account()
    await sim.bank_deposit(account["id"], "250.00")

    await sim.advance(0)

    (request,) = sim.receiver.requests
    signed_at(request, BANK_WEBHOOK_SECRET)


# --- the wall clock --------------------------------------------------------------------------


async def the_webhook(sim: Sim, event_type: str) -> None:
    """Wait on real time, for at most five seconds, until the receiver has such an event."""
    for _ in range(100):
        if sim.receiver.of_type(event_type):
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"no {event_type} arrived within five seconds")


async def test_a_realtime_clock_cannot_be_advanced(launch: Launch) -> None:
    sim = await launch(clock_mode="realtime")
    before = (await sim.control("GET", "/clock"))["now"]

    response = await sim.anonymous.post("/_control/clock/advance", json={"seconds": 3600})

    assert code_of(response) == (409, "clock_is_realtime")
    clock = await sim.control("GET", "/clock")
    assert clock["mode"] == "realtime"
    assert clock["now"][:13] == before[:13] or clock["now"] > before


async def test_in_realtime_a_pix_payout_settles_and_its_webhook_arrives_untouched(
    launch: Launch,
) -> None:
    sim = await launch(clock_mode="realtime")
    beneficiary = await sim.beneficiary("BRL")
    payout = await sim.payout(beneficiary["id"], "50.00", "BRL")

    await the_webhook(sim, "payout.completed")

    (request,) = sim.receiver.of_type("payout.completed")
    signed_at(request, BANK_WEBHOOK_SECRET)
    assert request.json()["data"]["payout_id"] == payout["id"]
    assert (await sim.get_payout(payout["id"]))["status"] == "completed"
    assert await sim.balance("bank", "BRL") == "-50.10"


async def test_a_reset_in_realtime_leaves_the_ticker_running(launch: Launch) -> None:
    sim = await launch(clock_mode="realtime", pix_settle_seconds=0)
    await sim.control("POST", "/reset")
    beneficiary = await sim.beneficiary("BRL")
    await sim.payout(beneficiary["id"], "50.00", "BRL")

    await the_webhook(sim, "payout.completed")

    assert await sim.balance("bank", "BRL") == "-50.10"

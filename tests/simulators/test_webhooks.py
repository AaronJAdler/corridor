"""Webhook delivery: signed, at least once, unordered and not guaranteed, on demand."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest

from corridor_sim.clock import format_time
from tests.simulators.conftest import (
    BANK_WEBHOOK_SECRET,
    BANK_WEBHOOK_URL,
    CUSTODY_WEBHOOK_SECRET,
    CUSTODY_WEBHOOK_URL,
    START,
    Answer,
    Sim,
    address_with_body,
    signed_at,
)

Launch = Callable[..., Awaitable[Sim]]

# The contract's tables: the fields each event's ``data`` carries, and who sends it.
DEPOSIT_FIELDS = {
    "deposit_id",
    "address_id",
    "address",
    "customer_reference",
    "asset",
    "amount",
    "tx_hash",
    "from_address",
    "confirmations",
}
BANK_EVENTS = {
    "deposit.received": {
        "deposit_id",
        "virtual_account_id",
        "customer_reference",
        "asset",
        "amount",
        "sender_name",
        "reference",
    },
    "deposit.returned": {"deposit_id", "asset", "amount", "reason"},
    "payout.completed": {"payout_id", "reference", "asset", "amount", "fee", "settled_at"},
    "payout.failed": {"payout_id", "reference", "asset", "amount", "failure_reason"},
}
CUSTODY_EVENTS = {
    "deposit.detected": DEPOSIT_FIELDS,
    "deposit.confirmed": DEPOSIT_FIELDS,
    "deposit.failed": {"deposit_id", "asset", "amount", "tx_hash", "reason"},
    "withdrawal.completed": {
        "withdrawal_id",
        "reference",
        "asset",
        "amount",
        "network_fee",
        "tx_hash",
    },
    "withdrawal.failed": {"withdrawal_id", "reference", "asset", "amount", "failure_reason"},
}

# The times of the six attempts on an event that keeps failing, in seconds after the first:
# each retry follows the failure before it by 1, 2, 4, 8 and 16 seconds.
ATTEMPT_TIMES = [0, 1, 3, 7, 15, 31]


def after(seconds: float) -> str:
    return format_time(START + timedelta(seconds=seconds))


async def never() -> int:
    await asyncio.Event().wait()
    return 200


async def one_of_everything(sim: Sim) -> None:
    """Make each provider send every type of event it has."""
    account = await sim.virtual_account()
    deposit = await sim.bank_deposit(account["id"], "250.00")
    await sim.control("POST", f"/bank/deposits/{deposit['id']}/return", {"reason": "recalled"})
    open_account = await sim.beneficiary()
    closed_account = await sim.beneficiary(account_number="000123450000")
    await sim.payout(open_account["id"], reference="wd-1")
    await sim.payout(closed_account["id"], reference="wd-2")

    address = await sim.address()
    await sim.chain_deposit(address["address"], "10.000000")
    lost = await sim.chain_deposit(address["address"], "5.000000")
    await sim.control("POST", f"/custody/deposits/{lost['id']}/drop")
    await sim.withdrawal(address_with_body("receiver".ljust(32, "a")), reference="wd-3")
    await sim.withdrawal(address_with_body("dead".ljust(32, "a")), reference="wd-4")
    await sim.advance(30)


async def a_deposit(sim: Sim, reference: str = "INV-2041") -> dict[str, Any]:
    account = await sim.virtual_account()
    return await sim.bank_deposit(account["id"], "250.00", reference=reference)


# --- what is sent ----------------------------------------------------------------------------


async def test_nothing_is_sent_when_an_event_is_made(sim: Sim) -> None:
    await a_deposit(sim)

    assert sim.receiver.requests == []
    (event,) = await sim.events()
    assert (event["status"], event["attempts"]) == ("pending", [])
    assert event["next_attempt_at"] == after(0)


async def test_every_event_is_delivered_exactly_once(sim: Sim) -> None:
    await one_of_everything(sim)
    await sim.advance(60)

    events = await sim.events()
    sent = [request.json()["id"] for request in sim.receiver.requests]
    assert sorted(sent) == sorted(event["id"] for event in events)
    assert {event["type"] for event in events} == set(BANK_EVENTS) | set(CUSTODY_EVENTS)
    for event in events:
        assert event["status"] == "delivered", event
        assert [attempt["status_code"] for attempt in event["attempts"]] == [200]
        assert event["next_attempt_at"] is None


@pytest.mark.parametrize("event_type", sorted(BANK_EVENTS))
async def test_a_bank_event_is_signed_with_the_banks_secret_and_carries_the_contracts_fields(
    sim: Sim, event_type: str
) -> None:
    await one_of_everything(sim)

    (request,) = sim.receiver.of_type(event_type)

    assert (request.method, request.url) == ("POST", BANK_WEBHOOK_URL)
    assert request.headers["content-type"] == "application/json"
    signed_at(request, BANK_WEBHOOK_SECRET)
    envelope = request.json()
    assert set(envelope) == {"id", "type", "created_at", "data"}
    assert envelope["id"].startswith("evt_")
    assert set(envelope["data"]) == BANK_EVENTS[event_type]


@pytest.mark.parametrize("event_type", sorted(CUSTODY_EVENTS))
async def test_a_custody_event_is_signed_with_the_custodians_secret_and_carries_the_contracts_fields(
    sim: Sim, event_type: str
) -> None:
    await one_of_everything(sim)

    requests = sim.receiver.of_type(event_type)

    # Two deposits were seen; one of everything else happened.
    assert len(requests) == (2 if event_type == "deposit.detected" else 1)
    for request in requests:
        assert (request.method, request.url) == ("POST", CUSTODY_WEBHOOK_URL)
        signed_at(request, CUSTODY_WEBHOOK_SECRET)
        envelope = request.json()
        assert set(envelope) == {"id", "type", "created_at", "data"}
        assert set(envelope["data"]) == CUSTODY_EVENTS[event_type]


async def test_each_provider_signs_with_its_own_secret(sim: Sim) -> None:
    await a_deposit(sim)
    await sim.deliver()

    (request,) = sim.receiver.requests

    with pytest.raises(AssertionError, match="does not match"):
        signed_at(request, CUSTODY_WEBHOOK_SECRET)


async def test_the_envelope_is_the_event_as_the_books_recorded_it(sim: Sim) -> None:
    await sim.advance(90)
    deposit = await a_deposit(sim)
    await sim.advance(5)

    (request,) = sim.receiver.requests
    (event,) = await sim.events()

    assert request.json() == {
        "id": event["id"],
        "type": "deposit.received",
        "created_at": after(90),
        "data": {
            "deposit_id": deposit["id"],
            "virtual_account_id": deposit["virtual_account_id"],
            "customer_reference": deposit["customer_reference"],
            "asset": "USD",
            "amount": "250.00",
            "sender_name": "Joao Souza",
            "reference": "INV-2041",
        },
    }


async def test_the_signatures_timestamp_is_the_simulators_time_of_sending(sim: Sim) -> None:
    await a_deposit(sim)
    await sim.advance(90)

    (request,) = sim.receiver.requests

    assert signed_at(request, BANK_WEBHOOK_SECRET) == int(START.timestamp()) + 90


async def test_the_deposit_events_follow_the_deposit_to_finality(sim: Sim) -> None:
    address = await sim.address()
    await sim.chain_deposit(address["address"], "10.000000")
    await sim.deliver()
    await sim.mine(3)

    (detected,) = sim.receiver.of_type("deposit.detected")
    (confirmed,) = sim.receiver.of_type("deposit.confirmed")

    assert detected.json()["data"]["confirmations"] == 0
    assert confirmed.json()["data"] == {**detected.json()["data"], "confirmations": 3}
    assert [request.json()["type"] for request in sim.receiver.requests] == [
        "deposit.detected",
        "deposit.confirmed",
    ]


async def test_a_provider_with_no_webhook_url_records_its_events_and_sends_none(
    launch: Launch,
) -> None:
    sim = await launch(custody_webhook_url=None)
    address = await sim.address()
    await sim.chain_deposit(address["address"], "10.000000")
    await a_deposit(sim)

    await sim.advance(60)

    assert [request.json()["type"] for request in sim.receiver.requests] == ["deposit.received"]
    custody = [event for event in await sim.events() if event["provider"] == "custody"]
    assert [event["type"] for event in custody] == ["deposit.detected", "deposit.confirmed"]
    assert all(event["status"] == "undeliverable" and not event["attempts"] for event in custody)


# --- retries ---------------------------------------------------------------------------------


async def step_through(sim: Sim, seconds: int) -> None:
    """Let time pass in half seconds, so that an attempt is seen at the moment it is due."""
    await sim.advance(0)
    for _ in range(seconds * 2):
        await sim.advance(0.5)


@pytest.mark.parametrize(
    ("answer", "status_code", "error"),
    [
        (500, 500, None),
        (404, 404, None),
        (302, 302, None),
        (RuntimeError("boom"), None, "RuntimeError"),
    ],
)
async def test_a_failing_receiver_is_retried_on_the_contracts_schedule_then_abandoned(
    sim: Sim, answer: Answer, status_code: int | None, error: str | None
) -> None:
    sim.receiver.answer(*[answer] * 10)
    await a_deposit(sim)

    await step_through(sim, 120)

    (event,) = await sim.events()
    assert event["attempts"] == [
        {"at": after(seconds), "status_code": status_code, "error": error, "duplicate": False}
        for seconds in ATTEMPT_TIMES
    ]
    assert (event["status"], event["next_attempt_at"]) == ("abandoned", None)
    assert len(sim.receiver.requests) == 6
    assert {request.json()["id"] for request in sim.receiver.requests} == {event["id"]}
    assert len({request.body for request in sim.receiver.requests}) == 1


async def test_a_receiver_that_does_not_answer_in_time_is_retried_the_same_way(
    launch: Launch,
) -> None:
    sim = await launch(webhook_timeout_seconds=0.02)
    sim.receiver.answer(*[never] * 10)
    await a_deposit(sim)

    await step_through(sim, 40)

    (event,) = await sim.events()
    assert [attempt["at"] for attempt in event["attempts"]] == [after(s) for s in ATTEMPT_TIMES]
    assert {attempt["error"] for attempt in event["attempts"]} == {"timeout"}
    assert event["status"] == "abandoned"


async def test_each_retry_is_signed_afresh_with_the_time_it_is_sent(sim: Sim) -> None:
    sim.receiver.answer(500, 500)
    await a_deposit(sim)

    await step_through(sim, 10)

    assert [
        signed_at(request, BANK_WEBHOOK_SECRET) - int(START.timestamp())
        for request in sim.receiver.requests
    ] == [0, 1, 3]


async def test_an_event_that_gets_through_on_a_retry_is_delivered_and_left_alone(
    sim: Sim,
) -> None:
    sim.receiver.answer(500, RuntimeError("boom"))
    await a_deposit(sim)

    await step_through(sim, 60)

    (event,) = await sim.events()
    assert [(a["at"], a["status_code"], a["error"]) for a in event["attempts"]] == [
        (after(0), 500, None),
        (after(1), None, "RuntimeError"),
        (after(3), 200, None),
    ]
    assert event["status"] == "delivered"
    assert len(sim.receiver.requests) == 3


async def test_a_retry_is_not_sent_before_it_is_due(sim: Sim) -> None:
    sim.receiver.answer(500, 500)
    await a_deposit(sim)

    await sim.advance(0)
    await sim.advance(0.999)
    before_the_first = len(sim.receiver.requests)
    await sim.advance(0.001)
    await sim.advance(1.999)
    before_the_second = len(sim.receiver.requests)
    await sim.advance(0.001)

    assert (before_the_first, before_the_second, len(sim.receiver.requests)) == (1, 2, 3)


async def test_one_tick_makes_one_attempt_however_far_the_clock_moved(sim: Sim) -> None:
    sim.receiver.answer(500, 500, 500)
    await a_deposit(sim)

    await sim.advance(3600)

    (event,) = await sim.events()
    assert [attempt["at"] for attempt in event["attempts"]] == [after(3600)]
    assert event["next_attempt_at"] == after(3601)


@pytest.mark.parametrize("answer", [500, RuntimeError("boom")])
async def test_a_delivery_failure_breaks_neither_the_clock_nor_a_provider_call(
    sim: Sim, answer: Answer
) -> None:
    sim.receiver.answer(*[answer] * 50)
    beneficiary = await sim.beneficiary()
    payout = await sim.payout(beneficiary["id"])

    for _ in range(70):
        await sim.advance(1)
    await sim.mine(1)
    deliveries = await sim.deliver()

    assert deliveries == []
    assert (await sim.get_payout(payout["id"]))["status"] == "completed"
    assert (await sim.post_payout(beneficiary["id"], reference="wd-2")).status_code == 201
    assert await sim.balance("bank", "USD") == "-100.25"
    (event,) = await sim.events("payout.completed")
    assert (event["status"], len(event["attempts"])) == ("abandoned", 6)


# --- the control endpoints -------------------------------------------------------------------


async def test_deliver_attempts_what_is_due_now_and_reports_each_outcome(sim: Sim) -> None:
    sim.receiver.answer(500)
    await a_deposit(sim, "INV-1")
    await a_deposit(sim, "INV-2")
    first, second = await sim.events()

    deliveries = await sim.deliver()

    assert deliveries == [
        {
            "event_id": first["id"],
            "type": "deposit.received",
            "provider": "bank",
            "event_status": "pending",
            "at": after(0),
            "status_code": 500,
            "error": None,
            "duplicate": False,
        },
        {
            "event_id": second["id"],
            "type": "deposit.received",
            "provider": "bank",
            "event_status": "delivered",
            "at": after(0),
            "status_code": 200,
            "error": None,
            "duplicate": False,
        },
    ]
    # Nothing more is due until the retry's second has passed.
    assert await sim.deliver() == []
    assert (await sim.control("GET", "/clock"))["now"] == after(0)


async def test_the_event_list_shows_every_event_and_every_attempt(sim: Sim) -> None:
    sim.receiver.answer(503)
    deposit = await a_deposit(sim)
    await sim.advance(0)
    await sim.advance(1)

    (event,) = await sim.events()

    assert set(event) == {
        "id",
        "type",
        "provider",
        "created_at",
        "data",
        "status",
        "next_attempt_at",
        "attempts",
    }
    assert (event["type"], event["provider"], event["created_at"]) == (
        "deposit.received",
        "bank",
        after(0),
    )
    assert event["data"]["deposit_id"] == deposit["id"]
    assert event["attempts"] == [
        {"at": after(0), "status_code": 503, "error": None, "duplicate": False},
        {"at": after(1), "status_code": 200, "error": None, "duplicate": False},
    ]


# --- behaviour switches ----------------------------------------------------------------------


async def test_the_behaviour_endpoint_changes_only_the_switches_it_is_given(sim: Sim) -> None:
    first = await sim.control("POST", "/webhooks/behaviour", {"duplicates": 2, "hold": True})
    second = await sim.control(
        "POST", "/webhooks/behaviour", {"drop_types": ["payout.failed"], "hold": False}
    )

    assert first == {"duplicates": 2, "drop_types": [], "hold": True, "reverse": False}
    assert second == {
        "duplicates": 2,
        "drop_types": ["payout.failed"],
        "hold": False,
        "reverse": False,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"duplicates": -1},
        {"duplicates": "2"},
        {"duplicates": 1000},
        {"drop_types": "payout.completed"},
        {"drop_types": [7]},
        {"hold": "yes"},
        {"reverse": 1},
        [],
    ],
)
async def test_a_malformed_behaviour_is_refused_and_changes_nothing(sim: Sim, body: object) -> None:
    response = await sim.anonymous.post("/_control/webhooks/behaviour", json=body)

    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_request")
    assert await sim.control("POST", "/webhooks/behaviour", {}) == {
        "duplicates": 0,
        "drop_types": [],
        "hold": False,
        "reverse": False,
    }


async def test_duplicates_sends_each_event_that_many_extra_times(sim: Sim) -> None:
    await sim.control("POST", "/webhooks/behaviour", {"duplicates": 2})
    await a_deposit(sim)

    await sim.advance(60)

    (event,) = await sim.events()
    assert len(sim.receiver.requests) == 3
    assert len({request.body for request in sim.receiver.requests}) == 1
    for request in sim.receiver.requests:
        signed_at(request, BANK_WEBHOOK_SECRET)
    assert [attempt["duplicate"] for attempt in event["attempts"]] == [False, True, True]
    assert event["status"] == "delivered"


async def test_an_event_is_duplicated_only_once_it_has_been_delivered(sim: Sim) -> None:
    await sim.control("POST", "/webhooks/behaviour", {"duplicates": 1})
    sim.receiver.answer(500)
    await a_deposit(sim)

    await sim.advance(0)
    after_the_failure = len(sim.receiver.requests)
    await sim.advance(1)

    assert (after_the_failure, len(sim.receiver.requests)) == (1, 3)


async def test_a_dropped_type_is_recorded_and_never_sent(sim: Sim) -> None:
    await sim.control("POST", "/webhooks/behaviour", {"drop_types": ["payout.completed"]})
    beneficiary = await sim.beneficiary()
    await sim.payout(beneficiary["id"])
    await a_deposit(sim)

    await sim.advance(120)
    await sim.control("POST", "/webhooks/behaviour", {"drop_types": []})
    await sim.advance(120)

    assert [request.json()["type"] for request in sim.receiver.requests] == ["deposit.received"]
    (dropped,) = await sim.events("payout.completed")
    assert (dropped["status"], dropped["attempts"]) == ("dropped", [])


async def test_held_events_wait_until_the_hold_is_released(sim: Sim) -> None:
    await sim.control("POST", "/webhooks/behaviour", {"hold": True})
    await a_deposit(sim, "INV-1")
    await sim.advance(600)
    await a_deposit(sim, "INV-2")
    held = await sim.deliver()
    sent_while_held = len(sim.receiver.requests)

    await sim.control("POST", "/webhooks/behaviour", {"hold": False})
    still_unsent = len(sim.receiver.requests)
    await sim.advance(0)

    assert (held, sent_while_held, still_unsent) == ([], 0, 0)
    assert [request.json()["data"]["reference"] for request in sim.receiver.requests] == [
        "INV-1",
        "INV-2",
    ]
    assert {event["status"] for event in await sim.events()} == {"delivered"}


async def test_due_events_are_sent_oldest_first(sim: Sim) -> None:
    for reference in ("INV-1", "INV-2", "INV-3"):
        await a_deposit(sim, reference)

    await sim.advance(0)

    assert [request.json()["data"]["reference"] for request in sim.receiver.requests] == [
        "INV-1",
        "INV-2",
        "INV-3",
    ]


async def test_reverse_sends_the_due_events_newest_first(sim: Sim) -> None:
    await sim.control("POST", "/webhooks/behaviour", {"reverse": True})
    for reference in ("INV-1", "INV-2", "INV-3"):
        await a_deposit(sim, reference)

    await sim.advance(0)

    assert [request.json()["data"]["reference"] for request in sim.receiver.requests] == [
        "INV-3",
        "INV-2",
        "INV-1",
    ]

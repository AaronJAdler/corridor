"""A withdrawal against a provider that fails: it is paid out exactly once, or not at all.

Every test runs the whole system, lets time pass until nothing is in flight, and then
asserts the same things: one payout at the provider for the withdrawal, the user's balance
and both fees to the unit, nothing left reserved, no event given up on, and books that
agree with the provider's.
"""

import asyncio

import pytest

from tests.chaos.checks import (
    Requested,
    assert_at_rest,
    assert_given_back,
    assert_paid_out_once,
)
from tests.chaos.scenarios import KINDS, MODES, requested
from tests.support.providers import CLOSED_ACCOUNT_NUMBER, REJECTED_ADDRESS
from tests.support.stack import SUBMIT_POINTS, WEBHOOK_POINTS, Stack


async def posts(stack: Stack, withdrawal: Requested) -> int:
    """How many times the provider was asked to send the withdrawal."""
    path = "/bank/v1/payouts" if withdrawal.kind == "bank" else "/custody/v1/withdrawals"
    return len(stack.sim.recorder.sent("POST", path))


@pytest.mark.parametrize("kind", KINDS)
async def test_a_withdrawal_with_nothing_going_wrong_is_paid_out_once(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)

    await stack.settle()

    await assert_paid_out_once(stack, withdrawal)
    assert await posts(stack, withdrawal) == 1


# --- a provider that fails to accept it cleanly ----------------------------------------------


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_withdrawal_is_paid_out_once_when_the_request_to_the_provider_fails(
    stack: Stack, kind: str, mode: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.fault(withdrawal.create_operation, mode)

    await stack.settle()

    await assert_paid_out_once(stack, withdrawal)
    # Asked again after the failure, under the same key: the assertion above found one payout.
    assert await posts(stack, withdrawal) == 2


@pytest.mark.parametrize("kind", KINDS)
async def test_a_withdrawal_is_paid_out_once_when_the_provider_fails_in_every_way_in_turn(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    for mode in ("timeout_after_effect", "error", "error_after_effect", "timeout"):
        await stack.fault(withdrawal.create_operation, mode)

    await stack.settle()

    await assert_paid_out_once(stack, withdrawal)
    # Not always all five: a chain withdrawal the custodian accepted is final within a few
    # blocks, and its completion can settle it before the last retries are due.
    assert 2 <= await posts(stack, withdrawal) <= 5


@pytest.mark.parametrize("kind", KINDS)
async def test_a_withdrawal_the_provider_refuses_goes_back_to_the_user(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.fault(withdrawal.create_operation, "error", status=422)

    await stack.settle()

    await assert_given_back(stack, withdrawal)
    assert (await stack.withdrawal(withdrawal.id))["failure_reason"] == "injected_fault"
    assert await stack.provider_sends(withdrawal.id) == []


@pytest.mark.parametrize("first", ["error_after_effect", "timeout_after_effect"])
@pytest.mark.parametrize("kind", KINDS)
async def test_a_refusal_of_the_retry_does_not_give_back_what_the_first_attempt_sent(
    stack: Stack, kind: str, first: str
) -> None:
    withdrawal = await requested(stack, kind)
    # The provider accepts the payout and fails to say so. The retry is then refused, as a
    # provider that is shedding load refuses everything: a refusal of that request, which
    # says nothing about the payout the first one made.
    await stack.fault(withdrawal.create_operation, first)
    await stack.fault(withdrawal.create_operation, "error", status=429)

    await stack.settle()

    await assert_paid_out_once(stack, withdrawal)


@pytest.mark.parametrize("status", [422, 429])
@pytest.mark.parametrize("kind", KINDS)
async def test_a_provider_that_refuses_a_request_it_carried_out_does_not_get_the_funds_released(
    stack: Stack, kind: str, status: int
) -> None:
    withdrawal = await requested(stack, kind)
    # A 4xx for a request that was carried out: the contract rules it out, the simulator
    # can do it, and a real provider's proxy or rate limiter could.
    await stack.fault(withdrawal.create_operation, "error_after_effect", status=status)

    await stack.settle()

    await assert_paid_out_once(stack, withdrawal)
    assert await posts(stack, withdrawal) == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_a_refusal_of_the_retry_gives_the_funds_back_when_nothing_was_ever_sent(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.fault(withdrawal.create_operation, "error")
    await stack.fault(withdrawal.create_operation, "error", status=422)

    await stack.settle()

    await assert_given_back(stack, withdrawal)
    assert await stack.provider_sends(withdrawal.id) == []


async def test_a_payout_the_bank_cannot_deliver_goes_back_to_the_user(stack: Stack) -> None:
    withdrawal = await requested(stack, "bank", account_number=CLOSED_ACCOUNT_NUMBER)
    await stack.fault(withdrawal.create_operation, "timeout_after_effect")

    await stack.settle()

    await assert_given_back(stack, withdrawal)
    assert (await stack.withdrawal(withdrawal.id))["failure_reason"] == "account_closed"
    assert len(await stack.provider_sends(withdrawal.id)) == 1


async def test_a_withdrawal_the_network_rejects_goes_back_to_the_user(stack: Stack) -> None:
    withdrawal = await requested(stack, "chain", to_address=REJECTED_ADDRESS)
    await stack.fault(withdrawal.create_operation, "error_after_effect")

    await stack.settle()

    await assert_given_back(stack, withdrawal)
    assert (await stack.withdrawal(withdrawal.id))["failure_reason"] == "rejected_by_network"


# --- a worker that dies ----------------------------------------------------------------------


@pytest.mark.parametrize("point", SUBMIT_POINTS)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_worker_that_dies_while_sending_a_withdrawal_does_not_send_it_twice(
    stack: Stack, kind: str, point: str
) -> None:
    withdrawal = await requested(stack, kind)
    stack.crashes.arm(point)

    await stack.settle()

    assert stack.crashes.hits == [point]
    await assert_paid_out_once(stack, withdrawal)


@pytest.mark.parametrize("point", WEBHOOK_POINTS)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_worker_that_dies_while_settling_a_withdrawal_does_not_settle_it_twice(
    stack: Stack, kind: str, point: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.turn(1)
    assert (await stack.withdrawal(withdrawal.id))["status"] == "submitted"
    # From here the next provider event is the one that says the payout is final.
    stack.crashes.arm(point)

    await stack.settle()

    assert stack.crashes.hits == [point]
    await assert_paid_out_once(stack, withdrawal)


@pytest.mark.parametrize("kind", KINDS)
async def test_a_worker_that_dies_at_every_step_twice_still_pays_out_once(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    for point in ("submit.before_provider", "submit.after_provider", "submit.end"):
        stack.crashes.arm(point, times=2)
    for point in WEBHOOK_POINTS:
        stack.crashes.arm(point)

    await stack.settle()

    assert len(stack.crashes.hits) == 9
    await assert_paid_out_once(stack, withdrawal)


# --- cancelling while it is being sent -------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_a_cancellation_that_arrives_before_the_worker_wins_and_nothing_is_sent(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)

    canceled = await stack.cancel(withdrawal.user, withdrawal.id)
    await stack.settle()

    assert (canceled.status_code, canceled.json()["status"]) == (200, "canceled")
    await assert_given_back(stack, withdrawal, status="canceled")
    assert await posts(stack, withdrawal) == 0


@pytest.mark.parametrize("kind", KINDS)
async def test_a_cancellation_between_the_mark_and_the_provider_call_is_refused(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    answers: list[tuple[int, str]] = []

    async def user_cancels(withdrawal_id: object) -> None:
        response = await stack.cancel(withdrawal.user, str(withdrawal_id))
        answers.append((response.status_code, response.json()["code"]))

    stack.hooks.before_send = user_cancels

    await stack.settle()

    assert answers == [(409, "withdrawal_not_cancelable")]
    # The payout was made, and what pays for it was never given back.
    await assert_paid_out_once(stack, withdrawal)


async def test_cancelling_while_the_worker_drains_never_releases_what_is_paid_out(
    stack: Stack,
) -> None:
    first = await requested(stack, "bank")
    beneficiary = await stack.beneficiary(first.user)
    asked = [first.id]
    for _ in range(11):
        response = await stack.withdraw(first.user, "10.00", beneficiary_id=beneficiary)
        assert response.status_code == 202, response.text
        asked.append(response.json()["id"])

    # The worker and the user at the same moment, for a dozen withdrawals at once.
    answers = (
        await asyncio.gather(
            stack.dispatcher.drain(), *(stack.cancel(first.user, wid) for wid in asked)
        )
    )[1:]
    await stack.settle()

    await assert_at_rest(stack)
    spent = 0
    for withdrawal_id, answer in zip(asked, answers, strict=True):
        row = await stack.withdrawal(withdrawal_id)
        sent = await stack.provider_sends(withdrawal_id)
        if answer.status_code == 200:
            assert (row["status"], sent) == ("canceled", [])
        else:
            assert (answer.status_code, row["status"], len(sent)) == (409, "completed", 1)
            spent += row["amount"] + row["fee"]
    assert await stack.wallet(first.user) == (first.funded - spent, 0)

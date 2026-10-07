"""Providers whose webhooks arrive twice, late, out of order, or never.

A provider's word is at least once, unordered and not guaranteed. What survives a webhook
that never comes is said here too, and so is what does not: a bank deposit whose only
announcement is lost stays uncredited, because nothing in this build reads the bank's
statement yet.
"""

from typing import Any

import pytest

from corridor.ledger import AccountKind
from tests.chaos.checks import (
    assert_at_rest,
    assert_books_match_the_providers,
    assert_given_back,
    assert_paid_out_once,
)
from tests.chaos.scenarios import FUNDING, KINDS, funded, requested
from tests.payments.support import rows
from tests.support.providers import CLOSED_ACCOUNT_NUMBER, REJECTED_ADDRESS
from tests.support.stack import Stack


async def deposit(stack: Stack, kind: str) -> tuple[Any, str, int, str]:
    """A new user's deposit, on its way: the user, the asset, the amount, the provider's id."""
    user = await stack.person()
    asset, amount, minor = FUNDING[kind]
    if kind == "bank":
        return user, asset, minor, f"simbank:{await stack.bank_deposit(user, amount)}"
    return user, asset, minor, f"simcustody:{await stack.chain_deposit(user, amount)}"


async def attempts(stack: Stack, event_type: str) -> list[tuple[int | None, bool]]:
    """Each delivery attempt of the events of one type: the answer, and whether it was a
    copy sent after the event had already been delivered."""
    return [
        (attempt["status_code"], attempt["duplicate"])
        for event in await stack.events()
        if event["type"] == event_type
        for attempt in event["attempts"]
    ]


# --- twice -----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_a_deposit_announced_four_times_is_acknowledged_each_time_and_credited_once(
    stack: Stack, kind: str
) -> None:
    await stack.webhooks_behave(duplicates=3)
    user, asset, amount, _ = await deposit(stack, kind)

    await stack.settle()

    await assert_at_rest(stack)
    assert await stack.wallet(user, asset) == (amount, 0)
    final = "deposit.received" if kind == "bank" else "deposit.confirmed"
    # Every copy got a 200: a repeat is acknowledged, not refused, or the provider would
    # go on retrying it.
    assert await attempts(stack, final) == [(200, False), (200, True), (200, True), (200, True)]
    stored = await rows(
        stack.db, "SELECT count(*) AS events FROM webhook_events WHERE type = :t", t=final
    )
    assert stored[0]["events"] == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_a_completion_announced_four_times_settles_the_withdrawal_once(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.webhooks_behave(duplicates=3)

    await stack.settle()

    await assert_paid_out_once(stack, withdrawal)
    assert await attempts(stack, withdrawal.completed_event) == [
        (200, False),
        (200, True),
        (200, True),
        (200, True),
    ]
    settled = await rows(
        stack.db, "SELECT count(*) AS entries FROM journal_entries WHERE kind = 'withdrawal_settle'"
    )
    assert settled[0]["entries"] == 1


# --- out of order ----------------------------------------------------------------------------


async def test_a_confirmation_delivered_before_its_detection_credits_the_deposit_once(
    stack: Stack,
) -> None:
    await stack.webhooks_behave(hold=True, reverse=True)
    user, asset, amount, _ = await deposit(stack, "chain")
    await stack.sim.mine(3)
    assert [event["type"] for event in await stack.events()] == [
        "deposit.detected",
        "deposit.confirmed",
    ]

    await stack.webhooks_behave(hold=False)
    await stack.settle()

    await assert_at_rest(stack)
    assert await stack.wallet(user, asset) == (amount, 0)
    received = await rows(stack.db, "SELECT type FROM webhook_events ORDER BY received_at, id")
    assert [event["type"] for event in received] == ["deposit.confirmed", "deposit.detected"]
    (stored,) = await stack.deposits()
    assert stored["status"] == "completed"


async def test_a_return_delivered_before_its_deposit_waits_for_it_and_then_takes_it_back(
    stack: Stack,
) -> None:
    await stack.webhooks_behave(hold=True, reverse=True)
    user, asset, _, source = await deposit(stack, "bank")
    await stack.sim.return_bank_deposit(source.removeprefix("simbank:"))

    await stack.webhooks_behave(hold=False)
    await stack.settle()

    # The return was tried first, found no deposit, and was retried after it.
    await assert_at_rest(stack)
    assert await stack.wallet(user, asset) == (0, 0)
    (stored,) = await stack.deposits()
    assert stored["status"] == "returned"
    retried = await rows(
        stack.db,
        "SELECT max(attempts) AS attempts FROM outbox_events WHERE topic = :t",
        t="webhook.received",
    )
    assert retried[0]["attempts"] >= 2


@pytest.mark.parametrize("kind", KINDS)
async def test_every_event_of_a_deposit_and_a_withdrawal_delivered_newest_first_converges(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.webhooks_behave(reverse=True, duplicates=1)
    second, asset, amount, _ = await deposit(stack, kind)

    await stack.settle()

    await assert_at_rest(stack)
    assert await stack.wallet(second, asset) == (amount, 0)
    assert (await stack.withdrawal(withdrawal.id))["status"] == "completed"
    assert await stack.wallet(withdrawal.user, asset) == (
        withdrawal.funded - withdrawal.amount - withdrawal.fee,
        0,
    )


# --- held back -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_a_completion_held_back_briefly_settles_the_withdrawal_when_it_is_let_go(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.webhooks_behave(hold=True)
    for _ in range(8):
        await stack.turn()
    # The provider has paid; Corridor has not been told, and is not yet worried.
    assert (await stack.withdrawal(withdrawal.id))["status"] == "submitted"
    assert [sent["status"] for sent in await stack.provider_sends(withdrawal.id)] == ["completed"]

    await stack.webhooks_behave(hold=False)
    await stack.settle()

    await assert_paid_out_once(stack, withdrawal)
    assert await attempts(stack, withdrawal.completed_event) == [(200, False)]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_completion_held_back_for_long_is_overtaken_by_the_sweeper_and_changes_nothing(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.webhooks_behave(hold=True)

    await stack.settle()

    # Settled by polling, with the webhook still in the provider's queue.
    assert (await stack.withdrawal(withdrawal.id))["status"] == "completed"
    assert await attempts(stack, withdrawal.completed_event) == []

    await stack.webhooks_behave(hold=False)
    await stack.settle()

    await assert_paid_out_once(stack, withdrawal)
    assert await attempts(stack, withdrawal.completed_event) == [(200, False)]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_deposit_whose_events_are_held_back_is_credited_when_they_are_let_go(
    stack: Stack, kind: str
) -> None:
    await stack.webhooks_behave(hold=True)
    user, asset, amount, _ = await deposit(stack, kind)
    await stack.settle(rounds=10)
    assert await stack.wallet(user, asset) == (0, 0)

    await stack.webhooks_behave(hold=False)
    await stack.settle()

    await assert_at_rest(stack)
    assert await stack.wallet(user, asset) == (amount, 0)


# --- never -----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_a_completion_that_is_never_delivered_is_found_by_the_sweeper(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.webhooks_behave(drop_types=[withdrawal.completed_event])

    await stack.settle()

    assert await attempts(stack, withdrawal.completed_event) == []
    await assert_paid_out_once(stack, withdrawal)


@pytest.mark.parametrize("kind", KINDS)
async def test_a_dropped_completion_is_found_though_the_provider_fails_when_it_is_first_asked(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.webhooks_behave(drop_types=[withdrawal.completed_event])
    await stack.fault(withdrawal.read_operation, "error", times=2)
    await stack.fault(withdrawal.read_operation, "timeout")

    await stack.settle()

    await assert_paid_out_once(stack, withdrawal)


@pytest.mark.parametrize("kind", KINDS)
async def test_a_dropped_completion_is_found_when_the_submission_was_never_recorded_either(
    stack: Stack, kind: str
) -> None:
    withdrawal = await requested(stack, kind)
    await stack.webhooks_behave(drop_types=[withdrawal.completed_event])
    # The first attempt reaches the provider, and neither it nor any retry for minutes
    # hears back. Nothing here knows the payout's id: the sweeper has only the reference.
    await stack.fault(withdrawal.create_operation, "error_after_effect", times=7)

    await stack.settle()

    await assert_paid_out_once(stack, withdrawal)
    path = "/bank/v1/payouts" if kind == "bank" else "/custody/v1/withdrawals"
    assert [
        request.url.params["reference"] for request in stack.sim.recorder.sent("GET", path)
    ] == [withdrawal.id]


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("bank", {"account_number": CLOSED_ACCOUNT_NUMBER}),
        ("chain", {"to_address": REJECTED_ADDRESS}),
    ],
)
async def test_a_failure_that_is_never_delivered_is_found_by_the_sweeper_and_the_funds_go_back(
    stack: Stack, kind: str, target: dict[str, str]
) -> None:
    withdrawal = await requested(stack, kind, **target)
    await stack.webhooks_behave(drop_types=[withdrawal.failed_event])

    await stack.settle()

    assert await attempts(stack, withdrawal.failed_event) == []
    await assert_given_back(stack, withdrawal)


async def test_a_chain_deposit_whose_detection_is_never_delivered_is_credited_on_confirmation(
    stack: Stack,
) -> None:
    await stack.webhooks_behave(drop_types=["deposit.detected"])
    user, asset, amount, _ = await deposit(stack, "chain")

    await stack.settle()

    await assert_at_rest(stack)
    assert await stack.wallet(user, asset) == (amount, 0)


async def test_a_bank_deposit_whose_announcement_is_never_delivered_stays_uncredited(
    stack: Stack,
) -> None:
    """Not recoverable in this build. ``deposit.received`` is the only way Corridor learns
    of a bank deposit, and nothing polls the bank's statement until reconciliation exists.
    This test states the gap: the money is at the bank, and no user has it."""
    kept = await funded(stack, "bank")
    await stack.webhooks_behave(drop_types=["deposit.received"])
    user, asset, amount, source = await deposit(stack, "bank")

    await stack.settle()
    for _ in range(60):
        await stack.turn(60)

    await assert_at_rest(stack, lost_deposits=[source])
    assert await stack.wallet(user, asset) == (0, 0)
    assert await stack.wallet(kept, asset) == (FUNDING["bank"][2], 0)
    assert len(await stack.deposits()) == 1
    # The bank holds what Corridor's books do not know about: exactly the lost deposit.
    assert (
        await stack.provider_balance("bank", asset)
        - await stack.book_balance(AccountKind.BANK_SETTLEMENT, asset, provider="simbank")
        == amount
    )
    with pytest.raises(AssertionError):
        await assert_books_match_the_providers(stack)


async def test_a_chain_deposit_whose_confirmation_is_never_delivered_stays_pending(
    stack: Stack,
) -> None:
    """The same gap on the chain: the detection recorded the deposit, so the user can see
    it coming, and without the confirmation it is never credited."""
    await stack.webhooks_behave(drop_types=["deposit.confirmed"])
    user, asset, amount, source = await deposit(stack, "chain")

    await stack.settle()

    await assert_at_rest(stack, lost_deposits=[source])
    assert await stack.wallet(user, asset) == (0, 0)
    (stored,) = await stack.deposits()
    assert (stored["status"], stored["entry_id"]) == ("pending", None)
    assert await stack.provider_balance("custody", asset) == amount
    assert await stack.book_balance(AccountKind.CUSTODY_OMNIBUS, asset, provider="simcustody") == 0

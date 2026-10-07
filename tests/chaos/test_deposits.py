"""A deposit against a provider and a worker that fail: it is credited exactly once.

A deposit begins at the provider. What Corridor asks of the provider is only where the
user should send the money, so that is the request that is made to fail here; after that,
the failures are in how the provider's word arrives and in the worker that applies it.
"""

import pytest

from tests.chaos.checks import assert_at_rest
from tests.chaos.scenarios import FUNDING, KINDS, MODES
from tests.payments.support import rows
from tests.support.stack import WEBHOOK_POINTS, Stack

CREATE = {"bank": "bank.create_virtual_account", "chain": "custody.create_address"}


@pytest.mark.parametrize("kind", KINDS)
async def test_a_deposit_with_nothing_going_wrong_is_credited_once(stack: Stack, kind: str) -> None:
    user = await stack.person()
    asset, amount, minor = FUNDING[kind]

    if kind == "bank":
        await stack.bank_deposit(user, amount)
    else:
        await stack.chain_deposit(user, amount)
    await stack.settle()

    await assert_at_rest(stack)
    assert await stack.wallet(user, asset) == (minor, 0)
    (stored,) = await stack.deposits()
    assert (stored["status"], str(stored["user_id"])) == ("completed", user.id)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_user_gets_one_place_to_deposit_when_the_provider_fails_to_issue_it_cleanly(
    stack: Stack, kind: str, mode: str
) -> None:
    user = await stack.person()
    asset, amount, minor = FUNDING[kind]
    await stack.fault(CREATE[kind], mode)

    refused = await stack.instruction(user, asset)
    again = await stack.instruction(user, asset)
    once_more = await stack.instruction(user, asset)

    # The failed call is the provider's fault and not the user's: asking again is safe.
    assert (refused.status_code, refused.json()["code"]) == (503, "provider_unavailable")
    assert (again.status_code, once_more.status_code) == (200, 200)
    assert again.json() == once_more.json()
    # However the first call ended, the provider issued one account or address, and the
    # user was given that one.
    issued = stack.sim.app.state.sim
    assert len(issued.bank._virtual_accounts if kind == "bank" else issued.custody._addresses) == 1
    stored = await rows(stack.db, "SELECT provider_ref FROM deposit_instructions")
    assert len(stored) == 1

    if kind == "bank":
        await stack.bank_deposit(user, amount)
    else:
        await stack.chain_deposit(user, amount)
    await stack.settle()

    await assert_at_rest(stack)
    assert await stack.wallet(user, asset) == (minor, 0)


@pytest.mark.parametrize("point", WEBHOOK_POINTS)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_worker_that_dies_while_crediting_a_deposit_does_not_credit_it_twice(
    stack: Stack, kind: str, point: str
) -> None:
    user = await stack.person()
    asset, amount, minor = FUNDING[kind]
    # A chain deposit is announced twice, detected and then confirmed: the worker dies at
    # this point in each.
    stack.crashes.arm(point, times=1 if kind == "bank" else 2)

    if kind == "bank":
        await stack.bank_deposit(user, amount)
    else:
        await stack.chain_deposit(user, amount)
    await stack.settle()

    assert stack.crashes.hits == [point] * (1 if kind == "bank" else 2)
    await assert_at_rest(stack)
    assert await stack.wallet(user, asset) == (minor, 0)
    announced = await rows(
        stack.db, "SELECT count(*) AS events FROM outbox_events WHERE topic = 'deposit.completed'"
    )
    assert announced[0]["events"] == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_a_deposit_survives_a_worker_dying_at_every_step_with_duplicated_reordered_events(
    stack: Stack, kind: str
) -> None:
    user = await stack.person()
    asset, amount, minor = FUNDING[kind]
    await stack.webhooks_behave(duplicates=2, reverse=True)
    for point in WEBHOOK_POINTS:
        stack.crashes.arm(point, times=2)

    for _ in range(3):
        if kind == "bank":
            await stack.bank_deposit(user, amount)
        else:
            await stack.chain_deposit(user, amount)
    await stack.settle()

    assert len(stack.crashes.hits) == 6
    await assert_at_rest(stack)
    assert await stack.wallet(user, asset) == (3 * minor, 0)


async def test_a_returned_deposit_is_taken_back_once_though_the_worker_dies_applying_it(
    stack: Stack,
) -> None:
    user = await stack.person()
    asset, amount, _ = FUNDING["bank"]
    deposit_id = await stack.bank_deposit(user, amount)
    await stack.settle()
    await stack.webhooks_behave(duplicates=2)
    stack.crashes.arm("webhook.after_apply")

    await stack.sim.return_bank_deposit(deposit_id)
    await stack.settle()

    assert stack.crashes.hits == ["webhook.after_apply"]
    await assert_at_rest(stack)
    assert await stack.wallet(user, asset) == (0, 0)
    reversals = await rows(
        stack.db, "SELECT count(*) AS entries FROM journal_entries WHERE kind = 'deposit_return'"
    )
    assert reversals[0]["entries"] == 1

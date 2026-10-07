"""A reconciliation run: it finds nothing when the two sides agree, and each kind of break
when the simulator is made to disagree.

The simulator is made to disagree through its control endpoints: a webhook that is never
delivered, a deposit that is recalled, or a provider that forgets everything it held.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text

from corridor import ops, recon, risk, wallets
from corridor.identity import Principal
from corridor.ledger import AccountKind
from corridor.platform.ids import new_id
from corridor.providers import SimBank, SimCustody
from tests.payments.support import rows
from tests.recon.conftest import WINDOW, Reconcile
from tests.recon.support import (
    BANK,
    CUSTODY,
    breaks,
    chain_submitted,
    funded,
    kinds,
    let_pass,
    submitted,
)
from tests.support.auth import RegisteredUser
from tests.support.ledger import fund
from tests.support.providers import CLOSED_ACCOUNT_NUMBER
from tests.support.stack import PROVIDER_FEE, Stack


async def forget(stack: Stack) -> None:
    """The providers lose their books: everything Corridor recorded is now unknown to them."""
    await stack.sim.control("POST", "/reset")


async def test_a_run_over_a_clean_window_finds_nothing(stack: Stack, reconcile: Reconcile) -> None:
    user, _ = await funded(stack)
    await stack.chain_deposit(user, "50.000000")
    await stack.settle()
    await submitted(stack, user)
    await chain_submitted(stack, user)
    await stack.settle()
    assert [w["status"] for w in await stack.withdrawals()] == ["completed", "completed"]

    result = await reconcile()

    assert (result.breaks, result.repaired) == ((), 0)
    assert (result.run.status, result.run.breaks_found, result.run.breaks_opened) == (
        "completed",
        0,
        0,
    )
    (stored,) = await rows(stack.db, "SELECT * FROM recon_runs")
    assert stored["id"] == result.run.id
    assert stored["window_end"] - stored["window_start"] == WINDOW
    assert await breaks(stack) == []


async def test_a_run_with_nothing_recorded_and_nothing_at_the_providers_finds_nothing(
    stack: Stack, reconcile: Reconcile
) -> None:
    assert (await reconcile()).breaks == ()


async def test_a_bank_deposit_whose_webhook_was_dropped_is_a_missing_deposit(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received"])
    deposit_id = await stack.bank_deposit(user, "250.00")
    await stack.settle()
    assert await stack.deposits() == []

    result = await reconcile()

    (found,) = result.breaks
    assert (found.kind, found.provider, found.provider_ref, found.asset) == (
        "missing_deposit",
        BANK,
        deposit_id,
        "USD",
    )
    assert (found.expected, found.actual) == (None, 250_00)


async def test_a_chain_deposit_whose_confirmation_was_dropped_is_a_missing_deposit(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.confirmed"])
    deposit_id = await stack.chain_deposit(user, "50.000000")
    await stack.settle()
    await let_pass(stack)
    (pending,) = await stack.deposits()
    assert pending["status"] == "pending"

    result = await reconcile()

    (found,) = result.breaks
    assert (found.kind, found.provider, found.provider_ref, found.actual) == (
        "missing_deposit",
        CUSTODY,
        deposit_id,
        50_000_000,
    )


async def test_a_deposit_the_provider_does_not_have_is_an_unknown_deposit(
    stack: Stack, reconcile: Reconcile
) -> None:
    _, deposit_id = await funded(stack)
    await forget(stack)
    await let_pass(stack)

    result = await reconcile()

    unknown = next(found for found in result.breaks if found.kind == "unknown_deposit")
    assert (unknown.provider, unknown.provider_ref) == (BANK, deposit_id)
    assert (unknown.expected, unknown.actual) == (500_00, None)
    # Nothing is done about it: only a person can say which side is right.
    assert (unknown.status, result.repaired) == ("open", 0)
    assert [row["status"] for row in await breaks(stack, "kind = 'unknown_deposit'")] == ["open"]


async def test_a_deposit_recorded_with_another_amount_is_an_amount_mismatch(
    stack: Stack, reconcile: Reconcile, bank: SimBank
) -> None:
    user, deposit_id = await funded(stack, "500.00")
    # The simulator numbers what it makes from a seed, so a bank that forgets everything
    # and is taken through the same steps gives the same ids again: this is the same
    # deposit, and the bank now says it was for 400.00.
    await forget(stack)
    account = await bank.create_virtual_account(
        customer_reference=user.id, asset_code="USD", idempotency_key=str(uuid.uuid4())
    )
    again = await stack.sim.control(
        "POST",
        "/bank/deposits",
        {
            "virtual_account_id": account.id,
            "amount": "400.00",
            "sender_name": "Maria Silva",
            "reference": "INV-2041",
        },
        expect=201,
    )
    assert again["id"] == deposit_id
    await let_pass(stack)

    result = await reconcile()

    mismatch = next(found for found in result.breaks if found.kind == "amount_mismatch")
    assert (mismatch.provider, mismatch.provider_ref) == (BANK, deposit_id)
    assert (mismatch.expected, mismatch.actual) == (500_00, 400_00)
    assert "unknown_deposit" not in kinds(result.breaks)
    assert "missing_deposit" not in kinds(result.breaks)
    # The books are not changed to the provider's figure.
    assert await stack.wallet(user) == (500_00, 0)
    assert result.repaired == 0


@pytest.mark.parametrize(
    ("account_number", "dropped", "provider_status"),
    [
        ("000123456789", "payout.completed", "completed"),
        (CLOSED_ACCOUNT_NUMBER, "payout.failed", "failed"),
    ],
    ids=["paid", "failed"],
)
async def test_a_payout_the_bank_has_finished_and_corridor_has_not_is_a_missing_payout_result(
    stack: Stack, reconcile: Reconcile, account_number: str, dropped: str, provider_status: str
) -> None:
    user, _ = await funded(stack)
    await stack.webhooks_behave(drop_types=[dropped])
    withdrawal_id = await submitted(stack, user, account_number=account_number)
    await let_pass(stack)
    (sent,) = await stack.provider_sends(withdrawal_id)
    assert sent["status"] == provider_status
    assert (await stack.withdrawal(withdrawal_id))["status"] == "submitted"

    result = await reconcile()

    (found,) = result.breaks
    assert (found.kind, found.provider, found.provider_ref) == (
        "missing_payout_result",
        BANK,
        sent["id"],
    )
    assert (found.expected, found.actual) == (100_00, 100_00)


async def test_a_chain_withdrawal_the_custodian_completed_is_a_missing_payout_result(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    await stack.chain_deposit(user, "50.000000")
    await stack.settle()
    await stack.webhooks_behave(drop_types=["withdrawal.completed"])
    withdrawal_id = await chain_submitted(stack, user)
    await let_pass(stack)
    (sent,) = await stack.provider_sends(withdrawal_id)
    assert sent["status"] == "completed"

    result = await reconcile()

    (found,) = result.breaks
    assert (found.kind, found.provider, found.provider_ref) == (
        "missing_payout_result",
        CUSTODY,
        sent["id"],
    )


async def test_a_settled_payout_the_provider_does_not_have_is_an_unknown_payout(
    stack: Stack, reconcile: Reconcile
) -> None:
    user, _ = await funded(stack)
    withdrawal_id = await submitted(stack, user)
    await stack.settle()
    settled = await stack.withdrawal(withdrawal_id)
    assert settled["status"] == "completed"
    await forget(stack)
    await let_pass(stack)

    result = await reconcile()

    unknown = next(found for found in result.breaks if found.kind == "unknown_payout")
    assert (unknown.provider, unknown.provider_ref) == (BANK, settled["provider_ref"])
    assert (unknown.expected, unknown.actual) == (100_00, None)
    assert unknown.status == "open"


async def test_a_payout_recorded_as_sent_that_the_provider_denies_is_an_unknown_payout(
    stack: Stack, reconcile: Reconcile
) -> None:
    user, _ = await funded(stack)
    withdrawal_id = await submitted(stack, user)
    sent_as = (await stack.withdrawal(withdrawal_id))["provider_ref"]
    await forget(stack)
    await let_pass(stack)

    result = await reconcile()

    unknown = next(found for found in result.breaks if found.kind == "unknown_payout")
    assert (unknown.provider_ref, unknown.expected, unknown.actual) == (sent_as, 100_00, None)
    # Its funds stay reserved: nothing here says the payout was not made.
    assert (await stack.withdrawal(withdrawal_id))["status"] == "submitted"
    assert await stack.wallet(user) == (500_00 - 101_50, 101_50)


async def test_a_payout_at_the_provider_that_is_no_withdrawal_is_an_unknown_payout(
    stack: Stack, reconcile: Reconcile, bank: SimBank
) -> None:
    user, _ = await funded(stack)
    stranger = str(uuid.uuid4())
    beneficiary = await bank.create_beneficiary(
        customer_reference=user.id,
        asset_code="USD",
        holder_name="Maria Silva",
        account_number="000123456789",
        routing_number="021000021",
        idempotency_key=str(uuid.uuid4()),
    )
    payout = await bank.create_payout(
        beneficiary_id=beneficiary.id,
        asset_code="USD",
        amount=75_00,
        reference=stranger,
        idempotency_key=stranger,
    )
    await let_pass(stack)

    result = await reconcile()

    unknown = next(found for found in result.breaks if found.kind == "unknown_payout")
    assert (unknown.provider_ref, unknown.expected, unknown.actual) == (payout.id, None, 75_00)
    # Money left the bank that the ledger knows nothing of, so the balance disagrees too.
    assert kinds(result.breaks) == ["settlement_balance", "unknown_payout"]


# --- the settlement balance ------------------------------------------------------------------


async def test_the_balance_check_allows_for_a_deposit_received_and_not_yet_credited(
    stack: Stack, reconcile: Reconcile
) -> None:
    await funded(stack)
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received"])
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack)
    assert await stack.provider_balance("bank", "USD") == 750_00
    assert await stack.book_balance(AccountKind.BANK_SETTLEMENT, "USD", provider=BANK) == 500_00

    result = await reconcile()

    assert kinds(result.breaks) == ["missing_deposit"]


async def test_the_balance_check_allows_for_a_payout_paid_and_not_yet_settled(
    stack: Stack, reconcile: Reconcile
) -> None:
    user, _ = await funded(stack)
    await stack.webhooks_behave(drop_types=["payout.completed"])
    await submitted(stack, user)
    await let_pass(stack)
    assert await stack.provider_balance("bank", "USD") == 500_00 - 100_00 - PROVIDER_FEE["USD"]
    assert await stack.book_balance(AccountKind.BANK_SETTLEMENT, "USD", provider=BANK) == 500_00

    result = await reconcile()

    assert kinds(result.breaks) == ["missing_payout_result"]


async def test_a_balance_that_differs_by_more_than_what_is_in_transit_is_a_break(
    stack: Stack, reconcile: Reconcile
) -> None:
    _, deposit_id = await funded(stack)
    # The bank takes a deposit back and Corridor never hears of it, and by the time a run
    # looks the return is older than its window. Nothing that is on its way explains the
    # difference, and nothing in the window is there to repair.
    await stack.webhooks_behave(drop_types=["deposit.returned"])
    await stack.sim.control("POST", f"/bank/deposits/{deposit_id}/return", {"reason": "recalled"})
    await let_pass(stack, 2 * 3600)

    result = await reconcile()

    (found,) = result.breaks
    assert (found.kind, found.provider, found.provider_ref, found.asset) == (
        "settlement_balance",
        BANK,
        "USD",
        "USD",
    )
    assert (found.expected, found.actual) == (500_00, 0)
    assert result.repaired == 0


async def recalled_unheard(stack: Stack, **hold: Any) -> tuple[RegisteredUser, str]:
    """A credited bank deposit that the bank took back, whose return never arrived."""
    user, deposit_id = await funded(stack)
    await stack.webhooks_behave(**(hold or {"drop_types": ["deposit.returned"]}))
    await stack.sim.control("POST", f"/bank/deposits/{deposit_id}/return", {"reason": "recalled"})
    return user, deposit_id


async def test_a_deposit_the_bank_recalled_that_is_still_credited_here_is_a_missing_return(
    stack: Stack, reconcile: Reconcile
) -> None:
    _, deposit_id = await recalled_unheard(stack)
    await let_pass(stack)

    result = await reconcile()

    # The one break. The balance is not a second one: the return is known to be on its way
    # onto the books, exactly as a deposit that is missing is.
    (found,) = result.breaks
    assert (found.kind, found.provider, found.provider_ref, found.asset) == (
        "missing_return",
        BANK,
        deposit_id,
        "USD",
    )
    # What is credited here, and what the bank still holds of it.
    assert (found.expected, found.actual) == (500_00, 0)


async def test_a_recalled_deposit_whose_return_is_on_the_books_is_no_break(
    stack: Stack, reconcile: Reconcile
) -> None:
    _, deposit_id = await funded(stack)
    await stack.sim.control("POST", f"/bank/deposits/{deposit_id}/return", {"reason": "recalled"})
    await stack.settle()
    await let_pass(stack)

    assert (await reconcile()).breaks == ()


async def test_a_return_the_bank_made_a_moment_ago_is_given_time_to_arrive(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    await recalled_unheard(stack, hold=True)
    await let_pass(stack, 30)

    async def run(grace: int) -> recon.RunResult:
        return await recon.run(
            stack.db,
            bank,
            custody,
            window_start=stack.clock.now() - WINDOW,
            window_end=stack.clock.now(),
            grace=timedelta(seconds=grace),
        )

    # Within the grace it is neither called missing nor counted against the balance.
    assert (await run(grace=120)).breaks == ()
    assert kinds((await run(grace=10)).breaks) == ["missing_return"]


async def test_a_recalled_deposit_in_suspense_is_a_missing_return_too(
    stack: Stack, reconcile: Reconcile
) -> None:
    arrived = await stack.sim.control(
        "POST",
        "/bank/deposits",
        {
            "virtual_account_id": "va_nobody",
            "amount": "40.00",
            "sender_name": "Somebody Else",
            "reference": "INV-1",
        },
        expect=201,
    )
    deposit_id = str(arrived["id"])
    await stack.settle()
    assert [deposit["status"] for deposit in await stack.deposits()] == ["suspense"]
    await stack.webhooks_behave(drop_types=["deposit.returned"])
    await stack.sim.control("POST", f"/bank/deposits/{deposit_id}/return", {"reason": "recalled"})
    await let_pass(stack)

    result = await reconcile()

    assert [(found.kind, found.provider_ref) for found in result.breaks] == [
        ("missing_return", deposit_id)
    ]


async def test_the_balance_is_compared_as_it_was_at_the_end_of_the_window(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    await funded(stack)
    ended = stack.clock.now()
    await let_pass(stack)
    # After the window: on the provider's books and on Corridor's, and in neither as they
    # were when the window ended.
    await funded(stack, "40.00")

    result = await recon.run(stack.db, bank, custody, window_start=ended - WINDOW, window_end=ended)

    assert result.breaks == ()


async def test_a_deposit_credited_after_the_window_ended_is_in_transit_for_that_window(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(hold=True)
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack)
    ended = stack.clock.now()
    await let_pass(stack)
    # The bank had it before the window ended; Corridor credited it afterwards.
    await stack.webhooks_behave(hold=False)
    await stack.settle()
    assert await stack.wallet(user) == (250_00, 0)

    result = await recon.run(stack.db, bank, custody, window_start=ended - WINDOW, window_end=ended)

    assert result.breaks == ()


# --- running it again, and when a provider cannot be read ------------------------------------


async def test_a_break_that_is_still_open_is_not_opened_again_by_the_next_run(
    stack: Stack, reconcile: Reconcile
) -> None:
    await funded(stack)
    await forget(stack)
    await let_pass(stack)

    first = await reconcile()
    second = await reconcile()

    assert kinds(first.breaks) == kinds(second.breaks) == ["settlement_balance", "unknown_deposit"]
    assert (first.run.breaks_found, first.run.breaks_opened) == (2, 2)
    assert (second.run.breaks_found, second.run.breaks_opened) == (2, 0)
    assert [found.id for found in second.breaks] == [found.id for found in first.breaks]
    stored = await breaks(stack)
    assert len(stored) == 2
    assert {row["run_id"] for row in stored} == {first.run.id}
    assert {row["last_seen_run_id"] for row in stored} == {second.run.id}


def changed(kind: str) -> float:
    """How many times this process has counted an open break of a kind as changed."""
    value = REGISTRY.get_sample_value("corridor_recon_break_changes_total", {"kind": kind})
    return value or 0.0


async def test_an_open_break_is_brought_up_to_date_by_a_run_that_finds_it_changed(
    stack: Stack, reconcile: Reconcile
) -> None:
    user, _ = await funded(stack)
    await forget(stack)
    await let_pass(stack)
    first = await reconcile()
    (balance,) = await breaks(stack, "kind = 'settlement_balance'")
    assert (balance["expected"], balance["actual"]) == (500_00, 0)
    before = (changed("settlement_balance"), changed("unknown_deposit"))
    # The books come to hold 40.00 more at the bank, which the bank knows nothing of.
    async with stack.db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), "USD")
        await fund(session, wallet.available_account_id, 40_00)
    await let_pass(stack)

    second = await reconcile()

    (balance,) = await breaks(stack, "kind = 'settlement_balance'")
    assert (balance["expected"], balance["actual"]) == (540_00, 0)
    assert (balance["run_id"], balance["last_seen_run_id"]) == (first.run.id, second.run.id)
    assert balance["status"] == "open"
    assert second.run.breaks_opened == 0
    (returned,) = [found for found in second.breaks if found.kind == "settlement_balance"]
    assert (returned.expected, returned.actual, returned.last_seen_run_id) == (
        540_00,
        0,
        second.run.id,
    )
    # The deposit the bank forgot is the same disagreement as before, seen again.
    (unknown,) = await breaks(stack, "kind = 'unknown_deposit'")
    assert unknown["last_seen_run_id"] == second.run.id
    assert (changed("settlement_balance"), changed("unknown_deposit")) == (
        before[0] + 1,
        before[1],
    )


async def test_a_break_no_later_run_sees_keeps_the_run_that_last_saw_it(
    stack: Stack, reconcile: Reconcile
) -> None:
    await funded(stack)
    await forget(stack)
    await let_pass(stack)
    first = await reconcile()
    # Long enough for the forgotten deposit to have left every window.
    await let_pass(stack, (recon.LOOKBACK + 2 * WINDOW).total_seconds())

    later = await reconcile()

    (unknown,) = await breaks(stack, "kind = 'unknown_deposit'")
    assert "unknown_deposit" not in kinds(later.breaks)
    assert (unknown["status"], unknown["last_seen_run_id"]) == ("open", first.run.id)


# --- a deposit too new to repair -----------------------------------------------------------------


async def test_a_missing_deposit_younger_than_the_grace_is_left_for_its_webhook(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received"])
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack, 60)

    async def run_with_grace() -> recon.RunResult:
        now = stack.clock.now()
        return await recon.run(
            stack.db,
            bank,
            custody,
            window_start=now - WINDOW,
            window_end=now,
            grace=timedelta(seconds=120),
        )

    early = await run_with_grace()

    # Not a break yet, and not a difference in the balance either: it is in transit.
    assert (early.breaks, early.repaired) == ((), 0)
    assert await stack.wallet(user) == (0, 0)

    await let_pass(stack, 61)
    late = await run_with_grace()

    assert (kinds(late.breaks), late.repaired) == (["missing_deposit"], 1)
    assert await stack.wallet(user) == (250_00, 0)


async def test_a_break_that_was_resolved_is_opened_again_if_it_is_found_again(
    stack: Stack, reconcile: Reconcile
) -> None:
    await funded(stack)
    await forget(stack)
    await let_pass(stack)
    await reconcile()
    async with stack.db.transaction() as session:
        await session.execute(
            text(
                "UPDATE recon_breaks SET status = 'resolved', resolved_by = 'someone',"
                " resolved_at = created_at WHERE kind = 'unknown_deposit'"
            )
        )

    again = await reconcile()

    assert again.run.breaks_opened == 1
    assert sorted(row["status"] for row in await breaks(stack, "kind = 'unknown_deposit'")) == [
        "open",
        "resolved",
    ]


async def test_a_provider_that_cannot_be_read_makes_the_run_incomplete_and_opens_nothing(
    stack: Stack, reconcile: Reconcile
) -> None:
    await funded(stack)
    await let_pass(stack)
    await stack.fault("bank.list_transactions", "error", times=3)

    result = await reconcile()

    assert (result.run.status, result.breaks) == ("incomplete", ())
    assert (await reconcile()).run.status == "completed"


async def test_a_worker_with_no_custodian_reconciles_the_bank_alone(
    stack: Stack, bank: SimBank
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received"])
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack)

    result = await recon.run(
        stack.db, bank, None, window_start=stack.clock.now() - WINDOW, window_end=stack.clock.now()
    )

    assert (kinds(result.breaks), result.run.status) == (["missing_deposit"], "completed")


async def test_a_window_that_ends_before_it_starts_is_refused(stack: Stack, bank: SimBank) -> None:
    now = stack.clock.now()

    with pytest.raises(ValueError, match="window"):
        await recon.run(stack.db, bank, None, window_start=now, window_end=now)

    assert await rows(stack.db, "SELECT 1 FROM recon_runs") == []


async def test_the_providers_are_read_with_no_transaction_open(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    user, _ = await funded(stack)
    await submitted(stack, user)
    await let_pass(stack, 2)
    open_transactions: list[int] = []

    class Watching:
        """A provider that looks, each time it is asked something, for a session of this
        database that is sitting in a transaction."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            call = getattr(self._inner, name)

            async def watched(*arguments: Any, **named: Any) -> Any:
                found = await rows(
                    stack.db,
                    "SELECT count(*) AS sessions FROM pg_stat_activity"
                    " WHERE datname = current_database() AND state = 'idle in transaction'",
                )
                open_transactions.append(found[0]["sessions"])
                return await call(*arguments, **named)

            return watched

    await recon.run(
        stack.db,
        Watching(bank),
        Watching(custody),
        window_start=stack.clock.now() - WINDOW,
        window_end=stack.clock.now(),
    )

    # Four statements and one payout looked up.
    assert len(open_transactions) == 5
    assert set(open_transactions) == {0}


# --- what is not a break, and what the window leaves out -------------------------------------


async def test_a_chain_deposit_that_is_seen_and_not_yet_final_is_no_break(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    await stack.chain_deposit(user, "50.000000")
    await stack.sim.control("POST", "/webhooks/deliver")
    await stack.dispatcher.drain()
    (pending,) = await stack.deposits()
    assert pending["status"] == "pending"
    # Time passes here and not on the chain: no block has confirmed the deposit.
    stack.clock.advance(seconds=10)

    result = await reconcile()

    assert result.breaks == ()


async def test_a_missing_deposit_from_before_the_window_shows_in_the_balance_and_is_not_repaired(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received"])
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack, 2 * 3600)

    result = await reconcile()

    (found,) = result.breaks
    assert (found.kind, found.expected, found.actual) == ("settlement_balance", 0, 250_00)
    assert (result.repaired, await stack.wallet(user)) == (0, (0, 0))


async def test_a_deposit_recalled_long_after_it_arrived_is_still_one_the_provider_knows(
    stack: Stack, reconcile: Reconcile
) -> None:
    _, deposit_id = await funded(stack)
    # Longer than a statement is read back: the deposit itself is no longer on it.
    await let_pass(stack, 30 * 3600)
    await stack.sim.control("POST", f"/bank/deposits/{deposit_id}/return", {"reason": "recalled"})
    await stack.settle()
    (returned,) = await stack.deposits()
    assert returned["status"] == "returned"
    await let_pass(stack)

    result = await reconcile()

    assert result.breaks == ()


async def test_a_return_booked_after_the_window_ended_is_in_transit_for_that_window(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    _, deposit_id = await funded(stack)
    await stack.webhooks_behave(hold=True)
    await stack.sim.control("POST", f"/bank/deposits/{deposit_id}/return", {"reason": "recalled"})
    await let_pass(stack)
    ended = stack.clock.now()
    await let_pass(stack)
    await stack.webhooks_behave(hold=False)
    await stack.settle()
    assert [deposit["status"] for deposit in await stack.deposits()] == ["returned"]

    result = await recon.run(stack.db, bank, custody, window_start=ended - WINDOW, window_end=ended)

    assert result.breaks == ()


async def test_a_payout_settled_after_the_window_ended_is_in_transit_for_that_window(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    user, _ = await funded(stack)
    await stack.webhooks_behave(hold=True)
    withdrawal_id = await submitted(stack, user)
    await let_pass(stack)
    ended = stack.clock.now()
    await let_pass(stack)
    await stack.webhooks_behave(hold=False)
    await stack.sim.control("POST", "/webhooks/deliver")
    await stack.dispatcher.drain()
    assert (await stack.withdrawal(withdrawal_id))["status"] == "completed"

    result = await recon.run(stack.db, bank, custody, window_start=ended - WINDOW, window_end=ended)

    assert result.breaks == ()


async def test_a_payout_that_answers_to_another_reference_is_not_acted_on(
    stack: Stack, reconcile: Reconcile, bank: SimBank
) -> None:
    user, _ = await funded(stack)
    await stack.webhooks_behave(drop_types=["payout.completed"])
    withdrawal_id = await submitted(stack, user)
    stranger = str(uuid.uuid4())
    beneficiary = await bank.create_beneficiary(
        customer_reference=user.id,
        asset_code="USD",
        holder_name="Maria Silva",
        account_number="000123456789",
        routing_number="021000021",
        idempotency_key=str(uuid.uuid4()),
    )
    other = await bank.create_payout(
        beneficiary_id=beneficiary.id,
        asset_code="USD",
        amount=100_00,
        reference=stranger,
        idempotency_key=stranger,
    )
    # The withdrawal's row names a payout that was made for something else.
    async with stack.db.transaction() as session:
        await session.execute(
            text("UPDATE withdrawals SET provider_ref = :ref WHERE id = :id"),
            {"ref": other.id, "id": uuid.UUID(withdrawal_id)},
        )
    await let_pass(stack)

    result = await reconcile()

    assert result.run.status == "incomplete"
    assert other.id not in {
        found.provider_ref for found in result.breaks if found.kind == "missing_payout_result"
    }
    assert (await stack.withdrawal(withdrawal_id))["status"] == "submitted"
    assert await stack.wallet(user) == (500_00 - 101_50, 101_50)


# --- counting, and deposits that changed after they arrived ----------------------------------


def opened(kind: str) -> float:
    """How many breaks of a kind this process has counted as opened."""
    value = REGISTRY.get_sample_value("corridor_recon_breaks_total", {"kind": kind})
    return value or 0.0


async def test_a_break_is_counted_when_it_is_opened_and_not_when_it_is_seen_again(
    stack: Stack, reconcile: Reconcile
) -> None:
    await funded(stack)
    await forget(stack)
    await let_pass(stack)
    before = (opened("unknown_deposit"), opened("settlement_balance"), opened("missing_deposit"))

    await reconcile()
    after_first = (
        opened("unknown_deposit"),
        opened("settlement_balance"),
        opened("missing_deposit"),
    )
    await reconcile()

    assert after_first == (before[0] + 1, before[1] + 1, before[2])
    assert (
        opened("unknown_deposit"),
        opened("settlement_balance"),
        opened("missing_deposit"),
    ) == after_first


async def test_a_deposit_released_from_suspense_long_after_it_arrived_is_no_break(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    async with stack.db.transaction() as session:
        await risk.add_to_denylist(session, kind="name", value="Maria Silva", outcome="review")
    await stack.bank_deposit(user, "250.00")
    await stack.settle()
    assert await stack.book_balance(AccountKind.SUSPENSE, "USD") == 250_00
    # A review can wait for days. By then the deposit's line is further back than any run
    # reads the statement, and the row was changed in the window all the same.
    await let_pass(stack, (recon.LOOKBACK + 2 * WINDOW).total_seconds())
    (review,) = await rows(stack.db, "SELECT id FROM risk_reviews")
    async with stack.db.transaction() as session:
        await ops.clear_review(
            session, Principal.for_user(new_id(), "admin", new_id()), review["id"]
        )
    await stack.settle()
    await let_pass(stack)

    result = await reconcile()

    assert await stack.wallet(user) == (250_00, 0)
    assert result.breaks == ()

"""Repair: what a run puts right by itself, and what it leaves for a person.

A payout result whose webhook never came is applied from the provider's own record,
through the function the webhook would have reached, and a deposit through the one for
deposits read from a statement. S5 is the first test here.
"""

from datetime import timedelta
from typing import Any

import pytest

from corridor import payments, recon, risk
from corridor.ledger import AccountKind
from corridor.providers import SimBank, SimCustody
from corridor.recon import repair as repair_module
from tests.payments.support import entries, rows
from tests.recon.conftest import Reconcile
from tests.recon.support import BANK, breaks, chain_submitted, funded, kinds, let_pass, submitted
from tests.support.providers import CLOSED_ACCOUNT_NUMBER
from tests.support.stack import PROVIDER_FEE, Stack


async def repairs(stack: Stack) -> list[dict[str, Any]]:
    """What the audit log says the repair closed."""
    return await rows(
        stack.db,
        "SELECT actor_type, actor_id, resource_id FROM audit_events"
        " WHERE action = 'recon.break_resolved' ORDER BY id",
    )


async def test_a_bank_deposit_whose_webhook_was_dropped_is_credited_by_the_run(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received"])
    deposit_id = await stack.bank_deposit(user, "250.00")
    await stack.settle()
    assert await stack.wallet(user) == (0, 0)

    result = await reconcile()

    assert (kinds(result.breaks), result.repaired) == (["missing_deposit"], 1)
    assert await stack.wallet(user) == (250_00, 0)
    (deposit,) = await stack.deposits()
    assert (deposit["provider_ref"], deposit["status"], str(deposit["user_id"])) == (
        deposit_id,
        "completed",
        user.id,
    )
    (posted,) = await entries(stack.db, "deposit", f"{BANK}:{deposit_id}")
    assert posted["postings"] == [("bank_settlement", "D", 250_00), ("user_available", "C", 250_00)]
    assert await stack.book_balance(
        AccountKind.BANK_SETTLEMENT, "USD", provider=BANK
    ) == await stack.provider_balance("bank", "USD")

    (closed,) = await breaks(stack)
    assert (closed["status"], closed["resolved_by"]) == ("resolved", "system")
    assert closed["resolved_at"] is not None
    assert closed["note"]
    assert await repairs(stack) == [
        {"actor_type": "system", "actor_id": "recon.repair", "resource_id": str(closed["id"])}
    ]


async def test_a_repaired_deposit_is_recorded_as_credited_without_screening(
    stack: Stack, reconcile: Reconcile
) -> None:
    """A statement does not say who sent a deposit, so the repair has nobody to ask the
    deny list about. The deposit goes to the user whose account it arrived at, and the
    audit log says that it was not screened: here, for a sender the list would have stopped."""
    user = await stack.person()
    async with stack.db.transaction() as session:
        await risk.add_to_denylist(session, kind="name", value="Maria Silva", outcome="deny")
    await stack.webhooks_behave(drop_types=["deposit.received"])
    await stack.bank_deposit(user, "250.00")
    await stack.settle()

    result = await reconcile()

    assert result.repaired == 1
    assert await stack.wallet(user) == (250_00, 0)
    (event,) = await rows(
        stack.db, "SELECT details FROM audit_events WHERE action = 'deposit.completed'"
    )
    assert event["details"]["screened"] is False
    assert await rows(stack.db, "SELECT 1 FROM risk_reviews") == []


async def test_the_run_after_a_repair_finds_nothing(stack: Stack, reconcile: Reconcile) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received", "payout.completed"])
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack)
    await reconcile()
    await submitted(stack, user)
    await let_pass(stack)
    assert (await reconcile()).repaired == 1
    await let_pass(stack)

    result = await reconcile()

    assert (result.breaks, result.repaired) == ((), 0)
    assert [row["status"] for row in await breaks(stack)] == ["resolved", "resolved"]
    assert await stack.wallet(user) == (250_00 - 101_50, 0)


async def test_a_chain_deposit_whose_confirmation_was_dropped_is_credited_by_the_run(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.confirmed"])
    await stack.chain_deposit(user, "50.000000")
    await stack.settle()
    await let_pass(stack)

    result = await reconcile()

    assert result.repaired == 1
    assert await stack.wallet(user, "USDC") == (50_000_000, 0)
    (deposit,) = await stack.deposits()
    assert deposit["status"] == "completed"
    assert [row["resolved_by"] for row in await breaks(stack)] == ["system"]


async def test_a_dropped_deposit_to_an_account_nobody_was_given_is_repaired_into_suspense(
    stack: Stack, reconcile: Reconcile
) -> None:
    await stack.webhooks_behave(drop_types=["deposit.received"])
    await stack.sim.control(
        "POST",
        "/bank/deposits",
        {
            "virtual_account_id": "va_nobody",
            "amount": "75.00",
            "sender_name": "Maria Silva",
            "reference": "INV-2041",
            "asset": "USD",
        },
        expect=201,
    )
    await let_pass(stack)

    result = await reconcile()

    assert result.repaired == 1
    (deposit,) = await stack.deposits()
    assert (deposit["status"], deposit["user_id"]) == ("suspense", None)
    assert await stack.book_balance(AccountKind.SUSPENSE, "USD") == 75_00


# --- a deposit the bank took back before Corridor heard of it --------------------------------


async def came_and_went(stack: Stack, amount: str = "250.00") -> tuple[Any, str]:
    """A bank deposit that was received and then recalled, with neither webhook delivered:
    the user it was for, and the bank's id for it."""
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received", "deposit.returned"])
    deposit_id = await stack.bank_deposit(user, amount)
    await let_pass(stack)
    await stack.sim.control("POST", f"/bank/deposits/{deposit_id}/return", {"reason": "recalled"})
    await let_pass(stack)
    return user, deposit_id


async def test_a_missing_deposit_the_statement_shows_as_returned_is_not_credited(
    stack: Stack, reconcile: Reconcile
) -> None:
    user, deposit_id = await came_and_went(stack)
    assert await stack.provider_balance("bank", "USD") == 0

    result = await reconcile()

    assert (kinds(result.breaks), result.repaired) == (["missing_deposit"], 1)
    assert await stack.wallet(user) == (0, 0)
    (deposit,) = await stack.deposits()
    assert (deposit["provider_ref"], deposit["status"]) == (deposit_id, "returned")
    assert (deposit["user_id"], deposit["entry_id"]) == (None, None)
    assert await entries(stack.db, "deposit", f"{BANK}:{deposit_id}") == []
    assert await stack.book_balance(AccountKind.BANK_SETTLEMENT, "USD", provider=BANK) == 0
    (closed,) = await breaks(stack)
    assert (closed["status"], closed["resolved_by"]) == ("resolved", "system")
    assert closed["note"] == "The deposit was returned before it was received."
    (recorded,) = await rows(
        stack.db,
        "SELECT actor_type, actor_id, resource_id, details FROM audit_events"
        " WHERE action = 'deposit.returned'",
    )
    assert (recorded["actor_type"], recorded["actor_id"]) == ("system", "recon.repair")
    assert recorded["resource_id"] == str(deposit["id"])
    assert recorded["details"]["received"] is False


async def test_the_run_after_a_deposit_was_recorded_as_returned_finds_nothing(
    stack: Stack, reconcile: Reconcile
) -> None:
    user, _ = await came_and_went(stack)
    await reconcile()
    await let_pass(stack)

    result = await reconcile()

    assert (result.breaks, result.repaired) == ((), 0)
    assert [row["status"] for row in await breaks(stack)] == ["resolved"]
    assert await stack.wallet(user) == (0, 0)


async def test_a_window_that_ended_before_the_return_was_recorded_still_balances(
    stack: Stack, bank: SimBank, custody: SimCustody, reconcile: Reconcile
) -> None:
    await came_and_went(stack)
    ended = stack.clock.now()
    await let_pass(stack)
    # Recorded as returned now, after the window below has ended.
    assert (await reconcile()).repaired == 1

    result = await recon.run(
        stack.db, bank, custody, window_start=ended - timedelta(hours=1), window_end=ended
    )

    assert result.breaks == ()


async def test_a_deposit_recorded_as_returned_is_not_credited_when_its_webhooks_arrive_late(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(hold=True)
    deposit_id = await stack.bank_deposit(user, "250.00")
    await let_pass(stack)
    await stack.sim.control("POST", f"/bank/deposits/{deposit_id}/return", {"reason": "recalled"})
    await let_pass(stack)
    assert (await reconcile()).repaired == 1

    await stack.webhooks_behave(hold=False)
    await stack.settle()

    assert await stack.wallet(user) == (0, 0)
    (deposit,) = await stack.deposits()
    assert (deposit["status"], deposit["entry_id"]) == ("returned", None)
    assert await entries(stack.db, "deposit", f"{BANK}:{deposit_id}") == []
    assert (await reconcile()).breaks == ()


async def test_a_missing_deposit_that_was_not_returned_is_still_credited_beside_one_that_was(
    stack: Stack, reconcile: Reconcile
) -> None:
    gone, _ = await came_and_went(stack)
    stayed = await stack.person()
    await stack.bank_deposit(stayed, "40.00")
    await let_pass(stack)

    result = await reconcile()

    assert (kinds(result.breaks), result.repaired) == (["missing_deposit"] * 2, 2)
    assert (await stack.wallet(gone), await stack.wallet(stayed)) == ((0, 0), (40_00, 0))
    assert await stack.book_balance(
        AccountKind.BANK_SETTLEMENT, "USD", provider=BANK
    ) == await stack.provider_balance("bank", "USD")


async def test_a_repaired_deposit_is_not_credited_again_when_its_webhook_arrives_late(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(hold=True)
    deposit_id = await stack.bank_deposit(user, "250.00")
    await let_pass(stack)
    assert (await reconcile()).repaired == 1

    await stack.webhooks_behave(hold=False)
    await stack.settle()

    assert await stack.wallet(user) == (250_00, 0)
    assert len(await entries(stack.db, "deposit", f"{BANK}:{deposit_id}")) == 1
    assert len(await stack.deposits()) == 1


async def test_a_paid_payout_whose_webhook_was_dropped_is_settled_by_the_run(
    stack: Stack, reconcile: Reconcile
) -> None:
    user, _ = await funded(stack)
    await stack.webhooks_behave(drop_types=["payout.completed"])
    withdrawal_id = await submitted(stack, user)
    await let_pass(stack)

    result = await reconcile()

    assert (kinds(result.breaks), result.repaired) == (["missing_payout_result"], 1)
    settled = await stack.withdrawal(withdrawal_id)
    assert (settled["status"], settled["provider_fee"]) == ("completed", PROVIDER_FEE["USD"])
    assert await stack.wallet(user) == (500_00 - 101_50, 0)
    assert await stack.book_balance(
        AccountKind.BANK_SETTLEMENT, "USD", provider=BANK
    ) == await stack.provider_balance("bank", "USD")
    (closed,) = await breaks(stack)
    assert (closed["status"], closed["resolved_by"]) == ("resolved", "system")


async def test_a_failed_payout_whose_webhook_was_dropped_is_given_back_by_the_run(
    stack: Stack, reconcile: Reconcile
) -> None:
    user, _ = await funded(stack)
    await stack.webhooks_behave(drop_types=["payout.failed"])
    withdrawal_id = await submitted(stack, user, account_number=CLOSED_ACCOUNT_NUMBER)
    await let_pass(stack)

    result = await reconcile()

    assert result.repaired == 1
    failed = await stack.withdrawal(withdrawal_id)
    assert (failed["status"], failed["failure_reason"]) == ("failed", "account_closed")
    assert await stack.wallet(user) == (500_00, 0)
    assert [row["status"] for row in await breaks(stack)] == ["resolved"]


async def test_a_completed_chain_withdrawal_whose_webhook_was_dropped_is_settled_by_the_run(
    stack: Stack, reconcile: Reconcile
) -> None:
    user = await stack.person()
    await stack.chain_deposit(user, "50.000000")
    await stack.settle()
    await stack.webhooks_behave(drop_types=["withdrawal.completed"])
    withdrawal_id = await chain_submitted(stack, user)
    await let_pass(stack)

    result = await reconcile()

    assert result.repaired == 1
    settled = await stack.withdrawal(withdrawal_id)
    assert (settled["status"], settled["provider_fee"]) == ("completed", PROVIDER_FEE["USDC"])
    assert await stack.book_balance(
        AccountKind.CUSTODY_OMNIBUS, "USDC", provider="simcustody"
    ) == await stack.provider_balance("custody", "USDC")


async def test_a_withdrawal_never_recorded_as_sent_is_settled_from_what_the_provider_holds(
    stack: Stack, reconcile: Reconcile
) -> None:
    user, _ = await funded(stack)
    await stack.webhooks_behave(drop_types=["payout.completed"])
    beneficiary_id = await stack.beneficiary(user)
    response = await stack.withdraw(user, "100.00", beneficiary_id=beneficiary_id)
    # The worker dies after the bank accepted the payout and before recording that it had.
    stack.crashes.arm("submit.after_provider")
    await stack.dispatcher.drain()
    withdrawal_id = response.json()["id"]
    left = await stack.withdrawal(withdrawal_id)
    assert (left["status"], left["provider_ref"]) == ("submitting", None)
    await let_pass(stack)

    result = await reconcile()

    assert (kinds(result.breaks), result.repaired) == (["missing_payout_result"], 1)
    assert (await stack.withdrawal(withdrawal_id))["status"] == "completed"
    assert [row["status"] for row in await breaks(stack)] == ["resolved"]


# --- what is left open -----------------------------------------------------------------------


async def refuse(*_arguments: Any, **_named: Any) -> None:
    raise payments.ProviderEventMismatch("not this one")


async def test_a_repair_that_payments_refuses_leaves_the_break_open_and_repairs_the_rest(
    stack: Stack, reconcile: Reconcile, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _ = await funded(stack)
    late = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received", "payout.completed"])
    await stack.bank_deposit(late, "250.00")
    await submitted(stack, user)
    await let_pass(stack)
    monkeypatch.setattr(payments, "apply_statement_deposit", refuse)

    result = await reconcile()

    assert (kinds(result.breaks), result.repaired) == (
        ["missing_deposit", "missing_payout_result"],
        1,
    )
    assert await stack.wallet(late) == (0, 0)
    assert {row["kind"]: row["status"] for row in await breaks(stack)} == {
        "missing_deposit": "open",
        "missing_payout_result": "resolved",
    }


async def test_a_break_that_something_else_put_right_is_closed_by_the_next_run(
    stack: Stack, reconcile: Reconcile, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(hold=True)
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack)
    with monkeypatch.context() as patch:
        patch.setattr(payments, "apply_statement_deposit", refuse)
        assert (await reconcile()).repaired == 0
    assert [row["status"] for row in await breaks(stack)] == ["open"]
    # The webhook arrives after all, and credits the deposit.
    await stack.webhooks_behave(hold=False)
    await stack.settle()
    assert await stack.wallet(user) == (250_00, 0)
    await let_pass(stack)

    result = await reconcile()

    assert (result.breaks, result.repaired) == ((), 1)
    (closed,) = await breaks(stack)
    assert (closed["status"], closed["resolved_by"]) == ("resolved", "system")


async def test_a_deposit_that_is_recorded_and_not_credited_does_not_close_its_break(
    stack: Stack, reconcile: Reconcile, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.confirmed"])
    await stack.chain_deposit(user, "50.000000")
    await stack.settle()
    await let_pass(stack)
    monkeypatch.setattr(payments, "apply_statement_deposit", refuse)

    result = await reconcile()

    # The detection recorded a row. A row is not a credit.
    (pending,) = await stack.deposits()
    assert (pending["status"], result.repaired) == ("pending", 0)
    assert [row["status"] for row in await breaks(stack)] == ["open"]


async def test_a_payout_break_is_closed_once_the_sweeper_has_settled_the_withdrawal(
    stack: Stack, reconcile: Reconcile, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _ = await funded(stack)
    await stack.webhooks_behave(drop_types=["payout.completed"])
    withdrawal_id = await submitted(stack, user)
    await let_pass(stack)
    with monkeypatch.context() as patch:
        patch.setattr(payments, "apply_payout_completed", refuse)
        assert (await reconcile()).repaired == 0
    assert [row["status"] for row in await breaks(stack)] == ["open"]
    await stack.settle()
    assert (await stack.withdrawal(withdrawal_id))["status"] == "completed"
    await let_pass(stack)

    result = await reconcile()

    assert (result.breaks, result.repaired) == ((), 1)
    assert [row["status"] for row in await breaks(stack)] == ["resolved"]


async def test_a_payout_break_stays_open_while_its_withdrawal_is_in_flight(
    stack: Stack, reconcile: Reconcile, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _ = await funded(stack)
    await stack.webhooks_behave(drop_types=["payout.completed"])
    await submitted(stack, user)
    await let_pass(stack)
    monkeypatch.setattr(payments, "apply_payout_completed", refuse)

    first = await reconcile()
    second = await reconcile()

    assert (first.repaired, second.repaired) == (0, 0)
    assert [row["status"] for row in await breaks(stack)] == ["open"]


async def test_a_run_that_could_not_read_the_provider_closes_no_payout_break(
    stack: Stack, reconcile: Reconcile, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _ = await funded(stack)
    await stack.webhooks_behave(drop_types=["payout.completed"])
    beneficiary_id = await stack.beneficiary(user)
    response = await stack.withdraw(user, "100.00", beneficiary_id=beneficiary_id)
    stack.crashes.arm("submit.after_provider")
    await stack.dispatcher.drain()
    await let_pass(stack)
    monkeypatch.setattr(payments, "apply_payout_completed", refuse)
    assert (await reconcile()).repaired == 0
    # The withdrawal carries no payout id, so only the provider can say whose payout the
    # open break is about; and this time the provider cannot be asked.
    await stack.fault("bank.get_payout", "error", times=5)
    await stack.fault("bank.list_transactions", "error", times=5)

    blind = await reconcile()

    assert (blind.run.status, blind.breaks, blind.repaired) == ("incomplete", (), 0)
    assert (await stack.withdrawal(response.json()["id"]))["status"] == "submitting"
    assert [row["status"] for row in await breaks(stack)] == ["open"]


async def test_a_run_that_meets_more_withdrawals_in_flight_than_it_reads_is_incomplete(
    stack: Stack, reconcile: Reconcile, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _ = await funded(stack)
    await submitted(stack, user)
    await let_pass(stack, 2)
    monkeypatch.setattr(repair_module, "MAX_IN_FLIGHT", 1)

    assert (await reconcile()).run.status == "incomplete"

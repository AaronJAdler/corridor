"""What must be true once the system has come to rest, whatever was done to it on the way.

These are the design's claims, stated once and asserted after every scenario: one payout
per withdrawal, one credit per deposit, nothing left in flight, nothing given up on, and
books that agree with the providers'.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from corridor.ledger import AccountKind
from tests.payments.support import rows
from tests.support.auth import RegisteredUser
from tests.support.stack import PROVIDER_FEE, TERMINAL, WITHDRAWAL_FEE_BPS, Stack

BANK_ASSETS = ("USD", "MXN", "BRL")


@dataclass(frozen=True)
class Requested:
    """A withdrawal a user asked for, and what is needed to say how it should end."""

    user: RegisteredUser
    id: str
    kind: str
    asset: str
    # Minor units: what the user had before asking, and what was asked for.
    funded: int
    amount: int

    @property
    def fee(self) -> int:
        return self.amount * WITHDRAWAL_FEE_BPS // 10_000

    @property
    def create_operation(self) -> str:
        return "bank.create_payout" if self.kind == "bank" else "custody.create_withdrawal"

    @property
    def read_operation(self) -> str:
        return "bank.get_payout" if self.kind == "bank" else "custody.get_withdrawal"

    @property
    def completed_event(self) -> str:
        return "payout.completed" if self.kind == "bank" else "withdrawal.completed"

    @property
    def failed_event(self) -> str:
        return "payout.failed" if self.kind == "bank" else "withdrawal.failed"


async def assert_at_rest(stack: Stack, *, lost_deposits: Collection[str] = ()) -> None:
    """Every claim that does not depend on what the scenario was.

    ``lost_deposits`` names the provider deposits whose only announcement was dropped, as
    ``provider:id``: nothing in this build can learn of them.
    """
    assert set(await stack.outbox()) <= {"done"}, await _unfinished(stack)
    unprocessed = await rows(stack.db, "SELECT type FROM webhook_events WHERE processed_at IS NULL")
    assert unprocessed == []

    references = set()
    for withdrawal in await stack.withdrawals():
        references.add(str(withdrawal["id"]))
        assert withdrawal["status"] in TERMINAL, withdrawal
        sent = await stack.provider_sends(withdrawal["id"])
        # One request to a provider makes one payout, however often it was repeated.
        assert len(sent) <= 1, sent
        if withdrawal["status"] == "completed":
            assert [(s["id"], s["status"]) for s in sent] == [
                (withdrawal["provider_ref"], "completed")
            ]
        elif sent:
            # Its funds went back to the user, so the provider must not have paid it out.
            assert (withdrawal["status"], sent[0]["status"]) == ("failed", "failed"), (
                withdrawal,
                sent,
            )
    for sent in await stack.sim.payouts() + await stack.sim.withdrawals():
        assert sent["reference"] in references, sent

    held = await rows(
        stack.db,
        "SELECT b.balance FROM account_balances b JOIN ledger_accounts a ON a.id = b.account_id"
        " WHERE a.kind = 'user_held' AND b.balance <> 0",
    )
    assert held == []

    await assert_each_deposit_credited_once(stack, lost_deposits=lost_deposits)
    if not lost_deposits:
        await assert_books_match_the_providers(stack)


async def assert_each_deposit_credited_once(
    stack: Stack, *, lost_deposits: Collection[str] = ()
) -> None:
    arrived = {f"simbank:{deposit['id']}" for deposit in await stack.provider_bank_deposits()}
    arrived |= {
        f"simcustody:{deposit['id']}"
        for deposit in await stack.provider_chain_deposits()
        if deposit["status"] == "confirmed"
    }
    credits = await rows(
        stack.db,
        "SELECT source_id, count(*) AS entries FROM journal_entries"
        " WHERE source_type = 'deposit' AND kind IN ('deposit', 'deposit_suspense')"
        " GROUP BY source_id",
    )
    assert {credit["source_id"]: credit["entries"] for credit in credits} == dict.fromkeys(
        arrived - set(lost_deposits), 1
    )


async def assert_books_match_the_providers(stack: Stack) -> None:
    """What Corridor's ledger says is at each provider is what the provider says is there."""
    for asset in BANK_ASSETS:
        assert await stack.book_balance(
            AccountKind.BANK_SETTLEMENT, asset, provider="simbank"
        ) == await stack.provider_balance("bank", asset), asset
    assert await stack.book_balance(
        AccountKind.CUSTODY_OMNIBUS, "USDC", provider="simcustody"
    ) == await stack.provider_balance("custody", "USDC")


async def assert_paid_out_once(stack: Stack, requested: Requested) -> None:
    """The withdrawal completed, the provider paid it exactly once, and every balance is
    what the amount and the two fees make it."""
    await assert_at_rest(stack)
    row = await stack.withdrawal(requested.id)
    provider_fee = PROVIDER_FEE[requested.asset]
    assert (row["status"], row["provider_fee"]) == ("completed", provider_fee)
    (sent,) = await stack.provider_sends(requested.id)
    assert sent["idempotency_key"] == requested.id
    assert await stack.wallet(requested.user, requested.asset) == (
        requested.funded - requested.amount - requested.fee,
        0,
    )
    assert await fees(stack, requested.asset) == (requested.fee, provider_fee)
    assert await at_provider(stack, requested) == requested.funded - requested.amount - provider_fee


async def assert_given_back(stack: Stack, requested: Requested, status: str = "failed") -> None:
    """The withdrawal ended without a payout, and the user has everything they had."""
    await assert_at_rest(stack)
    assert (await stack.withdrawal(requested.id))["status"] == status
    assert [sent["status"] for sent in await stack.provider_sends(requested.id)] in (
        [],
        ["failed"],
    )
    assert await stack.wallet(requested.user, requested.asset) == (requested.funded, 0)
    assert await fees(stack, requested.asset) == (0, 0)
    assert await at_provider(stack, requested) == requested.funded


async def fees(stack: Stack, asset: str) -> tuple[int, int]:
    """What Corridor earned, and what providers charged it, in an asset."""
    return (
        await stack.book_balance(AccountKind.FEE_REVENUE, asset),
        await stack.book_balance(AccountKind.PROVIDER_FEE_EXPENSE, asset),
    )


async def at_provider(stack: Stack, requested: Requested) -> int:
    """What Corridor's books say is at the provider the withdrawal goes out through."""
    if requested.kind == "bank":
        return await stack.book_balance(
            AccountKind.BANK_SETTLEMENT, requested.asset, provider="simbank"
        )
    return await stack.book_balance(
        AccountKind.CUSTODY_OMNIBUS, requested.asset, provider="simcustody"
    )


async def _unfinished(stack: Stack) -> list[dict[str, Any]]:
    return await rows(
        stack.db,
        "SELECT topic, status, attempts, last_error FROM outbox_events WHERE status <> 'done'",
    )

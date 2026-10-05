"""Properties of the ledger over generated sequences of operations.

Each example opens fresh accounts, runs a generated sequence against the real ledger and a
plain-Python model side by side, and then checks that they agree, that the verifier is
clean, and that the books balance.
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Literal

from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from corridor import ledger
from corridor.ledger import AccountKind, EntryDraft, InsufficientFunds, credit, debit
from corridor.platform.config import Settings
from corridor.platform.db import Database, create_engine
from corridor.platform.ids import new_id
from tests.support.ledger import PROVIDER

USERS = 3


@dataclass(frozen=True)
class Operation:
    kind: Literal["deposit", "transfer", "hold", "release", "settle"]
    user: int
    other: int
    amount: int
    fee: int


operations = st.lists(
    st.builds(
        Operation,
        kind=st.sampled_from(["deposit", "transfer", "hold", "release", "settle"]),
        user=st.integers(0, USERS - 1),
        other=st.integers(0, USERS - 1),
        amount=st.integers(1, 500_00),
        fee=st.integers(0, 2_00),
    ),
    max_size=40,
)


async def run_sequence(app_settings: Settings, sequence: list[Operation]) -> None:
    db = Database(create_engine(app_settings, application_name="corridor-property-test"))
    try:
        async with db.transaction() as session:
            owners = [new_id() for _ in range(USERS)]
            available = [
                (
                    await ledger.open_account(
                        session, AccountKind.USER_AVAILABLE, "USD", owner_id=owner
                    )
                ).id
                for owner in owners
            ]
            held = [
                (
                    await ledger.open_account(session, AccountKind.USER_HELD, "USD", owner_id=owner)
                ).id
                for owner in owners
            ]
            # A provider of its own, so this example's system-side totals are its alone.
            provider = f"{PROVIDER}-{new_id().hex[:12]}"
            settlement = (
                await ledger.open_account(
                    session, AccountKind.BANK_SETTLEMENT, "USD", provider=provider
                )
            ).id
            fees = (await ledger.open_account(session, AccountKind.FEE_REVENUE, "USD")).id
            fees_before = await ledger.get_balance(session, fees)

        model: dict[uuid.UUID, int] = dict.fromkeys([*available, *held, settlement], 0)
        fees_earned = 0

        for operation in sequence:
            user, other, amount, fee = (
                operation.user,
                operation.other,
                operation.amount,
                operation.fee,
            )
            if operation.kind == "deposit":
                postings = (debit(settlement, amount), credit(available[user], amount))
                changes = {settlement: amount, available[user]: amount}
            elif operation.kind == "transfer":
                if user == other:
                    continue
                postings = (debit(available[user], amount + fee), credit(available[other], amount))
                postings += (credit(fees, fee),) if fee else ()
                changes = {available[user]: -(amount + fee), available[other]: amount}
            elif operation.kind == "hold":
                postings = (debit(available[user], amount), credit(held[user], amount))
                changes = {available[user]: -amount, held[user]: amount}
            elif operation.kind == "release":
                postings = (debit(held[user], amount), credit(available[user], amount))
                changes = {held[user]: -amount, available[user]: amount}
            else:  # settle: held funds leave through the bank; the fee stays as revenue
                if amount <= fee:
                    continue
                postings = (debit(held[user], amount), credit(settlement, amount - fee))
                postings += (credit(fees, fee),) if fee else ()
                changes = {held[user]: -amount, settlement: -(amount - fee)}

            # Only user accounts are constrained. The settlement account may go either way.
            affordable = all(
                model[account] + change >= 0
                for account, change in changes.items()
                if account != settlement
            )
            draft = EntryDraft(operation.kind, "property", str(new_id()), postings)
            try:
                async with db.transaction() as session:
                    await ledger.post_entry(session, draft)
                posted = True
            except InsufficientFunds:
                posted = False

            assert posted == affordable, (
                f"{operation} posted={posted} but the model says affordable={affordable}"
            )
            if posted:
                for account, change in changes.items():
                    model[account] += change
                if operation.kind in ("transfer", "settle"):
                    fees_earned += fee

        async with db.transaction() as session:
            actual = await ledger.get_balances(session, list(model))
            derived = await ledger.derive_balances(session, list(model))
            fees_after = await ledger.get_balance(session, fees)
            findings = await ledger.verify(session)

        assert actual == model
        assert derived == model
        assert fees_after - fees_before == fees_earned
        assert all(balance >= 0 for account, balance in actual.items() if account != settlement)
        # The accounting equation for this example: what the bank holds equals what the
        # users are owed plus what was earned.
        assert model[settlement] == sum(model[a] for a in [*available, *held]) + fees_earned
        assert findings == []
    finally:
        await db.dispose()


@hypothesis_settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(sequence=operations)
def test_any_sequence_of_operations_leaves_the_ledger_sound(
    settings: Settings, sequence: list[Operation]
) -> None:
    asyncio.run(run_sequence(settings, sequence))

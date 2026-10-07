"""The design's central claim over generated histories: whatever users do and whatever
fails while they do it, the system comes to rest with every claim of ``checks`` true and
every balance equal to what the accepted operations add up to.

Each example gets a database and a simulator of its own. The sequence mixes deposits,
transfers, withdrawals and cancellations with a fault schedule: provider calls that fail in
each of the four ways or are refused, a worker that dies at a chosen point, and webhooks
that are duplicated, reordered, held back or dropped. Only failures this build can recover
from are generated: an event whose loss needs reconciliation is covered, by name, in
``test_webhook_delivery``.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from pydantic import SecretStr

from corridor import ledger
from corridor.ledger import AccountKind
from corridor.platform.clock import ManualClock, use_clock
from corridor.platform.config import Settings
from corridor.platform.db import Database, create_engine
from corridor.platform.money import format_amount
from corridor.platform.redis import RedisStore
from tests.chaos.checks import assert_at_rest, fees
from tests.support import postgres
from tests.support.auth import RegisteredUser
from tests.support.providers import (
    ACCOUNT_NUMBER,
    CLOSED_ACCOUNT_NUMBER,
    EXTERNAL_ADDRESS,
    REJECTED_ADDRESS,
    START,
)
from tests.support.stack import (
    PROVIDER_FEE,
    SUBMIT_POINTS,
    TRANSFER_FEE_BPS,
    WEBHOOK_POINTS,
    WITHDRAWAL_FEE_BPS,
    Stack,
    running_stack,
)

USERS = 3
# What each user is given before the history starts, in the units ``amounts`` are in.
STARTING_BALANCE = 1_000_00
ASSET = {"bank": "USD", "chain": "USDC"}
# Events whose loss the sweeper, or a later event, makes good.
RECOVERABLE_DROPS = (
    "payout.completed",
    "payout.failed",
    "withdrawal.completed",
    "withdrawal.failed",
    "deposit.detected",
)
FAULT_OPERATIONS = (
    "bank.create_payout",
    "custody.create_withdrawal",
    "bank.get_payout",
    "custody.get_withdrawal",
)


@dataclass(frozen=True)
class Deposit:
    user: int
    rail: str
    amount: int


@dataclass(frozen=True)
class Transfer:
    sender: int
    recipient: int
    rail: str
    amount: int


@dataclass(frozen=True)
class Withdraw:
    user: int
    rail: str
    amount: int
    # To an account that is closed, or an address the network rejects.
    doomed: bool


@dataclass(frozen=True)
class Cancel:
    # Which of the withdrawals asked for so far, counted round.
    which: int


@dataclass(frozen=True)
class Wait:
    seconds: int


@dataclass(frozen=True)
class Fault:
    operation: str
    mode: str
    times: int
    # What an ``error`` answers with: unavailable, refused, or shedding load.
    status: int


@dataclass(frozen=True)
class Die:
    point: str


@dataclass(frozen=True)
class Webhooks:
    duplicates: int
    reverse: bool
    hold: bool
    drop_types: tuple[str, ...]


Step = Deposit | Transfer | Withdraw | Cancel | Wait | Fault | Die | Webhooks

users = st.integers(0, USERS - 1)
rails = st.sampled_from(["bank", "chain"])
# Cents of USD, or hundredths of a USDC: from one unit to a few hundred.
amounts = st.integers(1, 300_00)
operations = st.one_of(
    st.builds(Deposit, users, rails, amounts),
    st.builds(Deposit, users, rails, amounts),
    st.builds(Transfer, users, users, rails, amounts),
    st.builds(Withdraw, users, rails, amounts, st.booleans()),
    st.builds(Withdraw, users, rails, amounts, st.just(False)),
    st.builds(Cancel, st.integers(0, 7)),
    st.builds(Wait, st.sampled_from([1, 5, 30, 150])),
    st.builds(
        Webhooks,
        st.integers(0, 2),
        st.booleans(),
        st.booleans(),
        st.lists(st.sampled_from(RECOVERABLE_DROPS), max_size=2, unique=True).map(tuple),
    ),
)
# At most two faults of at most two calls each, and two deaths: fewer failures than an
# outbox event has attempts, so that giving up on one is never the expected outcome. Any
# status goes with any mode, including a 4xx for a call that was carried out, which the
# provider contract rules out and which must lose no money all the same.
faults = st.lists(
    st.tuples(
        st.integers(0, 30),
        st.builds(
            Fault,
            st.sampled_from(FAULT_OPERATIONS),
            st.sampled_from(["error", "error_after_effect", "timeout", "timeout_after_effect"]),
            st.integers(1, 2),
            st.sampled_from([503, 422, 429]),
        ),
    ),
    max_size=2,
)
deaths = st.lists(
    st.tuples(st.integers(0, 30), st.builds(Die, st.sampled_from(SUBMIT_POINTS + WEBHOOK_POINTS))),
    max_size=2,
)


@st.composite
def histories(draw: st.DrawFn) -> list[Step]:
    """What users do, with the failures placed among it."""
    steps: list[Step] = list(draw(st.lists(operations, min_size=1, max_size=14)))
    for position, failure in sorted(draw(faults) + draw(deaths), key=lambda placed: placed[0]):
        steps.insert(min(position, len(steps)), failure)
    return steps


# The least a withdrawal costs in each asset: the defaults of ``withdrawal_min_fee``, in
# minor units. A history is free to withdraw a single cent.
WITHDRAWAL_MIN_FEE = {"USD": 25, "USDC": 150_000}


def minor(amount: int, rail: str) -> int:
    """The generated amount in the rail's minor units: cents, or 10,000 micro-USDC."""
    return amount if rail == "bank" else amount * 10_000


class History:
    """Runs a generated sequence against the system and keeps the books it should end with,
    from nothing but what the API accepted."""

    def __init__(self, stack: Stack, people: list[RegisteredUser]) -> None:
        self.stack, self.people = stack, people
        self.expected: dict[tuple[int, str], int] = defaultdict(int)
        self.transfer_fees: dict[str, int] = defaultdict(int)
        self.asked: list[tuple[int, str, dict[str, Any]]] = []
        self.canceled: set[str] = set()
        self.doomed: set[str] = set()
        self._beneficiaries: dict[tuple[int, bool], str] = {}

    async def apply(self, step: Step) -> None:
        stack = self.stack
        match step:
            case Deposit(user, rail, amount):
                text = format_amount(minor(amount, rail), ASSET[rail])
                if rail == "bank":
                    await stack.bank_deposit(self.people[user], text)
                else:
                    await stack.chain_deposit(self.people[user], text)
                # No event that a deposit depends on is ever dropped here, so it arrives.
                self.expected[user, ASSET[rail]] += minor(amount, rail)
            case Transfer(sender, recipient, rail, amount):
                if sender == recipient:
                    return
                sent = minor(amount, rail)
                response = await stack.transfer(
                    self.people[sender],
                    self.people[recipient],
                    format_amount(sent, ASSET[rail]),
                    ASSET[rail],
                )
                assert response.status_code in (201, 402), response.text
                if response.status_code == 201:
                    fee = sent * TRANSFER_FEE_BPS // 10_000
                    self.expected[sender, ASSET[rail]] -= sent + fee
                    self.expected[recipient, ASSET[rail]] += sent
                    self.transfer_fees[ASSET[rail]] += fee
            case Withdraw(user, rail, amount, doomed):
                text = format_amount(minor(amount, rail), ASSET[rail])
                if rail == "bank":
                    response = await stack.withdraw(
                        self.people[user],
                        text,
                        beneficiary_id=await self._beneficiary(user, doomed),
                    )
                else:
                    response = await stack.withdraw(
                        self.people[user],
                        text,
                        asset="USDC",
                        to_address=REJECTED_ADDRESS if doomed else EXTERNAL_ADDRESS,
                    )
                assert response.status_code in (202, 402), response.text
                if response.status_code == 202:
                    self.asked.append((user, rail, response.json()))
                    if doomed:
                        self.doomed.add(response.json()["id"])
            case Cancel(which):
                if not self.asked:
                    return
                user, _, withdrawal = self.asked[which % len(self.asked)]
                response = await stack.cancel(self.people[user], withdrawal["id"])
                assert response.status_code in (200, 409), response.text
                if response.status_code == 200:
                    self.canceled.add(withdrawal["id"])
            case Wait(seconds):
                await stack.turn(seconds)
            case Fault(operation, mode, times, status):
                await stack.fault(operation, mode, times=times, status=status)
            case Die(point):
                stack.crashes.arm(point)
            case Webhooks(duplicates, reverse, hold, drop_types):
                await stack.webhooks_behave(
                    duplicates=duplicates, reverse=reverse, hold=hold, drop_types=list(drop_types)
                )

    async def fund_everyone(self) -> None:
        """Before anything is made to fail, everyone has money in both assets: a history
        in which nobody can afford to withdraw says little about withdrawals."""
        for user in range(USERS):
            for rail in ASSET:
                await self.apply(Deposit(user, rail, STARTING_BALANCE))
        await self.stack.settle()

    async def _beneficiary(self, user: int, doomed: bool) -> str:
        if (user, doomed) not in self._beneficiaries:
            self._beneficiaries[user, doomed] = await self.stack.beneficiary(
                self.people[user],
                account_number=CLOSED_ACCOUNT_NUMBER if doomed else ACCOUNT_NUMBER,
            )
        return self._beneficiaries[user, doomed]

    async def check(self) -> None:
        stack = self.stack
        await assert_at_rest(stack)

        withdrawal_fees: dict[str, int] = defaultdict(int)
        completed: dict[str, int] = defaultdict(int)
        for user, rail, asked in self.asked:
            asset = ASSET[rail]
            row = await stack.withdrawal(asked["id"])
            if asked["id"] in self.canceled:
                # The API said it was called back, so it was never sent.
                assert row["status"] == "canceled", row
                assert await stack.provider_sends(asked["id"]) == []
            else:
                assert row["status"] in ("completed", "failed"), row
            if asked["id"] in self.doomed:
                assert row["status"] != "completed", row
            if row["status"] == "completed":
                assert row["fee"] == max(
                    WITHDRAWAL_MIN_FEE[asset], row["amount"] * WITHDRAWAL_FEE_BPS // 10_000
                )
                self.expected[user, asset] -= row["amount"] + row["fee"]
                withdrawal_fees[asset] += row["fee"]
                completed[asset] += 1

        for index, person in enumerate(self.people):
            for asset in ASSET.values():
                assert await stack.wallet(person, asset) == (self.expected[index, asset], 0), (
                    index,
                    asset,
                )
        for asset in ASSET.values():
            assert await fees(stack, asset) == (
                self.transfer_fees[asset] + withdrawal_fees[asset],
                completed[asset] * PROVIDER_FEE[asset],
            ), asset
            # Everything that came in and has not gone out is owed to a user or was earned.
            kind, provider = (
                (AccountKind.BANK_SETTLEMENT, "simbank")
                if asset == "USD"
                else (AccountKind.CUSTODY_OMNIBUS, "simcustody")
            )
            owed = sum(self.expected[index, asset] for index in range(USERS))
            earned = self.transfer_fees[asset] + withdrawal_fees[asset]
            spent = completed[asset] * PROVIDER_FEE[asset]
            assert await stack.book_balance(kind, asset, provider=provider) == (
                owed + earned - spent
            ), asset

        async with stack.db.transaction() as session:
            assert await ledger.verify(session) == []


async def run_history(settings: Settings, steps: list[Step], seed: int) -> None:
    db = Database(create_engine(settings, application_name="corridor-chaos-property"))
    try:
        with use_clock(ManualClock(START)) as clock:
            assert isinstance(clock, ManualClock)
            async with running_stack(settings, db, clock, seed=seed) as stack:
                history = History(stack, [await stack.person() for _ in range(USERS)])
                await history.fund_everyone()
                for step in steps:
                    await history.apply(step)
                # Whatever was being held back is let go; what was dropped stays dropped.
                await stack.webhooks_behave(hold=False)
                await stack.settle()
                await history.check()
    finally:
        await db.dispose()


@hypothesis_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=histories(), seed=st.integers(0, 2**16))
def test_any_history_of_money_movements_under_any_fault_schedule_converges(
    settings: Settings,
    database_template: str,
    redis: RedisStore,
    steps: list[Step],
    seed: int,
) -> None:
    # A database for this example alone: the claims are about everything in it.
    created = postgres.create_database(database_template)
    try:
        example = settings.model_copy(
            update={
                "database_url": SecretStr(created.app_url),
                "database_owner_url": SecretStr(created.owner_url),
            }
        )
        asyncio.run(run_history(example, steps, seed))
    finally:
        postgres.drop_database(created)

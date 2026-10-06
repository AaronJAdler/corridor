"""What the adapters make of a provider that fails or goes quiet.

The same four faults are injected into the two operations that move money out, a bank
payout and an on-chain withdrawal. In none of them may the adapter say the operation
failed: it does not know.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from corridor.platform.config import Settings
from corridor.providers import (
    Payout,
    ProviderOutcomeUnknown,
    ProviderRejected,
    SimBank,
    SimCustody,
    Withdrawal,
)
from tests.providers.conftest import (
    ACCOUNT_NUMBER,
    CUSTOMER,
    EXTERNAL_ADDRESS,
    REFERENCE,
    ROUTING_NUMBER,
    Sim,
)

# Long enough that a healthy in-process call never meets it, short enough to wait out.
DEADLINE_SECONDS = 0.2
# How long the simulator would keep the caller waiting if nobody gave up.
HANG_SECONDS = 30


@dataclass(frozen=True)
class MoneyOut:
    """One of the two operations, behind a common face."""

    operation: str
    send: Callable[[], Awaitable[Payout | Withdrawal]]
    held: Callable[[], Awaitable[list[dict[str, Any]]]]


@pytest.fixture(params=["payout", "withdrawal"])
async def out(request: pytest.FixtureRequest, sim: Sim, provider_settings: Settings) -> MoneyOut:
    impatient = provider_settings.model_copy(update={"provider_timeout_seconds": DEADLINE_SECONDS})
    if request.param == "payout":
        bank = SimBank(impatient, client=sim.http)
        beneficiary = await bank.create_beneficiary(
            customer_reference=CUSTOMER,
            asset_code="USD",
            holder_name="Maria Silva",
            account_number=ACCOUNT_NUMBER,
            routing_number=ROUTING_NUMBER,
            idempotency_key="ben-1",
        )

        async def send_payout() -> Payout:
            return await bank.create_payout(
                beneficiary_id=beneficiary.id,
                asset_code="USD",
                amount=100_00,
                reference=REFERENCE,
                idempotency_key=REFERENCE,
            )

        return MoneyOut("bank.create_payout", send_payout, sim.payouts)

    custody = SimCustody(impatient, client=sim.http)

    async def send_withdrawal() -> Withdrawal:
        return await custody.create_withdrawal(
            asset_code="USDC",
            amount=25_000_000,
            to_address=EXTERNAL_ADDRESS,
            reference=REFERENCE,
            idempotency_key=REFERENCE,
        )

    return MoneyOut("custody.create_withdrawal", send_withdrawal, sim.withdrawals)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_a_server_error_leaves_the_outcome_unknown_and_nothing_happened(
    out: MoneyOut, sim: Sim, status: int
) -> None:
    await sim.inject(out.operation, "error", status=status)

    with pytest.raises(ProviderOutcomeUnknown):
        await out.send()

    assert await out.held() == []


async def test_a_server_error_after_the_effect_leaves_exactly_one_which_a_retry_returns(
    out: MoneyOut, sim: Sim
) -> None:
    await sim.inject(out.operation, "error_after_effect")

    with pytest.raises(ProviderOutcomeUnknown):
        await out.send()

    [held] = await out.held()
    assert held["idempotency_key"] == REFERENCE
    retried = await out.send()
    assert retried.id == held["id"]
    assert len(await out.held()) == 1


async def test_a_timeout_leaves_the_outcome_unknown_within_the_deadline(
    out: MoneyOut, sim: Sim
) -> None:
    await sim.inject(out.operation, "timeout", hang_seconds=HANG_SECONDS)
    started = time.monotonic()

    with pytest.raises(ProviderOutcomeUnknown) as unknown:
        await out.send()

    assert "deadline" in unknown.value.detail

    assert time.monotonic() - started < 5
    assert await out.held() == []


async def test_a_timeout_after_the_effect_leaves_exactly_one_which_a_retry_returns(
    out: MoneyOut, sim: Sim
) -> None:
    await sim.inject(out.operation, "timeout_after_effect", hang_seconds=HANG_SECONDS)

    with pytest.raises(ProviderOutcomeUnknown):
        await out.send()

    [held] = await out.held()
    assert held["idempotency_key"] == REFERENCE
    retried = await out.send()
    assert retried.id == held["id"]
    assert len(await out.held()) == 1


async def test_a_refusal_is_not_an_unknown_outcome(out: MoneyOut, sim: Sim) -> None:
    await sim.inject(out.operation, "error", status=422)

    with pytest.raises(ProviderRejected) as refused:
        await out.send()

    assert (refused.value.status, refused.value.code) == (422, "injected_fault")


async def test_a_read_that_hangs_is_unknown_too(sim: Sim, provider_settings: Settings) -> None:
    impatient = provider_settings.model_copy(update={"provider_timeout_seconds": DEADLINE_SECONDS})
    await sim.inject("bank.get_payout", "timeout", hang_seconds=HANG_SECONDS)

    with pytest.raises(ProviderOutcomeUnknown):
        await SimBank(impatient, client=sim.http).get_payout("po_any")

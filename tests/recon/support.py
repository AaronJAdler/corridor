"""Builders for the reconciliation tests: money that arrived or left, and a webhook that
did not."""

from typing import Any

from tests.payments.support import rows
from tests.support.auth import RegisteredUser
from tests.support.providers import ACCOUNT_NUMBER, EXTERNAL_ADDRESS, advance
from tests.support.stack import Stack

BANK = "simbank"
CUSTODY = "simcustody"


async def let_pass(stack: Stack, seconds: float = 60) -> None:
    """Let time pass at the providers and here, and nothing else: no webhook is processed
    and no scheduled job runs, so whatever is missing stays missing for the run to find.

    In two steps, because a window is half-open: what the providers finish during the first
    step is then in the past, and not at the very instant a run's window ends."""
    await advance(stack.sim, stack.clock, seconds - 1)
    await advance(stack.sim, stack.clock, 1)


async def funded(stack: Stack, amount: str = "500.00") -> tuple[RegisteredUser, str]:
    """A user whose bank deposit arrived and was credited, and the bank's id for it."""
    user = await stack.person()
    deposit_id = await stack.bank_deposit(user, amount)
    await stack.settle()
    return user, deposit_id


async def submitted(
    stack: Stack, user: RegisteredUser, amount: str = "100.00", **beneficiary: str
) -> str:
    """A bank withdrawal of the user's, sent to the bank and not yet settled by it."""
    beneficiary_id = await stack.beneficiary(
        user, account_number=beneficiary.get("account_number", ACCOUNT_NUMBER)
    )
    response = await stack.withdraw(user, amount, beneficiary_id=beneficiary_id)
    assert response.status_code == 202, response.text
    await stack.dispatcher.drain()
    withdrawal_id = str(response.json()["id"])
    assert (await stack.withdrawal(withdrawal_id))["status"] == "submitted"
    return withdrawal_id


async def chain_submitted(stack: Stack, user: RegisteredUser, amount: str = "25.000000") -> str:
    response = await stack.withdraw(user, amount, asset="USDC", to_address=EXTERNAL_ADDRESS)
    assert response.status_code == 202, response.text
    await stack.dispatcher.drain()
    return str(response.json()["id"])


async def breaks(stack: Stack, where: str = "true") -> list[dict[str, Any]]:
    return await rows(stack.db, f"SELECT * FROM recon_breaks WHERE {where} ORDER BY id")  # noqa: S608


def kinds(found: Any) -> list[str]:
    return sorted(item.kind if hasattr(item, "kind") else item["kind"] for item in found)

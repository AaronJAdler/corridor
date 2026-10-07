"""The starting points the scenarios share: a user with money, and a withdrawal asked for."""

from tests.chaos.checks import Requested
from tests.support.auth import RegisteredUser
from tests.support.providers import ACCOUNT_NUMBER, EXTERNAL_ADDRESS
from tests.support.stack import Stack

KINDS = ("bank", "chain")
MODES = ("error", "error_after_effect", "timeout", "timeout_after_effect")

# What a funded user starts with, and what they withdraw, by kind, as the API takes them
# and in minor units.
FUNDING = {"bank": ("USD", "500.00", 500_00), "chain": ("USDC", "50.000000", 50_000_000)}
WITHDRAWN = {"bank": ("100.00", 100_00), "chain": ("25.000000", 25_000_000)}


async def funded(stack: Stack, kind: str) -> RegisteredUser:
    """A user whose deposit, by bank or on chain, has arrived and been credited."""
    user = await stack.person()
    asset, amount, minor = FUNDING[kind]
    if kind == "bank":
        await stack.bank_deposit(user, amount)
    else:
        await stack.chain_deposit(user, amount)
    await stack.settle()
    assert await stack.wallet(user, asset) == (minor, 0)
    return user


async def requested(
    stack: Stack,
    kind: str,
    *,
    account_number: str = ACCOUNT_NUMBER,
    to_address: str = EXTERNAL_ADDRESS,
) -> Requested:
    """A funded user's withdrawal, accepted by the API and not yet sent anywhere."""
    user = await funded(stack, kind)
    asset, _, funding = FUNDING[kind]
    amount, minor = WITHDRAWN[kind]
    if kind == "bank":
        beneficiary = await stack.beneficiary(user, account_number=account_number)
        response = await stack.withdraw(user, amount, beneficiary_id=beneficiary)
    else:
        response = await stack.withdraw(user, amount, asset=asset, to_address=to_address)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "held"
    return Requested(
        user=user, id=response.json()["id"], kind=kind, asset=asset, funded=funding, amount=minor
    )

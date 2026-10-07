"""The payments verifier: what the ledger holds for withdrawals and in suspense, against
the withdrawals and the deposits that account for it.

The ledger's own verifier proves that the books add up. It cannot say whether they add up
to the right thing, because it knows nothing of withdrawals or deposits. Two sums tie the
two together, and each is recomputed here:

- a user's held balance in an asset is the amount and the fee of that user's withdrawals
  in that asset whose funds are still reserved;
- what suspense holds in an asset is the amount of the deposits in suspense in that asset.

It reads only, and it reads the ledger and the payments tables in separate statements. On a
live system the caller runs it in one snapshot (a ``REPEATABLE READ`` transaction), or a
withdrawal that commits between two of them is reported as a difference.
"""

import uuid
from typing import Final, cast

from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger
from corridor.ledger import AccountKind, Finding
from corridor.payments.models import DepositRow, WithdrawalRow

# Core tables: every statement against them is written out below.
_deposits = cast(Table, DepositRow.__table__)
_withdrawals = cast(Table, WithdrawalRow.__table__)

HELD_MISMATCH: Final = "held_mismatch"
SUSPENSE_MISMATCH: Final = "suspense_mismatch"

# The states in which a withdrawal's amount and fee are in its user's held balance.
_RESERVED: Final = ("held", "submitting", "submitted")


async def verify(session: AsyncSession, *, limit_per_check: int = 100) -> list[Finding]:
    """Run both checks and return what they found. An empty list means that every held
    balance and every suspense balance is accounted for."""
    return [
        *(await _held(session))[:limit_per_check],
        *(await _suspense(session))[:limit_per_check],
    ]


async def _held(session: AsyncSession) -> list[Finding]:
    held: dict[tuple[uuid.UUID | None, str], int] = {
        (account.owner_id, account.asset_code): balance
        for account, balance in await ledger.balances_by_kind(session, AccountKind.USER_HELD)
    }
    rows = await session.execute(
        select(
            _withdrawals.c.user_id,
            _withdrawals.c.asset_code,
            func.sum(_withdrawals.c.amount + _withdrawals.c.fee).label("reserved"),
        )
        .where(_withdrawals.c.status.in_(_RESERVED))
        .group_by(_withdrawals.c.user_id, _withdrawals.c.asset_code)
    )
    reserved: dict[tuple[uuid.UUID | None, str], int] = {
        (row.user_id, row.asset_code): int(row.reserved) for row in rows
    }
    return [
        Finding(
            HELD_MISMATCH,
            f"{owner}:{asset}",
            f"user_held is {held.get((owner, asset), 0)} but the withdrawals that still"
            f" reserve funds come to {reserved.get((owner, asset), 0)}",
        )
        for owner, asset in sorted(held.keys() | reserved.keys(), key=str)
        if held.get((owner, asset), 0) != reserved.get((owner, asset), 0)
    ]


async def _suspense(session: AsyncSession) -> list[Finding]:
    booked = {
        account.asset_code: balance
        for account, balance in await ledger.balances_by_kind(session, AccountKind.SUSPENSE)
    }
    rows = await session.execute(
        select(_deposits.c.asset_code, func.sum(_deposits.c.amount).label("waiting"))
        .where(_deposits.c.status == "suspense")
        .group_by(_deposits.c.asset_code)
    )
    waiting = {row.asset_code: int(row.waiting) for row in rows}
    return [
        Finding(
            SUSPENSE_MISMATCH,
            asset,
            f"suspense holds {booked.get(asset, 0)} but the deposits in suspense come to"
            f" {waiting.get(asset, 0)}",
        )
        for asset in sorted(booked.keys() | waiting.keys())
        if booked.get(asset, 0) != waiting.get(asset, 0)
    ]

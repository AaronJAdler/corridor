"""Reads for the modules above payments that look at deposits and withdrawals as a whole:
reconciliation compares them with what the providers report.

Nothing here takes a principal, because none of it is asked for by a user: these are never
called from a route that serves one. Every function takes the caller's session and only
reads.
"""

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Final, cast

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.payments.deposits import as_deposit
from corridor.payments.models import DepositRow, WithdrawalRow
from corridor.payments.types import Deposit, Withdrawal
from corridor.payments.withdrawals import as_withdrawal

# Core tables: every statement against them is written out below.
_deposits = cast(Table, DepositRow.__table__)
_withdrawals = cast(Table, WithdrawalRow.__table__)

# A deposit in one of these has a journal entry: the money is on Corridor's books.
_CREDITED: Final = ("completed", "suspense", "returned")
# A withdrawal in one of these may be at its provider, with nothing heard of how it ended.
_IN_FLIGHT: Final = ("submitting", "submitted")


async def find_deposits(
    session: AsyncSession, provider: str, provider_refs: Collection[str]
) -> dict[str, Deposit]:
    """The recorded deposits among a provider's, by the provider's id for each."""
    if not provider_refs:
        return {}
    rows = await session.execute(
        select(_deposits).where(
            _deposits.c.provider == provider, _deposits.c.provider_ref.in_(list(provider_refs))
        )
    )
    return {row["provider_ref"]: as_deposit(row) for row in rows.mappings()}


async def deposits_credited_between(
    session: AsyncSession,
    provider: str,
    asset: str,
    start: datetime,
    end: datetime,
    *,
    limit: int,
) -> list[Deposit]:
    """A provider's deposits in one asset that are on the books and were last changed in
    ``[start, end)``, oldest first."""
    rows = await session.execute(
        select(_deposits)
        .where(
            _deposits.c.provider == provider,
            _deposits.c.asset_code == asset,
            _deposits.c.status.in_(_CREDITED),
            _deposits.c.updated_at >= start,
            _deposits.c.updated_at < end,
        )
        .order_by(_deposits.c.id)
        .limit(limit)
    )
    return [as_deposit(row) for row in rows.mappings()]


async def find_withdrawals(
    session: AsyncSession, withdrawal_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, Withdrawal]:
    if not withdrawal_ids:
        return {}
    rows = await session.execute(
        select(_withdrawals).where(_withdrawals.c.id.in_(list(withdrawal_ids)))
    )
    return {row["id"]: as_withdrawal(row) for row in rows.mappings()}


async def withdrawals_in_flight(session: AsyncSession, *, limit: int) -> list[Withdrawal]:
    """The withdrawals a provider may have and Corridor has not closed, oldest first."""
    rows = await session.execute(
        select(_withdrawals)
        .where(_withdrawals.c.status.in_(_IN_FLIGHT))
        .order_by(_withdrawals.c.id)
        .limit(limit)
    )
    return [as_withdrawal(row) for row in rows.mappings()]


async def withdrawals_completed_between(
    session: AsyncSession,
    provider: str,
    asset: str,
    start: datetime,
    end: datetime,
    *,
    limit: int,
) -> list[Withdrawal]:
    """A provider's withdrawals in one asset that were settled in ``[start, end)``, oldest
    first. Settling is the last thing that changes a withdrawal."""
    rows = await session.execute(
        select(_withdrawals)
        .where(
            _withdrawals.c.status == "completed",
            _withdrawals.c.provider == provider,
            _withdrawals.c.asset_code == asset,
            _withdrawals.c.updated_at >= start,
            _withdrawals.c.updated_at < end,
        )
        .order_by(_withdrawals.c.id)
        .limit(limit)
    )
    return [as_withdrawal(row) for row in rows.mappings()]

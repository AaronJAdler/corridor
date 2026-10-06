"""Wallet endpoints: what the user holds, and what happened to it.

Both read only the wallet of the user the credential acts for. There is no user id in the
path to get wrong.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import identity, wallets
from corridor.api.deps import Db, require
from corridor.identity import Principal, Scope
from corridor.ledger import Direction
from corridor.platform.money import format_amount
from corridor.platform.pagination import DEFAULT_LIMIT, Page

router = APIRouter(prefix="/v1/wallets", tags=["wallets"])

WalletReader = Annotated[Principal, Depends(require(Scope.WALLET_READ))]


class WalletResponse(BaseModel):
    asset: str
    # Decimal strings in major units, with exactly the asset's decimal places.
    available: str
    held: str
    total: str

    @classmethod
    def of(cls, balance: wallets.WalletBalance) -> Self:
        return cls(
            asset=balance.asset,
            available=format_amount(balance.available, balance.asset),
            held=format_amount(balance.held, balance.asset),
            total=format_amount(balance.total, balance.asset),
        )


class WalletListResponse(BaseModel):
    wallets: list[WalletResponse]


class EntryResponse(BaseModel):
    # The journal entry the movement is part of.
    id: uuid.UUID
    kind: str
    # A credit added to the available balance and a debit took from it.
    direction: Literal["credit", "debit"]
    asset: str
    amount: str
    balance_after: str
    posted_at: datetime

    @classmethod
    def of(cls, entry: wallets.StatementEntry) -> Self:
        return cls(
            id=entry.entry_id,
            kind=entry.kind,
            direction="credit" if entry.direction is Direction.CREDIT else "debit",
            asset=entry.asset,
            amount=format_amount(entry.amount, entry.asset),
            balance_after=format_amount(entry.balance_after, entry.asset),
            posted_at=entry.posted_at,
        )


class EntryPageResponse(BaseModel):
    items: list[EntryResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None

    @classmethod
    def of(cls, page: Page[wallets.StatementEntry]) -> Self:
        return cls(
            items=[EntryResponse.of(entry) for entry in page.items], next_cursor=page.next_cursor
        )


async def provision_for_new_user(session: AsyncSession, user: identity.User) -> None:
    """The registration hook: a user is created with a wallet in every asset."""
    await wallets.provision(session, user.id)


@router.get("", summary="The balances of every wallet")
async def list_wallets(principal: WalletReader, db: Db) -> WalletListResponse:
    balances = await db.run(lambda session: wallets.get_wallets(session, principal.user_id))
    return WalletListResponse(wallets=[WalletResponse.of(balance) for balance in balances])


@router.get("/{asset}/entries", summary="The statement of one wallet, newest entry first")
async def list_entries(
    asset: str,
    principal: WalletReader,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> EntryPageResponse:
    page = await db.run(
        lambda session: wallets.list_entries(
            session, principal.user_id, asset, cursor=cursor, limit=limit
        )
    )
    return EntryPageResponse.of(page)

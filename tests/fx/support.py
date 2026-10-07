"""Builders for FX tests: people with wallets, rates out of thin air, and a quote in a line."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import fx, ledger, wallets
from corridor.fx import Conversion, Quote
from corridor.identity import Principal, User
from corridor.ledger import AccountKind
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import Rate
from tests.identity.support import add_user
from tests.support.ledger import fund

USD_MXN = Decimal("17.25")


async def add_person(session: AsyncSession, name: str) -> User:
    """A registered user with a wallet in every asset, as registration over HTTP leaves one."""
    user = await add_user(session, name)
    await wallets.provision(session, user.id)
    return user


def acting_as(user: User) -> Principal:
    return Principal.for_user(user.id, user.role, new_id())


def agent_of(user: User, *scopes: str) -> Principal:
    return Principal(
        user_id=user.id,
        actor_type="agent",
        actor_id=new_id(),
        role="user",
        scopes=frozenset(scopes),
        session_id=None,
    )


def rate_of(
    mid: Decimal | str = USD_MXN,
    base: str = "USD",
    quote: str = "MXN",
    *,
    as_of: datetime | None = None,
) -> Rate:
    return Rate(base=base, quote=quote, mid=Decimal(mid), as_of=as_of or utcnow())


@dataclass
class StubRates:
    """A rate source that answers from memory and counts how often it was asked."""

    mid: Decimal = USD_MXN
    as_of: datetime | None = None
    failure: Exception | None = None
    name: str = "stubfx"
    asked: list[tuple[str, str]] = field(default_factory=list)

    async def get_rate(self, base: str, quote: str) -> Rate:
        self.asked.append((base, quote))
        if self.failure is not None:
            raise self.failure
        return Rate(base=base, quote=quote, mid=self.mid, as_of=self.as_of or utcnow())


async def deposit(db: Database, user: User, amount: int, asset: str = "USD") -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user.id, asset)
        await fund(session, wallet.available_account_id, amount, asset)


async def available(db: Database, user: User, asset: str = "USD") -> int:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user.id, asset)
        return await ledger.get_balance(session, wallet.available_account_id)


async def position(db: Database, asset: str) -> int:
    """Corridor's own position in an asset. Zero if nothing was ever converted in it."""
    async with db.transaction() as session:
        account = await ledger.find_account(session, AccountKind.FX_POSITION, asset)
        return 0 if account is None else await ledger.get_balance(session, account.id)


async def count(db: Database, table: str) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


async def rows(db: Database, statement: str, **parameters: Any) -> list[dict[str, Any]]:
    async with db.transaction() as session:
        found = await session.execute(text(statement), parameters)
        return [dict(row) for row in found.mappings()]


async def quote_status(db: Database, quote_id: uuid.UUID) -> str:
    (row,) = await rows(db, "SELECT status FROM fx_quotes WHERE id = :id", id=quote_id)
    return str(row["status"])


async def quote_for(
    db: Database,
    settings: Settings,
    user: User,
    sell_amount: int = 100_00,
    *,
    sell_asset: str = "USD",
    buy_asset: str = "MXN",
    mid: Decimal | str = USD_MXN,
    principal: Principal | None = None,
) -> Quote:
    """One quote in a transaction of its own, as the HTTP handler stores one."""
    async with db.transaction() as session:
        return await fx.create_quote(
            session,
            principal or acting_as(user),
            sell_asset=sell_asset,
            buy_asset=buy_asset,
            sell_amount=sell_amount,
            rate=rate_of(mid, sell_asset, buy_asset),
            settings=settings,
        )


async def convert(
    db: Database,
    user: User,
    quote: Quote | uuid.UUID,
    *,
    principal: Principal | None = None,
    conversion_id: uuid.UUID | None = None,
) -> Conversion:
    """One conversion in a transaction of its own, as the HTTP handler runs one."""
    async with db.transaction() as session:
        return await fx.convert(
            session,
            principal or acting_as(user),
            quote_id=quote.id if isinstance(quote, Quote) else quote,
            conversion_id=conversion_id or new_id(),
        )

"""Quotes: the two amounts of a conversion, worked out once and stored."""

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import RowMapping, Table, insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import identity
from corridor.fx import rounding
from corridor.fx.errors import SameAsset
from corridor.fx.models import QuoteRow
from corridor.fx.types import Quote
from corridor.identity import Principal, Scope
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.ids import new_id
from corridor.platform.money import MAX_MINOR_UNITS, InvalidAmount, get_asset
from corridor.providers import Rate

# A Core table: every statement against it is written out below.
_quotes = cast(Table, QuoteRow.__table__)


def check_pair(sell_asset: str, buy_asset: str) -> None:
    """Refuse a pair that could never be quoted, before a rate is fetched for it."""
    get_asset(sell_asset)
    get_asset(buy_asset)
    if sell_asset == buy_asset:
        raise SameAsset


async def create_quote(
    session: AsyncSession,
    principal: Principal,
    *,
    sell_asset: str,
    buy_asset: str,
    sell_amount: int,
    rate: Rate,
    settings: Settings,
) -> Quote:
    """Offer the principal's user a price for ``sell_amount`` minor units of ``sell_asset``.

    ``rate`` is the mid rate from ``sell_asset`` to ``buy_asset``, fetched by the caller
    before its transaction began. Both amounts are fixed here; converting the quote moves
    them as they are.
    """
    identity.require_scope(principal, Scope.FX_READ)
    check_pair(sell_asset, buy_asset)
    if isinstance(sell_amount, bool) or not isinstance(sell_amount, int) or sell_amount <= 0:
        raise InvalidAmount("The amount must be greater than zero.")
    if sell_amount > MAX_MINOR_UNITS:
        raise InvalidAmount("The amount is too large.")
    if (rate.base, rate.quote) != (sell_asset, buy_asset):
        raise ValueError(f"a {rate.base}/{rate.quote} rate cannot price {sell_asset}/{buy_asset}")

    customer_rate = rounding.customer_rate(rate.mid, settings.fx_spread_bps)
    buy_amount = rounding.buy_amount(sell_amount, sell_asset, buy_asset, customer_rate)
    if buy_amount > MAX_MINOR_UNITS:
        raise InvalidAmount("The amount is too large.")

    now = utcnow()
    inserted = await session.execute(
        insert(_quotes)
        .values(
            id=new_id(),
            user_id=principal.user_id,
            sell_asset=sell_asset,
            buy_asset=buy_asset,
            sell_amount=sell_amount,
            buy_amount=buy_amount,
            rate=rounding.format_rate(customer_rate),
            mid=rounding.format_rate(rate.mid),
            status="open",
            expires_at=now + timedelta(seconds=settings.fx_quote_ttl_seconds),
            created_at=now,
        )
        .returning(_quotes)
    )
    return quote_of(inserted.mappings().one())


def quote_of(row: RowMapping) -> Quote:
    quote_id: uuid.UUID = row["id"]
    return Quote(
        id=quote_id,
        user_id=row["user_id"],
        sell_asset=row["sell_asset"],
        buy_asset=row["buy_asset"],
        sell_amount=row["sell_amount"],
        buy_amount=row["buy_amount"],
        rate=Decimal(row["rate"]),
        mid=Decimal(row["mid"]),
        status=row["status"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )

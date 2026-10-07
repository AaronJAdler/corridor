"""Conversions: a quote executed, the whole movement in the caller's one transaction.

Every function takes the caller's session and never commits. A conversion, its journal
entry, its outbox event and its audit event are committed together or not at all.
"""

import uuid
from datetime import datetime
from typing import Final, cast

from sqlalchemy import Table, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, ledger, outbox, risk, wallets
from corridor.fx.errors import (
    ConversionNotFound,
    DuplicateConversion,
    QuoteAlreadyUsed,
    QuoteExpired,
    QuoteNotFound,
)
from corridor.fx.models import ConversionRow, QuoteRow
from corridor.fx.quotes import quote_of
from corridor.fx.rounding import format_rate
from corridor.fx.types import Conversion, Quote
from corridor.identity import Principal, Scope
from corridor.ledger import AccountKind, EntryDraft, credit, debit
from corridor.platform.clock import utcnow
from corridor.platform.db import advisory_xact_lock, lock_key
from corridor.risk import MoneyMovement

# Core tables: every statement against them is written out below.
_quotes = cast(Table, QuoteRow.__table__)
_conversions = cast(Table, ConversionRow.__table__)

ENTRY_KIND: Final = "conversion"
SOURCE_TYPE: Final = "fx_conversion"
FX_CONVERTED: Final = "fx.converted"


async def convert(
    session: AsyncSession,
    principal: Principal,
    *,
    quote_id: uuid.UUID,
    conversion_id: uuid.UUID,
) -> Conversion:
    """Execute a quote of the principal's user: sell what it said, buy what it said.

    The caller makes ``conversion_id``, a new one for each attempt.

    The steps run in the order the locks have to be taken in: the user's money-out lock,
    then the quote, then, inside ``ledger.post_entry``, the balance rows. Nothing is worked
    out again here. The amounts are the quote's, so the customer gets what was quoted or a
    refusal, and every refusal comes before the first write.
    """
    identity.require_scope(principal, Scope.FX_CONVERT)
    user_id = principal.user_id

    await advisory_xact_lock(session, [lock_key(risk.MONEY_OUT_LOCK, user_id)])

    found = await session.execute(select(_quotes).where(_quotes.c.id == quote_id).with_for_update())
    row = found.mappings().one_or_none()
    # Another user's quote is answered exactly as one that does not exist, so that an id
    # cannot be probed.
    if row is None or row["user_id"] != user_id:
        raise QuoteNotFound
    quote = quote_of(row)
    if quote.status == "used":
        raise QuoteAlreadyUsed
    now = utcnow()
    if quote.expires_at <= now:
        raise QuoteExpired

    await risk.authorize(
        session,
        MoneyMovement(
            kind="conversion",
            user_id=user_id,
            principal=principal,
            asset=quote.sell_asset,
            amount=quote.sell_amount,
            movement_id=conversion_id,
        ),
    )

    sell_amount, buy_amount = quote.sell_amount, quote.buy_amount
    selling = await wallets.resolve(session, user_id, quote.sell_asset)
    buying = await wallets.resolve(session, user_id, quote.buy_asset)
    sell_position = await ledger.open_account(session, AccountKind.FX_POSITION, quote.sell_asset)
    buy_position = await ledger.open_account(session, AccountKind.FX_POSITION, quote.buy_asset)
    entry = await ledger.post_entry(
        session,
        EntryDraft(
            kind=ENTRY_KIND,
            source_type=SOURCE_TYPE,
            source_id=str(conversion_id),
            postings=(
                debit(selling.available_account_id, sell_amount),
                credit(sell_position.id, sell_amount),
                debit(buy_position.id, buy_amount),
                credit(buying.available_account_id, buy_amount),
            ),
            metadata={"quote_id": str(quote.id)},
        ),
    )
    if not entry.created:
        # The ledger found this id already posted and moved nothing. Going on would spend
        # the quote without paying for it.
        raise DuplicateConversion(f"conversion {conversion_id} already exists")

    await session.execute(update(_quotes).where(_quotes.c.id == quote.id).values(status="used"))
    await session.execute(
        insert(_conversions).values(
            id=conversion_id,
            quote_id=quote.id,
            user_id=user_id,
            entry_id=entry.id,
            created_at=now,
        )
    )
    conversion = _conversion(conversion_id, entry.id, quote, now)

    rate = format_rate(conversion.rate)
    await outbox.enqueue(
        session,
        FX_CONVERTED,
        {
            "conversion_id": str(conversion.id),
            "quote_id": str(conversion.quote_id),
            "user_id": str(conversion.user_id),
            "entry_id": str(conversion.entry_id),
            "sell_asset": conversion.sell_asset,
            # Minor units as a string: a JSON number would lose precision above 2^53.
            "sell_amount": str(conversion.sell_amount),
            "buy_asset": conversion.buy_asset,
            "buy_amount": str(conversion.buy_amount),
            "rate": rate,
        },
    )
    await audit.record(
        session,
        actor=(
            audit.Actor.agent(principal.actor_id)
            if principal.is_agent
            else audit.Actor.user(principal.actor_id)
        ),
        action="fx.converted",
        principal_id=user_id,
        resource_type="fx_conversion",
        resource_id=conversion.id,
        details={
            "quote_id": str(conversion.quote_id),
            "sell_asset": conversion.sell_asset,
            "sell_amount": str(conversion.sell_amount),
            "buy_asset": conversion.buy_asset,
            "buy_amount": str(conversion.buy_amount),
            "rate": rate,
        },
    )
    return conversion


async def get_conversion(
    session: AsyncSession, principal: Principal, conversion_id: uuid.UUID
) -> Conversion:
    """A conversion of the principal's user.

    Anyone else's is answered exactly as one that does not exist, so that an id cannot be
    probed to learn whether a conversion was made.
    """
    identity.require_scope(principal, Scope.FX_READ)
    found = await session.execute(
        select(_conversions.c.entry_id, _conversions.c.created_at.label("converted_at"), _quotes)
        .join(_quotes, _quotes.c.id == _conversions.c.quote_id)
        .where(_conversions.c.id == conversion_id)
    )
    row = found.mappings().one_or_none()
    if row is None or row["user_id"] != principal.user_id:
        raise ConversionNotFound
    return _conversion(conversion_id, row["entry_id"], quote_of(row), row["converted_at"])


def _conversion(
    conversion_id: uuid.UUID, entry_id: uuid.UUID, quote: Quote, created_at: datetime
) -> Conversion:
    return Conversion(
        id=conversion_id,
        quote_id=quote.id,
        user_id=quote.user_id,
        entry_id=entry_id,
        sell_asset=quote.sell_asset,
        buy_asset=quote.buy_asset,
        sell_amount=quote.sell_amount,
        buy_amount=quote.buy_amount,
        rate=quote.rate,
        created_at=created_at,
    )

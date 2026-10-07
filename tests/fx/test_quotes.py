"""Quotes: a price and two amounts, fixed when the quote is made."""

from datetime import timedelta
from decimal import Decimal

import pytest

from corridor import fx
from corridor.fx import AmountTooSmall, SameAsset
from corridor.identity import InsufficientScope, Scope, User
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.money import InvalidAmount, UnknownAsset
from tests.fx.support import acting_as, agent_of, count, quote_for, rate_of, rows

pytestmark = pytest.mark.usefixtures("clock")


async def test_a_quote_fixes_both_amounts_at_the_mid_less_the_spread(
    db: Database, settings: Settings, maria: User
) -> None:
    quote = await quote_for(db, settings, maria, 100_00, mid="17.25")

    # 50 basis points off 17.25 is 17.16375, and 100.00 USD at that is 1716.375 MXN.
    assert (quote.sell_asset, quote.sell_amount) == ("USD", 100_00)
    assert (quote.buy_asset, quote.buy_amount) == ("MXN", 1_716_37)
    assert (quote.rate, quote.mid) == (Decimal("17.16375"), Decimal("17.25"))
    assert (quote.user_id, quote.status) == (maria.id, "open")
    assert type(quote.sell_amount) is int
    assert type(quote.buy_amount) is int
    assert type(quote.rate) is Decimal


async def test_a_quote_expires_after_the_configured_seconds(
    db: Database, settings: Settings, maria: User
) -> None:
    quote = await quote_for(db, settings, maria)

    assert settings.fx_quote_ttl_seconds == 30
    assert quote.created_at == utcnow()
    assert quote.expires_at == utcnow() + timedelta(seconds=30)


async def test_a_quote_is_stored_as_it_was_returned(
    db: Database, settings: Settings, maria: User
) -> None:
    quote = await quote_for(db, settings, maria, 100_00, mid="17.25")

    (row,) = await rows(db, "SELECT * FROM fx_quotes")

    assert row == {
        "id": quote.id,
        "user_id": maria.id,
        "sell_asset": "USD",
        "buy_asset": "MXN",
        "sell_amount": Decimal(100_00),
        "buy_amount": Decimal(1_716_37),
        "rate": "17.16375",
        "mid": "17.25",
        "status": "open",
        "expires_at": quote.expires_at,
        "created_at": quote.created_at,
    }


async def test_the_spread_comes_from_the_settings(
    db: Database, settings: Settings, maria: User
) -> None:
    free = settings.model_copy(update={"fx_spread_bps": 0})

    quote = await quote_for(db, free, maria, 100_00, mid="17.25")

    assert (quote.rate, quote.buy_amount) == (Decimal("17.25"), 1_725_00)


async def test_a_quote_moves_no_money(db: Database, settings: Settings, maria: User) -> None:
    await quote_for(db, settings, maria)

    assert await count(db, "journal_entries") == 0
    assert await count(db, "outbox_events") == 0


async def test_a_quote_from_an_asset_to_itself_is_refused(
    db: Database, settings: Settings, maria: User
) -> None:
    with pytest.raises(SameAsset) as refusal:
        async with db.transaction() as session:
            await fx.create_quote(
                session,
                acting_as(maria),
                sell_asset="USD",
                buy_asset="USD",
                sell_amount=100_00,
                rate=rate_of("1", "USD", "USD"),
                settings=settings,
            )

    assert (refusal.value.status, refusal.value.code) == (422, "same_asset")
    assert await count(db, "fx_quotes") == 0


async def test_an_amount_too_small_to_buy_anything_is_refused(
    db: Database, settings: Settings, maria: User
) -> None:
    with pytest.raises(AmountTooSmall):
        await quote_for(db, settings, maria, 1, sell_asset="MXN", buy_asset="USD", mid="0.057971")

    assert await count(db, "fx_quotes") == 0


@pytest.mark.parametrize("amount", [0, -1])
async def test_an_amount_that_is_not_positive_is_refused(
    db: Database, settings: Settings, maria: User, amount: int
) -> None:
    with pytest.raises(InvalidAmount):
        await quote_for(db, settings, maria, amount)


async def test_an_amount_whose_proceeds_cannot_be_stored_is_refused(
    db: Database, settings: Settings, maria: User
) -> None:
    with pytest.raises(InvalidAmount):
        await quote_for(db, settings, maria, 10**38 - 1, mid="17.25")


@pytest.mark.parametrize(("sell", "buy"), [("EUR", "USD"), ("USD", "EUR")])
async def test_an_unknown_asset_is_refused(
    db: Database, settings: Settings, maria: User, sell: str, buy: str
) -> None:
    with pytest.raises(UnknownAsset):
        await quote_for(db, settings, maria, sell_asset=sell, buy_asset=buy)


async def test_a_rate_for_another_pair_is_a_broken_contract(
    db: Database, settings: Settings, maria: User
) -> None:
    with pytest.raises(ValueError, match="rate"):
        async with db.transaction() as session:
            await fx.create_quote(
                session,
                acting_as(maria),
                sell_asset="USD",
                buy_asset="MXN",
                sell_amount=100_00,
                rate=rate_of("0.057971", "MXN", "USD"),
                settings=settings,
            )


async def test_an_agent_needs_the_fx_read_scope_to_quote(
    db: Database, settings: Settings, maria: User
) -> None:
    with pytest.raises(InsufficientScope):
        await quote_for(db, settings, maria, principal=agent_of(maria, Scope.WALLET_READ))

    quote = await quote_for(db, settings, maria, principal=agent_of(maria, Scope.FX_READ))
    assert quote.user_id == maria.id

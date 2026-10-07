"""Conversions: a quote executed once, for exactly the amounts it promised."""

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor import fx, identity
from corridor.fx import (
    ConversionNotFound,
    DuplicateConversion,
    QuoteAlreadyUsed,
    QuoteExpired,
    QuoteNotFound,
)
from corridor.identity import InsufficientScope, Scope, User
from corridor.ledger import InsufficientFunds
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.config import Settings
from corridor.platform.db import LOCK_NOT_AVAILABLE, Database, lock_key, sqlstate_of
from corridor.platform.ids import new_id
from corridor.risk import MONEY_OUT_LOCK, UserRestricted
from tests.fx.support import (
    acting_as,
    agent_of,
    available,
    convert,
    count,
    deposit,
    position,
    quote_for,
    quote_status,
    rows,
)

pytestmark = pytest.mark.usefixtures("clock")


async def nothing_was_written(db: Database) -> bool:
    return [
        await count(db, table) for table in ("fx_conversions", "outbox_events", "audit_events")
    ] == [0, 0, 0] and await count(db, "journal_entries") == 1  # the deposit


async def postings_of(db: Database, entry_id: Any) -> list[tuple[str, str, str, int]]:
    found = await rows(
        db,
        "SELECT a.kind, p.asset_code, p.direction, p.amount FROM postings p"
        " JOIN ledger_accounts a ON a.id = p.account_id"
        " WHERE p.entry_id = :entry ORDER BY p.seq",
        entry=entry_id,
    )
    return [(p["kind"], p["asset_code"], p["direction"], int(p["amount"])) for p in found]


async def test_a_conversion_moves_exactly_the_quoted_amounts(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria, 100_00, mid="17.25")

    conversion = await convert(db, maria, quote)

    assert (conversion.quote_id, conversion.user_id) == (quote.id, maria.id)
    assert (conversion.sell_asset, conversion.sell_amount) == ("USD", 100_00)
    assert (conversion.buy_asset, conversion.buy_amount) == ("MXN", 1_716_37)
    assert conversion.rate == Decimal("17.16375")
    assert conversion.created_at == utcnow()
    assert await available(db, maria, "USD") == 150_00
    assert await available(db, maria, "MXN") == 1_716_37


async def test_a_conversion_is_one_entry_that_balances_in_each_asset(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria, 100_00, mid="17.25")

    conversion = await convert(db, maria, quote)

    (entry,) = await rows(
        db,
        "SELECT id, kind FROM journal_entries WHERE source_type = 'fx_conversion'"
        " AND source_id = :id",
        id=str(conversion.id),
    )
    assert (entry["id"], entry["kind"]) == (conversion.entry_id, "conversion")
    assert await postings_of(db, entry["id"]) == [
        ("user_available", "USD", "D", 100_00),
        ("fx_position", "USD", "C", 100_00),
        ("fx_position", "MXN", "D", 1_716_37),
        ("user_available", "MXN", "C", 1_716_37),
    ]
    # Corridor is now short what it paid out and long what it took in.
    assert await position(db, "USD") == -100_00
    assert await position(db, "MXN") == 1_716_37


async def test_a_conversion_marks_its_quote_used_and_is_recorded_once(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria)

    conversion = await convert(db, maria, quote)

    assert await quote_status(db, quote.id) == "used"
    (row,) = await rows(db, "SELECT * FROM fx_conversions")
    assert row == {
        "id": conversion.id,
        "quote_id": quote.id,
        "user_id": maria.id,
        "entry_id": conversion.entry_id,
        "created_at": conversion.created_at,
    }


async def test_a_conversion_announces_itself_and_is_audited(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria, 100_00, mid="17.25")

    conversion = await convert(db, maria, quote)

    (event,) = await rows(db, "SELECT topic, payload FROM outbox_events")
    assert event["topic"] == "fx.converted"
    assert event["payload"] == {
        "conversion_id": str(conversion.id),
        "quote_id": str(quote.id),
        "user_id": str(maria.id),
        "entry_id": str(conversion.entry_id),
        "sell_asset": "USD",
        "sell_amount": "10000",
        "buy_asset": "MXN",
        "buy_amount": "171637",
        "rate": "17.16375",
    }
    (audited,) = await rows(
        db, "SELECT action, actor_type, principal_id, resource_type, resource_id FROM audit_events"
    )
    assert audited == {
        "action": "fx.converted",
        "actor_type": "user",
        "principal_id": maria.id,
        "resource_type": "fx_conversion",
        "resource_id": str(conversion.id),
    }


async def test_what_was_quoted_is_what_is_paid_whatever_the_spread_is_by_then(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria, 100_00, mid="17.25")
    assert quote.buy_amount != 100 * 17_25

    conversion = await convert(db, maria, quote)

    assert conversion.buy_amount == quote.buy_amount == 1_716_37
    assert await available(db, maria, "MXN") == quote.buy_amount


async def test_an_expired_quote_is_refused(
    db: Database, settings: Settings, maria: User, clock: ManualClock
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria)
    clock.advance(seconds=31)

    with pytest.raises(QuoteExpired) as refusal:
        await convert(db, maria, quote)

    assert (refusal.value.status, refusal.value.code) == (409, "quote_expired")
    assert await quote_status(db, quote.id) == "open"
    assert await available(db, maria, "USD") == 250_00
    assert await nothing_was_written(db)


async def test_a_quote_is_good_until_the_instant_it_expires(
    db: Database, settings: Settings, maria: User, clock: ManualClock
) -> None:
    await deposit(db, maria, 250_00)
    early = await quote_for(db, settings, maria, 10_00)
    late = await quote_for(db, settings, maria, 10_00)

    clock.advance(seconds=29.999)
    await convert(db, maria, early)
    clock.advance(seconds=0.001)
    with pytest.raises(QuoteExpired):
        await convert(db, maria, late)


async def test_a_quote_converts_once(db: Database, settings: Settings, maria: User) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria, 100_00)
    await convert(db, maria, quote)

    with pytest.raises(QuoteAlreadyUsed) as refusal:
        await convert(db, maria, quote)

    assert (refusal.value.status, refusal.value.code) == (409, "quote_already_used")
    assert await available(db, maria, "USD") == 150_00
    assert await count(db, "fx_conversions") == 1
    assert await count(db, "outbox_events") == 1


async def test_another_users_quote_is_answered_exactly_as_one_that_does_not_exist(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, joao, 250_00)
    quote = await quote_for(db, settings, maria)

    with pytest.raises(QuoteNotFound) as theirs:
        await convert(db, joao, quote)
    with pytest.raises(QuoteNotFound) as missing:
        await convert(db, joao, new_id())

    assert (theirs.value.status, theirs.value.code) == (404, "quote_not_found")
    assert (theirs.value.code, theirs.value.title, theirs.value.detail, theirs.value.extra) == (
        missing.value.code,
        missing.value.title,
        missing.value.detail,
        missing.value.extra,
    )
    assert await quote_status(db, quote.id) == "open"
    assert await available(db, joao, "USD") == 250_00
    assert await nothing_was_written(db)


async def test_a_conversion_the_balance_cannot_cover_writes_nothing_and_leaves_the_quote_open(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 99_99)
    quote = await quote_for(db, settings, maria, 100_00)

    with pytest.raises(InsufficientFunds):
        await convert(db, maria, quote)

    assert await quote_status(db, quote.id) == "open"
    assert await available(db, maria, "USD") == 99_99
    assert await available(db, maria, "MXN") == 0
    assert await nothing_was_written(db)
    # The quote is still good once the money is there.
    await deposit(db, maria, 1)
    await convert(db, maria, quote)
    assert await available(db, maria, "USD") == 0


async def test_a_restricted_user_cannot_convert(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria)
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "review")

    with pytest.raises(UserRestricted) as refusal:
        await convert(db, maria, quote)

    assert (refusal.value.status, refusal.value.code) == (403, "user_restricted")
    assert await quote_status(db, quote.id) == "open"
    assert await available(db, maria, "USD") == 250_00
    assert await nothing_was_written(db)


async def test_an_agent_needs_the_fx_convert_scope(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria)

    with pytest.raises(InsufficientScope):
        await convert(db, maria, quote, principal=agent_of(maria, Scope.FX_READ))
    assert await nothing_was_written(db)

    agent = agent_of(maria, Scope.FX_CONVERT)
    conversion = await convert(db, maria, quote, principal=agent)
    (audited,) = await rows(db, "SELECT actor_type, actor_id, principal_id FROM audit_events")
    assert conversion.user_id == maria.id
    assert audited == {
        "actor_type": "agent",
        "actor_id": str(agent.actor_id),
        "principal_id": maria.id,
    }


async def test_a_conversion_id_used_twice_is_a_bug_in_the_caller(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    first = await quote_for(db, settings, maria, 10_00)
    second = await quote_for(db, settings, maria, 10_00)
    conversion = await convert(db, maria, first)

    with pytest.raises(DuplicateConversion):
        await convert(db, maria, second, conversion_id=conversion.id)

    assert await quote_status(db, second.id) == "open"
    assert await available(db, maria, "USD") == 240_00


async def test_20_conversions_of_one_quote_at_once_give_exactly_one(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 5_000_00)
    quote = await quote_for(db, settings, maria, 100_00, mid="17.25")

    async def attempt() -> str:
        try:
            await db.run(
                lambda session: fx.convert(
                    session, acting_as(maria), quote_id=quote.id, conversion_id=new_id()
                )
            )
        except QuoteAlreadyUsed:
            return "used"
        return "converted"

    outcomes = await asyncio.gather(*(attempt() for _ in range(20)))

    assert sorted(outcomes) == ["converted"] + ["used"] * 19
    assert await available(db, maria, "USD") == 4_900_00
    assert await available(db, maria, "MXN") == 1_716_37
    assert await count(db, "fx_conversions") == 1
    assert await count(db, "outbox_events") == 1


async def test_a_conversion_waits_for_whoever_holds_its_quote(
    db: Database, impatient_db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria)

    async with db.transaction() as holder:
        # The weakest row lock there is. Marking the quote used would not wait for it;
        # only taking the row for update does, and that comes before anything is decided.
        await holder.execute(
            text("SELECT 1 FROM fx_quotes WHERE id = :id FOR KEY SHARE"), {"id": quote.id}
        )
        with pytest.raises(DBAPIError) as failure:
            await convert(impatient_db, maria, quote)

    assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
    assert await quote_status(db, quote.id) == "open"
    await convert(impatient_db, maria, quote)
    assert await quote_status(db, quote.id) == "used"


async def test_a_conversion_queues_behind_the_users_other_outgoing_money(
    db: Database, impatient_db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria)

    async with db.transaction() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": lock_key(MONEY_OUT_LOCK, maria.id)},
        )
        with pytest.raises(DBAPIError) as failure:
            await convert(impatient_db, maria, quote)

    assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
    assert await quote_status(db, quote.id) == "open"


async def test_the_owner_reads_their_conversion(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    conversion = await convert(db, maria, await quote_for(db, settings, maria))

    async with db.transaction() as session:
        found = await fx.get_conversion(session, acting_as(maria), conversion.id)

    assert found == conversion


async def test_nobody_else_can_tell_a_conversion_exists(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 250_00)
    conversion = await convert(db, maria, await quote_for(db, settings, maria))

    async with db.transaction() as session:
        with pytest.raises(ConversionNotFound) as theirs:
            await fx.get_conversion(session, acting_as(joao), conversion.id)
        with pytest.raises(ConversionNotFound) as missing:
            await fx.get_conversion(session, acting_as(joao), new_id())

    assert (theirs.value.status, theirs.value.code) == (404, "conversion_not_found")
    assert (theirs.value.detail, theirs.value.extra) == (missing.value.detail, missing.value.extra)


async def test_reading_a_conversion_needs_the_fx_read_scope(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    conversion = await convert(db, maria, await quote_for(db, settings, maria))

    async with db.transaction() as session:
        with pytest.raises(InsufficientScope):
            await fx.get_conversion(session, agent_of(maria, Scope.FX_CONVERT), conversion.id)


async def test_a_conversion_is_counted_against_the_limits_under_its_own_id(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 250_00)
    quote = await quote_for(db, settings, maria, 100_00, mid="17.25")

    conversion = await convert(db, maria, quote)

    (usage,) = await rows(db, "SELECT kind, movement_id, usd_value FROM risk_usage")
    assert (usage["kind"], usage["movement_id"], usage["usd_value"]) == (
        "conversion",
        conversion.id,
        100_00,
    )

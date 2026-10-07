"""Limits: how much a user, or an agent acting for one, may move at once and in a day.

The seeded defaults for a new user, who is in tier 0, are 1,000.00 USD for one movement
and 2,500.00 USD in 24 hours.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import identity, payments, risk
from corridor.identity import User
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.money import MAX_MINOR_UNITS, InvalidAmount, UnknownAsset
from corridor.risk import LimitExceeded
from tests.payments.support import acting_as, count, deposit
from tests.risk.support import agent_of, authorize, movement, usage, used

PER_TX = 1_000_00
DAILY = 2_500_00


async def limit(db: Database, **rule: object) -> None:
    async with db.transaction() as session:
        await risk.set_limit(session, **rule)  # type: ignore[arg-type]


# --- one movement ----------------------------------------------------------------------------


async def test_a_movement_within_the_limits_is_allowed_and_recorded(
    db: Database, maria: User, clock: ManualClock
) -> None:
    asked = movement(maria, 250_00)

    decision = await authorize(db, asked)

    assert decision == risk.Decision(outcome="allow")
    (row,) = await usage(db, maria)
    assert (row["kind"], row["asset"], row["amount"], row["usd_value"]) == (
        "transfer",
        "USD",
        250_00,
        250_00,
    )
    assert (row["movement_id"], row["agent_id"], row["created_at"]) == (
        asked.movement_id,
        None,
        clock.now(),
    )


async def test_a_movement_of_exactly_the_per_transaction_limit_is_allowed(
    db: Database, maria: User
) -> None:
    await authorize(db, movement(maria, PER_TX))

    assert await used(db, maria) == PER_TX


async def test_a_movement_over_the_per_transaction_limit_is_refused_and_not_recorded(
    db: Database, maria: User
) -> None:
    with pytest.raises(LimitExceeded) as refusal:
        await authorize(db, movement(maria, PER_TX + 1))

    assert isinstance(refusal.value, risk.Denied)
    assert (refusal.value.status, refusal.value.code) == (422, "limit_exceeded")
    assert refusal.value.extra == {"limit": "per_transaction", "scope": "account"}
    assert refusal.value.detail == "This is more than the limit of 1000.00 USD for one movement."
    assert await usage(db, maria) == []


async def test_movements_that_together_reach_the_daily_limit_are_allowed(
    db: Database, maria: User
) -> None:
    for amount in (1_000_00, 1_000_00, 500_00):
        await authorize(db, movement(maria, amount))

    assert await used(db, maria) == DAILY


async def test_a_movement_that_would_pass_the_daily_limit_is_refused_and_not_recorded(
    db: Database, maria: User
) -> None:
    for amount in (1_000_00, 1_000_00, 499_99):
        await authorize(db, movement(maria, amount))

    with pytest.raises(LimitExceeded) as refusal:
        await authorize(db, movement(maria, 2))

    assert refusal.value.extra == {"limit": "daily", "scope": "account"}
    assert refusal.value.detail == (
        "This would take the total past the limit of 2500.00 USD in 24 hours."
    )
    assert await used(db, maria) == DAILY - 1


async def test_what_another_user_moved_does_not_count(
    db: Database, maria: User, joao: User
) -> None:
    for _ in range(2):
        await authorize(db, movement(joao, PER_TX))
    await authorize(db, movement(joao, 500_00))

    await authorize(db, movement(maria, PER_TX))

    assert await used(db, maria) == PER_TX


async def test_every_kind_of_movement_counts_towards_the_daily_limit(
    db: Database, maria: User
) -> None:
    await authorize(db, movement(maria, 1_000_00, kind="transfer"))
    await authorize(db, movement(maria, 1_000_00, kind="withdrawal"))
    await authorize(db, movement(maria, 500_00, kind="conversion"))

    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, 1, kind="transfer"))


# --- the rolling window ----------------------------------------------------------------------


async def test_a_movement_still_counts_just_inside_24_hours(
    db: Database, maria: User, clock: ManualClock
) -> None:
    for amount in (1_000_00, 1_000_00, 500_00):
        await authorize(db, movement(maria, amount))
    clock.advance(hours=23, minutes=59, seconds=59)

    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, 1))


async def test_a_movement_stops_counting_24_hours_after_it_was_made(
    db: Database, maria: User, clock: ManualClock
) -> None:
    await authorize(db, movement(maria, 1_000_00))
    clock.advance(hours=1)
    await authorize(db, movement(maria, 1_000_00))
    await authorize(db, movement(maria, 500_00))
    clock.advance(hours=23)

    # The first has left the window, and what it used is free again: no more than that.
    await authorize(db, movement(maria, 1_000_00))
    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, 1))


# --- valuation -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("asset", "amount", "cents"),
    [
        ("USD", 1, 1),
        ("USD", 123_45, 123_45),
        ("USDC", 1_000_000, 1_00),
        ("USDC", 1, 1),
        ("USDC", 10_001, 2),
        ("MXN", 100_00, 5_80),
        ("MXN", 1, 1),
        ("MXN", 1_01, 6),
        ("MXN", 1_000_00, 58_00),
        ("BRL", 100_00, 18_00),
    ],
)
async def test_usage_is_valued_in_us_cents_and_rounded_up(
    db: Database, maria: User, asset: str, amount: int, cents: int
) -> None:
    await authorize(db, movement(maria, amount, asset))

    (row,) = await usage(db, maria)
    assert (row["asset"], row["amount"], row["usd_value"]) == (asset, amount, cents)


async def test_the_per_transaction_limit_is_applied_to_the_value_in_usd(
    db: Database, maria: User
) -> None:
    # 17,241.37 MXN is worth 999.99946 USD, which rounds up to exactly the limit.
    await authorize(db, movement(maria, 17_241_37, "MXN"))

    # One centavo more is worth 1000.00004 USD: past the limit once rounded up.
    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, 17_241_38, "MXN"))


async def test_movements_in_different_assets_count_towards_one_daily_limit(
    db: Database, maria: User
) -> None:
    await authorize(db, movement(maria, 1_000_00, "USD"))
    await authorize(db, movement(maria, 1_000_000_000, "USDC"))
    await authorize(db, movement(maria, 8_620_00, "MXN"))  # 499.96 USD
    assert await used(db, maria) == DAILY - 4

    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, 5_000_000, "USDC"))


async def test_an_asset_with_no_reference_rate_cannot_be_moved(
    db: Database, owner_db: Database, maria: User
) -> None:
    async with owner_db.transaction() as session:
        await session.execute(text("DELETE FROM risk_reference_rates WHERE asset = 'BRL'"))

    with pytest.raises(LookupError, match="BRL"):
        await authorize(db, movement(maria, 1_00, "BRL"))

    assert await usage(db, maria) == []


async def test_an_asset_corridor_does_not_have_is_refused_as_the_clients_mistake(
    db: Database, maria: User
) -> None:
    with pytest.raises(UnknownAsset):
        await authorize(db, movement(maria, 1_00, "EUR"))


async def test_an_amount_too_large_to_record_is_refused_as_an_amount_not_as_a_limit(
    db: Database, maria: User
) -> None:
    await limit(db, scope="user", user_id=maria.id, per_tx_usd=None, daily_usd=None)

    # In pesos its value in cents could be recorded. The amount itself could not.
    with pytest.raises(InvalidAmount) as refusal:
        await authorize(db, movement(maria, MAX_MINOR_UNITS + 1, "MXN"))

    assert refusal.value.detail == "The amount is too large."
    assert await usage(db, maria) == []


async def test_the_largest_amount_there_is_can_be_valued_and_recorded(
    db: Database, maria: User
) -> None:
    await limit(db, scope="user", user_id=maria.id, per_tx_usd=None, daily_usd=None)

    await authorize(db, movement(maria, MAX_MINOR_UNITS))

    assert await used(db, maria) == MAX_MINOR_UNITS


async def test_a_value_too_large_to_record_is_refused_as_an_amount(
    db: Database, owner_db: Database, maria: User
) -> None:
    await limit(db, scope="user", user_id=maria.id, per_tx_usd=None, daily_usd=None)
    async with owner_db.transaction() as session:
        await session.execute(
            text("UPDATE risk_reference_rates SET usd_per_unit = 2 WHERE asset = 'USD'")
        )

    # Storable as an amount, and worth twice as many cents as can be stored.
    with pytest.raises(InvalidAmount):
        await authorize(db, movement(maria, MAX_MINOR_UNITS))

    assert await usage(db, maria) == []


# --- which rule applies ----------------------------------------------------------------------


@pytest.mark.parametrize(("tier", "per_tx"), [(1, 10_000_00), (2, 100_000_00)])
async def test_a_higher_tier_has_higher_limits(
    db: Database, maria: User, tier: int, per_tx: int
) -> None:
    async with db.transaction() as session:
        await identity.set_kyc_tier(session, maria.id, tier)

    await authorize(db, movement(maria, per_tx))
    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, per_tx + 1))


async def test_a_users_own_rule_wins_over_the_tiers_when_it_is_lower(
    db: Database, maria: User, joao: User
) -> None:
    await limit(db, scope="user", user_id=maria.id, per_tx_usd=50_00, daily_usd=80_00)

    with pytest.raises(LimitExceeded) as refusal:
        await authorize(db, movement(maria, 50_01))
    assert refusal.value.detail == "This is more than the limit of 50.00 USD for one movement."
    await authorize(db, movement(maria, 50_00))
    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, 30_01))
    # Nobody else is bound by it.
    await authorize(db, movement(joao, PER_TX))


async def test_a_users_own_rule_wins_over_the_tiers_when_it_is_higher(
    db: Database, maria: User
) -> None:
    await limit(db, scope="user", user_id=maria.id, per_tx_usd=5_000_00, daily_usd=9_000_00)

    await authorize(db, movement(maria, 5_000_00))
    await authorize(db, movement(maria, 4_000_00))
    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, 1))


async def test_a_rule_for_one_kind_wins_over_a_rule_for_every_kind_in_the_same_scope(
    db: Database, maria: User
) -> None:
    await limit(db, scope="user", user_id=maria.id, per_tx_usd=500_00, daily_usd=500_00)
    await limit(
        db, scope="user", user_id=maria.id, kind="withdrawal", per_tx_usd=20_00, daily_usd=30_00
    )

    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, 20_01, kind="withdrawal"))
    await authorize(db, movement(maria, 20_00, kind="withdrawal"))
    # Other kinds are still under the rule for every kind.
    await authorize(db, movement(maria, 400_00, kind="transfer"))


async def test_a_rule_for_one_kind_counts_only_movements_of_that_kind(
    db: Database, maria: User
) -> None:
    await limit(
        db, scope="user", user_id=maria.id, kind="withdrawal", per_tx_usd=20_00, daily_usd=30_00
    )
    await authorize(db, movement(maria, 900_00, kind="transfer"))

    await authorize(db, movement(maria, 20_00, kind="withdrawal"))
    await authorize(db, movement(maria, 10_00, kind="withdrawal"))
    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, 1, kind="withdrawal"))


async def test_a_users_rule_for_every_kind_wins_over_the_tiers_rule_for_one_kind(
    db: Database, maria: User
) -> None:
    await limit(db, scope="tier", tier=0, kind="withdrawal", per_tx_usd=0, daily_usd=0)
    with pytest.raises(LimitExceeded):
        await authorize(db, movement(maria, 1, kind="withdrawal"))

    await limit(db, scope="user", user_id=maria.id, per_tx_usd=75_00, daily_usd=75_00)

    await authorize(db, movement(maria, 75_00, kind="withdrawal"))


async def test_a_rule_with_no_limit_of_one_sort_does_not_limit_it(
    db: Database, maria: User
) -> None:
    await limit(db, scope="user", user_id=maria.id, per_tx_usd=None, daily_usd=3_000_00)

    await authorize(db, movement(maria, 2_999_00))
    with pytest.raises(LimitExceeded) as refusal:
        await authorize(db, movement(maria, 1_01))
    assert refusal.value.extra["limit"] == "daily"


async def test_setting_a_rule_again_replaces_it(db: Database, maria: User) -> None:
    await limit(db, scope="user", user_id=maria.id, per_tx_usd=10_00, daily_usd=10_00)
    await limit(db, scope="user", user_id=maria.id, per_tx_usd=20_00, daily_usd=20_00)

    await authorize(db, movement(maria, 20_00))
    assert await count(db, "risk_limits") == 4


async def test_a_user_with_no_rule_at_all_moves_nothing(db: Database, maria: User) -> None:
    async with db.transaction() as session:
        await session.execute(text("DELETE FROM risk_limits"))

    with pytest.raises(LookupError):
        await authorize(db, movement(maria, 1))

    assert await usage(db, maria) == []


@pytest.mark.parametrize(
    "rule",
    [
        {"scope": "tier"},
        {"scope": "tier", "tier": 0, "user_id": uuid.UUID(int=1)},
        {"scope": "user", "tier": 1},
        {"scope": "agent", "user_id": uuid.UUID(int=1)},
        {"scope": "user", "user_id": uuid.UUID(int=1), "per_tx_usd": -1},
        {"scope": "user", "user_id": uuid.UUID(int=1), "daily_usd": True},
    ],
)
async def test_a_rule_that_does_not_name_its_subject_or_has_a_bad_amount_is_not_stored(
    db: Database, rule: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match=r"^a (rule of scope|limit is a whole number)"):
        await limit(db, **{"per_tx_usd": 1, "daily_usd": 1, **rule})

    assert await count(db, "risk_limits") == 3


# --- agents ----------------------------------------------------------------------------------


async def test_an_agents_rule_limits_what_that_agent_moves(db: Database, maria: User) -> None:
    agent, other = new_id(), new_id()
    await limit(db, scope="agent", agent_id=agent, per_tx_usd=20_00, daily_usd=50_00)

    with pytest.raises(LimitExceeded) as refusal:
        await authorize(db, movement(maria, 20_01, principal=agent_of(maria, agent)))
    assert refusal.value.extra == {"limit": "per_transaction", "scope": "agent"}

    for amount in (20_00, 20_00, 10_00):
        await authorize(db, movement(maria, amount, principal=agent_of(maria, agent)))
    with pytest.raises(LimitExceeded) as refusal:
        await authorize(db, movement(maria, 1, principal=agent_of(maria, agent)))
    assert refusal.value.extra == {"limit": "daily", "scope": "agent"}

    # The user themselves, and any other agent of theirs, are not bound by it.
    await authorize(db, movement(maria, 100_00))
    await authorize(db, movement(maria, 100_00, principal=agent_of(maria, other)))
    (by_agent,) = [row for row in await usage(db, maria) if row["agent_id"] == other]
    assert by_agent["usd_value"] == 100_00


async def test_what_the_user_moved_themselves_does_not_use_up_the_agents_limit(
    db: Database, maria: User
) -> None:
    agent = new_id()
    await limit(db, scope="agent", agent_id=agent, per_tx_usd=50_00, daily_usd=50_00)
    await authorize(db, movement(maria, 900_00))

    await authorize(db, movement(maria, 50_00, principal=agent_of(maria, agent)))


async def test_an_agent_cannot_move_more_than_its_user_may_whatever_its_own_rule_says(
    db: Database, maria: User
) -> None:
    agent = new_id()
    await limit(db, scope="agent", agent_id=agent, per_tx_usd=9_000_00, daily_usd=9_000_00)

    with pytest.raises(LimitExceeded) as refusal:
        await authorize(db, movement(maria, PER_TX + 1, principal=agent_of(maria, agent)))
    assert refusal.value.extra == {"limit": "per_transaction", "scope": "account"}

    # What the agent moves counts towards the user's day as well as its own.
    for amount in (1_000_00, 1_000_00):
        await authorize(db, movement(maria, amount, principal=agent_of(maria, agent)))
    await authorize(db, movement(maria, 500_00))
    with pytest.raises(LimitExceeded) as refusal:
        await authorize(db, movement(maria, 1, principal=agent_of(maria, agent)))
    assert refusal.value.extra == {"limit": "daily", "scope": "account"}


# --- what is recorded, and when --------------------------------------------------------------


async def test_a_movement_that_is_rolled_back_leaves_no_usage(db: Database, maria: User) -> None:
    class Abandoned(Exception):
        pass

    with pytest.raises(Abandoned):
        async with db.transaction() as session:
            await risk.authorize(session, movement(maria, PER_TX))
            raise Abandoned

    assert await usage(db, maria) == []
    # So the whole day is still there to use.
    for amount in (1_000_00, 1_000_00, 500_00):
        await authorize(db, movement(maria, amount))


async def test_a_movement_authorised_twice_is_counted_once(db: Database, maria: User) -> None:
    asked = movement(maria, PER_TX)

    await authorize(db, asked)
    await authorize(db, asked)

    assert await used(db, maria) == PER_TX


async def test_a_movement_authorised_again_at_the_daily_limit_is_not_refused_for_itself(
    db: Database, maria: User
) -> None:
    await authorize(db, movement(maria, 1_000_00))
    await authorize(db, movement(maria, 1_000_00))
    last = movement(maria, 500_00)
    await authorize(db, last)

    await authorize(db, last)

    assert await used(db, maria) == DAILY


async def test_the_same_id_names_a_different_movement_of_another_kind(
    db: Database, maria: User
) -> None:
    shared = new_id()

    await authorize(db, movement(maria, 100_00, kind="transfer", movement_id=shared))
    await authorize(db, movement(maria, 100_00, kind="withdrawal", movement_id=shared))

    assert await used(db, maria) == 200_00


async def test_a_movement_with_no_id_is_still_counted(db: Database, maria: User) -> None:
    unnamed = risk.MoneyMovement(
        kind="transfer", user_id=maria.id, principal=acting_as(maria), asset="USD", amount=PER_TX
    )

    for _ in range(2):
        await authorize(db, unnamed)

    assert await used(db, maria) == 2 * PER_TX


async def test_a_restricted_user_is_refused_as_restricted_before_any_limit(
    db: Database, maria: User
) -> None:
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "review")

    with pytest.raises(risk.UserRestricted):
        await authorize(db, movement(maria, PER_TX + 1))


# --- through a real money path, under contention ----------------------------------------------

# Each is worth 200.00 USD or a little less: 20 of them are worth far more than a day allows.
MIXED = (("USD", 200_00), ("MXN", 3_448_00), ("USDC", 200_000_000))


def sending(
    settings: Settings, sender: User, recipient: User, amount: int, asset: str
) -> Callable[[AsyncSession], Awaitable[uuid.UUID | None]]:
    """A unit of work as the HTTP handler runs one: the id of the transfer, or None if a
    limit refused it."""

    async def work(session: AsyncSession) -> uuid.UUID | None:
        try:
            transfer = await payments.create_transfer(
                session,
                acting_as(sender),
                transfer_id=new_id(),
                recipient=str(recipient.id),
                asset=asset,
                amount=amount,
                memo=None,
                settings=settings,
            )
        except LimitExceeded:
            return None
        return transfer.id

    return work


async def test_20_concurrent_transfers_in_three_assets_cannot_together_pass_the_daily_limit(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_000_00, "USD")
    await deposit(db, maria, 100_000_00, "MXN")
    await deposit(db, maria, 10_000_000_000, "USDC")

    done = await asyncio.gather(
        *(db.run(sending(settings, maria, joao, *reversed(MIXED[n % 3]))) for n in range(20))
    )

    sent = [transfer_id for transfer_id in done if transfer_id is not None]
    total = await used(db, maria)
    assert total <= DAILY
    # The limit was what stopped the rest: not even the smallest of them still fits.
    assert total + 199_99 > DAILY
    assert len(sent) == await count(db, "transfers") == len(await usage(db, maria)) == 12

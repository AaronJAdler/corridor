"""What the database itself guarantees about risk's tables, whatever the application does."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor.platform.db import (
    CHECK_VIOLATION,
    UNIQUE_VIOLATION,
    Database,
    constraint_of,
    sqlstate_of,
)
from corridor.platform.ids import new_id
from corridor.platform.money import ASSETS

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
INSUFFICIENT_PRIVILEGE = "42501"

INSERT_LIMIT = text(
    "INSERT INTO risk_limits (id, scope, tier, user_id, agent_id, kind, per_tx_usd, daily_usd,"
    " created_at) VALUES (:id, :scope, :tier, :user_id, :agent_id, :kind, :per_tx_usd,"
    " :daily_usd, :now)"
)
INSERT_USAGE = text(
    "INSERT INTO risk_usage (id, user_id, agent_id, kind, asset, amount, usd_value, movement_id,"
    " created_at) VALUES (:id, :user_id, NULL, :kind, 'USD', :amount, :usd_value, :movement_id,"
    " :now)"
)
INSERT_LISTED = text(
    "INSERT INTO risk_denylist (id, kind, value_normalised, outcome, note, created_at)"
    " VALUES (:id, :kind, :value, :outcome, NULL, :now)"
)
INSERT_REVIEW = text(
    "INSERT INTO risk_reviews (id, subject_type, subject_id, user_id, outcome, status,"
    " created_at, resolved_at) VALUES (:id, :subject_type, :subject_id, NULL, :outcome,"
    " :status, :now, :resolved_at)"
)


async def add(db: Database, statement: Any, defaults: dict[str, Any], **overrides: Any) -> None:
    async with db.transaction() as session:
        await session.execute(statement, {"id": new_id(), "now": NOW, **defaults, **overrides})


async def add_limit(db: Database, **overrides: Any) -> None:
    defaults = {
        "scope": "user",
        "tier": None,
        "user_id": new_id(),
        "agent_id": None,
        "kind": None,
        "per_tx_usd": 1,
        "daily_usd": 1,
    }
    await add(db, INSERT_LIMIT, defaults, **overrides)


async def add_usage(db: Database, **overrides: Any) -> None:
    defaults = {
        "user_id": new_id(),
        "kind": "transfer",
        "amount": 1,
        "usd_value": 1,
        "movement_id": new_id(),
    }
    await add(db, INSERT_USAGE, defaults, **overrides)


async def add_listed(db: Database, **overrides: Any) -> None:
    await add(db, INSERT_LISTED, {"kind": "name", "value": "x", "outcome": "deny"}, **overrides)


async def add_review(db: Database, **overrides: Any) -> None:
    defaults = {
        "subject_type": "withdrawal",
        "subject_id": new_id(),
        "outcome": "review",
        "status": "open",
        "resolved_at": None,
    }
    await add(db, INSERT_REVIEW, defaults, **overrides)


def violated(error: pytest.ExceptionInfo[DBAPIError]) -> tuple[str | None, str | None]:
    return sqlstate_of(error.value), constraint_of(error.value)


# --- what the migration seeds ----------------------------------------------------------------


async def test_every_tier_has_a_default_rule_and_each_tier_allows_more_than_the_last(
    db: Database,
) -> None:
    async with db.transaction() as session:
        found = await session.execute(
            text(
                "SELECT tier, kind, per_tx_usd, daily_usd FROM risk_limits"
                " WHERE scope = 'tier' ORDER BY tier"
            )
        )
        rules = [tuple(row) for row in found]

    assert rules == [
        (0, None, 1_000_00, 2_500_00),
        (1, None, 10_000_00, 25_000_00),
        (2, None, 100_000_00, 250_000_00),
    ]


async def test_every_asset_has_a_reference_rate(db: Database) -> None:
    async with db.transaction() as session:
        found = await session.execute(text("SELECT asset FROM risk_reference_rates"))
        assert set(found.scalars()) == set(ASSETS)


# --- what the application role may not do ----------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE risk_usage SET usd_value = 0",
        "DELETE FROM risk_usage",
        "UPDATE risk_reference_rates SET usd_per_unit = 0.0001",
        "DELETE FROM risk_reference_rates",
        "INSERT INTO risk_reference_rates (asset, usd_per_unit, updated_at)"
        " VALUES ('EUR', 1, CURRENT_TIMESTAMP)",
        "DELETE FROM risk_reviews",
        "TRUNCATE risk_usage",
    ],
)
async def test_the_application_cannot_rewrite_usage_rates_or_reviews(
    db: Database, statement: str
) -> None:
    await add_usage(db)
    await add_review(db)

    with pytest.raises(DBAPIError) as refused:
        async with db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(refused.value) == INSUFFICIENT_PRIVILEGE


# --- limits ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        {"scope": "tier", "user_id": None},
        {"scope": "tier", "tier": 1},
        {"scope": "user", "user_id": None},
        {"scope": "user", "agent_id": new_id()},
        {"scope": "agent"},
        {"scope": "agent", "user_id": None, "agent_id": new_id(), "tier": 0},
    ],
)
async def test_a_rule_names_exactly_the_subject_its_scope_says(
    db: Database, rule: dict[str, Any]
) -> None:
    with pytest.raises(DBAPIError) as refused:
        await add_limit(db, **rule)

    assert violated(refused) == (CHECK_VIOLATION, "ck_risk_limits_subject")


@pytest.mark.parametrize(
    ("rule", "constraint"),
    [
        ({"scope": "everyone"}, "ck_risk_limits_scope"),
        ({"scope": "tier", "user_id": None, "tier": 3}, "ck_risk_limits_tier"),
        ({"kind": "deposit"}, "ck_risk_limits_kind"),
        ({"per_tx_usd": -1}, "ck_risk_limits_per_tx_usd"),
        ({"daily_usd": -1}, "ck_risk_limits_daily_usd"),
    ],
)
async def test_a_rule_with_a_value_outside_what_is_known_is_refused(
    db: Database, rule: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as refused:
        await add_limit(db, **rule)

    assert violated(refused) == (CHECK_VIOLATION, constraint)


@pytest.mark.parametrize("kind", [None, "withdrawal"])
async def test_a_subject_has_one_rule_for_a_kind_and_one_for_every_kind(
    db: Database, kind: str | None
) -> None:
    user_id = new_id()
    await add_limit(db, user_id=user_id, kind=kind)

    with pytest.raises(DBAPIError) as refused:
        await add_limit(db, user_id=user_id, kind=kind)

    assert violated(refused) == (UNIQUE_VIOLATION, "uq_risk_limits_subject")


async def test_a_tier_cannot_be_given_a_second_default(db: Database) -> None:
    with pytest.raises(DBAPIError) as refused:
        await add_limit(db, scope="tier", user_id=None, tier=0)

    assert violated(refused) == (UNIQUE_VIOLATION, "uq_risk_limits_subject")


# --- usage -----------------------------------------------------------------------------------


async def test_a_movement_is_recorded_once_for_its_kind(db: Database) -> None:
    movement_id = new_id()
    await add_usage(db, movement_id=movement_id)
    await add_usage(db, movement_id=movement_id, kind="withdrawal")

    with pytest.raises(DBAPIError) as refused:
        await add_usage(db, movement_id=movement_id)

    assert violated(refused) == (UNIQUE_VIOLATION, "uq_risk_usage_kind_movement_id")


@pytest.mark.parametrize(
    ("usage", "constraint"),
    [
        ({"kind": "deposit"}, "ck_risk_usage_kind"),
        ({"amount": 0}, "ck_risk_usage_amount"),
        ({"usd_value": -1}, "ck_risk_usage_usd_value"),
    ],
)
async def test_usage_with_a_value_outside_what_is_known_is_refused(
    db: Database, usage: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as refused:
        await add_usage(db, **usage)

    assert violated(refused) == (CHECK_VIOLATION, constraint)


async def test_a_reference_rate_is_more_than_nothing(owner_db: Database) -> None:
    with pytest.raises(DBAPIError) as refused:
        async with owner_db.transaction() as session:
            await session.execute(
                text("UPDATE risk_reference_rates SET usd_per_unit = 0 WHERE asset = 'MXN'")
            )

    assert violated(refused) == (CHECK_VIOLATION, "ck_risk_reference_rates_usd_per_unit")


# --- the deny list and reviews ---------------------------------------------------------------


async def test_a_party_is_listed_once_for_its_kind(db: Database) -> None:
    await add_listed(db)
    await add_listed(db, kind="address")

    with pytest.raises(DBAPIError) as refused:
        await add_listed(db, outcome="review")

    assert violated(refused) == (UNIQUE_VIOLATION, "uq_risk_denylist_kind_value_normalised")


@pytest.mark.parametrize(
    ("listed", "constraint"),
    [
        ({"kind": "email"}, "ck_risk_denylist_kind"),
        ({"value": ""}, "ck_risk_denylist_value_normalised"),
        ({"outcome": "clear"}, "ck_risk_denylist_outcome"),
    ],
)
async def test_a_listed_party_with_a_value_outside_what_is_known_is_refused(
    db: Database, listed: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as refused:
        await add_listed(db, **listed)

    assert violated(refused) == (CHECK_VIOLATION, constraint)


async def test_a_movement_has_one_review(db: Database) -> None:
    subject_id = new_id()
    await add_review(db, subject_id=subject_id)
    await add_review(db, subject_id=subject_id, subject_type="deposit")

    with pytest.raises(DBAPIError) as refused:
        await add_review(db, subject_id=subject_id)

    assert violated(refused) == (UNIQUE_VIOLATION, "uq_risk_reviews_subject_type_subject_id")


@pytest.mark.parametrize(
    ("review", "constraint"),
    [
        ({"subject_type": "transfer"}, "ck_risk_reviews_subject_type"),
        ({"outcome": "clear"}, "ck_risk_reviews_outcome"),
        ({"status": "pending", "resolved_at": NOW}, "ck_risk_reviews_status"),
        ({"status": "open", "resolved_at": NOW}, "ck_risk_reviews_resolved"),
        ({"status": "cleared"}, "ck_risk_reviews_resolved"),
        ({"status": "rejected"}, "ck_risk_reviews_resolved"),
    ],
)
async def test_a_review_with_a_value_outside_what_is_known_is_refused(
    db: Database, review: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(DBAPIError) as refused:
        await add_review(db, **review)

    assert violated(refused) == (CHECK_VIOLATION, constraint)

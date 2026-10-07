"""The risk admin endpoints: an admin keeps the deny list and sets limits, nobody else
can, and every change is in the audit log."""

import uuid
from typing import Any

import httpx
import pytest

from corridor import risk
from corridor.identity import Principal, User
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.risk import LimitExceeded, MoneyMovement
from tests.ops.support import admin, audited
from tests.payments.support import add_person, rows
from tests.support.auth import register_user

DENYLIST = "/v1/admin/risk/denylist"
LIMITS = "/v1/admin/risk/limits"

LISTING = {"kind": "name", "value": "Maria  SILVA", "outcome": "deny", "note": "sanctions"}


def rule(**changes: Any) -> dict[str, Any]:
    return {
        "scope": "user",
        "user_id": str(uuid.uuid4()),
        "per_transaction_usd": "50.00",
        "daily_usd": "120.00",
        **changes,
    }


async def authorize(db: Database, user: User, amount: int) -> None:
    async with db.transaction() as session:
        await risk.authorize(
            session,
            MoneyMovement(
                kind="transfer",
                user_id=user.id,
                principal=Principal.for_user(user.id, user.role, new_id()),
                asset="USD",
                amount=amount,
                movement_id=new_id(),
            ),
        )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", DENYLIST, None),
        ("POST", DENYLIST, LISTING),
        ("GET", LIMITS, None),
        ("PUT", LIMITS, rule()),
    ],
)
async def test_the_risk_endpoints_need_an_administrator(
    client: httpx.AsyncClient, db: Database, method: str, path: str, body: Any
) -> None:
    maria = await register_user(client)
    limits_before = await rows(db, "SELECT count(*) AS n FROM risk_limits")

    anonymous = await client.request(method, path, json=body)
    refused = await client.request(method, path, json=body, headers=maria.headers)

    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "unauthenticated")
    assert (refused.status_code, refused.json()["code"]) == (403, "permission_denied")
    assert await rows(db, "SELECT 1 FROM risk_denylist") == []
    assert await rows(db, "SELECT count(*) AS n FROM risk_limits") == limits_before


# --- the deny list ---------------------------------------------------------------------------


async def test_an_admin_lists_a_party_and_screening_then_stops_it(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)

    response = await client.post(DENYLIST, json=LISTING, headers=root.headers)

    assert response.status_code == 201, response.text
    entry = response.json()
    # As screening compares it, not as it was typed.
    assert {key: entry[key] for key in ("kind", "value", "outcome", "note")} == {
        "kind": "name",
        "value": "maria silva",
        "outcome": "deny",
        "note": "sanctions",
    }
    async with db.transaction() as session:
        assert await risk.screen_party(session, kind="name", value="MARIA SILVA") == "deny"
    (event,) = await audited(db, "risk.denylist_changed")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert event["resource_id"] == entry["id"]
    # Who is listed is not copied into the audit log.
    assert event["details"] == {"kind": "name", "outcome": "deny"}


async def test_listing_a_party_again_changes_the_entry_it_has(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    first = await client.post(DENYLIST, json=LISTING, headers=root.headers)

    second = await client.post(
        DENYLIST,
        json={"kind": "name", "value": "maria silva", "outcome": "review"},
        headers=root.headers,
    )

    assert second.status_code == 201, second.text
    assert (second.json()["id"], second.json()["outcome"], second.json()["note"]) == (
        first.json()["id"],
        "review",
        None,
    )
    assert len(await rows(db, "SELECT 1 FROM risk_denylist")) == 1


@pytest.mark.parametrize(
    "body",
    [
        {**LISTING, "kind": "account", "value": "--- ---"},
        {**LISTING, "value": ""},
        {**LISTING, "kind": "email"},
        {**LISTING, "outcome": "clear"},
        {**LISTING, "value": "x" * 321},
        {**LISTING, "value": "Maria\x00Silva"},
        {**LISTING, "surprise": True},
        {"kind": "name", "outcome": "deny"},
    ],
)
async def test_a_listing_that_is_not_one_is_refused_and_nothing_is_listed(
    client: httpx.AsyncClient, db: Database, settings: Settings, body: dict[str, Any]
) -> None:
    root = await admin(client, db, settings)

    response = await client.post(DENYLIST, json=body, headers=root.headers)

    assert response.status_code == 422, response.text
    assert await rows(db, "SELECT 1 FROM risk_denylist") == []
    assert await audited(db, "risk.denylist_changed") == []


async def test_an_admin_reads_the_deny_list_newest_first_a_page_at_a_time(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    for number in range(5):
        listed = await client.post(
            DENYLIST, json={**LISTING, "value": f"party {number}"}, headers=root.headers
        )
        assert listed.status_code == 201, listed.text

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(DENYLIST, params=params, headers=root.headers)).json()
        seen += [item["value"] for item in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert (seen, pages) == ([f"party {number}" for number in (4, 3, 2, 1, 0)], 3)
    assert len(await audited(db, "risk.denylist_listed")) == 3
    bad = await client.get(DENYLIST, params={"cursor": "nonsense"}, headers=root.headers)
    assert (bad.status_code, bad.json()["code"]) == (422, "invalid_cursor")


# --- limits ----------------------------------------------------------------------------------


async def test_an_admin_sets_a_users_limits_and_they_apply(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    async with db.transaction() as session:
        maria = await add_person(session, "maria")

    response = await client.put(LIMITS, json=rule(user_id=str(maria.id)), headers=root.headers)

    assert response.status_code == 200, response.text
    assert {key: value for key, value in response.json().items() if key != "id"} == {
        "scope": "user",
        "tier": None,
        "user_id": str(maria.id),
        "agent_id": None,
        "kind": None,
        "per_transaction_usd": "50.00",
        "daily_usd": "120.00",
    }
    await authorize(db, maria, 50_00)
    with pytest.raises(LimitExceeded):
        await authorize(db, maria, 50_01)
    (event,) = await audited(db, "risk.limit_set")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert event["resource_id"] == response.json()["id"]
    assert event["details"] == {
        "scope": "user",
        "tier": None,
        "agent_id": None,
        "kind": None,
        "per_transaction_usd": "50.00",
        "daily_usd": "120.00",
    }


async def test_setting_a_rule_again_replaces_it_and_null_sets_no_limit(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    async with db.transaction() as session:
        maria = await add_person(session, "maria")
    first = await client.put(LIMITS, json=rule(user_id=str(maria.id)), headers=root.headers)

    second = await client.put(
        LIMITS,
        json=rule(user_id=str(maria.id), per_transaction_usd=None, daily_usd=None),
        headers=root.headers,
    )

    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert (second.json()["per_transaction_usd"], second.json()["daily_usd"]) == (None, None)
    await authorize(db, maria, 9_000_000_00)


async def test_an_admin_sets_a_tiers_limit_for_one_kind_of_movement(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)

    response = await client.put(
        LIMITS,
        json={
            "scope": "tier",
            "tier": 0,
            "kind": "withdrawal",
            "per_transaction_usd": "0",
            "daily_usd": None,
        },
        headers=root.headers,
    )

    assert response.status_code == 200, response.text
    assert (response.json()["kind"], response.json()["per_transaction_usd"]) == (
        "withdrawal",
        "0.00",
    )


@pytest.mark.parametrize(
    "body",
    [
        rule(user_id=None),
        rule(tier=0),
        rule(scope="tier"),
        {"scope": "tier", "tier": 3, "per_transaction_usd": "1.00", "daily_usd": "1.00"},
        {"scope": "tier", "tier": "1", "per_transaction_usd": "1.00", "daily_usd": "1.00"},
        rule(scope="everyone"),
        rule(kind="deposit"),
        rule(per_transaction_usd="1.005"),
        rule(daily_usd="-1.00"),
        rule(daily_usd=100),
        {"scope": "user", "user_id": str(uuid.uuid4()), "per_transaction_usd": "1.00"},
        rule(surprise=True),
    ],
)
async def test_a_rule_that_is_not_one_is_refused_and_nothing_is_set(
    client: httpx.AsyncClient, db: Database, settings: Settings, body: dict[str, Any]
) -> None:
    root = await admin(client, db, settings)
    before = await rows(db, "SELECT * FROM risk_limits ORDER BY id")

    response = await client.put(LIMITS, json=body, headers=root.headers)

    assert response.status_code == 422, response.text
    assert await rows(db, "SELECT * FROM risk_limits ORDER BY id") == before
    assert await audited(db, "risk.limit_set") == []


async def test_an_admin_cannot_set_an_agents_limits_which_are_its_owners_policy(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    before = await rows(db, "SELECT * FROM risk_limits ORDER BY id")

    response = await client.put(
        LIMITS,
        json={
            "scope": "agent",
            "agent_id": str(uuid.uuid4()),
            "per_transaction_usd": "1000000.00",
            "daily_usd": None,
        },
        headers=root.headers,
    )

    assert (response.status_code, response.json()["code"]) == (409, "agent_limit_not_settable")
    assert await rows(db, "SELECT * FROM risk_limits ORDER BY id") == before
    assert await audited(db, "risk.limit_set") == []


async def test_a_rule_for_a_user_who_does_not_exist_is_not_set(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    before = await rows(db, "SELECT * FROM risk_limits ORDER BY id")

    response = await client.put(LIMITS, json=rule(), headers=root.headers)

    assert (response.status_code, response.json()["code"]) == (404, "user_not_found")
    assert await rows(db, "SELECT * FROM risk_limits ORDER BY id") == before
    assert await audited(db, "risk.limit_set") == []


async def test_an_admin_reads_every_rule_newest_first_a_page_at_a_time(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    mine = (await client.put(LIMITS, json=rule(user_id=root.id), headers=root.headers)).json()

    first = (await client.get(LIMITS, params={"limit": 2}, headers=root.headers)).json()
    rest = (
        await client.get(
            LIMITS, params={"limit": 2, "cursor": first["next_cursor"]}, headers=root.headers
        )
    ).json()

    listed = first["items"] + rest["items"]
    assert listed[0] == mine
    # The three seeded tiers follow, the highest first.
    assert [(item["scope"], item["tier"]) for item in listed[1:]] == [
        ("tier", 2),
        ("tier", 1),
        ("tier", 0),
    ]
    assert listed[-1]["per_transaction_usd"] == "1000.00"
    assert rest["next_cursor"] is None
    assert len(await audited(db, "risk.limits_listed")) == 2

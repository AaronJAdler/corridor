"""The review endpoints: an admin reads the movements screening held back and decides
them, nobody else can, and a decision is made once."""

import uuid
from typing import Any

import httpx
import pytest

from corridor import risk
from corridor.identity import User
from corridor.platform.config import Settings
from corridor.platform.db import Database
from tests.ops.support import admin, audited
from tests.payments.support import add_person, available, deposit, held, rows, withdraw
from tests.support.auth import register_user
from tests.support.providers import EXTERNAL_ADDRESS

REVIEWS = "/v1/admin/reviews"
FUNDED = 50_000_000
AMOUNT = 1_000_000


@pytest.fixture
async def maria(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "maria")


async def under_review(db: Database, settings: Settings, user: User) -> tuple[str, str, int]:
    """A held chain withdrawal to an address listed for review: its id, its review's id,
    and what it reserved."""
    await deposit(db, user, FUNDED, "USDC")
    async with db.transaction() as session:
        await risk.add_to_denylist(
            session, kind="address", value=EXTERNAL_ADDRESS, outcome="review"
        )
    withdrawal = await withdraw(
        db, settings, user, AMOUNT, asset="USDC", to_address=EXTERNAL_ADDRESS
    )
    (review,) = await rows(db, "SELECT id FROM risk_reviews")
    return str(withdrawal.id), str(review["id"]), AMOUNT + withdrawal.fee


async def statuses(db: Database) -> list[tuple[str, str]]:
    found = await rows(
        db,
        "SELECT r.status AS review, w.status AS withdrawal FROM risk_reviews r"
        " JOIN withdrawals w ON w.id = r.subject_id",
    )
    return [(row["review"], row["withdrawal"]) for row in found]


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", ""), ("POST", "/{id}/clear"), ("POST", "/{id}/reject")],
)
async def test_the_review_endpoints_need_an_administrator(
    client: httpx.AsyncClient,
    db: Database,
    settings: Settings,
    maria: User,
    method: str,
    path: str,
) -> None:
    someone = await register_user(client)
    _, review_id, _ = await under_review(db, settings, maria)
    url = REVIEWS + path.format(id=review_id)

    anonymous = await client.request(method, url)
    refused = await client.request(method, url, headers=someone.headers)

    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "unauthenticated")
    assert (refused.status_code, refused.json()["code"]) == (403, "permission_denied")
    assert await statuses(db) == [("open", "held")]
    assert await audited(db, "review.listed") == []


async def test_an_admin_lists_the_open_reviews(
    client: httpx.AsyncClient, db: Database, settings: Settings, maria: User
) -> None:
    root = await admin(client, db, settings)
    withdrawal_id, review_id, _ = await under_review(db, settings, maria)

    response = await client.get(REVIEWS, headers=root.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    (item,) = body["items"]
    assert {key: item[key] for key in item if key != "created_at"} == {
        "id": review_id,
        "subject_type": "withdrawal",
        "subject_id": withdrawal_id,
        "user_id": str(maria.id),
        "screening": "review",
        "status": "open",
        "resolved_at": None,
    }
    (listing,) = await audited(db, "review.listed")
    assert (listing["actor_type"], listing["actor_id"]) == ("admin", root.id)


async def test_the_open_reviews_are_paged_with_a_cursor_and_leave_out_the_resolved(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    ids = []
    for _ in range(5):
        async with db.transaction() as session:
            review = await risk.open_review(
                session, subject_type="deposit", subject_id=uuid.uuid4(), outcome="deny"
            )
        ids.append(str(review.id))
    rejected = await client.post(f"{REVIEWS}/{ids[2]}/reject", headers=root.headers)
    assert rejected.status_code == 200, rejected.text

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 3} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(REVIEWS, params=params, headers=root.headers)).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == [ids[4], ids[3], ids[1], ids[0]]
    bad = await client.get(REVIEWS, params={"cursor": "nonsense"}, headers=root.headers)
    assert (bad.status_code, bad.json()["code"]) == (422, "invalid_cursor")


async def test_an_admin_clears_a_review_once_and_the_withdrawal_is_asked_to_be_sent(
    client: httpx.AsyncClient, db: Database, settings: Settings, maria: User
) -> None:
    root = await admin(client, db, settings)
    _, review_id, _ = await under_review(db, settings, maria)

    cleared = await client.post(f"{REVIEWS}/{review_id}/clear", headers=root.headers)
    again = await client.post(f"{REVIEWS}/{review_id}/clear", headers=root.headers)
    late = await client.post(f"{REVIEWS}/{review_id}/reject", headers=root.headers)

    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["id"], cleared.json()["status"]) == (review_id, "cleared")
    assert cleared.json()["resolved_at"] is not None
    for repeat in (again, late):
        assert (repeat.status_code, repeat.json()["code"]) == (409, "review_already_resolved")
    assert await statuses(db) == [("cleared", "held")]
    asked = await rows(db, "SELECT 1 FROM outbox_events WHERE topic = 'withdrawal.submit'")
    assert len(asked) == 2
    assert len(await audited(db, "review.cleared")) == 1
    assert (await client.get(REVIEWS, headers=root.headers)).json()["items"] == []


async def test_an_admin_rejects_a_review_once_and_the_withdrawal_is_given_back(
    client: httpx.AsyncClient, db: Database, settings: Settings, maria: User
) -> None:
    root = await admin(client, db, settings)
    _, review_id, reserved = await under_review(db, settings, maria)
    assert await held(db, maria, "USDC") == reserved

    rejected = await client.post(f"{REVIEWS}/{review_id}/reject", headers=root.headers)
    again = await client.post(f"{REVIEWS}/{review_id}/reject", headers=root.headers)

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert (again.status_code, again.json()["code"]) == (409, "review_already_resolved")
    assert await statuses(db) == [("rejected", "failed")]
    assert (await available(db, maria, "USDC"), await held(db, maria, "USDC")) == (FUNDED, 0)
    (event,) = await audited(db, "review.rejected")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)


async def test_a_review_that_does_not_exist_is_not_found(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)

    for action in ("clear", "reject"):
        missing = await client.post(f"{REVIEWS}/{uuid.uuid4()}/{action}", headers=root.headers)
        malformed = await client.post(f"{REVIEWS}/not-an-id/{action}", headers=root.headers)

        assert (missing.status_code, missing.json()["code"]) == (404, "review_not_found")
        assert malformed.status_code == 422

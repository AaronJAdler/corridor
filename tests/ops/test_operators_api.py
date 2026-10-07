"""What an operator reads and does over HTTP where the runbook used to say SQL: the
deposits in suspense, restricting a user and lifting it again, and the audit log."""

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor import ops, risk, wallets
from corridor.identity import Principal, User
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.config import Settings
from corridor.platform.db import (
    LOCK_NOT_AVAILABLE,
    Database,
    advisory_xact_lock,
    lock_key,
    sqlstate_of,
)
from corridor.platform.errors import PermissionDenied
from corridor.providers import SimBank, SimCustody
from tests.ops.support import admin, audited, suspended
from tests.ops.test_reviews import clear, deposit_under_review
from tests.payments.support import acting_as, rows
from tests.support.auth import RegisteredUser, register_user
from tests.support.ledger import fund
from tests.support.providers import Sim

SUSPENSE = "/v1/admin/deposits/suspense"
AUDIT = "/v1/admin/audit"


def restrict_url(user_id: object) -> str:
    return f"/v1/admin/users/{user_id}/restrict"


def lift_url(user_id: object) -> str:
    return f"/v1/admin/users/{user_id}/lift-restriction"


async def standing(db: Database, user_id: object) -> tuple[str, str | None]:
    (row,) = await rows(
        db, "SELECT status, restricted_reason FROM users WHERE id = :id", id=uuid.UUID(str(user_id))
    )
    return str(row["status"]), row["restricted_reason"]


# --- who may ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", SUSPENSE, None),
        ("GET", AUDIT, None),
        ("POST", restrict_url(uuid.UUID(int=1)), {"reason": "under review"}),
        ("POST", lift_url(uuid.UUID(int=1)), {"reason": "cleared"}),
    ],
)
async def test_an_operators_endpoint_needs_an_administrator(
    client: httpx.AsyncClient, db: Database, method: str, path: str, body: dict[str, str] | None
) -> None:
    maria = await register_user(client)

    anonymous = await client.request(method, path, json=body)
    as_user = await client.request(method, path, json=body, headers=maria.headers)

    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "unauthenticated")
    assert (as_user.status_code, as_user.json()["code"]) == (403, "permission_denied")
    assert await rows(db, "SELECT 1 FROM audit_events WHERE actor_type = 'admin'") == []


@pytest.mark.parametrize("call", ["suspense", "audit", "restrict", "lift"])
async def test_the_service_refuses_a_principal_who_is_not_an_administrator(
    db: Database, maria: User, call: str
) -> None:
    # The route asks for an administrator, and so does the function behind it.
    principal = acting_as(maria)
    target = uuid.UUID(int=1)

    with pytest.raises(PermissionDenied):
        async with db.transaction() as session:
            if call == "suspense":
                await ops.list_suspense_deposits(session, principal)
            elif call == "audit":
                await ops.list_audit_events(session, principal)
            elif call == "restrict":
                await ops.restrict_user(session, principal, target, "under review")
            else:
                await ops.lift_restriction(session, principal, target, "cleared")


# --- deposits in suspense --------------------------------------------------------------------


async def test_an_admin_lists_the_deposits_in_suspense_newest_first_and_it_is_audited(
    client: httpx.AsyncClient,
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
) -> None:
    root = await admin(client, db, settings)
    reviewed, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    nobodys = await suspended(db, "75.00")

    response = await client.get(SUSPENSE, headers=root.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    assert body["items"] == [
        {
            "id": str(nobodys["id"]),
            "provider": "simbank",
            "asset": "USD",
            "amount": "75.00",
            "received_at": nobodys["created_at"].isoformat().replace("+00:00", "Z"),
            "review_id": None,
        },
        {
            "id": str(reviewed["id"]),
            "provider": "simbank",
            "asset": "USD",
            "amount": "250.00",
            "received_at": reviewed["created_at"].isoformat().replace("+00:00", "Z"),
            "review_id": str(review_id),
        },
    ]
    (event,) = await audited(db, "deposit.suspense_listed")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert event["details"] == {"returned": 2}


async def test_a_deposit_that_has_left_suspense_is_not_listed(
    client: httpx.AsyncClient,
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    ana: Principal,
) -> None:
    root = await admin(client, db, settings)
    _, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    still_there = await suspended(db)
    await clear(db, ana, review_id)

    listed = (await client.get(SUSPENSE, headers=root.headers)).json()["items"]

    assert [item["id"] for item in listed] == [str(still_there["id"])]


async def test_the_suspense_list_is_read_a_page_at_a_time(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    deposits = [str((await suspended(db))["id"]) for _ in range(3)]

    first = (await client.get(SUSPENSE, params={"limit": 2}, headers=root.headers)).json()
    second = (
        await client.get(
            SUSPENSE, params={"limit": 2, "cursor": first["next_cursor"]}, headers=root.headers
        )
    ).json()

    assert [item["id"] for item in first["items"]] == deposits[:0:-1]
    assert [item["id"] for item in second["items"]] == deposits[:1]
    assert second["next_cursor"] is None
    # One audit event for each request, whatever it returned.
    assert [event["details"] for event in await audited(db, "deposit.suspense_listed")] == [
        {"returned": 2},
        {"returned": 1},
    ]


async def test_the_suspense_list_refuses_a_cursor_from_another_list_and_a_page_of_nothing(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    await suspended(db)
    await suspended(db)
    elsewhere = (await client.get(AUDIT, params={"limit": 1}, headers=root.headers)).json()

    foreign = await client.get(
        SUSPENSE, params={"cursor": elsewhere["next_cursor"]}, headers=root.headers
    )
    empty = await client.get(SUSPENSE, params={"limit": 0}, headers=root.headers)

    assert elsewhere["next_cursor"] is not None
    assert (foreign.status_code, foreign.json()["code"]) == (422, "invalid_cursor")
    assert (empty.status_code, empty.json()["code"]) == (422, "invalid_request")


# --- restricting a user ----------------------------------------------------------------------


async def test_an_admin_restricts_a_user_and_it_is_audited_with_the_reason(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)

    response = await client.post(
        restrict_url(maria.id),
        json={"reason": "chargeback under investigation"},
        headers=root.headers,
    )

    assert response.status_code == 200, response.text
    assert (response.json()["id"], response.json()["status"]) == (maria.id, "restricted")
    assert await standing(db, maria.id) == ("restricted", "chargeback under investigation")
    (event,) = await audited(db, "user.restricted")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert (event["resource_type"], event["resource_id"]) == ("user", maria.id)
    assert event["details"] == {"reason": "chargeback under investigation"}


async def test_a_restricted_user_moves_no_money_out_until_the_restriction_is_lifted(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    maria, joao = await register_user(client), await register_user(client)
    await give(db, maria, 50_00)
    await client.post(restrict_url(maria.id), json={"reason": "under review"}, headers=root.headers)

    refused = await send(client, maria, joao)
    lifted = await client.post(
        lift_url(maria.id), json={"reason": "review closed"}, headers=root.headers
    )
    sent = await send(client, maria, joao)

    assert (refused.status_code, refused.json()["code"]) == (403, "user_restricted")
    assert (lifted.status_code, lifted.json()["status"]) == (200, "active")
    assert sent.status_code == 201, sent.text
    assert await standing(db, maria.id) == ("active", None)
    (event,) = await audited(db, "user.restriction_lifted")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert (event["resource_type"], event["resource_id"]) == ("user", maria.id)
    assert event["details"] == {"reason": "review closed"}


async def give(db: Database, user: RegisteredUser, amount: int) -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), "USD")
        await fund(session, wallet.available_account_id, amount)


async def send(
    client: httpx.AsyncClient, sender: RegisteredUser, recipient: RegisteredUser
) -> httpx.Response:
    return await client.post(
        "/v1/transfers",
        json={"recipient": recipient.id, "asset": "USD", "amount": "1.00"},
        headers={**sender.headers, "Idempotency-Key": f"op-{uuid.uuid4()}"},
    )


@pytest.mark.parametrize("url", [restrict_url, lift_url])
async def test_an_admin_cannot_restrict_themselves_or_lift_their_own_restriction(
    client: httpx.AsyncClient, db: Database, settings: Settings, url: Any
) -> None:
    root = await admin(client, db, settings)

    response = await client.post(url(root.id), json={"reason": "a slip"}, headers=root.headers)

    assert (response.status_code, response.json()["code"]) == (409, "own_account")
    assert await standing(db, root.id) == ("active", None)
    assert await audited(db, "user.restricted") == []
    assert await audited(db, "user.restriction_lifted") == []


@pytest.mark.parametrize("url", [restrict_url, lift_url])
@pytest.mark.parametrize(
    "body", [{}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}, {"reason": "a", "b": 1}]
)
async def test_a_restriction_and_its_lifting_need_a_reason(
    client: httpx.AsyncClient, db: Database, settings: Settings, url: Any, body: dict[str, object]
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)

    response = await client.post(url(maria.id), json=body, headers=root.headers)

    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    assert await standing(db, maria.id) == ("active", None)


@pytest.mark.parametrize("url", [restrict_url, lift_url])
async def test_restricting_nobody_or_a_closed_account_is_refused(
    client: httpx.AsyncClient, db: Database, settings: Settings, url: Any
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)
    closed = await client.post(f"/v1/admin/users/{maria.id}/close", headers=root.headers)

    nobody = await client.post(url(uuid.uuid4()), json={"reason": "r"}, headers=root.headers)
    gone = await client.post(url(maria.id), json={"reason": "r"}, headers=root.headers)

    assert closed.status_code == 200, closed.text
    assert (nobody.status_code, nobody.json()["code"]) == (404, "user_not_found")
    assert (gone.status_code, gone.json()["code"]) == (409, "conflict")
    assert await standing(db, maria.id) == ("closed", None)
    assert await audited(db, "user.restricted") == []
    assert await audited(db, "user.restriction_lifted") == []


async def test_an_admins_restriction_waits_for_the_users_money_out_lock(
    db: Database, ana: Principal, maria: User
) -> None:
    # A movement out holds this lock until it commits: the restriction cannot overtake it.
    async with db.transaction() as moving:
        await advisory_xact_lock(moving, [lock_key(risk.MONEY_OUT_LOCK, maria.id)])

        with pytest.raises(DBAPIError) as failure:
            async with db.transaction() as restricting:
                await restricting.execute(text("SET LOCAL lock_timeout = '100ms'"))
                await ops.restrict_user(restricting, ana, maria.id, "under review")

        assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
    assert await standing(db, maria.id) == ("active", None)


# --- the audit log ---------------------------------------------------------------------------


async def audit_page(
    client: httpx.AsyncClient, root: RegisteredUser, **params: object
) -> dict[str, Any]:
    response = await client.get(AUDIT, params=params, headers=root.headers)
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


async def test_an_admin_reads_the_audit_log_newest_first(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)
    await client.post(restrict_url(maria.id), json={"reason": "under review"}, headers=root.headers)
    await client.post(lift_url(maria.id), json={"reason": "closed"}, headers=root.headers)

    page = await audit_page(client, root, action="user.restrict")

    assert [event["action"] for event in page["items"]] == [
        "user.restriction_lifted",
        "user.restricted",
    ]
    assert page["next_cursor"] is None
    event = page["items"][1]
    assert set(event) == {
        "id",
        "occurred_at",
        "actor_type",
        "actor_id",
        "principal_id",
        "action",
        "resource_type",
        "resource_id",
        "outcome",
        "request_id",
        "details",
    }
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert (event["principal_id"], event["resource_type"], event["resource_id"]) == (
        maria.id,
        "user",
        maria.id,
    )
    assert event["outcome"] == "success"
    assert event["details"] == {"reason": "under review"}


async def test_reading_the_audit_log_is_audited_once_for_each_request_and_not_for_each_row(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    for _ in range(3):
        await register_user(client)
    since = "2020-01-01T00:00:00Z"

    page = await audit_page(client, root, action="auth.reg", since=since, limit=50)

    assert len(page["items"]) >= 3
    (event,) = await audited(db, "audit.listed")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert event["resource_type"] == "audit_event"
    assert event["details"] == {
        "returned": len(page["items"]),
        "filters": {"action": "auth.reg", "since": "2020-01-01T00:00:00+00:00"},
    }
    # The read is in the log for the next reader, and was not in its own page.
    assert "audit.listed" not in {item["action"] for item in page["items"]}
    assert [
        item["action"] for item in (await audit_page(client, root, action="audit."))["items"]
    ] == ["audit.listed"]


async def test_the_audit_log_is_filtered_by_actor_by_subject_and_by_what_was_done(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root, other = await admin(client, db, settings), await admin(client, db, settings)
    maria, joao = await register_user(client), await register_user(client)
    await client.post(restrict_url(maria.id), json={"reason": "one"}, headers=root.headers)
    await client.post(restrict_url(joao.id), json={"reason": "two"}, headers=other.headers)
    await client.put(
        f"/v1/admin/users/{maria.id}/kyc-tier", json={"kyc_tier": 1}, headers=other.headers
    )

    by_actor = await audit_page(client, root, actor=other.id, action="user.")
    by_subject = await audit_page(client, root, subject=maria.id, action="user.restricted")
    by_action = await audit_page(client, root, action="user.restricted")
    exact_only_as_a_prefix = await audit_page(client, root, action="user.restricted_")
    both = await audit_page(client, root, actor=other.id, subject=maria.id)

    assert {(e["actor_id"], e["resource_id"]) for e in by_actor["items"]} == {
        (other.id, joao.id),
        (other.id, maria.id),
    }
    assert [(e["actor_id"], e["resource_id"]) for e in by_subject["items"]] == [(root.id, maria.id)]
    assert [e["resource_id"] for e in by_action["items"]] == [joao.id, maria.id]
    assert exact_only_as_a_prefix["items"] == []
    assert {e["actor_id"] for e in both["items"]} == {other.id}
    assert both["items"]
    assert {maria.id} >= {e["resource_id"] for e in both["items"]} - {None}


async def test_a_subject_is_what_was_acted_on_or_the_user_it_was_done_for(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)

    # A login is about a session, and is done for a user.
    logins = await audit_page(client, root, subject=maria.id, action="auth.logged_in")
    not_an_id = await audit_page(client, root, subject="no such thing")

    assert [(e["principal_id"], e["resource_type"]) for e in logins["items"]] == [
        (maria.id, "session")
    ]
    assert not_an_id["items"] == []


async def test_a_prefix_is_matched_as_it_is_written_and_not_as_a_pattern(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    await register_user(client)

    # "_" matches any one character in a LIKE pattern, and "auth_" is the beginning of no
    # action: "auth.registered" has a dot there.
    page = await audit_page(client, root, action="auth_")

    assert page["items"] == []


async def test_the_audit_log_is_filtered_by_when_it_happened(
    client: httpx.AsyncClient, db: Database, settings: Settings, clock: ManualClock
) -> None:
    root = await admin(client, db, settings)
    maria = await register_user(client)
    await client.post(restrict_url(maria.id), json={"reason": "early"}, headers=root.headers)
    clock.advance(seconds=30)
    boundary = utcnow()
    await client.post(lift_url(maria.id), json={"reason": "late"}, headers=root.headers)

    before = await audit_page(client, root, action="user.restrict", until=boundary.isoformat())
    after = await audit_page(client, root, action="user.restrict", since=boundary.isoformat())
    neither = await audit_page(
        client,
        root,
        action="user.restrict",
        since=(boundary + timedelta(seconds=1)).isoformat(),
    )

    # From ``since`` on, and up to but not including ``until``.
    assert [e["action"] for e in before["items"]] == ["user.restricted"]
    assert [e["action"] for e in after["items"]] == ["user.restriction_lifted"]
    assert neither["items"] == []


async def test_the_audit_log_is_read_a_page_at_a_time(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    users = [await register_user(client) for _ in range(3)]
    for user in users:
        await client.post(restrict_url(user.id), json={"reason": "r"}, headers=root.headers)

    first = await audit_page(client, root, action="user.restricted", limit=2)
    second = await audit_page(
        client, root, action="user.restricted", limit=2, cursor=first["next_cursor"]
    )

    assert [e["resource_id"] for e in first["items"]] == [users[2].id, users[1].id]
    assert [e["resource_id"] for e in second["items"]] == [users[0].id]
    assert second["next_cursor"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"action": "User."},
        {"action": "user%"},
        {"action": "a" * 101},
        {"since": "2026-01-15T12:00:00"},
        {"until": "yesterday"},
        {"subject": "x" * 201},
        {"actor": "x" * 201},
        {"limit": 0},
        {"cursor": "not-a-cursor"},
    ],
)
async def test_the_audit_log_refuses_a_filter_it_cannot_apply(
    client: httpx.AsyncClient, db: Database, settings: Settings, params: dict[str, object]
) -> None:
    root = await admin(client, db, settings)

    response = await client.get(AUDIT, params=params, headers=root.headers)

    assert response.status_code == 422, response.text
    assert response.json()["code"] in {"invalid_request", "invalid_cursor"}
    assert await audited(db, "audit.listed") == []

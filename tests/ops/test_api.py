"""The adjustment endpoints: asked for by one admin and approved by another, over HTTP,
under idempotency keys."""

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from corridor import ledger, wallets
from corridor.ledger import AccountKind
from corridor.platform.config import Settings
from corridor.platform.db import Database
from tests.ops.support import admin, audited, deposit_row, keyed, suspended
from tests.ops.test_adjustments import BANK
from tests.payments.support import rows
from tests.support.auth import RegisteredUser, register_user, served_routes

ADJUSTMENTS = "/v1/admin/adjustments"


async def goodwill(db: Database, user: RegisteredUser, amount: str = "25.00") -> dict[str, Any]:
    """The body of a credit to a user against the bank."""
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), "USD")
        settlement = await ledger.open_account(
            session, AccountKind.BANK_SETTLEMENT, "USD", provider=BANK
        )
    return {
        "reason": "goodwill credit",
        "legs": [
            {
                "account_id": str(settlement.id),
                "asset": "USD",
                "direction": "debit",
                "amount": amount,
            },
            {
                "account_id": str(wallet.available_account_id),
                "asset": "USD",
                "direction": "credit",
                "amount": amount,
            },
        ],
    }


async def balance(client: httpx.AsyncClient, user: RegisteredUser, asset: str = "USD") -> str:
    wallets_ = (await client.get("/v1/wallets", headers=user.headers)).json()["wallets"]
    return str(next(wallet["available"] for wallet in wallets_ if wallet["asset"] == asset))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", ADJUSTMENTS),
        ("GET", f"{ADJUSTMENTS}/{uuid.uuid4()}"),
        ("POST", ADJUSTMENTS),
        ("POST", f"{ADJUSTMENTS}/suspense-release"),
        ("POST", f"{ADJUSTMENTS}/suspense-return"),
        ("POST", f"{ADJUSTMENTS}/{uuid.uuid4()}/approve"),
        ("POST", f"{ADJUSTMENTS}/{uuid.uuid4()}/reject"),
    ],
)
async def test_the_adjustment_endpoints_need_an_administrator(
    client: httpx.AsyncClient, db: Database, method: str, path: str
) -> None:
    maria = await register_user(client)
    body = await goodwill(db, maria) if method == "POST" else None

    anonymous = await client.request(method, path, json=body)
    refused = await client.request(method, path, json=body, headers=keyed(maria))

    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "unauthenticated")
    assert (refused.status_code, refused.json()["code"]) == (403, "permission_denied")
    assert await rows(db, "SELECT 1 FROM ops_adjustments") == []


async def test_one_admin_asks_and_another_approves_and_the_user_is_credited(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana, bruno = await admin(client, db, settings), await admin(client, db, settings)
    maria = await register_user(client)

    asked = await client.post(ADJUSTMENTS, json=await goodwill(db, maria), headers=keyed(ana))

    assert asked.status_code == 201, asked.text
    pending = asked.json()
    assert (pending["status"], pending["requested_by"], pending["approved_by"]) == (
        "pending",
        ana.id,
        None,
    )
    assert [(leg["direction"], leg["asset"], leg["amount"]) for leg in pending["legs"]] == [
        ("debit", "USD", "25.00"),
        ("credit", "USD", "25.00"),
    ]
    assert await balance(client, maria) == "0.00"

    approved = await client.post(f"{ADJUSTMENTS}/{pending['id']}/approve", headers=keyed(bruno))

    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert (body["status"], body["approved_by"]) == ("approved", bruno.id)
    assert body["entry_id"] is not None
    assert await balance(client, maria) == "25.00"
    read = await client.get(f"{ADJUSTMENTS}/{pending['id']}", headers=ana.headers)
    assert read.json() == body
    listed = await client.get(ADJUSTMENTS, params={"status": "approved"}, headers=ana.headers)
    assert [item["id"] for item in listed.json()["items"]] == [pending["id"]]
    assert [event["actor_id"] for event in await audited(db, "adjustment.requested")] == [ana.id]
    assert [event["actor_id"] for event in await audited(db, "adjustment.approved")] == [bruno.id]


async def test_the_requester_is_refused_when_approving_their_own_over_http(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana = await admin(client, db, settings)
    maria = await register_user(client)
    pending = (
        await client.post(ADJUSTMENTS, json=await goodwill(db, maria), headers=keyed(ana))
    ).json()

    refused = await client.post(f"{ADJUSTMENTS}/{pending['id']}/approve", headers=keyed(ana))

    assert (refused.status_code, refused.json()["code"]) == (403, "self_approval")
    assert await balance(client, maria) == "0.00"
    read = await client.get(f"{ADJUSTMENTS}/{pending['id']}", headers=ana.headers)
    assert read.json()["status"] == "pending"


async def test_a_repeated_request_asks_for_one_adjustment(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana = await admin(client, db, settings)
    maria = await register_user(client)
    body = await goodwill(db, maria)

    first = await client.post(ADJUSTMENTS, json=body, headers=keyed(ana, "adj-1"))
    again = await client.post(ADJUSTMENTS, json=body, headers=keyed(ana, "adj-1"))
    other = await client.post(
        ADJUSTMENTS, json={**body, "reason": "something else"}, headers=keyed(ana, "adj-1")
    )

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert (other.status_code, other.json()["code"]) == (422, "idempotency_key_reused")
    assert len(await rows(db, "SELECT 1 FROM ops_adjustments")) == 1


async def test_a_repeated_approval_posts_once_and_answers_the_same(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana, bruno = await admin(client, db, settings), await admin(client, db, settings)
    maria = await register_user(client)
    pending = (
        await client.post(ADJUSTMENTS, json=await goodwill(db, maria), headers=keyed(ana))
    ).json()
    path = f"{ADJUSTMENTS}/{pending['id']}/approve"

    first = await client.post(path, headers=keyed(bruno, "approve-1"))
    again = await client.post(path, headers=keyed(bruno, "approve-1"))
    anew = await client.post(path, headers=keyed(bruno, "approve-2"))

    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert (anew.status_code, anew.json()["code"]) == (409, "adjustment_not_pending")
    assert await balance(client, maria) == "25.00"
    assert len(await rows(db, "SELECT 1 FROM journal_entries WHERE kind = 'adjustment'")) == 1


@pytest.mark.parametrize(
    "path", [ADJUSTMENTS, f"{ADJUSTMENTS}/suspense-release", f"{ADJUSTMENTS}/suspense-return"]
)
async def test_asking_for_an_adjustment_needs_an_idempotency_key(
    client: httpx.AsyncClient, db: Database, settings: Settings, path: str
) -> None:
    ana = await admin(client, db, settings)
    maria = await register_user(client)
    deposit = await suspended(db)
    bodies: dict[str, dict[str, Any]] = {
        ADJUSTMENTS: await goodwill(db, maria),
        f"{ADJUSTMENTS}/suspense-release": {
            "reason": "for maria",
            "deposit_id": str(deposit["id"]),
            "user_id": maria.id,
        },
        f"{ADJUSTMENTS}/suspense-return": {
            "reason": "unclaimed",
            "deposit_id": str(deposit["id"]),
        },
    }

    response = await client.post(path, json=bodies[path], headers=ana.headers)

    assert (response.status_code, response.json()["code"]) == (400, "idempotency_key_required")
    assert await rows(db, "SELECT 1 FROM ops_adjustments") == []


async def test_a_key_that_approved_one_adjustment_does_not_answer_for_another(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana, bruno = await admin(client, db, settings), await admin(client, db, settings)
    maria = await register_user(client)
    one, other = [
        (await client.post(ADJUSTMENTS, json=await goodwill(db, maria), headers=keyed(ana))).json()
        for _ in range(2)
    ]

    first = await client.post(
        f"{ADJUSTMENTS}/{one['id']}/approve", headers=keyed(bruno, "approve-1")
    )
    reused = await client.post(
        f"{ADJUSTMENTS}/{other['id']}/approve", headers=keyed(bruno, "approve-1")
    )
    as_rejection = await client.post(
        f"{ADJUSTMENTS}/{one['id']}/reject", headers=keyed(bruno, "approve-1")
    )

    assert first.status_code == 200, first.text
    # Not the first one's stored answer, which would say "approved" of one that is not.
    assert (reused.status_code, reused.json()["code"]) == (422, "idempotency_key_reused")
    assert (as_rejection.status_code, as_rejection.json()["code"]) == (
        422,
        "idempotency_key_reused",
    )
    assert await rows(db, "SELECT status FROM ops_adjustments ORDER BY id") == [
        {"status": "approved"},
        {"status": "pending"},
    ]
    assert await balance(client, maria) == "25.00"


async def test_deciding_an_adjustment_needs_an_idempotency_key(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana, bruno = await admin(client, db, settings), await admin(client, db, settings)
    maria = await register_user(client)
    pending = (
        await client.post(ADJUSTMENTS, json=await goodwill(db, maria), headers=keyed(ana))
    ).json()

    for decision in ("approve", "reject"):
        response = await client.post(
            f"{ADJUSTMENTS}/{pending['id']}/{decision}", headers=bruno.headers
        )

        assert (response.status_code, response.json()["code"]) == (400, "idempotency_key_required")
    assert await rows(db, "SELECT status FROM ops_adjustments") == [{"status": "pending"}]


async def test_an_admin_rejects_an_adjustment_over_http(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana, bruno = await admin(client, db, settings), await admin(client, db, settings)
    maria = await register_user(client)
    pending = (
        await client.post(ADJUSTMENTS, json=await goodwill(db, maria), headers=keyed(ana))
    ).json()

    rejected = await client.post(f"{ADJUSTMENTS}/{pending['id']}/reject", headers=keyed(bruno))
    late = await client.post(f"{ADJUSTMENTS}/{pending['id']}/approve", headers=keyed(bruno))

    assert (rejected.status_code, rejected.json()["status"]) == (200, "rejected")
    assert (late.status_code, late.json()["code"]) == (409, "adjustment_not_pending")
    assert await balance(client, maria) == "0.00"


async def test_a_deposit_in_suspense_is_released_to_a_user_over_http(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana, bruno = await admin(client, db, settings), await admin(client, db, settings)
    maria = await register_user(client)
    deposit = await suspended(db)

    asked = await client.post(
        f"{ADJUSTMENTS}/suspense-release",
        json={"reason": "for maria", "deposit_id": str(deposit["id"]), "user_id": maria.id},
        headers=keyed(ana),
    )
    assert asked.status_code == 201, asked.text
    assert (asked.json()["kind"], asked.json()["deposit_id"], asked.json()["user_id"]) == (
        "suspense_release",
        str(deposit["id"]),
        maria.id,
    )
    approved = await client.post(
        f"{ADJUSTMENTS}/{asked.json()['id']}/approve", headers=keyed(bruno)
    )

    assert approved.status_code == 200, approved.text
    assert [(leg["direction"], leg["amount"]) for leg in approved.json()["legs"]] == [
        ("debit", "75.00"),
        ("credit", "75.00"),
    ]
    assert await balance(client, maria) == "75.00"
    assert (await deposit_row(db, deposit["provider_ref"]))["status"] == "completed"


async def test_a_deposit_in_suspense_is_booked_as_returned_over_http(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana, bruno = await admin(client, db, settings), await admin(client, db, settings)
    deposit = await suspended(db)

    asked = await client.post(
        f"{ADJUSTMENTS}/suspense-return",
        json={"reason": "unclaimed", "deposit_id": str(deposit["id"])},
        headers=keyed(ana),
    )
    approved = await client.post(
        f"{ADJUSTMENTS}/{asked.json()['id']}/approve", headers=keyed(bruno)
    )

    assert (asked.status_code, approved.status_code) == (201, 200)
    assert (approved.json()["kind"], approved.json()["user_id"]) == ("suspense_return", None)
    async with db.transaction() as session:
        held = await ledger.find_account(session, AccountKind.SUSPENSE, "USD")
        assert held is not None
        assert await ledger.get_balance(session, held.id) == 0
    assert (await deposit_row(db, deposit["provider_ref"]))["status"] == "returned"


async def test_a_second_release_of_one_deposit_is_a_conflict_over_http(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana, bruno = await admin(client, db, settings), await admin(client, db, settings)
    maria = await register_user(client)
    deposit = await suspended(db)
    body = {"reason": "for maria", "deposit_id": str(deposit["id"]), "user_id": maria.id}
    first = await client.post(f"{ADJUSTMENTS}/suspense-release", json=body, headers=keyed(ana))
    second = await client.post(f"{ADJUSTMENTS}/suspense-release", json=body, headers=keyed(ana))

    one = await client.post(f"{ADJUSTMENTS}/{first.json()['id']}/approve", headers=keyed(bruno))
    two = await client.post(f"{ADJUSTMENTS}/{second.json()['id']}/approve", headers=keyed(bruno))
    late = await client.post(f"{ADJUSTMENTS}/suspense-release", json=body, headers=keyed(ana))

    assert one.status_code == 200, one.text
    assert (two.status_code, two.json()["code"]) == (409, "deposit_not_in_suspense")
    assert (late.status_code, late.json()["code"]) == (409, "deposit_not_in_suspense")
    assert await balance(client, maria) == "75.00"


async def test_a_suspense_adjustment_names_a_deposit_and_not_an_amount(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana = await admin(client, db, settings)
    maria = await register_user(client)
    await suspended(db)

    by_amount = await client.post(
        f"{ADJUSTMENTS}/suspense-release",
        json={"reason": "for maria", "asset": "USD", "amount": "75.00", "user_id": maria.id},
        headers=keyed(ana),
    )
    unknown = await client.post(
        f"{ADJUSTMENTS}/suspense-return",
        json={"reason": "unclaimed", "deposit_id": str(uuid.uuid4())},
        headers=keyed(ana),
    )

    assert by_amount.status_code == 422
    assert (unknown.status_code, unknown.json()["code"]) == (404, "deposit_not_found")
    assert await rows(db, "SELECT 1 FROM ops_adjustments") == []


def with_legs(body: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**body, "legs": [{**body["legs"][0], **changes}, body["legs"][1]]}


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda body: with_legs(body, amount="24.99"), "invalid_adjustment"),
        (lambda body: with_legs(body, amount=25), "invalid_request"),
        (lambda body: with_legs(body, amount="25.001"), "invalid_amount"),
        (lambda body: with_legs(body, asset="EUR"), "unknown_asset"),
        (lambda body: with_legs(body, direction="D"), "invalid_request"),
        (lambda body: with_legs(body, account_id=str(uuid.uuid4())), "invalid_adjustment"),
        (lambda body: {**body, "reason": " "}, "invalid_adjustment"),
        (lambda body: {**body, "legs": body["legs"][:1]}, "invalid_adjustment"),
        (lambda body: {**body, "note": "extra"}, "invalid_request"),
    ],
    ids=[
        "unbalanced",
        "amount-as-a-number",
        "too-many-decimals",
        "unknown-asset",
        "direction",
        "unknown-account",
        "blank-reason",
        "one-leg",
        "unknown-field",
    ],
)
async def test_a_malformed_adjustment_is_refused(
    client: httpx.AsyncClient, db: Database, settings: Settings, change: Any, code: str
) -> None:
    ana = await admin(client, db, settings)
    maria = await register_user(client)

    response = await client.post(
        ADJUSTMENTS, json=change(await goodwill(db, maria)), headers=keyed(ana)
    )

    assert (response.status_code, response.json()["code"]) == (422, code)
    assert await rows(db, "SELECT 1 FROM ops_adjustments") == []


async def test_an_adjustment_that_does_not_exist_is_not_found(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    ana = await admin(client, db, settings)
    missing = uuid.uuid4()

    read = await client.get(f"{ADJUSTMENTS}/{missing}", headers=ana.headers)
    approved = await client.post(f"{ADJUSTMENTS}/{missing}/approve", headers=keyed(ana))

    assert (read.status_code, read.json()["code"]) == (404, "adjustment_not_found")
    assert (approved.status_code, approved.json()["code"]) == (404, "adjustment_not_found")


async def test_every_admin_route_is_behind_the_admin_dependency(app: FastAPI) -> None:
    """The service refuses a principal who is not an admin too, so a route that forgot the
    dependency would still answer 403. This is what notices that it forgot."""
    routes = [route for route in served_routes(app) if route.path.startswith("/v1/admin/")]

    unguarded = [
        (route.method, route.path)
        for route in routes
        if not any(getattr(call, "__name__", "") == "_require_admin" for call in route.calls)
    ]

    assert unguarded == []
    assert {(route.method, route.path) for route in routes} == {
        ("GET", "/v1/admin/recon/runs"),
        ("GET", "/v1/admin/recon/breaks"),
        ("GET", "/v1/admin/reviews"),
        ("POST", "/v1/admin/reviews/{review_id}/clear"),
        ("POST", "/v1/admin/reviews/{review_id}/reject"),
        ("GET", "/v1/admin/risk/denylist"),
        ("POST", "/v1/admin/risk/denylist"),
        ("GET", "/v1/admin/risk/limits"),
        ("PUT", "/v1/admin/risk/limits"),
        ("PUT", "/v1/admin/users/{user_id}/kyc-tier"),
        ("POST", "/v1/admin/users/{user_id}/role"),
        ("POST", "/v1/admin/users/{user_id}/close"),
        ("POST", "/v1/admin/users/{user_id}/restrict"),
        ("POST", "/v1/admin/users/{user_id}/lift-restriction"),
        ("GET", "/v1/admin/deposits/suspense"),
        ("GET", "/v1/admin/audit"),
        ("POST", "/v1/admin/recon/breaks/{break_id}/resolve"),
        ("GET", "/v1/admin/outbox/dead"),
        ("POST", "/v1/admin/outbox/dead/{event_id}/requeue"),
        ("GET", "/v1/admin/adjustments"),
        ("GET", "/v1/admin/adjustments/{adjustment_id}"),
        ("POST", "/v1/admin/adjustments"),
        ("POST", "/v1/admin/adjustments/suspense-release"),
        ("POST", "/v1/admin/adjustments/suspense-return"),
        ("POST", "/v1/admin/adjustments/{adjustment_id}/approve"),
        ("POST", "/v1/admin/adjustments/{adjustment_id}/reject"),
    }

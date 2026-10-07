"""The reconciliation admin endpoints: an admin lists the breaks and resolves one with a
note, nobody else can, and both are in the audit log."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest

from corridor import recon
from corridor.identity import Principal
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.errors import PermissionDenied
from corridor.platform.ids import new_id
from tests.ops.support import admin, audited
from tests.payments.support import rows
from tests.recon.test_schema import NOW, add_break, add_run
from tests.support.auth import register_user

BREAKS = "/v1/admin/recon/breaks"
RUNS = "/v1/admin/recon/runs"


async def some_breaks(db: Database, count: int, **changes: Any) -> list[str]:
    """Open breaks, oldest first, as their ids."""
    run_id = await add_run(db)
    return [
        str(await add_break(db, run_id, provider_ref=f"dep_{new_id()}", **changes))
        for _ in range(count)
    ]


def ordinary_user() -> Principal:
    return Principal.for_user(new_id(), "user", new_id())


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", BREAKS), ("POST", f"{BREAKS}/{uuid.uuid4()}/resolve")],
)
async def test_the_break_endpoints_need_an_administrator(
    client: httpx.AsyncClient, db: Database, method: str, path: str
) -> None:
    maria = await register_user(client)
    (break_id,) = await some_breaks(db, 1)
    path = path if method == "GET" else f"{BREAKS}/{break_id}/resolve"
    body = None if method == "GET" else {"note": "looked into it"}

    anonymous = await client.request(method, path, json=body)
    refused = await client.request(method, path, json=body, headers=maria.headers)

    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "unauthenticated")
    assert (refused.status_code, refused.json()["code"]) == (403, "permission_denied")
    assert await rows(db, "SELECT status FROM recon_breaks") == [{"status": "open"}]
    assert await audited(db, "recon.breaks_listed") == []


async def test_the_service_refuses_a_principal_who_is_not_an_admin_whatever_the_route_did(
    db: Database,
) -> None:
    (break_id,) = await some_breaks(db, 1)

    async with db.transaction() as session:
        with pytest.raises(PermissionDenied):
            await recon.list_breaks(session, ordinary_user())
        with pytest.raises(PermissionDenied):
            await recon.resolve_break(
                session, ordinary_user(), uuid.UUID(break_id), note="looked into it"
            )


async def test_an_admin_lists_the_breaks_newest_first(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    first, second = await some_breaks(db, 2, expected=250_00, actual=None)

    response = await client.get(BREAKS, headers=root.headers)

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [second, first]
    assert body["next_cursor"] is None
    newest = body["items"][0]
    assert {key: newest[key] for key in ("kind", "provider", "asset", "expected", "actual")} == {
        "kind": "unknown_deposit",
        "provider": "simbank",
        "asset": "USD",
        "expected": "250.00",
        "actual": None,
    }
    assert (newest["status"], newest["note"], newest["resolved_by"]) == ("open", None, None)


async def test_a_negative_balance_is_shown_with_its_sign(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    await some_breaks(db, 1, kind="settlement_balance", expected=10_00, actual=-25)

    (item,) = (await client.get(BREAKS, headers=root.headers)).json()["items"]

    assert (item["expected"], item["actual"]) == ("10.00", "-0.25")


async def test_the_breaks_are_paged_with_a_cursor(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    ids = await some_breaks(db, 5)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(BREAKS, params=params, headers=root.headers)).json()
        seen += [item["id"] for item in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert (seen, pages) == (ids[::-1], 3)


async def test_the_breaks_can_be_listed_by_status_and_a_cursor_belongs_to_its_list(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    open_ids = await some_breaks(db, 3)
    (resolved_id,) = await some_breaks(
        db, 1, status="resolved", resolved_by="system", resolved_at=NOW
    )

    resolved = await client.get(BREAKS, params={"status": "resolved"}, headers=root.headers)
    still_open = await client.get(
        BREAKS, params={"status": "open", "limit": 2}, headers=root.headers
    )
    crossed = await client.get(
        BREAKS,
        params={"status": "resolved", "cursor": still_open.json()["next_cursor"]},
        headers=root.headers,
    )
    unknown = await client.get(BREAKS, params={"status": "ignored"}, headers=root.headers)

    assert [item["id"] for item in resolved.json()["items"]] == [resolved_id]
    assert [item["id"] for item in still_open.json()["items"]] == open_ids[:0:-1]
    assert (crossed.status_code, crossed.json()["code"]) == (422, "invalid_cursor")
    assert unknown.status_code == 422


@pytest.mark.parametrize("params", [{"limit": 0}, {"cursor": "not-a-cursor"}])
async def test_a_bad_page_request_is_refused(
    client: httpx.AsyncClient, db: Database, settings: Settings, params: dict[str, Any]
) -> None:
    root = await admin(client, db, settings)

    response = await client.get(BREAKS, params=params, headers=root.headers)

    assert response.status_code == 422


async def test_listing_the_breaks_is_audited(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    await some_breaks(db, 2)

    await client.get(BREAKS, params={"status": "open"}, headers=root.headers)

    (event,) = await audited(db, "recon.breaks_listed")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert event["details"] == {"status": "open", "returned": 2}


async def test_an_admin_resolves_a_break_with_a_note(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    (break_id,) = await some_breaks(db, 1)

    response = await client.post(
        f"{BREAKS}/{break_id}/resolve",
        json={"note": "  The bank confirmed the deposit by phone.  "},
        headers=root.headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["id"], body["status"], body["resolved_by"]) == (break_id, "resolved", root.id)
    assert body["note"] == "The bank confirmed the deposit by phone."
    assert body["resolved_at"] is not None
    listed = await client.get(BREAKS, params={"status": "open"}, headers=root.headers)
    assert listed.json()["items"] == []
    (event,) = await audited(db, "recon.break_resolved")
    assert (event["actor_type"], event["actor_id"], event["resource_id"]) == (
        "admin",
        root.id,
        break_id,
    )
    assert event["details"]["note"] == "The bank confirmed the deposit by phone."


async def test_a_break_is_resolved_once(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root, other = await admin(client, db, settings), await admin(client, db, settings)
    (break_id,) = await some_breaks(db, 1)
    await client.post(f"{BREAKS}/{break_id}/resolve", json={"note": "first"}, headers=root.headers)

    again = await client.post(
        f"{BREAKS}/{break_id}/resolve", json={"note": "second"}, headers=other.headers
    )

    assert (again.status_code, again.json()["code"]) == (409, "recon_break_not_open")
    (item,) = (await client.get(BREAKS, headers=root.headers)).json()["items"]
    assert (item["note"], item["resolved_by"]) == ("first", root.id)
    assert len(await audited(db, "recon.break_resolved")) == 1


async def test_resolving_a_break_that_does_not_exist_is_not_found(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)

    response = await client.post(
        f"{BREAKS}/{uuid.uuid4()}/resolve", json={"note": "nothing there"}, headers=root.headers
    )

    assert (response.status_code, response.json()["code"]) == (404, "recon_break_not_found")


@pytest.mark.parametrize(
    "body", [{}, {"note": ""}, {"note": "   "}, {"note": "x" * 501}, {"note": "a", "extra": 1}]
)
async def test_a_resolution_needs_a_note_of_a_sensible_length(
    client: httpx.AsyncClient, db: Database, settings: Settings, body: dict[str, Any]
) -> None:
    root = await admin(client, db, settings)
    (break_id,) = await some_breaks(db, 1)

    response = await client.post(f"{BREAKS}/{break_id}/resolve", json=body, headers=root.headers)

    assert response.status_code == 422
    (item,) = (await client.get(BREAKS, headers=root.headers)).json()["items"]
    assert item["status"] == "open"


async def test_two_admins_resolving_a_break_at_once_resolve_it_once(db: Database) -> None:
    (break_id,) = await some_breaks(db, 1)
    admins = [Principal.for_user(new_id(), "admin", new_id()) for _ in range(6)]

    async def resolve(principal: Principal) -> str:
        try:
            await db.run(
                lambda session: recon.resolve_break(
                    session, principal, uuid.UUID(break_id), note=f"by {principal.user_id}"
                )
            )
        except recon.BreakNotOpen:
            return "refused"
        return "resolved"

    outcomes = await asyncio.gather(*(resolve(principal) for principal in admins))

    assert sorted(outcomes) == ["refused"] * 5 + ["resolved"]
    assert len(await audited(db, "recon.break_resolved")) == 1


# --- runs ------------------------------------------------------------------------------------


async def test_the_runs_endpoint_needs_an_administrator(
    client: httpx.AsyncClient, db: Database
) -> None:
    maria = await register_user(client)
    await add_run(db)

    anonymous = await client.get(RUNS)
    refused = await client.get(RUNS, headers=maria.headers)

    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "unauthenticated")
    assert (refused.status_code, refused.json()["code"]) == (403, "permission_denied")
    assert await audited(db, "recon.runs_listed") == []
    async with db.transaction() as session:
        with pytest.raises(PermissionDenied):
            await recon.list_runs(session, ordinary_user())


async def test_an_admin_lists_the_runs_newest_first_a_page_at_a_time(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    ids = [str(await add_run(db)) for _ in range(4)]
    ids.append(str(await add_run(db, status="incomplete", breaks_found=3, breaks_opened=2)))

    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(RUNS, params=params, headers=root.headers)).json()
        seen += page["items"]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert ([item["id"] for item in seen], pages) == (ids[::-1], 3)
    newest = seen[0]
    assert (newest["status"], newest["breaks_found"], newest["breaks_opened"]) == (
        "incomplete",
        3,
        2,
    )
    assert newest["window_start"] < newest["window_end"]
    listings = await audited(db, "recon.runs_listed")
    assert [event["details"] for event in listings] == [
        {"returned": 2},
        {"returned": 2},
        {"returned": 1},
    ]
    assert {event["actor_id"] for event in listings} == {root.id}


async def test_a_cursor_of_the_breaks_is_not_one_of_the_runs(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    await some_breaks(db, 2)
    of_breaks = (await client.get(BREAKS, params={"limit": 1}, headers=root.headers)).json()

    crossed = await client.get(
        RUNS, params={"cursor": of_breaks["next_cursor"]}, headers=root.headers
    )
    none = await client.get(RUNS, params={"limit": 0}, headers=root.headers)

    assert (crossed.status_code, crossed.json()["code"]) == (422, "invalid_cursor")
    assert none.status_code == 422

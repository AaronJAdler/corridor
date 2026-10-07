"""Dead-letter administration: an admin lists the events that ran out of attempts and
requeues one, nobody else can, and both are in the audit log."""

import uuid
from typing import Any

import httpx
import pytest

from corridor import ops
from corridor.identity import Principal
from corridor.outbox import EventStatus
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.errors import PermissionDenied
from corridor.platform.ids import new_id
from corridor.platform.pagination import encode_cursor
from tests.ops.support import admin, audited
from tests.outbox.helpers import TOPIC, Recorder, dispatcher_for, enqueue_event, load
from tests.support.auth import register_user

DEAD = "/v1/admin/outbox/dead"


async def dead_event(db: Database, settings: Settings, **payload: Any) -> uuid.UUID:
    """An event that died the way one does: a worker claimed it and had no handler."""
    event_id = await enqueue_event(db, payload=payload)
    await dispatcher_for(db, settings, {}).run_once()
    assert (await load(db, event_id)).status is EventStatus.DEAD
    return event_id


def ordinary_user() -> Principal:
    return Principal.for_user(new_id(), "user", new_id())


async def test_the_dead_letter_endpoints_need_an_administrator(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    maria = await register_user(client)
    event_id = await dead_event(db, settings)

    for method, path in (("GET", DEAD), ("POST", f"{DEAD}/{event_id}/requeue")):
        anonymous = await client.request(method, path)
        refused = await client.request(method, path, headers=maria.headers)

        assert (anonymous.status_code, anonymous.json()["code"]) == (401, "unauthenticated")
        assert (refused.status_code, refused.json()["code"]) == (403, "permission_denied")
    assert (await load(db, event_id)).status is EventStatus.DEAD


async def test_the_service_refuses_a_principal_who_is_not_an_admin_whatever_the_route_did(
    db: Database, settings: Settings
) -> None:
    event_id = await dead_event(db, settings)

    async with db.transaction() as session:
        with pytest.raises(PermissionDenied):
            await ops.list_dead_letters(session, ordinary_user())
        with pytest.raises(PermissionDenied):
            await ops.requeue_dead_letter(session, ordinary_user(), event_id)

    assert (await load(db, event_id)).status is EventStatus.DEAD


async def test_an_admin_lists_the_dead_events_newest_first_and_no_others(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    first = await dead_event(db, settings, number=1)
    second = await dead_event(db, settings, number=2)
    await enqueue_event(db)

    response = await client.get(DEAD, headers=root.headers)

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(second), str(first)]
    assert body["next_cursor"] is None
    newest = body["items"][0]
    assert (newest["topic"], newest["payload"], newest["status"]) == (TOPIC, {"number": 2}, "dead")
    assert newest["attempts"] == 1
    assert newest["last_error"]
    assert newest["finished_at"] is not None


async def test_the_dead_events_are_paged_with_a_cursor(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    ids = [str(await dead_event(db, settings)) for _ in range(5)]

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(DEAD, params=params, headers=root.headers)).json()
        seen += [item["id"] for item in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert (seen, pages) == (ids[::-1], 3)


@pytest.mark.parametrize("params", [{"limit": 0}, {"cursor": "not-a-cursor"}])
async def test_a_bad_page_request_is_refused(
    client: httpx.AsyncClient, db: Database, settings: Settings, params: dict[str, Any]
) -> None:
    root = await admin(client, db, settings)

    response = await client.get(DEAD, params=params, headers=root.headers)

    assert response.status_code == 422


async def test_a_cursor_from_another_list_is_refused(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    foreign = encode_cursor(kind="recon_breaks", scope="all", position=str(new_id()))

    response = await client.get(DEAD, params={"cursor": foreign}, headers=root.headers)

    assert (response.status_code, response.json()["code"]) == (422, "invalid_cursor")


async def test_listing_the_dead_events_is_audited(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    await dead_event(db, settings)

    await client.get(DEAD, headers=root.headers)

    (event,) = await audited(db, "outbox.dead_listed")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert event["details"] == {"returned": 1}


async def test_an_admin_requeues_a_dead_event_and_a_worker_then_handles_it(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    event_id = await dead_event(db, settings, number=7)

    response = await client.post(f"{DEAD}/{event_id}/requeue", headers=root.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["id"], body["status"], body["attempts"]) == (str(event_id), "pending", 0)
    assert body["finished_at"] is None
    assert (await client.get(DEAD, headers=root.headers)).json()["items"] == []
    handled = Recorder()
    assert await dispatcher_for(db, settings, {TOPIC: handled}).run_once() == 1
    assert handled.ids == [event_id]
    assert (await load(db, event_id)).status is EventStatus.DONE


async def test_requeueing_is_audited(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    event_id = await dead_event(db, settings)

    await client.post(f"{DEAD}/{event_id}/requeue", headers=root.headers)

    (event,) = await audited(db, "outbox.dead_requeued")
    assert (event["actor_type"], event["actor_id"]) == ("admin", root.id)
    assert (event["resource_type"], event["resource_id"]) == ("outbox_event", str(event_id))
    assert event["details"]["topic"] == TOPIC


async def test_an_event_that_is_not_dead_cannot_be_requeued(
    client: httpx.AsyncClient, db: Database, settings: Settings
) -> None:
    root = await admin(client, db, settings)
    revived = await dead_event(db, settings)
    await client.post(f"{DEAD}/{revived}/requeue", headers=root.headers)
    waiting = await enqueue_event(db)

    for event_id in (waiting, revived, uuid.uuid4()):
        response = await client.post(f"{DEAD}/{event_id}/requeue", headers=root.headers)

        assert (response.status_code, response.json()["code"]) == (404, "dead_letter_not_found")
    assert len(await audited(db, "outbox.dead_requeued")) == 1

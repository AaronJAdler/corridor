"""The idempotency layer: a request that carries a key is performed at most once, and every
later request with that key gets the first one's answer."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from corridor.api.deps import Db
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.api.idempotency import (
    IdempotencyKey,
    IdempotencyKeyReused,
    RequestInProgress,
    StoredResponse,
    fingerprint,
    purge_expired,
    run_idempotent,
    to_response,
)
from corridor.api.middleware import route_template
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.db import Database, lock_key
from corridor.platform.errors import Conflict
from corridor.platform.ids import new_id
from tests.support import postgres

ROUTE = "/probe/charges"
ACTOR_HEADER = "X-Test-Actor"
LOCK_TIMEOUT_HEADER = "X-Test-Lock-Timeout-Ms"

Work = Callable[[AsyncSession], Awaitable[StoredResponse]]


class Declined(Conflict):
    code = "declined"
    title = "Declined"


@pytest.fixture(autouse=True)
async def _probe_table(owner_db: Database) -> None:
    """A table for the work to write to, so a test can count how often the work ran and
    whether what it wrote survived."""
    async with owner_db.transaction() as session:
        await session.execute(
            text("CREATE TABLE idem_probe (id uuid PRIMARY KEY, note text NOT NULL)")
        )


def charging(note: str = "charged", *, fail: str | None = None) -> Work:
    """Work that writes one probe row and then succeeds, is declined, or crashes."""

    async def work(session: AsyncSession) -> StoredResponse:
        charge = new_id()
        await session.execute(
            text("INSERT INTO idem_probe (id, note) VALUES (:id, :note)"),
            {"id": charge, "note": note},
        )
        if fail == "declined":
            raise Declined("The charge was declined.", headers={"X-Decline": "yes"}, reason="funds")
        if fail == "crash":
            raise RuntimeError("the work broke")
        return StoredResponse(201, {"id": str(charge), "note": note}, {"Location": f"/c/{charge}"})

    return work


async def call(
    db: Database,
    work: Work,
    *,
    actor: uuid.UUID,
    key: str = "key-1",
    body: bytes | dict[str, Any] = b'{"amount":"5.00"}',
    lock_timeout_ms: int | None = None,
) -> tuple[StoredResponse, bool]:
    return await run_idempotent(
        db,
        actor_id=actor,
        key=key,
        method="POST",
        route=ROUTE,
        body=body,
        work=work,
        lock_timeout_ms=lock_timeout_ms,
    )


async def count(db: Database, table: str) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


async def holding_key_lock(
    database: postgres.TestDatabase, actor: uuid.UUID, key: str
) -> asyncpg.Connection:
    """Another connection that holds the key's lock, as a request still in flight would."""
    dsn = database.app_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(dsn)
    await connection.execute("SELECT pg_advisory_lock($1)", lock_key("idem", f"{actor}:{key}"))
    return connection


async def charge_endpoint(request: Request, db: Db, key: IdempotencyKey) -> JSONResponse:
    """A route as a real one would be written, with the actor taken from a test header."""
    raw = await request.body()
    wanted = json.loads(raw)
    timeout = request.headers.get(LOCK_TIMEOUT_HEADER)
    stored, replayed = await run_idempotent(
        db,
        actor_id=uuid.UUID(request.headers[ACTOR_HEADER]),
        key=key,
        method=request.method,
        route=route_template(request.scope),
        body=raw,
        work=charging(wanted.get("note", "charged"), fail=wanted.get("fail")),
        lock_timeout_ms=int(timeout) if timeout is not None else None,
    )
    return to_response(stored, replayed, request)


@pytest.fixture
async def http(app: FastAPI, client: httpx.AsyncClient) -> httpx.AsyncClient:
    app.add_api_route(ROUTE, charge_endpoint, methods=["POST"])
    return client


def headers(actor: uuid.UUID, key: str | None = "key-1", **more: str) -> dict[str, str]:
    result = {ACTOR_HEADER: str(actor), **more}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


async def test_the_first_request_runs_the_work_once_and_stores_its_response(db: Database) -> None:
    actor = new_id()

    stored, replayed = await call(db, charging(), actor=actor)

    assert replayed is False
    assert stored.status_code == 201
    assert stored.body["note"] == "charged"
    assert await count(db, "idem_probe") == 1
    async with db.transaction() as session:
        row = (
            await session.execute(
                text(
                    "SELECT actor_id, key, fingerprint, status_code, response_body,"
                    " response_headers, created_at, completed_at FROM idempotency_keys"
                )
            )
        ).one()
    assert (row.actor_id, row.key, row.status_code) == (actor, "key-1", 201)
    assert row.fingerprint == fingerprint("POST", ROUTE, b'{"amount":"5.00"}')
    assert row.response_body == stored.body
    assert row.response_headers == dict(stored.headers)
    assert row.completed_at is not None
    assert row.created_at <= row.completed_at


async def test_a_repeated_request_gets_the_stored_response_and_the_work_does_not_run(
    db: Database,
) -> None:
    actor = new_id()
    first, _ = await call(db, charging(), actor=actor)

    second, replayed = await call(db, charging("must not run"), actor=actor)

    assert replayed is True
    assert second == first
    assert await count(db, "idem_probe") == 1


async def test_a_replay_over_http_is_identical_and_says_it_is_a_replay(
    http: httpx.AsyncClient,
) -> None:
    actor = new_id()
    first = await http.post(ROUTE, headers=headers(actor), json={"note": "a"})
    second = await http.post(ROUTE, headers=headers(actor), json={"note": "a"})

    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["Location"] == first.headers["Location"]
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"


async def test_50_concurrent_identical_requests_run_the_work_once_and_all_get_one_response(
    db: Database,
) -> None:
    actor = new_id()

    outcomes = await asyncio.gather(*(call(db, charging(), actor=actor) for _ in range(50)))

    assert await count(db, "idem_probe") == 1
    assert await count(db, "idempotency_keys") == 1
    assert len({json.dumps(stored.body, sort_keys=True) for stored, _ in outcomes}) == 1
    assert {stored.status_code for stored, _ in outcomes} == {201}
    assert [replayed for _, replayed in outcomes].count(False) == 1


async def test_a_key_sent_again_with_a_different_body_is_refused(db: Database) -> None:
    actor = new_id()
    await call(db, charging(), actor=actor, body=b'{"amount":"5.00"}')

    with pytest.raises(IdempotencyKeyReused) as refused:
        await call(db, charging(), actor=actor, body=b'{"amount":"6.00"}')

    assert (refused.value.status, refused.value.code) == (422, "idempotency_key_reused")
    assert await count(db, "idem_probe") == 1


def test_the_fingerprint_covers_the_method_and_the_route_template() -> None:
    body = {"amount": "5.00"}
    base = fingerprint("POST", "/v1/transfers", body)

    assert len(base) == 64
    assert fingerprint("post", "/v1/transfers", body) == base
    assert fingerprint("PUT", "/v1/transfers", body) != base
    assert fingerprint("POST", "/v1/withdrawals", body) != base


def test_the_fingerprint_ignores_key_order_and_whitespace() -> None:
    compact = b'{"amount":"5.00","to":{"a":1,"b":[1,2]}}'
    spaced = b'{ "to" : {"b": [1, 2],\n "a": 1},\t"amount": "5.00" }'

    assert fingerprint("POST", ROUTE, spaced) == fingerprint("POST", ROUTE, compact)
    assert fingerprint("POST", ROUTE, json.loads(compact)) == fingerprint("POST", ROUTE, compact)
    assert fingerprint("POST", ROUTE, b'{"amount":"5.0"}') != fingerprint("POST", ROUTE, compact)


async def test_a_body_that_differs_only_in_layout_is_the_same_request(db: Database) -> None:
    actor = new_id()
    first, _ = await call(db, charging(), actor=actor, body=b'{"a":1,"b":2}')

    second, replayed = await call(db, charging(), actor=actor, body={"b": 2, "a": 1})

    assert replayed is True
    assert second == first


async def test_the_same_key_from_another_actor_is_a_request_of_its_own(db: Database) -> None:
    first, _ = await call(db, charging(), actor=new_id())
    second, replayed = await call(db, charging(), actor=new_id())

    assert replayed is False
    assert second.body["id"] != first.body["id"]
    assert await count(db, "idem_probe") == 2


async def test_a_domain_error_is_stored_without_the_partial_work_and_replayed(
    db: Database,
) -> None:
    actor = new_id()

    stored, replayed = await call(db, charging(fail="declined"), actor=actor)

    assert replayed is False
    assert stored.status_code == 409
    assert stored.body == {
        "type": "https://corridor.example/problems/declined",
        "title": "Declined",
        "status": 409,
        "code": "declined",
        "detail": "The charge was declined.",
        "reason": "funds",
    }
    assert stored.headers["X-Decline"] == "yes"
    # The row the work wrote before it was declined is gone; the key is not.
    assert await count(db, "idem_probe") == 0
    assert await count(db, "idempotency_keys") == 1

    again, replayed = await call(db, charging("would succeed now"), actor=actor)

    assert replayed is True
    assert again == stored
    assert await count(db, "idem_probe") == 0


async def test_a_stored_domain_error_is_rendered_as_a_problem_document(
    http: httpx.AsyncClient,
) -> None:
    actor = new_id()
    first = await http.post(ROUTE, headers=headers(actor), json={"fail": "declined"})
    second = await http.post(ROUTE, headers=headers(actor), json={"fail": "declined"})

    for response in (first, second):
        assert response.status_code == 409
        assert response.headers["Content-Type"] == PROBLEM_CONTENT_TYPE
        assert response.json()["code"] == "declined"
        # The id of the request being answered, not of the one that was stored.
        assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert first.json()["request_id"] != second.json()["request_id"]
    assert second.headers["Idempotent-Replayed"] == "true"


async def test_an_unexpected_error_leaves_no_key_and_a_retry_runs(db: Database) -> None:
    actor = new_id()

    with pytest.raises(RuntimeError, match="the work broke"):
        await call(db, charging(fail="crash"), actor=actor)

    assert await count(db, "idempotency_keys") == 0
    assert await count(db, "idem_probe") == 0

    stored, replayed = await call(db, charging(), actor=actor)

    assert (stored.status_code, replayed) == (201, False)
    assert await count(db, "idem_probe") == 1


async def test_a_key_whose_lock_is_held_is_refused_as_in_progress(
    db: Database, database: postgres.TestDatabase
) -> None:
    actor = new_id()
    holder = await holding_key_lock(database, actor, "key-1")
    try:
        with pytest.raises(RequestInProgress) as refused:
            await call(db, charging(), actor=actor, lock_timeout_ms=100)
    finally:
        await holder.close()

    assert (refused.value.status, refused.value.code) == (409, "request_in_progress")
    assert refused.value.headers == {"Retry-After": "1"}
    assert await count(db, "idem_probe") == 0
    # Nothing was stored, so the client's retry is performed.
    _, replayed = await call(db, charging(), actor=actor, lock_timeout_ms=100)
    assert replayed is False


async def test_a_held_lock_is_a_409_with_retry_after_over_http(
    http: httpx.AsyncClient, database: postgres.TestDatabase
) -> None:
    actor = new_id()
    holder = await holding_key_lock(database, actor, "key-1")
    try:
        response = await http.post(
            ROUTE, headers=headers(actor, **{LOCK_TIMEOUT_HEADER: "100"}), json={}
        )
    finally:
        await holder.close()

    assert response.status_code == 409
    assert response.json()["code"] == "request_in_progress"
    assert response.headers["Retry-After"] == "1"


async def test_the_short_wait_for_the_key_does_not_shorten_the_works_own_lock_waits(
    db: Database,
) -> None:
    seen: list[str] = []

    async def work(session: AsyncSession) -> StoredResponse:
        seen.append((await session.execute(text("SHOW lock_timeout"))).scalar_one())
        return StoredResponse(200, {}, {})

    async with db.transaction() as session:
        configured = (await session.execute(text("SHOW lock_timeout"))).scalar_one()
    await call(db, work, actor=new_id(), lock_timeout_ms=100)

    assert seen == [configured]


async def test_a_key_row_without_an_outcome_is_reported_as_in_progress(db: Database) -> None:
    actor = new_id()
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO idempotency_keys (actor_id, key, fingerprint, created_at)"
                " VALUES (:actor, 'key-1', :fingerprint, :now)"
            ),
            {
                "actor": actor,
                "fingerprint": fingerprint("POST", ROUTE, b'{"amount":"5.00"}'),
                "now": utcnow(),
            },
        )

    with pytest.raises(RequestInProgress):
        await call(db, charging(), actor=actor)

    assert await count(db, "idem_probe") == 0


async def test_a_request_without_a_key_is_refused(http: httpx.AsyncClient, db: Database) -> None:
    response = await http.post(ROUTE, headers=headers(new_id(), key=None), json={})

    assert response.status_code == 400
    assert response.json()["code"] == "idempotency_key_required"
    assert await count(db, "idem_probe") == 0


@pytest.mark.parametrize("key", ["has space", "x" * 256, "café", "tab\there", "\x7f"], ids=repr)
async def test_a_key_that_is_not_1_to_255_visible_ascii_characters_is_refused(
    http: httpx.AsyncClient, db: Database, key: str
) -> None:
    response = await http.post(
        ROUTE,
        headers={ACTOR_HEADER: str(new_id())} | {"Idempotency-Key": key.encode("latin-1")},  # type: ignore[dict-item]
        json={},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_idempotency_key"
    assert await count(db, "idem_probe") == 0


async def test_an_empty_key_is_refused(http: httpx.AsyncClient) -> None:
    response = await http.post(ROUTE, headers=headers(new_id(), key=""), json={})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_idempotency_key"


@pytest.mark.parametrize("key", ["k", "x" * 255, "!~{}:/=+.-_"], ids=["one", "longest", "marks"])
async def test_a_key_of_visible_ascii_within_the_length_limit_is_accepted(
    http: httpx.AsyncClient, key: str
) -> None:
    response = await http.post(ROUTE, headers=headers(new_id(), key=key), json={})

    assert response.status_code == 201


async def test_the_purge_deletes_keys_older_than_the_cutoff_and_only_those(
    db: Database, clock: ManualClock
) -> None:
    actor = new_id()
    await call(db, charging(), actor=actor, key="old")
    clock.advance(seconds=25 * 3600)
    await call(db, charging(), actor=actor, key="recent")

    async with db.transaction() as session:
        deleted = await purge_expired(session, older_than=utcnow() - timedelta(hours=24))

    assert deleted == 1
    # The recent key still replays; the purged one is a new request again.
    assert (await call(db, charging(), actor=actor, key="recent"))[1] is True
    assert (await call(db, charging(), actor=actor, key="old"))[1] is False

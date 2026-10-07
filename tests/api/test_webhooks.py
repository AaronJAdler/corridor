"""Inbound webhooks over HTTP: verified, stored once, acknowledged after the commit.

Deliveries come from two senders. One is the real simulator, wired straight into the API,
so that what Corridor verifies is what the provider really signs. The other is this file,
signing by hand from the contract's text, for everything a well-behaved provider never sends.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import webhooks
from corridor.api.app import create_app
from corridor.api.deps import PUBLIC_ROUTES, get_principal
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.redis import RedisStore
from corridor_sim.app import create_app as create_sim
from corridor_sim.settings import SimSettings
from tests.support.auth import bearer, served_routes
from tests.webhooks.helpers import (
    BANK_NEXT_SECRET,
    BANK_SECRET,
    CUSTODY_SECRET,
    UNKNOWN_SECRET,
    enqueued,
    envelope,
    sign,
    stored_events,
    unix,
    with_secrets,
    without_secrets,
)

BANK = "/v1/webhooks/simbank"
CUSTODY = "/v1/webhooks/simcustody"
BASE_URL = "http://corridor.test"
SIM_API_KEY = "sim-test-api-key-of-32-characters-or-more"  # pragma: allowlist secret
REQUEST_ID = "test-request-0001"
MAX_BODY_BYTES = 64 * 1024

# What every refused signature gets, whatever was wrong with it.
REFUSED = {
    "type": "https://corridor.example/problems/invalid-signature",
    "title": "Invalid signature",
    "status": 401,
    "code": "invalid_signature",
    "request_id": REQUEST_ID,
}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The suite's settings, with a signing secret for each provider."""
    return with_secrets(settings)


@asynccontextmanager
async def serving(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """An API of its own, for a test about settings other than the fixture's."""
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            yield http


class Provider:
    """The real simulator, delivering its webhooks into the API under test."""

    def __init__(self, control: httpx.AsyncClient) -> None:
        self._control = control

    async def control(self, method: str, path: str, body: object = None) -> Any:
        response = await self._control.request(method, f"/_control{path}", json=body)
        assert response.status_code in (200, 201), response.text
        return response.json()

    async def bank_deposit(self) -> None:
        # The bank accepts a deposit to an account it never issued, and announces it.
        await self.control(
            "POST",
            "/bank/deposits",
            {
                "virtual_account_id": "va_unknown",
                "amount": "250.00",
                "sender_name": "Joao Souza",
                "reference": "INV-2041",
            },
        )

    async def chain_deposit(self) -> None:
        issued = await self._control.post(
            "/custody/v1/addresses",
            json={"customer_reference": "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10", "asset": "USDC"},
            headers=bearer(SIM_API_KEY),
        )
        assert issued.status_code in (200, 201), issued.text
        await self.control(
            "POST",
            "/custody/deposits",
            {
                "address": issued.json()["address"],
                "amount": "10.000000",
                "from_address": issued.json()["address"],
            },
        )

    async def deliver(self) -> list[dict[str, Any]]:
        deliveries: list[dict[str, Any]] = (await self.control("POST", "/webhooks/deliver"))[
            "deliveries"
        ]
        return deliveries


@pytest.fixture
async def provider(client: httpx.AsyncClient, clock: ManualClock) -> AsyncIterator[Provider]:
    sim_settings = SimSettings(
        _env_file=None,
        api_key=SecretStr(SIM_API_KEY),
        bank_webhook_url=BASE_URL + BANK,
        custody_webhook_url=BASE_URL + CUSTODY,
        bank_webhook_secret=SecretStr(BANK_SECRET),
        custody_webhook_secret=SecretStr(CUSTODY_SECRET),
        clock_mode="manual",
        start_time=clock.now(),
    )
    simulator = create_sim(sim_settings, webhook_client=client)
    async with simulator.router.lifespan_context(simulator):
        transport = httpx.ASGITransport(app=simulator)
        async with httpx.AsyncClient(transport=transport, base_url="http://sim.test") as control:
            yield Provider(control)


async def deliver(
    client: httpx.AsyncClient,
    body: bytes,
    signature: str | None,
    *,
    path: str = BANK,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    sent = {"Content-Type": "application/json", "X-Request-ID": REQUEST_ID, **(headers or {})}
    if signature is not None:
        sent["X-Signature"] = signature
    return await client.post(path, content=body, headers=sent)


# --- the provider's own deliveries -----------------------------------------------------------


async def test_a_delivery_from_the_bank_is_stored_enqueued_and_acknowledged(
    provider: Provider, db: Database, clock: ManualClock
) -> None:
    await provider.bank_deposit()

    (delivery,) = await provider.deliver()

    assert (delivery["status_code"], delivery["event_status"]) == (200, "delivered")
    (stored,) = await stored_events(db)
    assert (stored["provider"], stored["event_id"]) == ("simbank", delivery["event_id"])
    assert (stored["type"], stored["received_at"]) == ("deposit.received", clock.now())
    assert stored["payload"]["data"]["amount"] == "250.00"
    assert (stored["processed_at"], stored["outcome"]) == (None, None)
    assert await enqueued(db) == [stored["id"]]


async def test_a_delivery_from_the_custodian_is_verified_with_the_custodians_secret(
    provider: Provider, db: Database
) -> None:
    await provider.chain_deposit()

    (delivery,) = await provider.deliver()

    assert delivery["status_code"] == 200
    (stored,) = await stored_events(db)
    assert (stored["provider"], stored["type"]) == ("simcustody", "deposit.detected")


async def test_an_event_the_provider_sends_again_is_acknowledged_and_stored_once(
    provider: Provider, db: Database
) -> None:
    await provider.control(
        "POST",
        "/webhooks/behaviour",
        {"duplicates": 2, "drop_types": [], "hold": False, "reverse": False},
    )
    await provider.bank_deposit()

    deliveries = await provider.deliver()

    assert [delivery["status_code"] for delivery in deliveries] == [200, 200, 200]
    (stored,) = await stored_events(db)
    assert await enqueued(db) == [stored["id"]]


async def test_the_provider_may_deliver_minutes_late_within_the_tolerance(
    provider: Provider, db: Database, clock: ManualClock
) -> None:
    await provider.bank_deposit()
    clock.advance(seconds=300)

    (delivery,) = await provider.deliver()

    assert delivery["status_code"] == 200
    assert len(await stored_events(db)) == 1


async def test_a_delivery_the_provider_signed_too_long_ago_is_refused(
    provider: Provider, db: Database, clock: ManualClock
) -> None:
    await provider.bank_deposit()
    clock.advance(seconds=301)

    (delivery,) = await provider.deliver()

    assert delivery["status_code"] == 401
    assert await stored_events(db) == []


# --- deliveries signed by hand ---------------------------------------------------------------


async def test_a_signed_delivery_is_answered_received_once_it_is_stored(
    client: httpx.AsyncClient, db: Database
) -> None:
    body = envelope("evt_1", "payout.completed", {"payout_id": "po_1"})

    response = await deliver(client, body, sign(BANK_SECRET, body))

    assert (response.status_code, response.json()) == (200, {"received": True})
    (stored,) = await stored_events(db)
    assert stored["payload"] == json.loads(body)
    assert await enqueued(db) == [stored["id"]]


async def test_a_replayed_event_gets_the_same_answer_and_changes_nothing(
    client: httpx.AsyncClient, db: Database, clock: ManualClock
) -> None:
    body = envelope("evt_1")
    first = await deliver(client, body, sign(BANK_SECRET, body))
    before = await stored_events(db)
    clock.advance(seconds=30)
    changed = envelope("evt_1", "payout.failed", {"payout_id": "po_other"})

    again = await deliver(client, body, sign(BANK_SECRET, body))
    reused = await deliver(client, changed, sign(BANK_SECRET, changed))

    assert (again.status_code, again.json()) == (first.status_code, first.json())
    assert (reused.status_code, reused.json()) == (200, {"received": True})
    assert await stored_events(db) == before
    assert len(await enqueued(db)) == 1


async def test_fifty_concurrent_deliveries_of_one_event_store_it_once(
    client: httpx.AsyncClient, db: Database
) -> None:
    body = envelope("evt_1")
    signature = sign(BANK_SECRET, body)

    responses = await asyncio.gather(*(deliver(client, body, signature) for _ in range(50)))

    assert {response.status_code for response in responses} == {200}
    assert len(await stored_events(db)) == 1
    assert len(await enqueued(db)) == 1


async def test_each_provider_has_event_ids_of_its_own(
    client: httpx.AsyncClient, db: Database
) -> None:
    body = envelope("evt_1")

    await deliver(client, body, sign(BANK_SECRET, body), path=BANK)
    await deliver(client, body, sign(CUSTODY_SECRET, body), path=CUSTODY)

    assert [row["provider"] for row in await stored_events(db)] == ["simbank", "simcustody"]


def _digest_of(signature: str) -> str:
    return signature.partition(",v1=")[2]


async def test_every_refused_signature_gets_one_and_the_same_answer(
    client: httpx.AsyncClient, db: Database, clock: ManualClock
) -> None:
    body = envelope("evt_1")
    good = sign(BANK_SECRET, body)
    now = unix()
    attempts: dict[str, tuple[bytes, str | None]] = {
        "a wrong digest": (body, f"t={now},v1={'0' * 64}"),
        "an unknown secret": (body, sign(UNKNOWN_SECRET, body)),
        "the other provider's secret": (body, sign(CUSTODY_SECRET, body)),
        "no header": (body, None),
        "an empty header": (body, ""),
        "a duplicated timestamp": (body, f"t={now},{good}"),
        "a duplicated digest": (body, f"{good},v1={_digest_of(good)}"),
        "an unknown field": (body, f"{good},v0={_digest_of(good)}"),
        "no timestamp": (body, f"v1={_digest_of(good)}"),
        "a stale timestamp": (body, sign(BANK_SECRET, body, now - 301)),
        "a future timestamp": (body, sign(BANK_SECRET, body, now + 301)),
        "a timestamp moved after signing": (body, f"t={now + 1},v1={_digest_of(good)}"),
        "a body altered after signing": (body.replace(b"100.00", b"900.00"), good),
        "a body with a byte appended": (body + b" ", good),
        "an unsigned body that is not an event": (b"not json", f"t={now},v1={'0' * 64}"),
    }

    answers = {
        what: await deliver(client, sent, signature) for what, (sent, signature) in attempts.items()
    }

    assert {what: answer.status_code for what, answer in answers.items()} == dict.fromkeys(
        attempts, 401
    )
    assert {what: answer.json() for what, answer in answers.items()} == dict.fromkeys(
        attempts, REFUSED
    )
    assert len({answer.content for answer in answers.values()}) == 1
    assert len({answer.headers["content-type"] for answer in answers.values()}) == 1
    assert all("www-authenticate" not in answer.headers for answer in answers.values())
    assert await stored_events(db) == []
    assert await enqueued(db) == []


async def test_a_signature_header_sent_twice_is_refused(
    client: httpx.AsyncClient, db: Database
) -> None:
    body = envelope("evt_1")
    good = sign(BANK_SECRET, body)

    response = await client.post(
        BANK,
        content=body,
        headers=[("X-Request-ID", REQUEST_ID), ("X-Signature", good), ("X-Signature", good)],
    )

    assert (response.status_code, response.json()) == (401, REFUSED)
    assert await stored_events(db) == []


async def test_a_timestamp_five_minutes_stale_by_our_clock_is_refused_after_being_accepted(
    client: httpx.AsyncClient, db: Database, clock: ManualClock
) -> None:
    first, second = envelope("evt_1"), envelope("evt_2")
    signed_at = unix()
    clock.advance(seconds=300)
    accepted = await deliver(client, first, sign(BANK_SECRET, first, signed_at))
    clock.advance(seconds=1)

    refused = await deliver(client, second, sign(BANK_SECRET, second, signed_at))

    assert accepted.status_code == 200
    assert (refused.status_code, refused.json()) == (401, REFUSED)
    assert [row["event_id"] for row in await stored_events(db)] == ["evt_1"]


async def test_a_timestamp_from_the_future_is_accepted_once_our_clock_is_near_it(
    client: httpx.AsyncClient, db: Database, clock: ManualClock
) -> None:
    body = envelope("evt_1")
    signature = sign(BANK_SECRET, body, unix(clock.now() + timedelta(seconds=301)))
    refused = await deliver(client, body, signature)
    clock.advance(seconds=1)

    accepted = await deliver(client, body, signature)

    assert (refused.status_code, refused.json()) == (401, REFUSED)
    assert accepted.status_code == 200


async def test_a_provider_with_no_secret_configured_accepts_nothing(
    settings: Settings, db: Database, clock: ManualClock
) -> None:
    body = envelope("evt_1")
    signed_with_nothing = sign("", body)

    async with serving(without_secrets(settings)) as http:
        unsigned = await deliver(http, body, None)
        empty_key = await deliver(http, body, signed_with_nothing)
        other = await deliver(http, body, sign(CUSTODY_SECRET, body))

    for response in (unsigned, empty_key, other):
        assert (response.status_code, response.json()) == (401, REFUSED)
    assert await stored_events(db) == []


async def test_during_a_rotation_both_secrets_are_accepted(
    settings: Settings, db: Database
) -> None:
    rotating = with_secrets(settings, bank=(BANK_SECRET, BANK_NEXT_SECRET))
    old, new = envelope("evt_old"), envelope("evt_new")

    async with serving(rotating) as http:
        with_old = await deliver(http, old, sign(BANK_SECRET, old))
        with_new = await deliver(http, new, sign(BANK_NEXT_SECRET, new))

    assert (with_old.status_code, with_new.status_code) == (200, 200)
    assert [row["event_id"] for row in await stored_events(db)] == ["evt_old", "evt_new"]


async def test_a_retired_secret_is_no_longer_accepted(
    client: httpx.AsyncClient, db: Database
) -> None:
    body = envelope("evt_1")

    response = await deliver(client, body, sign(BANK_NEXT_SECRET, body))

    assert (response.status_code, response.json()) == (401, REFUSED)


@pytest.mark.parametrize("name", ["bank", "simbanks", "SIMBANK", "stripe"])
async def test_an_unknown_provider_is_not_found(
    client: httpx.AsyncClient, db: Database, name: str
) -> None:
    body = envelope("evt_1")

    response = await deliver(client, body, sign(BANK_SECRET, body), path=f"/v1/webhooks/{name}")

    assert (response.status_code, response.json()["code"]) == (404, "not_found")
    assert await stored_events(db) == []


def _padded(size: int) -> bytes:
    """A valid envelope of exactly ``size`` bytes."""
    empty = envelope("evt_big", data={"padding": ""})
    return envelope("evt_big", data={"padding": "x" * (size - len(empty))})


async def test_a_body_of_exactly_the_cap_is_accepted(
    client: httpx.AsyncClient, db: Database
) -> None:
    body = _padded(MAX_BODY_BYTES)
    assert len(body) == MAX_BODY_BYTES

    response = await deliver(client, body, sign(BANK_SECRET, body))

    assert response.status_code == 200
    assert len(await stored_events(db)) == 1


async def test_a_body_over_the_cap_is_refused_whoever_signed_it(
    client: httpx.AsyncClient, db: Database
) -> None:
    body = _padded(MAX_BODY_BYTES + 1)

    signed = await deliver(client, body, sign(BANK_SECRET, body))
    unsigned = await deliver(client, body, None)

    for response in (signed, unsigned):
        assert (response.status_code, response.json()["code"]) == (413, "payload_too_large")
    assert await stored_events(db) == []


async def test_an_oversize_body_sent_in_pieces_without_a_length_is_refused(
    client: httpx.AsyncClient, db: Database
) -> None:
    body = _padded(MAX_BODY_BYTES + 1)

    async def pieces() -> AsyncIterator[bytes]:
        for start in range(0, len(body), 8192):
            yield body[start : start + 8192]

    response = await client.post(
        BANK, content=pieces(), headers={"X-Signature": sign(BANK_SECRET, body)}
    )

    assert response.status_code == 413
    assert await stored_events(db) == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"id":"evt_1","type":"payout.completed","created_at":"2026-01-15T12:00:00Z"}',
        b'{"id":"evt_1","type":"payout.completed","created_at":"2026-01-15T12:00:00Z","data":[]}',
        b'{"id":7,"type":"payout.completed","created_at":"2026-01-15T12:00:00Z","data":{}}',
    ],
)
async def test_a_signed_body_that_is_not_an_event_is_refused_and_not_stored(
    client: httpx.AsyncClient, db: Database, body: bytes
) -> None:
    response = await deliver(client, body, sign(BANK_SECRET, body))

    assert (response.status_code, response.json()["code"]) == (422, "malformed_event")
    assert "evt_1" not in response.text
    assert await stored_events(db) == []
    assert await enqueued(db) == []


# --- the route -------------------------------------------------------------------------------


async def test_the_route_is_public_by_decision_and_asks_for_no_principal(
    app: Any, client: httpx.AsyncClient
) -> None:
    (route,) = [r for r in served_routes(app) if r.path.startswith("/v1/webhooks")]
    body = envelope("evt_1")

    with_a_stray_credential = await deliver(
        client, body, sign(BANK_SECRET, body), headers=bearer("not-a-token")
    )

    assert (route.path, route.method) == ("/v1/webhooks/{provider}", "POST")
    assert (route.path, route.method) in PUBLIC_ROUTES
    assert get_principal not in route.calls
    assert with_a_stray_credential.status_code == 200


async def test_a_bearer_credential_does_not_stand_in_for_a_signature(
    client: httpx.AsyncClient,
) -> None:
    response = await deliver(client, envelope("evt_1"), None, headers=bearer("not-a-token"))

    assert (response.status_code, response.json()) == (401, REFUSED)


async def test_only_post_is_served(client: httpx.AsyncClient) -> None:
    assert (await client.get(BANK)).status_code == 405


async def test_deliveries_are_counted_in_a_rate_limit_group_of_their_own(
    client: httpx.AsyncClient, redis: RedisStore, settings: Settings
) -> None:
    body = envelope("evt_1")

    await deliver(client, body, sign(BANK_SECRET, body))

    keys = [key async for key in redis.client.scan_iter(match=f"{settings.redis_key_prefix}*")]
    groups = {str(key if isinstance(key, str) else key.decode()).split(":")[-2] for key in keys}
    assert groups == {"global", "webhooks"}


async def test_the_webhook_limit_refuses_a_flood(settings: Settings) -> None:
    body = envelope("evt_1")

    async with serving(settings.model_copy(update={"rate_limit_per_minute": 2})) as http:
        answers = [await deliver(http, body, sign(BANK_SECRET, body)) for _ in range(3)]

    assert [answer.status_code for answer in answers] == [200, 200, 429]


# --- the commit ------------------------------------------------------------------------------


async def test_an_event_that_could_not_be_enqueued_is_not_stored_and_not_acknowledged(
    client: httpx.AsyncClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(session: AsyncSession, *args: object, **options: object) -> None:
        raise RuntimeError("the outbox is unavailable")

    body = envelope("evt_1")
    with monkeypatch.context() as patched:
        patched.setattr("corridor.outbox.enqueue", unavailable)
        failed = await deliver(client, body, sign(BANK_SECRET, body))
    retried = await deliver(client, body, sign(BANK_SECRET, body))

    assert failed.status_code == 500
    assert retried.status_code == 200
    (stored,) = await stored_events(db)
    assert await enqueued(db) == [stored["id"]]


async def test_what_is_enqueued_can_be_processed(client: httpx.AsyncClient, db: Database) -> None:
    seen: list[webhooks.WebhookEvent] = []

    async def handler(database: Database, event: webhooks.WebhookEvent) -> None:
        seen.append(event)

    registry = webhooks.WebhookRegistry()
    registry.register(webhooks.Provider.SIMBANK, "payout.completed", handler)
    body = envelope("evt_1", "payout.completed", {"payout_id": "po_1"})
    await deliver(client, body, sign(BANK_SECRET, body))
    (event_id,) = await enqueued(db)

    await webhooks.process(db, event_id, registry)

    assert [(event.event_id, event.data) for event in seen] == [("evt_1", {"payout_id": "po_1"})]


# --- the log ---------------------------------------------------------------------------------


async def test_neither_the_body_nor_the_signature_is_ever_logged(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    body = envelope("evt_1", "payout.completed", {"payout_id": "po_marker_7f3a"})
    good = sign(BANK_SECRET, body)
    malformed = b'{"marker":"po_marker_7f3a"}'
    capsys.readouterr()

    accepted = await deliver(client, body, good)
    replayed = await deliver(client, body, good)
    refused = await deliver(client, body, sign(CUSTODY_SECRET, body))
    stale = await deliver(client, body, sign(BANK_SECRET, body, unix() - 900))
    unparsed = await deliver(client, malformed, sign(BANK_SECRET, malformed))

    written = capsys.readouterr().out
    events = [json.loads(line)["event"] for line in written.splitlines() if line.startswith("{")]
    assert [r.status_code for r in (accepted, replayed, refused, stale, unparsed)] == [
        200,
        200,
        401,
        401,
        422,
    ]
    assert events.count("webhook.received") == 2
    assert events.count("webhook.signature_refused") == 2
    assert events.count("http.request") == 5
    assert "po_marker_7f3a" not in written
    assert "v1=" not in written
    assert _digest_of(good) not in written
    for secret in (BANK_SECRET, CUSTODY_SECRET):
        assert secret not in written

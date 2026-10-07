"""The metrics the architecture promises: each exists, each counts what it says, and none
is labelled with anything a client chose."""

import dataclasses
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger, webhooks
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import ProviderOutcomeUnknown, ProviderRejected, SimBank
from corridor.webhooks import Provider, WebhookEvent, WebhookRegistry
from tests.support.ledger import funded_user, open_user, transfer_draft

# The simulated providers and the adapters wired to them, for the provider metrics.
# Imported so that pytest finds them as fixtures of this module.
from tests.support.providers import (  # noqa: F401
    ACCOUNT_NUMBER,
    ROUTING_NUMBER,
    Sim,
    bank,
    provider_settings,
    sim,
)
from tests.webhooks.helpers import BANK_SECRET, envelope, sign, with_secrets

# Section 15 of the architecture, metric by metric.
PROMISED = {
    # request rate, errors and latency by route template
    "corridor_http_requests": "counter",
    "corridor_http_request_duration_seconds": "histogram",
    # ledger entries by kind
    "corridor_ledger_entries": "counter",
    # outbox depth, oldest pending age and dead-letter count
    "corridor_outbox_pending": "gauge",
    "corridor_outbox_oldest_pending_seconds": "gauge",
    "corridor_outbox_dead": "gauge",
    # webhook counts
    "corridor_webhook_deliveries": "counter",
    "corridor_webhook_events_processed": "counter",
    # provider latency and errors
    "corridor_provider_calls": "counter",
    "corridor_provider_call_duration_seconds": "histogram",
    # open reconciliation breaks
    "corridor_recon_open_breaks": "gauge",
    # rate-limit rejections
    "corridor_rate_limit_rejections": "counter",
    # retry-on-deadlock count
    "corridor_db_transaction_retries": "counter",
    # ledger verifier result
    "corridor_ledger_verifier_findings": "gauge",
}


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def samples(name: str) -> Iterator[dict[str, str]]:
    """The labels of every time series of one metric family."""
    for family in REGISTRY.collect():
        if family.name == name:
            for sample in family.samples:
                yield dict(sample.labels)


def test_every_metric_the_architecture_promises_is_defined() -> None:
    defined = {family.name: family.type for family in REGISTRY.collect()}

    assert {name: defined.get(name) for name in PROMISED} == PROMISED


def test_every_metric_says_what_it_is() -> None:
    ours = [family for family in REGISTRY.collect() if family.name.startswith("corridor_")]

    assert ours
    assert [family.name for family in ours if not family.documentation.strip()] == []


# --- requests ------------------------------------------------------------------------------------


async def test_a_request_is_counted_under_its_route_template_and_not_its_path(
    client: httpx.AsyncClient,
) -> None:
    template = "/v1/transfers/{transfer_id}"
    labels = {"method": "GET", "route": template, "status": "401"}
    before = value("corridor_http_requests_total", **labels)
    transfer_id = str(uuid.uuid4())

    await client.get(f"/v1/transfers/{transfer_id}")

    assert value("corridor_http_requests_total", **labels) == before + 1
    # The id that was asked for is in no label of any request metric.
    for name in ("corridor_http_requests", "corridor_http_request_duration_seconds"):
        assert [s for s in samples(name) if transfer_id in "".join(s.values())] == []


async def test_a_request_is_timed_under_its_route_template(client: httpx.AsyncClient) -> None:
    labels = {"method": "GET", "route": "/healthz"}
    before = value("corridor_http_request_duration_seconds_count", **labels)
    spent = value("corridor_http_request_duration_seconds_sum", **labels)

    await client.get("/healthz")

    assert value("corridor_http_request_duration_seconds_count", **labels) == before + 1
    assert value("corridor_http_request_duration_seconds_sum", **labels) > spent


async def test_paths_that_match_no_route_are_all_one_series(client: httpx.AsyncClient) -> None:
    labels = {"method": "GET", "route": "unmatched", "status": "404"}
    before = value("corridor_http_requests_total", **labels)

    await client.get("/wp-login.php")
    await client.get(f"/{uuid.uuid4()}")

    assert value("corridor_http_requests_total", **labels) == before + 2
    assert [s for s in samples("corridor_http_requests") if "wp-login" in s["route"]] == []


async def test_a_method_nobody_knows_is_counted_as_other(client: httpx.AsyncClient) -> None:
    before = value("corridor_http_requests_total", method="OTHER", route="/healthz", status="405")

    await client.request("BREW", "/healthz")

    assert (
        value("corridor_http_requests_total", method="OTHER", route="/healthz", status="405")
        == before + 1
    )
    assert [s for s in samples("corridor_http_requests") if s["method"] == "BREW"] == []


async def test_an_error_is_counted_by_its_status(app: FastAPI, client: httpx.AsyncClient) -> None:
    async def crash() -> None:
        raise RuntimeError("unexpected")

    app.add_api_route("/crash", crash)
    labels = {"method": "GET", "route": "/crash", "status": "500"}
    before = value("corridor_http_requests_total", **labels)

    await client.get("/crash")

    assert value("corridor_http_requests_total", **labels) == before + 1


# --- the ledger ----------------------------------------------------------------------------------


async def test_an_entry_is_counted_by_its_kind_once(db: Database) -> None:
    before = value("corridor_ledger_entries_total", kind="transfer")
    deposits = value("corridor_ledger_entries_total", kind="deposit")
    async with db.transaction() as session:
        maria, joao = await funded_user(session, 100_00), await open_user(session)
        draft = transfer_draft(maria.available, joao.available, 10_00)

        posted = await ledger.post_entry(session, draft)
        # The same event again: found, and not written a second time.
        replayed = await ledger.post_entry(session, draft)

    assert (posted.created, replayed.created) == (True, False)
    assert value("corridor_ledger_entries_total", kind="transfer") == before + 1
    assert value("corridor_ledger_entries_total", kind="deposit") == deposits + 1


async def test_an_entry_that_is_refused_is_not_counted(db: Database) -> None:
    before = value("corridor_ledger_entries_total", kind="transfer")
    async with db.transaction() as session:
        maria, joao = await funded_user(session, 5_00), await open_user(session)
        with pytest.raises(ledger.InsufficientFunds):
            await ledger.post_entry(session, transfer_draft(maria.available, joao.available, 10_00))

    assert value("corridor_ledger_entries_total", kind="transfer") == before


# --- webhooks ------------------------------------------------------------------------------------


def delivered(outcome: str) -> float:
    return value("corridor_webhook_deliveries_total", provider="simbank", outcome=outcome)


@pytest.fixture
def signed_settings(settings: Settings) -> Settings:
    return with_secrets(settings)


async def test_a_delivery_is_counted_by_what_became_of_it(
    app: FastAPI, client: httpx.AsyncClient, signed_settings: Settings
) -> None:
    app.state.container = dataclasses.replace(app.state.container, settings=signed_settings)
    before = {
        outcome: delivered(outcome)
        for outcome in ("accepted", "duplicate", "bad_signature", "malformed")
    }
    body = envelope(f"evt_{new_id().hex}")
    url = "/v1/webhooks/simbank"

    first = await client.post(url, content=body, headers={"X-Signature": sign(BANK_SECRET, body)})
    again = await client.post(url, content=body, headers={"X-Signature": sign(BANK_SECRET, body)})
    forged = await client.post(url, content=body, headers={"X-Signature": sign("x" * 40, body)})
    nonsense = b'{"not": "an event"}'
    garbled = await client.post(
        url, content=nonsense, headers={"X-Signature": sign(BANK_SECRET, nonsense)}
    )

    assert [r.status_code for r in (first, again, forged, garbled)] == [200, 200, 401, 422]
    assert {outcome: delivered(outcome) - was for outcome, was in before.items()} == {
        "accepted": 1,
        "duplicate": 1,
        "bad_signature": 1,
        "malformed": 1,
    }


async def _done(db: Database, event: WebhookEvent) -> None:
    """A handler that has nothing to do."""


async def test_a_processed_event_is_counted_by_type_and_an_unknown_type_is_not_a_label(
    db: Database,
) -> None:
    strange = f"made.up.{new_id().hex}"
    registry = WebhookRegistry()
    registry.register(Provider.SIMBANK, "payout.completed", _done)
    handled = {"provider": "simbank", "type": "payout.completed", "outcome": "processed"}
    ignored = {"provider": "simbank", "type": "unhandled", "outcome": "ignored"}
    before = (
        value("corridor_webhook_events_processed_total", **handled),
        value("corridor_webhook_events_processed_total", **ignored),
    )

    async def store(event_type: str) -> uuid.UUID:
        parsed = webhooks.parse_envelope(envelope(f"evt_{new_id().hex}", event_type))

        async def work(session: AsyncSession) -> uuid.UUID:
            return (await webhooks.record(session, Provider.SIMBANK, parsed)).id

        return await db.run(work)

    known, unknown = await store("payout.completed"), await store(strange)
    await webhooks.process(db, known, registry)
    await webhooks.process(db, unknown, registry)
    # An event that is already processed is left alone, and not counted again.
    await webhooks.process(db, known, registry)

    assert (
        value("corridor_webhook_events_processed_total", **handled),
        value("corridor_webhook_events_processed_total", **ignored),
    ) == (before[0] + 1, before[1] + 1)
    # The type came from the provider. It names no time series.
    assert [s for s in samples("corridor_webhook_events_processed") if s["type"] == strange] == []


# --- providers -----------------------------------------------------------------------------------


def calls(operation: str, outcome: str) -> float:
    return value(
        "corridor_provider_calls_total", provider="simbank", operation=operation, outcome=outcome
    )


def timed(operation: str) -> float:
    return value(
        "corridor_provider_call_duration_seconds_count", provider="simbank", operation=operation
    )


async def register_account(bank: SimBank, **changes: Any) -> None:  # noqa: F811
    await bank.create_beneficiary(
        **{
            "customer_reference": str(new_id()),
            "asset_code": "USD",
            "holder_name": "Maria Silva",
            "account_number": ACCOUNT_NUMBER,
            "routing_number": ROUTING_NUMBER,
            "idempotency_key": f"metrics-{new_id()}",
            **changes,
        }
    )


async def test_a_provider_call_is_counted_and_timed_by_operation_and_outcome(
    bank: SimBank,  # noqa: F811
) -> None:
    ok, rejected = calls("create_beneficiary", "ok"), calls("create_beneficiary", "rejected")
    before = timed("create_beneficiary")

    await register_account(bank)
    with pytest.raises(ProviderRejected):
        await register_account(bank, account_number="12")

    assert calls("create_beneficiary", "ok") == ok + 1
    assert calls("create_beneficiary", "rejected") == rejected + 1
    assert timed("create_beneficiary") == before + 2


async def test_a_provider_that_does_not_answer_is_counted_as_unknown(
    sim: Sim,  # noqa: F811
    bank: SimBank,  # noqa: F811
) -> None:
    before = calls("create_beneficiary", "unknown")
    await sim.inject("bank.create_beneficiary", "error")

    with pytest.raises(ProviderOutcomeUnknown):
        await register_account(bank)

    assert calls("create_beneficiary", "unknown") == before + 1

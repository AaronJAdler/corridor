"""Retention: the personal fields of a provider's event are removed once the event has
been processed and the retention period has passed. The event itself stays."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import webhooks
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.webhooks import Provider, WebhookEvent, WebhookRegistry
from corridor.worker import build_jobs
from corridor.worker.jobs import PURGE_INTERVAL_SECONDS, WEBHOOK_REDACTION_JOB
from tests.webhooks.helpers import envelope

BANK_DEPOSIT = {
    "deposit_id": "dep_7k2m9q",
    "virtual_account_id": "va_8f3k2m9q",
    "customer_reference": "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10",
    "asset": "USD",
    "amount": "250.00",
    "sender_name": "Maria Silva",
    "reference": "INV-2041",
}
CHAIN_DEPOSIT = {
    "deposit_id": "cd_4t7w1x",
    "address": "sim1qxyz",
    "asset": "USDC",
    "amount": "25.000000",
    "tx_hash": "0xabc",
    "from_address": "sim1sender",
    "confirmations": 0,
}
RETENTION = timedelta(days=30)


async def _done(db: Database, event: WebhookEvent) -> None:
    """A handler that has nothing to do."""


async def record(
    db: Database,
    data: dict[str, Any],
    *,
    event_id: str,
    event_type: str = "deposit.received",
    provider: Provider = Provider.SIMBANK,
) -> uuid.UUID:
    parsed = webhooks.parse_envelope(envelope(event_id, event_type, data))

    async def work(session: AsyncSession) -> uuid.UUID:
        return (await webhooks.record(session, provider, parsed)).id

    return await db.run(work)


async def process(db: Database, event_id: uuid.UUID, event_type: str = "deposit.received") -> None:
    registry = WebhookRegistry()
    registry.register(Provider.SIMBANK, event_type, _done)
    registry.register(Provider.SIMCUSTODY, event_type, _done)
    await webhooks.process(db, event_id, registry)


async def processed(db: Database, data: dict[str, Any], **naming: Any) -> uuid.UUID:
    event_id = await record(db, data, **naming)
    await process(db, event_id, naming.get("event_type", "deposit.received"))
    return event_id


async def row(db: Database, event_id: uuid.UUID) -> dict[str, Any]:
    async with db.transaction() as session:
        found = await session.execute(
            text("SELECT payload, redacted_at, processed_at FROM webhook_events WHERE id = :id"),
            {"id": event_id},
        )
        return dict(found.mappings().one())


async def redact(db: Database, *, limit: int = webhooks.REDACTION_BATCH) -> int:
    async with db.transaction() as session:
        return await webhooks.redact_payloads(
            session, processed_before=utcnow() - RETENTION, limit=limit
        )


def test_the_personal_fields_are_the_three_a_provider_sends_about_someone_else() -> None:
    assert webhooks.PERSONAL_FIELDS == ("sender_name", "reference", "from_address")


async def test_the_personal_fields_of_an_old_processed_event_are_set_to_null(
    db: Database, clock: ManualClock
) -> None:
    event_id = await processed(db, BANK_DEPOSIT, event_id="evt_1")
    clock.advance(seconds=RETENTION.total_seconds() + 1)

    redacted = await redact(db)

    stored = await row(db, event_id)
    assert redacted == 1
    assert stored["payload"]["data"] == {**BANK_DEPOSIT, "sender_name": None, "reference": None}
    assert stored["redacted_at"] == clock.now()


async def test_the_sending_address_of_an_on_chain_deposit_is_removed(
    db: Database, clock: ManualClock
) -> None:
    event_id = await processed(
        db,
        CHAIN_DEPOSIT,
        event_id="evt_1",
        event_type="deposit.detected",
        provider=Provider.SIMCUSTODY,
    )
    clock.advance(seconds=RETENTION.total_seconds() + 1)

    await redact(db)

    assert (await row(db, event_id))["payload"]["data"] == {**CHAIN_DEPOSIT, "from_address": None}


async def test_everything_else_about_the_event_is_kept(db: Database, clock: ManualClock) -> None:
    event_id = await processed(db, BANK_DEPOSIT, event_id="evt_1")
    before = (await row(db, event_id))["payload"]
    clock.advance(seconds=RETENTION.total_seconds() + 1)

    await redact(db)

    after = (await row(db, event_id))["payload"]
    assert {key: after[key] for key in after if key != "data"} == {
        key: before[key] for key in before if key != "data"
    }
    assert after["id"] == "evt_1"
    assert set(after["data"]) == set(before["data"])


async def test_an_event_processed_within_the_retention_period_is_left_whole(
    db: Database, clock: ManualClock
) -> None:
    event_id = await processed(db, BANK_DEPOSIT, event_id="evt_1")
    # Processed exactly as long ago as events are kept for: not yet older than that.
    clock.advance(seconds=RETENTION.total_seconds())

    redacted = await redact(db)

    stored = await row(db, event_id)
    assert redacted == 0
    assert (stored["payload"]["data"], stored["redacted_at"]) == (BANK_DEPOSIT, None)


async def test_an_event_that_was_never_processed_is_left_whole_however_old(
    db: Database, clock: ManualClock
) -> None:
    # Processing is what reads these fields: the sender is screened by name.
    event_id = await record(db, BANK_DEPOSIT, event_id="evt_1")
    clock.advance(seconds=RETENTION.total_seconds() * 3)

    redacted = await redact(db)

    assert redacted == 0
    assert (await row(db, event_id))["payload"]["data"] == BANK_DEPOSIT


async def test_an_event_is_dealt_with_once(db: Database, clock: ManualClock) -> None:
    event_id = await processed(db, BANK_DEPOSIT, event_id="evt_1")
    clock.advance(seconds=RETENTION.total_seconds() + 1)
    await redact(db)
    first = (await row(db, event_id))["redacted_at"]
    clock.advance(seconds=3600)

    again = await redact(db)

    assert again == 0
    assert (await row(db, event_id))["redacted_at"] == first


async def test_an_old_event_with_no_personal_field_is_marked_so_that_it_is_not_read_again(
    db: Database, clock: ManualClock
) -> None:
    payout = {"payout_id": "po_2h5j8n", "amount": "100.00"}
    event_id = await processed(db, payout, event_id="evt_1", event_type="payout.completed")
    clock.advance(seconds=RETENTION.total_seconds() + 1)

    assert await redact(db) == 1
    assert await redact(db) == 0
    assert (await row(db, event_id))["payload"]["data"] == payout


async def test_one_call_deals_with_at_most_a_batch_oldest_first(
    db: Database, clock: ManualClock
) -> None:
    oldest = await processed(db, BANK_DEPOSIT, event_id="evt_1")
    clock.advance(seconds=60)
    middle = await processed(db, BANK_DEPOSIT, event_id="evt_2")
    clock.advance(seconds=60)
    newest = await processed(db, BANK_DEPOSIT, event_id="evt_3")
    clock.advance(seconds=RETENTION.total_seconds() + 1)

    first = await redact(db, limit=2)

    assert first == 2
    assert [(await row(db, e))["redacted_at"] is not None for e in (oldest, middle, newest)] == [
        True,
        True,
        False,
    ]
    assert await redact(db, limit=2) == 1


# --- the job -------------------------------------------------------------------------------------


def test_the_retention_period_is_30_days_unless_configured(settings: Settings) -> None:
    assert settings.webhook_payload_retention_days == 30


def test_the_job_runs_hourly(settings: Settings) -> None:
    (job,) = [job for job in build_jobs(settings) if job.name == WEBHOOK_REDACTION_JOB]

    assert job.interval_seconds == PURGE_INTERVAL_SECONDS == 3600


async def test_the_job_redacts_what_is_older_than_the_configured_period(
    db: Database, clock: ManualClock, settings: Settings
) -> None:
    old = await processed(db, BANK_DEPOSIT, event_id="evt_1")
    clock.advance(seconds=timedelta(days=23).total_seconds())
    recent = await processed(db, BANK_DEPOSIT, event_id="evt_2")
    clock.advance(seconds=timedelta(days=7, seconds=1).total_seconds())
    (job,) = [job for job in build_jobs(settings) if job.name == WEBHOOK_REDACTION_JOB]

    await job.run(db)

    assert (await row(db, old))["payload"]["data"]["sender_name"] is None
    assert (await row(db, recent))["payload"]["data"]["sender_name"] == "Maria Silva"


async def test_the_job_works_through_more_than_one_batch(
    db: Database, clock: ManualClock, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webhooks, "REDACTION_BATCH", 2)
    events = [await processed(db, BANK_DEPOSIT, event_id=f"evt_{n}") for n in range(5)]
    clock.advance(seconds=RETENTION.total_seconds() + 1)
    (job,) = [job for job in build_jobs(settings) if job.name == WEBHOOK_REDACTION_JOB]

    await job.run(db)

    assert [(await row(db, e))["redacted_at"] is not None for e in events] == [True] * 5

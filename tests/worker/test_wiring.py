"""What a worker is built from: which handler each topic and each provider event reaches,
which jobs run, and the providers a worker process opens and closes."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from corridor import payments, webhooks
from corridor.api.app import create_app
from corridor.identity import User
from corridor.outbox import Dispatcher
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.providers import SimBank, SimCustody
from corridor.webhooks import Envelope, Provider, WebhookEvent
from corridor.worker import Scheduler, build_jobs, build_registry
from corridor.worker import main as worker_main
from corridor.worker.webhook_routes import build_webhook_registry
from tests.outbox.helpers import status_counts
from tests.payments.support import (
    add_person,
    available,
    held_bank_withdrawal,
    held_chain_withdrawal,
    instruction_for,
    rows,
    withdrawal_row,
)
from tests.support.providers import EXTERNAL_ADDRESS, Sim, advance
from tests.webhooks.helpers import BANK_SECRET, CUSTODY_SECRET, with_secrets

# The table the worker is built from: which payment function each provider event reaches.
ROUTES = [
    ("simbank", "deposit.received", "apply_bank_deposit_received"),
    ("simbank", "deposit.returned", "apply_bank_deposit_returned"),
    ("simbank", "payout.completed", "apply_payout_completed"),
    ("simbank", "payout.failed", "apply_payout_failed"),
    ("simcustody", "deposit.detected", "apply_chain_deposit_detected"),
    ("simcustody", "deposit.confirmed", "apply_chain_deposit_confirmed"),
    ("simcustody", "deposit.failed", "apply_chain_deposit_failed"),
    ("simcustody", "withdrawal.completed", "apply_withdrawal_completed"),
    ("simcustody", "withdrawal.failed", "apply_withdrawal_failed"),
]
SWEEP_JOB = "payments.sweep_payouts"
RECEIVED_AT = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
async def maria(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "maria")


def stored_event(provider: str, event_type: str, data: Mapping[str, Any]) -> WebhookEvent:
    return WebhookEvent(
        id=uuid.uuid4(),
        provider=Provider(provider),
        event_id="evt_test",
        type=event_type,
        payload={"id": "evt_test", "type": event_type, "data": dict(data)},
        received_at=RECEIVED_AT,
        processed_at=None,
        outcome=None,
    )


async def receive(
    db: Database, provider: Provider, event_type: str, data: Mapping[str, Any]
) -> None:
    """Store a provider's event and enqueue its processing, as the webhook route does."""
    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    payload = {
        "id": event_id,
        "type": event_type,
        "created_at": "2026-01-15T12:00:00Z",
        "data": dict(data),
    }
    async with db.transaction() as session:
        await webhooks.record(session, provider, Envelope(event_id, event_type, payload))


async def drain(db: Database, settings: Settings, **providers: Any) -> None:
    """Process everything that is due, as a worker with these providers would."""
    await Dispatcher(db, build_registry(db, settings, **providers), settings).drain()


async def outbox_rows(db: Database) -> list[dict[str, Any]]:
    return await rows(
        db, "SELECT topic, status, attempts, last_error FROM outbox_events ORDER BY id"
    )


# --- provider events -------------------------------------------------------------------------


@pytest.mark.parametrize(("provider", "event_type", "function"), ROUTES)
async def test_each_provider_event_is_handed_to_its_payment_function_with_the_events_data(
    db: Database, monkeypatch: pytest.MonkeyPatch, provider: str, event_type: str, function: str
) -> None:
    calls: list[tuple[str, Database, Mapping[str, Any]]] = []

    def recording(name: str) -> Any:
        async def apply(given: Database, data: Mapping[str, Any]) -> None:
            calls.append((name, given, data))

        return apply

    for _provider, _type, name in ROUTES:
        monkeypatch.setattr(payments, name, recording(name))
    event = stored_event(provider, event_type, {"deposit_id": "dep_1", "amount": "1.00"})

    handler = build_webhook_registry().handler_for(Provider(provider), event_type)
    assert handler is not None
    await handler(db, event)

    assert calls == [(function, db, {"deposit_id": "dep_1", "amount": "1.00"})]


def test_no_other_provider_event_has_a_handler() -> None:
    registry = build_webhook_registry()
    routed = {(provider, event_type) for provider, event_type, _function in ROUTES}
    types = {event_type for _provider, event_type, _function in ROUTES} | {"payout.created"}

    handled = {
        (provider.value, event_type)
        for provider in Provider
        for event_type in types
        if registry.handler_for(provider, event_type) is not None
    }

    assert handled == routed


# --- topics ----------------------------------------------------------------------------------


async def test_a_stored_bank_deposit_is_credited_by_a_drain_with_the_real_registry(
    db: Database, settings: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruction_for(db, maria, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "250.00")
    await receive(db, Provider.SIMBANK, "deposit.received", data)

    await drain(db, settings)

    assert await available(db, maria) == 250_00
    (stored,) = await rows(db, "SELECT outcome, processed_at FROM webhook_events")
    assert stored["outcome"] == "processed"
    assert stored["processed_at"] is not None
    # The delivery, and the completed deposit it led to, which nothing consumes yet.
    assert [(event["topic"], event["status"]) for event in await outbox_rows(db)] == [
        ("webhook.received", "done"),
        ("deposit.completed", "done"),
    ]


async def test_a_provider_event_of_an_unknown_type_is_stored_and_ignored(
    db: Database, settings: Settings
) -> None:
    await receive(db, Provider.SIMBANK, "payout.created", {"payout_id": "po_1"})

    await drain(db, settings)

    (stored,) = await rows(db, "SELECT outcome FROM webhook_events")
    assert stored["outcome"] == "ignored"
    assert await status_counts(db) == {"done": 1}


async def test_a_requested_bank_withdrawal_is_sent_by_a_drain_with_the_real_registry(
    db: Database, settings: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, settings, bank, maria)

    await drain(db, settings, bank=bank, custody=custody)

    (payout,) = await sim.payouts()
    assert payout["idempotency_key"] == str(withdrawal.id)
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["provider_ref"]) == ("submitted", payout["id"])
    assert await status_counts(db) == {"done": 1}


async def test_a_chain_withdrawal_is_sent_by_a_worker_that_has_no_bank(
    db: Database, settings: Settings, sim: Sim, custody: SimCustody, maria: User
) -> None:
    withdrawal = await held_chain_withdrawal(db, settings, maria, EXTERNAL_ADDRESS)

    await drain(db, settings, custody=custody)

    (sent,) = await sim.withdrawals()
    assert sent["idempotency_key"] == str(withdrawal.id)
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"


async def test_a_withdrawal_whose_provider_is_not_configured_fails_its_event_and_is_retried(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, settings, bank, maria)

    # A worker with a custodian and no bank. The drain itself does not fail.
    await drain(db, settings, custody=custody)

    (event,) = await outbox_rows(db)
    assert (event["topic"], event["status"], event["attempts"]) == (
        "withdrawal.submit",
        "pending",
        1,
    )
    assert "no bank rail is configured" in event["last_error"]
    # Nothing was sent and nothing changed: the user can still call it back.
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "held"
    assert await sim.payouts() == []

    clock.advance(seconds=2)
    await drain(db, settings, bank=bank, custody=custody)

    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"
    assert await status_counts(db) == {"done": 1}


# --- jobs ------------------------------------------------------------------------------------


def test_the_payout_sweep_runs_every_30_seconds_when_a_provider_is_configured(
    settings: Settings, bank: SimBank
) -> None:
    by_name = {job.name: job for job in build_jobs(settings, bank=bank)}

    assert by_name[SWEEP_JOB].interval_seconds == 30


def test_a_worker_with_no_provider_has_no_payout_sweep(settings: Settings) -> None:
    assert SWEEP_JOB not in {job.name for job in build_jobs(settings)}


async def test_the_sweep_job_settles_a_withdrawal_whose_webhook_never_came(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, settings, bank, maria)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, settings.payout_sweep_after_seconds)

    ran = await Scheduler(db, build_jobs(settings, bank=bank, custody=custody)).tick()

    assert SWEEP_JOB in ran
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "completed"
    (run,) = await rows(db, "SELECT last_error FROM job_runs WHERE name = :name", name=SWEEP_JOB)
    assert run["last_error"] is None


async def test_the_sweep_job_of_a_worker_with_no_bank_leaves_bank_withdrawals_and_does_not_fail(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    clock: ManualClock,
    maria: User,
) -> None:
    withdrawal = await held_bank_withdrawal(db, settings, bank, maria)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    await advance(sim, clock, settings.payout_sweep_after_seconds)

    await Scheduler(db, build_jobs(settings, custody=custody)).tick()

    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"
    (run,) = await rows(db, "SELECT last_error FROM job_runs WHERE name = :name", name=SWEEP_JOB)
    assert run["last_error"] is None


# --- the process -----------------------------------------------------------------------------


class FakeProvider:
    """Stands in for an adapter: remembers that it was built and whether it was closed."""

    built: list[FakeProvider]

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.closed = False
        type(self).built.append(self)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run ``_serve`` with a worker that returns at once, and record what it was given."""
    seen: dict[str, Any] = {}

    class Bank(FakeProvider):
        built: list[FakeProvider] = []  # noqa: RUF012 - one list per test

    class Custody(FakeProvider):
        built: list[FakeProvider] = []  # noqa: RUF012 - one list per test

    def registry(_db: Database, _settings: Settings, **providers: Any) -> object:
        seen["registry"] = providers
        return object()

    def jobs(_settings: Settings, **providers: Any) -> list[Any]:
        seen["jobs"] = providers
        return []

    class StoppedWorker:
        def __init__(self, *_arguments: Any, **_options: Any) -> None:
            pass

        async def run(self) -> None:
            seen["open_while_running"] = [
                not provider.closed for provider in Bank.built + Custody.built
            ]
            if seen.get("crash"):
                raise RuntimeError("the worker died")

        def request_stop(self) -> None:
            pass

    monkeypatch.setattr(worker_main, "SimBank", Bank)
    monkeypatch.setattr(worker_main, "SimCustody", Custody)
    monkeypatch.setattr(worker_main, "build_registry", registry)
    monkeypatch.setattr(worker_main, "build_jobs", jobs)
    monkeypatch.setattr(worker_main, "Worker", StoppedWorker)
    seen["bank"], seen["custody"] = Bank.built, Custody.built
    return seen


async def test_a_worker_process_builds_the_providers_that_have_an_address_and_closes_them(
    provider_settings: Settings, served: dict[str, Any]
) -> None:
    await worker_main._serve(provider_settings)

    (bank_built,) = served["bank"]
    (custody_built,) = served["custody"]
    assert served["registry"] == {"bank": bank_built, "custody": custody_built}
    # A deployed worker reconciles: it is asked for here and nowhere else.
    assert served["jobs"] == {"bank": bank_built, "custody": custody_built, "reconcile": True}
    assert served["open_while_running"] == [True, True]
    assert (bank_built.closed, custody_built.closed) == (True, True)


async def test_a_worker_process_closes_its_providers_when_the_worker_fails(
    provider_settings: Settings, served: dict[str, Any]
) -> None:
    served["crash"] = True

    with pytest.raises(RuntimeError, match="the worker died"):
        await worker_main._serve(provider_settings)

    assert [provider.closed for provider in served["bank"] + served["custody"]] == [True, True]


async def test_a_worker_process_leaves_out_a_provider_that_has_no_address(
    settings: Settings, served: dict[str, Any]
) -> None:
    await worker_main._serve(settings)

    assert (served["bank"], served["custody"]) == ([], [])
    assert served["registry"] == {"bank": None, "custody": None}
    assert served["jobs"] == {"bank": None, "custody": None, "reconcile": True}


# --- the API's start-up check ----------------------------------------------------------------


async def test_the_api_refuses_to_start_when_the_providers_share_a_webhook_secret(
    settings: Settings,
) -> None:
    shared = with_secrets(settings, bank=(BANK_SECRET,), custody=(CUSTODY_SECRET, BANK_SECRET))
    application = create_app(shared)

    with pytest.raises(ValueError, match="share") as refused:
        async with application.router.lifespan_context(application):
            pass

    assert BANK_SECRET not in str(refused.value)


async def test_the_api_starts_when_each_provider_has_secrets_of_its_own(
    settings: Settings,
) -> None:
    application = create_app(with_secrets(settings))

    async with application.router.lifespan_context(application):
        assert application.state.container.settings.bank_rail_webhook_secrets == [
            SecretStr(BANK_SECRET)
        ]

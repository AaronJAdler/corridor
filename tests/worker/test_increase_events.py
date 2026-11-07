"""Increase's events, from the stored delivery to the ledger, through the worker.

The adapter is the real one. What it talks to is a stand-in for Increase that keeps the
objects Corridor made and answers with documents of the shapes Increase's API reference
gives them, so the whole path runs: the event names an object, the worker reads it through
the adapter, and the payment functions act on what it says, under the name ``increase``.
"""

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from corridor import payments, webhooks
from corridor.identity import User
from corridor.ledger import AccountKind
from corridor.platform.clock import ManualClock
from corridor.platform.config import INCREASE_SANDBOX_URL, Settings
from corridor.platform.db import Database
from corridor.providers import (
    INCREASE,
    BankEvent,
    IncreaseBank,
    ListedEvent,
    ProviderMisconfigured,
    SimBank,
)
from corridor.webhooks import Envelope, Provider, WebhookEvent
from corridor.worker import build_jobs
from corridor.worker.bank_events import BANK_EVENT_POLL_JOB, BankEventPoller
from corridor.worker.webhook_routes import build_webhook_registry, require_event_source
from tests.payments.support import (
    add_person,
    available,
    balance_of,
    held_bank_withdrawal,
    instruction_for,
    rows,
    withdrawal_row,
)
from tests.worker.test_wiring import drain, outbox_rows

ACCOUNT = "account_in71c4amph0vgo2qllky"
ACCOUNT_NUMBER_ID = "account_number_v18nkfqm6afpsrvy82b2"
EXTERNAL_ACCOUNT = "external_account_ukk55lr923a3ac0pp7iv"
INBOUND = "inbound_ach_transfer_tdrwqr3fq9gnnq49odev"
TRANSFER = "ach_transfer_uoxatyh3lt5evrsdvo7q"
CREATED = "2026-01-15T12:00:00Z"


class FakeIncrease:
    """Increase as far as these tests need it: account numbers, external accounts, ACH
    transfers it keeps by idempotency key, and inbound transfers a test puts there."""

    def __init__(self) -> None:
        self.transfers: dict[str, dict[str, Any]] = {}
        self.inbound: dict[str, dict[str, Any]] = {}
        # What GET /events lists, oldest first, whatever it is asked.
        self.events: list[dict[str, Any]] = []
        self.listings: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if (method, path) == ("POST", "/account_numbers"):
            body = json.loads(request.content)
            return httpx.Response(200, json=self._account_number(body))
        if (method, path) == ("POST", "/external_accounts"):
            body = json.loads(request.content)
            return httpx.Response(200, json=self._external_account(body))
        if (method, path) == ("POST", "/ach_transfers"):
            body = json.loads(request.content)
            key = request.headers["idempotency-key"]
            transfer = self.transfers.setdefault(key, self._transfer(body, key))
            return httpx.Response(200, json=transfer)
        if method == "GET" and path.startswith("/ach_transfers/"):
            wanted = path.rsplit("/", 1)[1]
            for transfer in self.transfers.values():
                if transfer["id"] == wanted:
                    return httpx.Response(200, json=transfer)
        if (method, path) == ("GET", "/events"):
            self.listings.append(request)
            return httpx.Response(200, json={"data": self.events, "next_cursor": None})
        if method == "GET" and path.startswith("/inbound_ach_transfers/"):
            found = self.inbound.get(path.rsplit("/", 1)[1])
            if found is not None:
                return httpx.Response(200, json=found)
        return httpx.Response(
            404, json={"status": 404, "type": "object_not_found_error", "title": "Not found."}
        )

    @staticmethod
    def _account_number(body: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": ACCOUNT_NUMBER_ID,
            "account_id": body["account_id"],
            "account_number": "1234567890",
            "routing_number": "101050001",
            "name": body["name"],
            "status": "active",
            "inbound_ach_debits": body["inbound_ach_debits"],
            "created_at": CREATED,
            "type": "account_number",
        }

    @staticmethod
    def _external_account(body: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": EXTERNAL_ACCOUNT,
            "account_number": body["account_number"],
            "routing_number": body["routing_number"],
            "description": body["description"],
            "status": "active",
            "created_at": CREATED,
            "type": "external_account",
        }

    @staticmethod
    def _transfer(body: Mapping[str, Any], key: str) -> dict[str, Any]:
        return {
            "id": TRANSFER,
            "account_id": body["account_id"],
            "amount": body["amount"],
            "currency": "USD",
            "external_account_id": body["external_account_id"],
            "idempotency_key": key,
            "status": "pending_submission",
            "settlement": None,
            "return": None,
            "created_at": CREATED,
            "type": "ach_transfer",
        }

    def credit(self, amount: int, *, status: str = "accepted", **changes: object) -> None:
        self.inbound[INBOUND] = {
            "id": INBOUND,
            "account_id": ACCOUNT,
            "account_number_id": ACCOUNT_NUMBER_ID,
            "amount": amount,
            "direction": "credit",
            "status": status,
            "originator_company_name": "BIG BANK",
            "originator_company_entry_description": "PAYROLL",
            "transfer_return": None,
            "created_at": CREATED,
            "type": "inbound_ach_transfer",
            **changes,
        }

    def settle(self, **changes: object) -> None:
        (transfer,) = self.transfers.values()
        transfer.update({"status": "submitted", "settlement": {"settled_at": CREATED}, **changes})


@pytest.fixture
def fake() -> FakeIncrease:
    return FakeIncrease()


@pytest.fixture
def increase_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "bank_rail_provider": "increase_sandbox",
            "increase_base_url": INCREASE_SANDBOX_URL,
            "increase_account_id": ACCOUNT,
            # Made up: nothing here reaches Increase.
            "increase_api_key": SecretStr("placeholder-increase-key-" + "0" * 16),
        }
    )


@pytest.fixture
async def increase(fake: FakeIncrease, increase_settings: Settings) -> AsyncIterator[IncreaseBank]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    bank = IncreaseBank(increase_settings, client=client)
    yield bank
    await bank.aclose()
    await client.aclose()


@pytest.fixture
async def maria(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "maria")


async def receive(db: Database, category: str, object_type: str, object_id: str) -> None:
    """Store an Increase event and enqueue its processing, as the webhook route does."""
    event_id = f"event_{uuid.uuid4().hex[:20]}"
    payload = {
        "id": event_id,
        "created_at": CREATED,
        "category": category,
        "associated_object_type": object_type,
        "associated_object_id": object_id,
        "type": "event",
    }
    async with db.transaction() as session:
        await webhooks.record(session, Provider.INCREASE, Envelope(event_id, category, payload))


async def increase_settlement(db: Database) -> int:
    return await balance_of(db, AccountKind.BANK_SETTLEMENT, "USD", provider=INCREASE)


# --- deposits ------------------------------------------------------------------------------


async def test_an_inbound_ach_credit_is_credited_to_the_owner_of_the_account_number(
    db: Database,
    settings: Settings,
    fake: FakeIncrease,
    increase: IncreaseBank,
    custody: Any,
    maria: User,
) -> None:
    instruction = await instruction_for(db, maria, "USD", increase, custody)
    fake.credit(250_00)

    await receive(db, "inbound_ach_transfer.created", "inbound_ach_transfer", INBOUND)
    await drain(db, settings, bank=increase, custody=custody)

    assert (instruction.provider, instruction.provider_ref) == (INCREASE, ACCOUNT_NUMBER_ID)
    assert await available(db, maria) == 250_00
    assert await increase_settlement(db) == 250_00
    (deposit,) = await rows(db, "SELECT provider, provider_ref, status FROM deposits")
    assert deposit == {"provider": INCREASE, "provider_ref": INBOUND, "status": "completed"}


async def test_the_same_credit_told_twice_and_out_of_order_is_credited_once(
    db: Database,
    settings: Settings,
    fake: FakeIncrease,
    increase: IncreaseBank,
    custody: Any,
    maria: User,
) -> None:
    await instruction_for(db, maria, "USD", increase, custody)
    fake.credit(250_00)

    # The update first, then the creation: each reads the transfer as it is now.
    await receive(db, "inbound_ach_transfer.updated", "inbound_ach_transfer", INBOUND)
    await receive(db, "inbound_ach_transfer.created", "inbound_ach_transfer", INBOUND)
    await drain(db, settings, bank=increase, custody=custody)

    assert await available(db, maria) == 250_00
    assert len(await rows(db, "SELECT id FROM deposits")) == 1


async def test_a_credit_increase_has_not_accepted_yet_credits_nothing_until_it_is(
    db: Database,
    settings: Settings,
    fake: FakeIncrease,
    increase: IncreaseBank,
    custody: Any,
    maria: User,
) -> None:
    await instruction_for(db, maria, "USD", increase, custody)
    fake.credit(250_00, status="pending")

    await receive(db, "inbound_ach_transfer.created", "inbound_ach_transfer", INBOUND)
    await drain(db, settings, bank=increase, custody=custody)
    assert await available(db, maria) == 0

    fake.credit(250_00)
    await receive(db, "inbound_ach_transfer.updated", "inbound_ach_transfer", INBOUND)
    await drain(db, settings, bank=increase, custody=custody)
    assert await available(db, maria) == 250_00


async def test_a_returned_credit_is_taken_back(
    db: Database,
    settings: Settings,
    fake: FakeIncrease,
    increase: IncreaseBank,
    custody: Any,
    maria: User,
) -> None:
    await instruction_for(db, maria, "USD", increase, custody)
    fake.credit(250_00)
    await receive(db, "inbound_ach_transfer.created", "inbound_ach_transfer", INBOUND)
    await drain(db, settings, bank=increase, custody=custody)

    fake.credit(
        250_00,
        status="returned",
        transfer_return={"reason": "credit_entry_refused_by_receiver"},
    )
    await receive(db, "inbound_ach_transfer.updated", "inbound_ach_transfer", INBOUND)
    await drain(db, settings, bank=increase, custody=custody)

    assert await available(db, maria) == 0
    assert await increase_settlement(db) == 0
    (deposit,) = await rows(db, "SELECT status FROM deposits")
    assert deposit == {"status": "returned"}


# --- payouts -------------------------------------------------------------------------------


async def sent(
    db: Database, settings: Settings, increase: IncreaseBank, custody: Any, maria: User
) -> uuid.UUID:
    withdrawal = await held_bank_withdrawal(db, settings, increase, maria)
    await drain(db, settings, bank=increase, custody=custody)
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"
    return withdrawal.id


async def test_a_withdrawal_goes_out_as_an_ach_transfer_and_settles_on_its_event(
    db: Database,
    settings: Settings,
    fake: FakeIncrease,
    increase: IncreaseBank,
    custody: Any,
    maria: User,
) -> None:
    withdrawal_id = await sent(db, settings, increase, custody, maria)
    (transfer,) = fake.transfers.values()
    # Sent under the withdrawal's id, which is how it is found again.
    assert transfer["idempotency_key"] == str(withdrawal_id)
    assert (await withdrawal_row(db, withdrawal_id))["provider_ref"] == TRANSFER

    # Created, and still on its way: nothing changes.
    await receive(db, "ach_transfer.created", "ach_transfer", TRANSFER)
    await drain(db, settings, bank=increase, custody=custody)
    assert (await withdrawal_row(db, withdrawal_id))["status"] == "submitted"

    fake.settle()
    await receive(db, "ach_transfer.updated", "ach_transfer", TRANSFER)
    await drain(db, settings, bank=increase, custody=custody)

    row = await withdrawal_row(db, withdrawal_id)
    assert (row["status"], row["provider"], row["provider_fee"]) == ("completed", INCREASE, 0)
    assert await increase_settlement(db) == -100_00


async def test_a_returned_transfer_gives_the_funds_back(
    db: Database,
    settings: Settings,
    fake: FakeIncrease,
    increase: IncreaseBank,
    custody: Any,
    maria: User,
) -> None:
    withdrawal_id = await sent(db, settings, increase, custody, maria)
    before = await available(db, maria)

    (transfer,) = fake.transfers.values()
    transfer.update({"status": "returned", "return": {"return_reason_code": "no_account"}})
    await receive(db, "ach_transfer.updated", "ach_transfer", TRANSFER)
    await drain(db, settings, bank=increase, custody=custody)

    row = await withdrawal_row(db, withdrawal_id)
    assert (row["status"], row["failure_reason"]) == ("failed", "no_account")
    assert await available(db, maria) > before


# --- a worker that cannot read the event ---------------------------------------------------


async def test_an_increase_event_at_a_worker_without_increase_waits_and_is_not_lost(
    db: Database, settings: Settings, bank: SimBank, custody: Any
) -> None:
    await receive(db, "inbound_ach_transfer.created", "inbound_ach_transfer", INBOUND)

    await drain(db, settings, bank=bank, custody=custody)

    (event,) = await rows(db, "SELECT processed_at, outcome FROM webhook_events")
    assert event == {"processed_at": None, "outcome": None}
    (queued,) = await outbox_rows(db)
    assert queued["status"] == "pending"
    assert "resolve_event" in queued["last_error"]


class _NamedIncreaseWithoutEvents:
    """A bank rail that answers to the name and cannot read events."""

    name = INCREASE


class _ReadsAnything:
    """A bank rail of another name that would answer any question about any object."""

    name = "simbank"

    async def resolve_event(self, **_: str) -> tuple[BankEvent, ...]:
        return (BankEvent("deposit.received", {}),)

    async def list_events(self, **_: object) -> tuple[ListedEvent, ...]:
        return ()


@pytest.mark.parametrize("given", [None, _NamedIncreaseWithoutEvents(), _ReadsAnything()])
def test_only_the_senders_own_adapter_is_asked_what_its_event_means(given: object) -> None:
    with pytest.raises(ProviderMisconfigured):
        require_event_source(given, INCREASE)  # type: ignore[arg-type]


async def test_each_resolved_event_reaches_its_payment_function_told_it_was_increase(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []

    def recording(name: str) -> Any:
        async def apply(_db: Database, data: Mapping[str, Any], **told: Any) -> None:
            calls.append((name, data, told))

        return apply

    for name in (
        "apply_bank_deposit_received",
        "apply_bank_deposit_returned",
        "apply_payout_completed",
        "apply_payout_failed",
    ):
        monkeypatch.setattr(payments, name, recording(name))

    class Reader:
        name = INCREASE

        async def resolve_event(self, **_: str) -> tuple[BankEvent, ...]:
            return (
                BankEvent("payout.failed", {"payout_id": "a"}),
                BankEvent("deposit.returned", {"deposit_id": "b"}),
            )

        async def list_events(self, **_: object) -> tuple[ListedEvent, ...]:
            return ()

    handler = build_webhook_registry(Reader()).handler_for(  # type: ignore[arg-type]
        Provider.INCREASE, "ach_transfer.updated"
    )
    assert handler is not None
    event = WebhookEvent(
        id=uuid.uuid4(),
        provider=Provider.INCREASE,
        event_id="event_1",
        type="ach_transfer.updated",
        payload={"associated_object_type": "ach_transfer", "associated_object_id": TRANSFER},
        received_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        processed_at=None,
        outcome=None,
    )
    await handler(db, event)

    assert calls == [
        ("apply_payout_failed", {"payout_id": "a"}, {"provider": INCREASE}),
        ("apply_bank_deposit_returned", {"deposit_id": "b"}, {"provider": INCREASE}),
    ]


# --- listing events instead of receiving them ----------------------------------------------


def listed_event(
    identifier: str, category: str, object_type: str, object_id: str, created_at: str = CREATED
) -> dict[str, Any]:
    return {
        "id": identifier,
        "created_at": created_at,
        "category": category,
        "associated_object_type": object_type,
        "associated_object_id": object_id,
        "type": "event",
    }


async def test_a_deposit_reaches_a_worker_that_lists_events_and_receives_no_webhook(
    db: Database,
    settings: Settings,
    fake: FakeIncrease,
    increase: IncreaseBank,
    custody: Any,
    maria: User,
    clock: ManualClock,
) -> None:
    await instruction_for(db, maria, "USD", increase, custody)
    fake.credit(250_00)
    fake.events.append(
        listed_event("event_1", "inbound_ach_transfer.created", "inbound_ach_transfer", INBOUND)
    )

    await BankEventPoller(increase)(db)
    await drain(db, settings, bank=increase, custody=custody)

    assert await available(db, maria) == 250_00
    (stored,) = await rows(db, "SELECT provider, event_id, type FROM webhook_events")
    assert stored == {
        "provider": INCREASE,
        "event_id": "event_1",
        "type": "inbound_ach_transfer.created",
    }


async def test_an_event_listed_again_or_also_delivered_is_stored_and_applied_once(
    db: Database,
    settings: Settings,
    fake: FakeIncrease,
    increase: IncreaseBank,
    custody: Any,
    maria: User,
    clock: ManualClock,
) -> None:
    await instruction_for(db, maria, "USD", increase, custody)
    fake.credit(250_00)
    fake.events.append(
        listed_event("event_1", "inbound_ach_transfer.created", "inbound_ach_transfer", INBOUND)
    )
    poller = BankEventPoller(increase)

    await poller(db)
    await poller(db)
    async with db.transaction() as session:
        # The same event, delivered by webhook as well.
        await webhooks.record(
            session,
            Provider.INCREASE,
            Envelope("event_1", "inbound_ach_transfer.created", fake.events[0]),
        )
    await drain(db, settings, bank=increase, custody=custody)

    assert len(await rows(db, "SELECT id FROM webhook_events")) == 1
    assert len(await rows(db, "SELECT id FROM deposits")) == 1
    assert await available(db, maria) == 250_00


async def test_the_listing_starts_a_day_back_then_follows_the_newest_event_with_an_overlap(
    db: Database, fake: FakeIncrease, increase: IncreaseBank, clock: ManualClock
) -> None:
    poller = BankEventPoller(increase)

    await poller(db)
    fake.events.append(
        listed_event(
            "event_1", "ach_transfer.updated", "ach_transfer", TRANSFER, "2026-01-15T11:58:00Z"
        )
    )
    await poller(db)
    await poller(db)

    starts = [listing.url.params["created_at.on_or_after"] for listing in fake.listings]
    assert starts == [
        "2026-01-14T12:00:00Z",  # a day before the clock
        "2026-01-14T12:00:00Z",  # nothing was listed: from where it was
        "2026-01-15T11:53:00Z",  # five minutes before the newest event
    ]


async def test_only_a_bank_rail_whose_events_are_read_from_its_api_has_a_listing_job(
    settings: Settings, increase: IncreaseBank, bank: SimBank
) -> None:
    assert BANK_EVENT_POLL_JOB in {job.name for job in build_jobs(settings, bank=increase)}
    assert BANK_EVENT_POLL_JOB not in {job.name for job in build_jobs(settings, bank=bank)}
    assert BANK_EVENT_POLL_JOB not in {job.name for job in build_jobs(settings)}

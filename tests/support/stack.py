"""Corridor and its providers, whole and in one process, for the tests that break things.

The real pieces, wired as they are deployed: the API application, the provider simulator
delivering its signed webhooks to that API, a dispatcher draining the outbox through the
worker's own registry, and the worker's scheduled jobs. Nothing is replaced by a stand-in.
What a test adds is failure: faults and misbehaving webhooks through the simulator's
control endpoints, and a worker that dies at a chosen point through ``Crashes``.

Time is two manual clocks, Corridor's and the simulator's, moved together. ``settle`` lets
it pass in steps until nothing is in flight, and a test then asserts where the money is.
"""

import random
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import pytest
from pydantic import SecretStr

from corridor import ledger, payments, webhooks
from corridor.api.app import create_app as create_api
from corridor.ledger import AccountKind
from corridor.outbox import Dispatcher, Handler, OutboxEvent, Registry
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.money import parse_amount
from corridor.providers import SimBank, SimCustody
from corridor.worker import Scheduler, build_jobs, build_registry
from corridor.worker import main as worker_main
from corridor_sim.app import create_app as create_sim
from corridor_sim.settings import SimSettings
from tests.payments.support import rows
from tests.support.auth import RegisteredUser, register_user
from tests.support.providers import (
    ACCOUNT_NUMBER,
    API_KEY,
    BASE_URL,
    EXTERNAL_ADDRESS,
    ROUTING_NUMBER,
    Recorder,
    Sim,
    advance,
    wire,
    with_providers,
)
from tests.webhooks.helpers import BANK_SECRET, CUSTODY_SECRET, with_secrets

API_URL: Final = "http://corridor.test"

# What Corridor charges in these tests: 1.5% of a withdrawal and 1% of a transfer.
WITHDRAWAL_FEE_BPS: Final = 150
TRANSFER_FEE_BPS: Final = 100
# What the simulated providers charge Corridor for a payout that completes, in minor units.
PROVIDER_FEE: Final[Mapping[str, int]] = {"USD": 25, "MXN": 5_00, "BRL": 10, "USDC": 150_000}

# How long an adapter waits for a provider that has been told to hang: what an injected
# timeout costs in real time. Every other call waits as long as a deployed adapter does.
IMPATIENT_TIMEOUT_SECONDS: Final = 0.2
# How long the provider hangs: longer than the impatient adapter waits, so an injected
# timeout always outlasts it.
HANG_SECONDS: Final = 5.0
_HANGS: Final = frozenset({"timeout", "timeout_after_effect"})

# Which fault operation of the simulator each adapter method meets.
_OPERATIONS: Final[Mapping[str, Mapping[str, str]]] = {
    "bank": {
        "create_virtual_account": "bank.create_virtual_account",
        "create_beneficiary": "bank.create_beneficiary",
        "create_payout": "bank.create_payout",
        "get_payout": "bank.get_payout",
        "find_payouts": "bank.get_payout",
        "list_transactions": "bank.list_transactions",
    },
    "custody": {
        "create_address": "custody.create_address",
        "create_withdrawal": "custody.create_withdrawal",
        "get_withdrawal": "custody.get_withdrawal",
        "find_withdrawals": "custody.get_withdrawal",
        "list_transactions": "custody.list_transactions",
    },
}

# The points at which a worker can be made to die while it sends a withdrawal:
# before anything, between marking it and asking the provider, between the provider's
# answer and recording it, and between recording it and the outbox marking the event done.
SUBMIT_POINTS: Final = (
    "submit.start",
    "submit.before_provider",
    "submit.after_provider",
    "submit.end",
)
# And while it processes a provider's event: before anything, between applying the event
# and marking it processed, and between that mark and the outbox marking the event done.
WEBHOOK_POINTS: Final = ("webhook.start", "webhook.after_apply", "webhook.end")

_APPLY_FUNCTIONS: Final = (
    "apply_bank_deposit_received",
    "apply_bank_deposit_returned",
    "apply_payout_completed",
    "apply_payout_failed",
    "apply_chain_deposit_detected",
    "apply_chain_deposit_confirmed",
    "apply_chain_deposit_failed",
    "apply_withdrawal_completed",
    "apply_withdrawal_failed",
)
_TOPICS: Final = (
    worker_main.PING_TOPIC,
    worker_main.TRANSFER_COMPLETED_TOPIC,
    worker_main.DEPOSIT_COMPLETED_TOPIC,
    worker_main.WITHDRAWAL_SUBMIT_TOPIC,
    webhooks.RECEIVED_TOPIC,
)
_CRASHING_TOPICS: Final = {
    worker_main.WITHDRAWAL_SUBMIT_TOPIC: "submit",
    webhooks.RECEIVED_TOPIC: "webhook",
}

TERMINAL: Final = frozenset({"completed", "failed", "canceled", "released"})
IN_FLIGHT: Final = frozenset({"held", "under_review", "submitting", "submitted"})


class Crash(Exception):
    """A worker dying at a chosen point. Whatever it had committed stays committed."""


class Crashes:
    """Where the worker is to die next, and where it has."""

    def __init__(self) -> None:
        self._armed: Counter[str] = Counter()
        self.hits: list[str] = []

    def arm(self, point: str, times: int = 1) -> None:
        if point not in SUBMIT_POINTS + WEBHOOK_POINTS:
            raise ValueError(f"{point!r} is not a point a worker can be made to die at")
        self._armed[point] += times

    def at(self, point: str) -> None:
        """Die here, if a test said so."""
        if self._armed[point] > 0:
            self._armed[point] -= 1
            self.hits.append(point)
            raise Crash(point)


class _Line:
    """Corridor's line to one provider: an adapter with the deadline a deployed one has,
    and beside it an impatient one that takes the calls the provider is about to hang on.

    A deadline is real time, and this suite shares its machine. With one short deadline
    for every call, a moment's stall anywhere would pass for a provider timeout that no
    test asked for. This way only a call that is meant to time out can.
    """

    def __init__(self, provider: str, patient: Any, impatient: Any, sim_app: Any) -> None:
        self._operations = _OPERATIONS[provider]
        self._patient, self._impatient, self._sim_app = patient, impatient, sim_app

    def __getattr__(self, name: str) -> Any:
        operation = self._operations.get(name)
        hanging = operation is not None and any(
            fault.operation == operation and fault.mode in _HANGS
            # Read from the application each time: a reset replaces its state.
            for fault in self._sim_app.state.sim.faults.queued()
        )
        return getattr(self._impatient if hanging else self._patient, name)


class _WorkerProvider:
    """The worker's line to a provider. Every call is the adapter's own; the two that send a
    withdrawal can be interrupted on either side of the request."""

    def __init__(self, inner: Any, stack_hooks: _Hooks, crashes: Crashes) -> None:
        self._inner, self._hooks, self._crashes = inner, stack_hooks, crashes

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def create_payout(self, **arguments: Any) -> Any:
        return await self._send(self._inner.create_payout, arguments)

    async def create_withdrawal(self, **arguments: Any) -> Any:
        return await self._send(self._inner.create_withdrawal, arguments)

    async def _send(self, send: Callable[..., Awaitable[Any]], arguments: dict[str, Any]) -> Any:
        self._crashes.at("submit.before_provider")
        if self._hooks.before_send is not None:
            await self._hooks.before_send(uuid.UUID(arguments["reference"]))
        answer = await send(**arguments)
        self._crashes.at("submit.after_provider")
        return answer


@dataclass
class _Hooks:
    # Run after a withdrawal has been marked as being sent and before the provider is
    # asked, with the withdrawal's id: how a test puts something exactly in between.
    before_send: Callable[[uuid.UUID], Awaitable[None]] | None = None


@dataclass
class Stack:
    """The running system, and what a test does to it and reads from it."""

    settings: Settings
    db: Database
    clock: ManualClock
    api: httpx.AsyncClient
    sim: Sim
    dispatcher: Dispatcher
    scheduler: Scheduler
    crashes: Crashes
    hooks: _Hooks = field(default_factory=_Hooks)

    # --- time ----------------------------------------------------------------------------

    async def turn(self, seconds: float = 5.0) -> None:
        """Let ``seconds`` pass everywhere, and let every part do what is then due."""
        # Advancing the simulator settles its payouts, mines its blocks and attempts the
        # deliveries that are due.
        await advance(self.sim, self.clock, seconds)
        await self.dispatcher.drain()
        await self.sim.control("POST", "/webhooks/deliver")
        await self.dispatcher.drain()
        await self.scheduler.tick()
        await self.dispatcher.drain()

    async def settle(self, *, step: float = 5.0, rounds: int = 200) -> int:
        """Let time pass until nothing is in flight anywhere, and say how many steps it
        took. Bounded: a system that never comes to rest is returned as it is, for the
        test's assertions to say what is stuck."""
        for taken in range(1, rounds + 1):
            await self.turn(step)
            if await self.at_rest():
                return taken
        return rounds

    async def at_rest(self) -> bool:
        """Whether anything is still on its way: an event to process, a withdrawal not yet
        ended, a payout or a transaction the provider has not finished, a webhook it has
        still to deliver."""
        waiting = await rows(
            self.db,
            "SELECT 1 FROM outbox_events WHERE status IN ('pending', 'processing') LIMIT 1",
        )
        if waiting or any(w["status"] in IN_FLIGHT for w in await self.withdrawals()):
            return False
        if any(payout["status"] == "pending" for payout in await self.sim.payouts()):
            return False
        if any(w["status"] in ("pending", "broadcast") for w in await self.sim.withdrawals()):
            return False
        if any(d["status"] == "detected" for d in await self.provider_chain_deposits()):
            return False
        behaviour = self.sim.app.state.sim.webhooks.behaviour
        return behaviour.hold or all(event["status"] != "pending" for event in await self.events())

    # --- what the outside world does -------------------------------------------------------

    async def person(self) -> RegisteredUser:
        return await register_user(self.api)

    async def instruction(self, user: RegisteredUser, asset: str) -> httpx.Response:
        """Ask where to deposit ``asset``, as a client does."""
        return await self.api.get(
            "/v1/deposit-instructions", params={"asset": asset}, headers=user.headers
        )

    async def bank_deposit(self, user: RegisteredUser, amount: str, asset: str = "USD") -> str:
        """A bank transfer to the user's virtual account arrives. Returns the bank's id."""
        response = await self.instruction(user, asset)
        assert response.status_code == 200, response.text
        (stored,) = await rows(
            self.db,
            "SELECT provider_ref FROM deposit_instructions WHERE user_id = :u AND asset_code = :a",
            u=uuid.UUID(user.id),
            a=asset,
        )
        deposit = await self.sim.control(
            "POST",
            "/bank/deposits",
            {
                "virtual_account_id": stored["provider_ref"],
                "amount": amount,
                "sender_name": "Maria Silva",
                "reference": "INV-2041",
            },
            expect=201,
        )
        return str(deposit["id"])

    async def chain_deposit(self, user: RegisteredUser, amount: str) -> str:
        """A transaction to the user's deposit address appears. Returns the custodian's id."""
        response = await self.instruction(user, "USDC")
        assert response.status_code == 200, response.text
        deposit = await self.sim.control(
            "POST",
            "/custody/deposits",
            {
                "address": response.json()["details"]["address"],
                "amount": amount,
                "from_address": EXTERNAL_ADDRESS,
            },
            expect=201,
        )
        return str(deposit["id"])

    async def beneficiary(
        self, user: RegisteredUser, *, account_number: str = ACCOUNT_NUMBER
    ) -> str:
        response = await self.api.post(
            "/v1/beneficiaries",
            json={
                "asset": "USD",
                "holder_name": "Maria Silva",
                "account_number": account_number,
                "routing_number": ROUTING_NUMBER,
            },
            headers={**user.headers, "Idempotency-Key": f"ben-{new_id()}"},
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    async def withdraw(
        self,
        user: RegisteredUser,
        amount: str,
        *,
        asset: str = "USD",
        beneficiary_id: str | None = None,
        to_address: str | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"asset": asset, "amount": amount}
        if beneficiary_id is not None:
            body["beneficiary_id"] = beneficiary_id
        if to_address is not None:
            body["to_address"] = to_address
        return await self.api.post(
            "/v1/withdrawals",
            json=body,
            headers={**user.headers, "Idempotency-Key": f"wd-{new_id()}"},
        )

    async def cancel(self, user: RegisteredUser, withdrawal_id: str | uuid.UUID) -> httpx.Response:
        return await self.api.post(f"/v1/withdrawals/{withdrawal_id}/cancel", headers=user.headers)

    async def transfer(
        self, sender: RegisteredUser, recipient: RegisteredUser, amount: str, asset: str = "USD"
    ) -> httpx.Response:
        return await self.api.post(
            "/v1/transfers",
            json={"recipient": recipient.id, "asset": asset, "amount": amount},
            headers={**sender.headers, "Idempotency-Key": f"tr-{new_id()}"},
        )

    async def fault(self, operation: str, mode: str, **extra: object) -> None:
        """Make the next call, or calls, to a provider operation fail in one of four ways."""
        await self.sim.inject(operation, mode, **{"hang_seconds": HANG_SECONDS, **extra})

    async def webhooks_behave(self, **behaviour: object) -> None:
        await self.sim.control("POST", "/webhooks/behaviour", behaviour)

    # --- reading -------------------------------------------------------------------------

    async def wallet(self, user: RegisteredUser, asset: str = "USD") -> tuple[int, int]:
        """The user's available and held balances in minor units, as the API reports them."""
        response = await self.api.get("/v1/wallets", headers=user.headers)
        assert response.status_code == 200, response.text
        wallet = next(w for w in response.json()["wallets"] if w["asset"] == asset)
        return (
            parse_amount(wallet["available"], asset, allow_zero=True),
            parse_amount(wallet["held"], asset, allow_zero=True),
        )

    async def withdrawals(self) -> list[dict[str, Any]]:
        return await rows(self.db, "SELECT * FROM withdrawals ORDER BY id")

    async def withdrawal(self, withdrawal_id: str | uuid.UUID) -> dict[str, Any]:
        (row,) = await rows(
            self.db, "SELECT * FROM withdrawals WHERE id = :id", id=uuid.UUID(str(withdrawal_id))
        )
        return row

    async def deposits(self) -> list[dict[str, Any]]:
        return await rows(self.db, "SELECT * FROM deposits ORDER BY id")

    async def outbox(self) -> dict[str, int]:
        counted = await rows(
            self.db, "SELECT status, count(*) AS events FROM outbox_events GROUP BY status"
        )
        return {row["status"]: row["events"] for row in counted}

    async def events(self) -> list[dict[str, Any]]:
        """Every webhook event the providers made, with its delivery attempts."""
        listed: list[dict[str, Any]] = (await self.sim.control("GET", "/webhooks/events"))["events"]
        return listed

    async def provider_bank_deposits(self) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = (await self.sim.control("GET", "/bank/deposits"))["deposits"]
        return listed

    async def provider_chain_deposits(self) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = (await self.sim.control("GET", "/custody/deposits"))[
            "deposits"
        ]
        return listed

    async def provider_sends(self, withdrawal_id: str | uuid.UUID) -> list[dict[str, Any]]:
        """The payouts and on-chain withdrawals the providers hold for one of Corridor's."""
        reference = str(withdrawal_id)
        return [
            sent
            for sent in await self.sim.payouts() + await self.sim.withdrawals()
            if sent["reference"] == reference
        ]

    async def provider_balance(self, kind: str, asset: str) -> int:
        """What a provider says Corridor has with it, in minor units. May be negative."""
        balances = (await self.sim.control("GET", f"/{kind}/balances"))["balances"]
        text = str(balances.get(asset, "0"))
        negative = text.startswith("-")
        amount = parse_amount(text.lstrip("-"), asset, allow_zero=True)
        return -amount if negative else amount

    async def book_balance(
        self, kind: AccountKind, asset: str, *, provider: str | None = None
    ) -> int:
        """What Corridor's ledger says, derived from postings. Zero if never opened."""
        async with self.db.transaction() as session:
            account = await ledger.find_account(session, kind, asset, provider=provider)
            if account is None:
                return 0
            return (await ledger.derive_balances(session, [account.id]))[account.id]


def stack_settings(settings: Settings) -> Settings:
    """The test settings, as a deployment with both providers and both fees would have them."""
    return with_providers(
        with_secrets(settings),
        withdrawal_fee_bps=WITHDRAWAL_FEE_BPS,
        transfer_fee_bps=TRANSFER_FEE_BPS,
        # Time is moved by hours in a test, and by many requests in what it calls a minute.
        access_token_ttl_seconds=7 * 24 * 3600,
        rate_limit_enabled=False,
    )


@asynccontextmanager
async def running_stack(
    settings: Settings, db: Database, clock: ManualClock, *, seed: int = 0
) -> AsyncIterator[Stack]:
    """Start everything against the test's database, and stop it afterwards.

    ``seed`` drives the dispatcher's retry jitter, so that a run can be repeated exactly.
    """
    corridor = stack_settings(settings)
    crashes, hooks = Crashes(), _Hooks()
    async with AsyncExitStack() as exits:
        api_app = create_api(corridor)
        await exits.enter_async_context(api_app.router.lifespan_context(api_app))
        # An unhandled error is returned as the 500 a provider or a client would see.
        api = await exits.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
                base_url=API_URL,
            )
        )

        sim_app = create_sim(
            SimSettings(
                _env_file=None,
                api_key=SecretStr(API_KEY),
                bank_webhook_url=f"{API_URL}/v1/webhooks/simbank",
                custody_webhook_url=f"{API_URL}/v1/webhooks/simcustody",
                bank_webhook_secret=SecretStr(BANK_SECRET),
                custody_webhook_secret=SecretStr(CUSTODY_SECRET),
                clock_mode="manual",
                start_time=clock.now(),
            ),
            webhook_client=api,
        )
        await exits.enter_async_context(sim_app.router.lifespan_context(sim_app))
        transport = httpx.ASGITransport(app=sim_app)
        recorder = Recorder(transport)
        line = await exits.enter_async_context(httpx.AsyncClient(transport=recorder))
        control = await exits.enter_async_context(
            httpx.AsyncClient(transport=transport, base_url=BASE_URL)
        )
        sim = Sim(app=sim_app, http=line, recorder=recorder, _control=control)

        impatient = corridor.model_copy(
            update={"provider_timeout_seconds": IMPATIENT_TIMEOUT_SECONDS}
        )
        bank: Any = _Line(
            "bank", SimBank(corridor, client=line), SimBank(impatient, client=line), sim_app
        )
        custody: Any = _Line(
            "custody",
            SimCustody(corridor, client=line),
            SimCustody(impatient, client=line),
            sim_app,
        )
        wire(api_app, bank, custody)
        worker_bank: Any = _WorkerProvider(bank, hooks, crashes)
        worker_custody: Any = _WorkerProvider(custody, hooks, crashes)

        yield Stack(
            settings=corridor,
            db=db,
            clock=clock,
            api=api,
            sim=sim,
            dispatcher=Dispatcher(
                db,
                _registry(db, corridor, worker_bank, worker_custody, crashes),
                corridor,
                rng=random.Random(seed),  # noqa: S311 - retry jitter, repeatable on purpose
            ),
            scheduler=Scheduler(db, build_jobs(corridor, bank=worker_bank, custody=worker_custody)),
            crashes=crashes,
            hooks=hooks,
        )


def _registry(
    db: Database, settings: Settings, bank: Any, custody: Any, crashes: Crashes
) -> Registry:
    """The worker's own registry, with a place to die on either side of the two handlers
    that move money, and one inside the processing of a provider's event."""
    with pytest.MonkeyPatch.context() as patch:
        # The registry binds these when it is built, so the patch need not outlive that.
        for name in _APPLY_FUNCTIONS:
            patch.setattr(payments, name, _dying_after(getattr(payments, name), crashes))
        built = build_registry(db, settings, bank=bank, custody=custody)

    registry = Registry()
    for topic in _TOPICS:
        handler = built.handler_for(topic)
        assert handler is not None, f"the worker has no handler for {topic}"
        registry.register(topic, _dying_around(handler, _CRASHING_TOPICS.get(topic), crashes))
    return registry


def _dying_after(apply: Callable[..., Awaitable[None]], crashes: Crashes) -> Any:
    async def applied(db: Database, data: Mapping[str, Any]) -> None:
        await apply(db, data)
        crashes.at("webhook.after_apply")

    return applied


def _dying_around(handler: Handler, name: str | None, crashes: Crashes) -> Handler:
    if name is None:
        return handler

    async def handle(event: OutboxEvent) -> None:
        crashes.at(f"{name}.start")
        await handler(event)
        crashes.at(f"{name}.end")

    return handle

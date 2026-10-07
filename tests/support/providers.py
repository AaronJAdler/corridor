"""The simulated providers, in-process, wired to the adapters Corridor talks to them with.

A suite that needs a bank or a custodian imports the fixtures it wants into its
``conftest.py`` (or its test module)::

    from tests.support.providers import bank, custody, provider_settings, sim  # noqa: F401

The simulator runs with a manual clock that starts at the instant the ``clock`` fixture
starts the application's at. A test that lets time pass moves both, with ``advance``.
"""

import dataclasses
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.providers import BankRail, Custodian, SimBank, SimCustody
from corridor_sim.app import create_app
from corridor_sim.custody import address_for
from corridor_sim.settings import SimSettings

# Not secrets: the simulator needs some value for each, and these exist only here.
API_KEY: Final = "sim-test-api-key-of-32-characters-or-more"  # pragma: allowlist secret
WRONG_API_KEY: Final = (
    "sim-test-another-api-key-of-32-characters-or-more"  # pragma: allowlist secret
)
WEBHOOK_SECRET: Final = (
    "sim-test-webhook-secret-of-32-characters-or-more"  # pragma: allowlist secret
)

START: Final = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
BASE_URL: Final = "http://sim.test"

# Synthetic identifiers of the right shape for the ACH rail. The account number is the one
# the leak tests search every table and log line for.
ACCOUNT_NUMBER: Final = "000123456789"
ROUTING_NUMBER: Final = "021000021"
# An 18-digit CLABE, for the SPEI rail.
CLABE: Final = "032180000118359719"
# An account the simulated bank treats as closed: a payout to it fails when it settles.
CLOSED_ACCOUNT_NUMBER: Final = "000123450000"

# Valid simchain addresses that are not ones the custodian issued. A withdrawal to the
# second is rejected by the network when it is broadcast.
EXTERNAL_ADDRESS: Final = address_for("external".ljust(32, "a"))
REJECTED_ADDRESS: Final = address_for("dead".ljust(32, "a"))


class Recorder(httpx.AsyncBaseTransport):
    """Passes requests on to the simulator and keeps each one, so a test can see exactly
    what an adapter sent."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [
            request
            for request in self.requests
            if request.method == method and request.url.path == path
        ]


@dataclass
class Sim:
    """A running simulator, the recorded line to it, and its control endpoints."""

    app: FastAPI
    http: httpx.AsyncClient
    """What an adapter is given. It carries no credentials of its own."""
    recorder: Recorder
    _control: httpx.AsyncClient = field(repr=False)

    async def control(
        self, method: str, path: str, body: object = None, *, expect: int = 200
    ) -> Any:
        response = await self._control.request(method, f"/_control{path}", json=body)
        assert response.status_code == expect, response.text
        return response.json()

    async def advance(self, seconds: float) -> None:
        await self.control("POST", "/clock/advance", {"seconds": seconds})

    async def mine(self, blocks: int) -> None:
        await self.control("POST", "/chain/mine", {"blocks": blocks})

    async def inject(self, operation: str, mode: str, **extra: object) -> None:
        await self.control(
            "POST", "/faults", {"operation": operation, "mode": mode, **extra}, expect=201
        )

    async def payouts(self) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = (await self.control("GET", "/bank/payouts"))["payouts"]
        return listed

    async def withdrawals(self) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = (await self.control("GET", "/custody/withdrawals"))[
            "withdrawals"
        ]
        return listed

    async def events(self, event_type: str) -> list[dict[str, Any]]:
        """The ``data`` of every webhook event of one type, oldest first: exactly what a
        verified delivery would hand to Corridor."""
        listed = (await self.control("GET", "/webhooks/events"))["events"]
        return [event["data"] for event in listed if event["type"] == event_type]

    async def last_event(self, event_type: str) -> dict[str, Any]:
        return (await self.events(event_type))[-1]

    async def bank_deposit(
        self, virtual_account_id: str, amount: str, **more: object
    ) -> dict[str, Any]:
        """A customer's bank transfer arrives. Returns the ``deposit.received`` data."""
        await self.control(
            "POST",
            "/bank/deposits",
            {
                "virtual_account_id": virtual_account_id,
                "amount": amount,
                "sender_name": "Maria Silva",
                "reference": "INV-2041",
                **more,
            },
            expect=201,
        )
        return await self.last_event("deposit.received")

    async def return_bank_deposit(self, deposit_id: str) -> dict[str, Any]:
        """The sending bank recalls a deposit. Returns the ``deposit.returned`` data."""
        await self.control("POST", f"/bank/deposits/{deposit_id}/return", {"reason": "recalled"})
        return await self.last_event("deposit.returned")

    async def chain_deposit(self, address: str, amount: str) -> dict[str, Any]:
        """A transaction to a deposit address appears. Returns the ``deposit.detected`` data."""
        await self.control(
            "POST",
            "/custody/deposits",
            {"address": address, "amount": amount, "from_address": EXTERNAL_ADDRESS},
            expect=201,
        )
        return await self.last_event("deposit.detected")

    async def drop_chain_deposit(self, deposit_id: str) -> dict[str, Any]:
        await self.control("POST", f"/custody/deposits/{deposit_id}/drop")
        return await self.last_event("deposit.failed")


async def advance(sim: Sim, clock: ManualClock, seconds: float) -> None:
    """Let time pass for Corridor and for the providers alike."""
    clock.advance(seconds=seconds)
    await sim.advance(seconds)


def with_providers(settings: Settings, **overrides: object) -> Settings:
    """The settings, pointing the bank and custody adapters at the simulator."""
    return settings.model_copy(
        update={
            "bank_rail_url": BASE_URL,
            "custody_url": BASE_URL,
            "bank_rail_api_key": SecretStr(API_KEY),
            "custody_api_key": SecretStr(API_KEY),
            **overrides,
        }
    )


def wire(app: FastAPI, bank: BankRail | None, custody: Custodian | None) -> None:
    """Give a started API these provider clients in place of whatever it built."""
    app.state.container = dataclasses.replace(app.state.container, bank=bank, custody=custody)


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    settings = SimSettings(
        _env_file=None,
        api_key=SecretStr(API_KEY),
        bank_webhook_secret=SecretStr(WEBHOOK_SECRET),
        custody_webhook_secret=SecretStr(WEBHOOK_SECRET),
        clock_mode="manual",
        start_time=START,
    )
    app = create_app(settings)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        # An unexpected error in the simulator is raised into the test rather than
        # rendered, so it cannot pass for a response.
        transport = httpx.ASGITransport(app=app)
        recorder = Recorder(transport)
        http = await stack.enter_async_context(httpx.AsyncClient(transport=recorder))
        control = await stack.enter_async_context(
            httpx.AsyncClient(transport=transport, base_url=BASE_URL)
        )
        yield Sim(app=app, http=http, recorder=recorder, _control=control)


@pytest.fixture
def provider_settings(settings: Settings) -> Settings:
    return with_providers(settings)


@pytest.fixture
def bank(sim: Sim, provider_settings: Settings) -> SimBank:
    return SimBank(provider_settings, client=sim.http)


@pytest.fixture
def custody(sim: Sim, provider_settings: Settings) -> SimCustody:
    return SimCustody(provider_settings, client=sim.http)

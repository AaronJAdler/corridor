"""Fixtures for the provider adapters' tests.

The adapters talk to the real simulator, running in-process with a manual clock, through a
transport that records every request they send. Nothing here uses the database or Redis:
an adapter holds no state of its own.
"""

import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from corridor.platform.config import Settings
from corridor.platform.logging import configure_logging
from corridor.providers import SimBank, SimCustody, SimRates
from corridor_sim.app import create_app
from corridor_sim.custody import address_for
from corridor_sim.settings import SimSettings

# Not secrets: the simulator needs some value for each, and these exist only here.
API_KEY = "sim-test-api-key-of-32-characters-or-more"  # pragma: allowlist secret
WRONG_API_KEY = "sim-test-another-api-key-of-32-characters-or-more"  # pragma: allowlist secret
WEBHOOK_SECRET = "sim-test-webhook-secret-of-32-characters-or-more"  # pragma: allowlist secret

START = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
BASE_URL = "http://sim.test"
CUSTOMER = "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10"
REFERENCE = "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f"

# Synthetic identifiers of the right shape for the ACH rail.
ACCOUNT_NUMBER = "000123456789"
ROUTING_NUMBER = "021000021"
CLABE = "032180000118359719"

# A valid simchain address that is not one the custodian issued.
EXTERNAL_ADDRESS = address_for("external".ljust(32, "a"))

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder(httpx.AsyncBaseTransport):
    """Passes requests on to the simulator and keeps each one, so a test can see exactly
    what an adapter sent."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)

    def sent(self, method: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == method]


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


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    settings = SimSettings(
        _env_file=None,
        api_key=SecretStr(API_KEY),
        bank_webhook_secret=SecretStr(WEBHOOK_SECRET),
        custody_webhook_secret=SecretStr(WEBHOOK_SECRET),
        clock_mode="manual",
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
def provider_settings() -> Settings:
    """Settings that point every adapter at the simulator. The stores are never opened."""
    return Settings(
        _env_file=None,
        environment="test",
        database_url=SecretStr("postgresql://unused.invalid/unused"),
        redis_url=SecretStr("redis://unused.invalid/0"),
        bank_rail_url=BASE_URL,
        custody_url=BASE_URL,
        fx_rates_url=BASE_URL,
        bank_rail_api_key=SecretStr(API_KEY),
        custody_api_key=SecretStr(API_KEY),
        fx_rates_api_key=SecretStr(API_KEY),
    )


@pytest.fixture
def bank(sim: Sim, provider_settings: Settings) -> SimBank:
    return SimBank(provider_settings, client=sim.http)


@pytest.fixture
def custody(sim: Sim, provider_settings: Settings) -> SimCustody:
    return SimCustody(provider_settings, client=sim.http)


@pytest.fixture
def rates(sim: Sim, provider_settings: Settings) -> SimRates:
    return SimRates(provider_settings, client=sim.http)


@pytest.fixture
async def stub() -> AsyncIterator[Callable[[Handler], httpx.AsyncClient]]:
    """A client whose every response is written by the test: a provider that misbehaves in
    ways the simulator does not."""
    async with AsyncExitStack() as stack:

        def build(handler: Handler) -> httpx.AsyncClient:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            stack.push_async_callback(client.aclose)
            return client

        yield build


def answer(status: int, document: object) -> Handler:
    return lambda _request: httpx.Response(status, json=document)


LogReader = Callable[[], str]


@contextmanager
def captured_logs(capsys: pytest.CaptureFixture[str]) -> Iterator[LogReader]:
    """Everything logged inside the block, as the JSON lines a deployment would write."""
    configure_logging("INFO", "json")
    capsys.readouterr()
    yield lambda: capsys.readouterr().out


def log_events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]

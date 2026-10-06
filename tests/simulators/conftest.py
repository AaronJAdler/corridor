"""Fixtures for the simulators' own tests.

The simulator runs in-process with a manual clock. Where Corridor's webhook endpoints would
be there is a scripted receiver that records what it is sent. Nothing here uses the
repository's database, Redis, clock or app fixtures: the simulator has its own clock and no
stores.
"""

import hashlib
import hmac
import json
import os
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from starlette.types import Receive, Scope, Send

from corridor_sim.app import create_app
from corridor_sim.settings import SimSettings

# Not secrets: the simulator needs some value for each, and these exist only here.
API_KEY = "sim-test-api-key"  # pragma: allowlist secret
BANK_WEBHOOK_SECRET = "sim-test-bank-webhook-secret"  # pragma: allowlist secret
CUSTODY_WEBHOOK_SECRET = "sim-test-custody-webhook-secret"  # pragma: allowlist secret

START = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
BASE_URL = "http://sim.test"
BANK_WEBHOOK_URL = "http://corridor.test/v1/webhooks/simbank"
CUSTODY_WEBHOOK_URL = "http://corridor.test/v1/webhooks/simcustody"

CUSTOMER = "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10"
# An account identifier of the right shape for each asset's rail.
ACCOUNTS: Mapping[str, Mapping[str, str]] = {
    "USD": {"account_number": "000123456789", "routing_number": "021000021"},
    "MXN": {"account_number": "032180000118359719"},
    "BRL": {"account_number": "maria.silva@example.com"},
}

# What the receiver does with a request: answer with a status, raise, or run a coroutine
# that returns the status (to hang, or to wait for the test).
Answer = int | BaseException | Callable[[], Awaitable[int]]


@dataclass(frozen=True)
class Received:
    """One request as the receiver saw it."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.body)
        return document


class Receiver:
    """A stand-in for Corridor's webhook endpoints, as a bare ASGI application."""

    def __init__(self) -> None:
        self.requests: list[Received] = []
        self._script: deque[Answer] = deque()

    def answer(self, *answers: Answer) -> None:
        """Queue answers for the next requests. When they run out, every answer is 200."""
        self._script.extend(answers)

    def of_type(self, event_type: str) -> list[Received]:
        return [request for request in self.requests if request.json()["type"] == event_type]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        headers = {
            name.decode("latin-1"): value.decode("latin-1") for name, value in scope["headers"]
        }
        self.requests.append(
            Received(
                method=scope["method"],
                url=f"{scope['scheme']}://{headers['host']}{scope['path']}",
                headers=headers,
                body=body,
            )
        )

        answer = self._script.popleft() if self._script else 200
        if isinstance(answer, BaseException):
            raise answer
        status = answer if isinstance(answer, int) else await answer()
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def signed_at(request: Received, secret: str) -> int:
    """Verify a delivery's signature and return its timestamp.

    Written from the contract's text, not from the simulator's code: the header is
    ``X-Signature: t=<unix seconds>,v1=<hex>`` and ``v1`` is HMAC-SHA256, keyed with the
    shared secret, over the bytes ``<t>.<raw request body>``.
    """
    fields = dict(part.split("=", 1) for part in request.headers["x-signature"].split(","))
    assert set(fields) == {"t", "v1"}
    expected = hmac.new(
        secret.encode(), fields["t"].encode() + b"." + request.body, hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(fields["v1"], expected), "the signature does not match the body"
    return int(fields["t"])


def address_with_body(body: str) -> str:
    """A simchain address, built from the contract's text: ``sim1``, a 32-character body,
    then the first four bytes of the SHA-256 of the body's ASCII bytes, in hex."""
    assert len(body) == 32
    return "sim1" + body + hashlib.sha256(body.encode("ascii")).digest()[:4].hex()


def sim_settings(**overrides: Any) -> SimSettings:
    values: dict[str, Any] = {
        "api_key": SecretStr(API_KEY),
        "bank_webhook_secret": SecretStr(BANK_WEBHOOK_SECRET),
        "custody_webhook_secret": SecretStr(CUSTODY_WEBHOOK_SECRET),
        "bank_webhook_url": BANK_WEBHOOK_URL,
        "custody_webhook_url": CUSTODY_WEBHOOK_URL,
        "clock_mode": "manual",
    }
    values.update(overrides)
    return SimSettings(_env_file=None, **values)


@dataclass
class Sim:
    """A running simulator and the two ways of talking to it."""

    app: FastAPI
    api: httpx.AsyncClient
    """Sends the API key, as Corridor's adapters do."""
    anonymous: httpx.AsyncClient
    """Sends no credentials, as a test driving ``/_control`` does."""
    receiver: Receiver
    settings: SimSettings
    _keys: int = field(default=0, repr=False)

    def key(self) -> str:
        """A fresh idempotency key."""
        self._keys += 1
        return f"key-{self._keys}"

    # --- control ---------------------------------------------------------------------------

    async def control(
        self, method: str, path: str, body: object = None, *, expect: int = 200
    ) -> Any:
        response = await self.anonymous.request(method, f"/_control{path}", json=body)
        assert response.status_code == expect, response.text
        return response.json()

    async def advance(self, seconds: float) -> None:
        await self.control("POST", "/clock/advance", {"seconds": seconds})

    async def deliver(self) -> list[dict[str, Any]]:
        deliveries: list[dict[str, Any]] = (await self.control("POST", "/webhooks/deliver"))[
            "deliveries"
        ]
        return deliveries

    async def mine(self, blocks: int) -> int:
        height: int = (await self.control("POST", "/chain/mine", {"blocks": blocks}))["height"]
        return height

    async def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = (await self.control("GET", "/webhooks/events"))["events"]
        return [event for event in events if event_type in (None, event["type"])]

    async def balance(self, provider: str, asset: str) -> str:
        balances: dict[str, str] = (await self.control("GET", f"/{provider}/balances"))["balances"]
        return balances[asset]

    async def inspect(self, provider: str, what: str) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = (await self.control("GET", f"/{provider}/{what}"))[what]
        return listed

    async def bank_deposit(
        self,
        virtual_account_id: str,
        amount: str,
        *,
        sender_name: str = "Joao Souza",
        reference: str = "INV-2041",
        **extra: object,
    ) -> dict[str, Any]:
        deposit: dict[str, Any] = await self.control(
            "POST",
            "/bank/deposits",
            {
                "virtual_account_id": virtual_account_id,
                "amount": amount,
                "sender_name": sender_name,
                "reference": reference,
                **extra,
            },
            expect=201,
        )
        return deposit

    async def chain_deposit(
        self, address: str, amount: str, *, from_address: str | None = None
    ) -> dict[str, Any]:
        deposit: dict[str, Any] = await self.control(
            "POST",
            "/custody/deposits",
            {
                "address": address,
                "amount": amount,
                "from_address": from_address or address_with_body("sender".ljust(32, "a")),
            },
            expect=201,
        )
        return deposit

    # --- bank ------------------------------------------------------------------------------

    async def virtual_account(self, asset: str = "USD", customer: str = CUSTOMER) -> dict[str, Any]:
        response = await self.api.post(
            "/bank/v1/virtual-accounts", json={"customer_reference": customer, "asset": asset}
        )
        assert response.status_code in (200, 201), response.text
        account: dict[str, Any] = response.json()
        return account

    async def beneficiary(self, asset: str = "USD", **fields: object) -> dict[str, Any]:
        response = await self.api.post(
            "/bank/v1/beneficiaries",
            json={
                "customer_reference": CUSTOMER,
                "asset": asset,
                "holder_name": "Maria Silva",
                **ACCOUNTS[asset],
                **fields,
            },
        )
        assert response.status_code == 201, response.text
        beneficiary: dict[str, Any] = response.json()
        return beneficiary

    async def post_payout(
        self,
        beneficiary_id: str,
        amount: str = "100.00",
        asset: str = "USD",
        *,
        reference: str = "wd-1",
        key: str | None = None,
    ) -> httpx.Response:
        return await self.api.post(
            "/bank/v1/payouts",
            json={
                "beneficiary_id": beneficiary_id,
                "asset": asset,
                "amount": amount,
                "reference": reference,
            },
            headers={"Idempotency-Key": key or self.key()},
        )

    async def payout(
        self,
        beneficiary_id: str,
        amount: str = "100.00",
        asset: str = "USD",
        *,
        reference: str = "wd-1",
        key: str | None = None,
    ) -> dict[str, Any]:
        response = await self.post_payout(
            beneficiary_id, amount, asset, reference=reference, key=key
        )
        assert response.status_code == 201, response.text
        payout: dict[str, Any] = response.json()
        return payout

    async def get_payout(self, payout_id: str) -> dict[str, Any]:
        response = await self.api.get(f"/bank/v1/payouts/{payout_id}")
        assert response.status_code == 200, response.text
        payout: dict[str, Any] = response.json()
        return payout

    async def statement(
        self,
        provider: str,
        asset: str,
        start: str = "2026-01-01T00:00:00Z",
        end: str = "2027-01-01T00:00:00Z",
    ) -> dict[str, Any]:
        response = await self.api.get(
            f"/{provider}/v1/transactions", params={"asset": asset, "from": start, "to": end}
        )
        assert response.status_code == 200, response.text
        statement: dict[str, Any] = response.json()
        return statement

    # --- custody ---------------------------------------------------------------------------

    async def address(self, customer: str = CUSTOMER) -> dict[str, Any]:
        response = await self.api.post(
            "/custody/v1/addresses", json={"customer_reference": customer, "asset": "USDC"}
        )
        assert response.status_code in (200, 201), response.text
        address: dict[str, Any] = response.json()
        return address

    async def post_withdrawal(
        self,
        to_address: str,
        amount: str = "25.000000",
        *,
        asset: str = "USDC",
        reference: str = "wd-1",
        key: str | None = None,
    ) -> httpx.Response:
        return await self.api.post(
            "/custody/v1/withdrawals",
            json={
                "asset": asset,
                "amount": amount,
                "to_address": to_address,
                "reference": reference,
            },
            headers={"Idempotency-Key": key or self.key()},
        )

    async def withdrawal(
        self,
        to_address: str,
        amount: str = "25.000000",
        *,
        reference: str = "wd-1",
        key: str | None = None,
    ) -> dict[str, Any]:
        response = await self.post_withdrawal(to_address, amount, reference=reference, key=key)
        assert response.status_code == 201, response.text
        withdrawal: dict[str, Any] = response.json()
        return withdrawal

    async def get_withdrawal(self, withdrawal_id: str) -> dict[str, Any]:
        response = await self.api.get(f"/custody/v1/withdrawals/{withdrawal_id}")
        assert response.status_code == 200, response.text
        withdrawal: dict[str, Any] = response.json()
        return withdrawal


@asynccontextmanager
async def running_sim(**overrides: Any) -> AsyncIterator[Sim]:
    """Start a simulator with its lifespan running, wired to a fresh receiver."""
    settings = sim_settings(**overrides)
    receiver = Receiver()
    async with AsyncExitStack() as stack:
        webhook_client = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=receiver))
        )
        app = create_app(settings, webhook_client=webhook_client)
        await stack.enter_async_context(app.router.lifespan_context(app))
        # An unexpected error in the simulator is raised into the test rather than
        # rendered, so it cannot pass for a response.
        transport = httpx.ASGITransport(app=app)
        api = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=transport,
                base_url=BASE_URL,
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
        )
        anonymous = await stack.enter_async_context(
            httpx.AsyncClient(transport=transport, base_url=BASE_URL)
        )
        yield Sim(app=app, api=api, anonymous=anonymous, receiver=receiver, settings=settings)


@pytest.fixture(autouse=True)
def no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray ``CORRIDOR_SIM_`` variable in the environment never reaches a test."""
    for name in list(os.environ):
        if name.startswith("CORRIDOR_SIM_"):
            monkeypatch.delenv(name)


@pytest.fixture
async def launch() -> AsyncIterator[Callable[..., Awaitable[Sim]]]:
    """Start simulators with settings of the test's choosing; all are stopped afterwards."""
    async with AsyncExitStack() as stack:

        async def start(**overrides: Any) -> Sim:
            return await stack.enter_async_context(running_sim(**overrides))

        yield start


@pytest.fixture
async def sim(launch: Callable[..., Awaitable[Sim]]) -> Sim:
    """A simulator with a manual clock at 2026-01-15T12:00:00Z and both webhook URLs set."""
    return await launch()

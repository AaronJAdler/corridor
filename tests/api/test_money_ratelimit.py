"""The limit on the routes that move money: counted by who is acting, and refusing a write
that it cannot count."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY
from pydantic import SecretStr
from sqlalchemy import text

from corridor import wallets
from corridor.api.app import create_app
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.api.ratelimit import money_rate_limit
from corridor.identity import Scope
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.redis import RedisStore
from tests.agents.support import an_agent
from tests.support.auth import RegisteredUser, register_user, served_routes
from tests.support.ledger import fund

TRANSFERS = "/v1/transfers"
# Nothing listens on port 1, so a connection there is refused immediately.
DEAD_REDIS = SecretStr("redis://127.0.0.1:1/0")

WRITES = 2
READS = 3


@pytest.fixture(autouse=True)
def _frozen_clock_and_own_keys(clock: ManualClock, redis: RedisStore) -> None:
    """Every test here runs with the clock frozen, so a bucket refills only when the test
    moves time, and has its Redis keys removed when it ends."""


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "rate_limit_money_write_per_minute": WRITES,
            "rate_limit_money_read_per_minute": READS,
        }
    )


@asynccontextmanager
async def serving(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://corridor.test") as http:
            yield http


async def a_user_with_money(client: httpx.AsyncClient, db: Database) -> RegisteredUser:
    user = await register_user(client)
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), "USD")
        await fund(session, wallet.available_account_id, 100_00, "USD")
    return user


async def send(
    client: httpx.AsyncClient, headers: dict[str, str], recipient: RegisteredUser
) -> httpx.Response:
    return await client.post(
        TRANSFERS,
        json={"recipient": recipient.id, "asset": "USD", "amount": "1.00"},
        headers={**headers, "Idempotency-Key": f"limit-{new_id()}"},
    )


async def transfers(db: Database) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text("SELECT count(*) FROM transfers"))).scalar_one())


def rejections(group: str) -> float:
    return (
        REGISTRY.get_sample_value("corridor_rate_limit_rejections_total", {"group": group}) or 0.0
    )


def assert_problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body: dict[str, Any] = response.json()
    assert body["code"] == code
    return body


# --- which routes ------------------------------------------------------------------------------


def test_the_money_routes_are_the_ones_that_carry_the_limit(app: FastAPI) -> None:
    limited = {
        (route.method, route.path)
        for route in served_routes(app)
        if money_rate_limit in route.calls
    }

    # Written out in full: a money route added without the limit shows up as missing here.
    assert limited == {
        ("POST", "/v1/transfers"),
        ("GET", "/v1/transfers"),
        ("GET", "/v1/transfers/{transfer_id}"),
        ("POST", "/v1/withdrawals"),
        ("GET", "/v1/withdrawals"),
        ("GET", "/v1/withdrawals/{withdrawal_id}"),
        ("POST", "/v1/withdrawals/{withdrawal_id}/cancel"),
        ("POST", "/v1/fx/quotes"),
        ("POST", "/v1/fx/conversions"),
        ("GET", "/v1/fx/conversions/{conversion_id}"),
        ("POST", "/v1/beneficiaries"),
        ("GET", "/v1/beneficiaries"),
        ("GET", "/v1/deposit-instructions"),
        # Approving what an agent asked for makes the movement.
        ("POST", "/v1/approvals/{approval_id}/approve"),
    }


def test_the_defaults_leave_room_for_a_person_and_stop_a_loop() -> None:
    fields = Settings.model_fields

    assert fields["rate_limit_money_write_per_minute"].default == 120
    assert fields["rate_limit_money_read_per_minute"].default == 600


# --- counted by who is acting ------------------------------------------------------------------


async def test_a_user_moves_money_up_to_the_limit_and_is_then_refused(
    client: httpx.AsyncClient, db: Database
) -> None:
    maria, joao = await a_user_with_money(client, db), await register_user(client)
    before = rejections("money_write")

    served = [(await send(client, maria.headers, joao)).status_code for _ in range(WRITES)]
    refused = await send(client, maria.headers, joao)

    assert served == [201] * WRITES
    assert_problem(refused, 429, "rate_limited")
    assert refused.headers["retry-after"] == "30"
    assert await transfers(db) == WRITES
    assert rejections("money_write") == before + 1


async def test_the_limit_is_earned_back_with_time(
    client: httpx.AsyncClient, db: Database, clock: ManualClock
) -> None:
    maria, joao = await a_user_with_money(client, db), await register_user(client)
    for _ in range(WRITES):
        await send(client, maria.headers, joao)

    clock.advance(seconds=29)
    too_soon = await send(client, maria.headers, joao)
    clock.advance(seconds=1)
    in_time = await send(client, maria.headers, joao)

    assert (too_soon.status_code, in_time.status_code) == (429, 201)


async def test_each_user_has_a_limit_of_their_own_though_they_share_an_address(
    client: httpx.AsyncClient, db: Database
) -> None:
    maria, joao = await a_user_with_money(client, db), await a_user_with_money(client, db)
    for _ in range(WRITES):
        await send(client, maria.headers, joao)

    spent = await send(client, maria.headers, joao)
    other = await send(client, joao.headers, maria)

    assert (spent.status_code, other.status_code) == (429, 201)


async def test_an_agent_has_a_limit_of_its_own_and_does_not_use_up_its_owners(
    client: httpx.AsyncClient, db: Database
) -> None:
    maria, joao = await a_user_with_money(client, db), await register_user(client)
    agent = await an_agent(client, maria, Scope.TRANSFERS_CREATE, any_recipient=True)

    by_agent = [(await send(client, agent.headers, joao)).status_code for _ in range(WRITES + 1)]
    by_owner = await send(client, maria.headers, joao)

    assert by_agent == [201] * WRITES + [429]
    assert by_owner.status_code == 201


async def test_reads_are_counted_apart_from_writes(client: httpx.AsyncClient, db: Database) -> None:
    maria, joao = await a_user_with_money(client, db), await register_user(client)
    before = rejections("money_read")

    reads = [
        (await client.get(TRANSFERS, headers=maria.headers)).status_code for _ in range(READS + 1)
    ]
    # The reads being spent takes nothing from what may be written.
    written = await send(client, maria.headers, joao)

    assert reads == [200] * READS + [429]
    assert written.status_code == 201
    assert rejections("money_read") == before + 1


async def test_a_request_with_no_credential_is_refused_as_such_and_counted_against_nobody(
    client: httpx.AsyncClient, db: Database
) -> None:
    maria, joao = await a_user_with_money(client, db), await register_user(client)

    anonymous = [await send(client, {}, joao) for _ in range(WRITES + 1)]
    after = await send(client, maria.headers, joao)

    assert [response.status_code for response in anonymous] == [401] * (WRITES + 1)
    assert after.status_code == 201


async def test_a_route_outside_the_money_routes_is_not_counted(
    client: httpx.AsyncClient, db: Database
) -> None:
    maria = await a_user_with_money(client, db)

    served = [
        (await client.get("/v1/wallets", headers=maria.headers)).status_code
        for _ in range(READS + 2)
    ]

    assert served == [200] * (READS + 2)


# --- Redis unavailable -------------------------------------------------------------------------


async def test_with_redis_unreachable_a_write_is_refused_and_nothing_moves(
    settings: Settings, db: Database
) -> None:
    async with serving(settings.model_copy(update={"redis_url": DEAD_REDIS})) as http:
        maria, joao = await a_user_with_money(http, db), await register_user(http)

        refused = await send(http, maria.headers, joao)

    body = assert_problem(refused, 503, "rate_limiter_unavailable")
    assert body["title"] == "Service unavailable"
    assert refused.headers["retry-after"] == "5"
    assert await transfers(db) == 0


async def test_with_redis_unreachable_a_read_is_served(settings: Settings, db: Database) -> None:
    async with serving(settings.model_copy(update={"redis_url": DEAD_REDIS})) as http:
        maria = await a_user_with_money(http, db)

        reads = [
            (await http.get(TRANSFERS, headers=maria.headers)).status_code for _ in range(READS + 1)
        ]

    # Uncounted, and so not limited either.
    assert reads == [200] * (READS + 1)


async def test_with_rate_limiting_switched_off_redis_is_not_asked(
    settings: Settings, db: Database
) -> None:
    off = settings.model_copy(update={"redis_url": DEAD_REDIS, "rate_limit_enabled": False})
    async with serving(off) as http:
        maria, joao = await a_user_with_money(http, db), await register_user(http)

        served = [(await send(http, maria.headers, joao)).status_code for _ in range(WRITES + 1)]

    assert served == [201] * (WRITES + 1)

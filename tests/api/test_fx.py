"""FX over HTTP: a quote from the simulated rate source, and its conversion."""

import asyncio
import dataclasses
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text

from corridor import identity, wallets
from corridor.api.deps import get_principal
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.identity import Principal, Scope
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import SimRates
from tests.support.auth import RegisteredUser, register_user
from tests.support.ledger import fund
from tests.support.providers import API_KEY, BASE_URL, Sim, sim  # noqa: F401

QUOTES = "/v1/fx/quotes"
CONVERSIONS = "/v1/fx/conversions"
RATE_PATH = "/fx/v1/rates/USD/MXN"


async def deposit(db: Database, user: RegisteredUser, amount: int, asset: str = "USD") -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), asset)
        await fund(session, wallet.available_account_id, amount, asset)


async def available(client: httpx.AsyncClient, user: RegisteredUser, asset: str = "USD") -> str:
    response = await client.get("/v1/wallets", headers=user.headers)
    assert response.status_code == 200, response.text
    return next(w["available"] for w in response.json()["wallets"] if w["asset"] == asset)


async def count(db: Database, table: str) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


def assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == code


def agent_for(user: RegisteredUser, *scopes: str) -> Principal:
    return Principal(
        user_id=uuid.UUID(user.id),
        actor_type="agent",
        actor_id=new_id(),
        role="user",
        scopes=frozenset(scopes),
        session_id=None,
    )


async def pin(
    sim: Sim,  # noqa: F811
    mid: str,
    base: str = "USD",
    quote: str = "MXN",
) -> None:
    await sim.control("POST", "/fx/rates", {"base": base, "quote": quote, "mid": mid})


async def ask(
    client: httpx.AsyncClient, user: RegisteredUser, amount: str = "100.00", **more: Any
) -> httpx.Response:
    body = {"sell_asset": "USD", "buy_asset": "MXN", "sell_amount": amount, **more}
    return await client.post(QUOTES, json=body, headers=user.headers)


async def quoted(client: httpx.AsyncClient, user: RegisteredUser, amount: str = "100.00") -> str:
    response = await ask(client, user, amount)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def convert(
    client: httpx.AsyncClient, user: RegisteredUser, quote_id: str, *, key: str | None = "key-1"
) -> httpx.Response:
    headers = dict(user.headers)
    if key is not None:
        headers["Idempotency-Key"] = key
    return await client.post(CONVERSIONS, json={"quote_id": quote_id}, headers=headers)


@pytest.fixture
async def rates(
    app: FastAPI,
    settings: Settings,
    sim: Sim,  # noqa: F811
    clock: ManualClock,
) -> SimRates:
    """The running API, given the simulated rate source with USD/MXN pinned at 17.25.

    The application's clock and the simulator's start at the same instant, so the
    simulator's rates are fresh until a test lets the application's clock run ahead.
    """
    source = SimRates(
        settings.model_copy(
            update={"fx_rates_url": BASE_URL, "fx_rates_api_key": SecretStr(API_KEY)}
        ),
        client=sim.http,
    )
    app.state.container = dataclasses.replace(app.state.container, rates=source)
    await pin(sim, "17.25")
    return source


@pytest.fixture
async def maria(client: httpx.AsyncClient, db: Database, clock: ManualClock) -> RegisteredUser:
    """A user with 250.00 USD."""
    user = await register_user(client, handle="maria")
    await deposit(db, user, 250_00)
    return user


@pytest.fixture
async def joao(client: httpx.AsyncClient, clock: ManualClock) -> RegisteredUser:
    return await register_user(client, handle="joao")


@pytest.mark.usefixtures("rates")
async def test_a_quote_is_made_and_converted(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    quote = await ask(client, maria, "100.00")

    assert quote.status_code == 201, quote.text
    assert quote.json() == {
        "id": quote.json()["id"],
        "sell_asset": "USD",
        "sell_amount": "100.00",
        "buy_asset": "MXN",
        # 50 basis points off 17.25, and the half centavo of 1716.375 is not paid.
        "buy_amount": "1716.37",
        "rate": "17.16375",
        "expires_at": "2026-01-15T12:00:30Z",
    }

    conversion = await convert(client, maria, quote.json()["id"])

    assert conversion.status_code == 201, conversion.text
    assert "idempotent-replayed" not in conversion.headers
    body = conversion.json()
    uuid.UUID(body["id"])
    assert body == {
        "id": body["id"],
        "quote_id": quote.json()["id"],
        "sell_asset": "USD",
        "sell_amount": "100.00",
        "buy_asset": "MXN",
        "buy_amount": "1716.37",
        "rate": "17.16375",
        "created_at": "2026-01-15T12:00:00Z",
    }
    assert await available(client, maria, "USD") == "150.00"
    assert await available(client, maria, "MXN") == "1716.37"

    read = await client.get(f"{CONVERSIONS}/{body['id']}", headers=maria.headers)
    assert read.status_code == 200, read.text
    assert read.json() == body


@pytest.mark.usefixtures("rates")
async def test_amounts_are_decimal_strings_in_each_assets_own_scale(
    client: httpx.AsyncClient,
    maria: RegisteredUser,
    sim: Sim,  # noqa: F811
) -> None:
    await pin(sim, "1.0001", "USD", "USDC")

    quote = await ask(client, maria, "10", buy_asset="USDC")

    assert quote.status_code == 201, quote.text
    assert (quote.json()["sell_amount"], quote.json()["buy_amount"]) == ("10.00", "9.950995")
    assert quote.json()["rate"] == "0.9950995"


@pytest.mark.usefixtures("rates")
async def test_the_quoted_amounts_are_paid_even_if_the_rate_has_moved(
    client: httpx.AsyncClient,
    maria: RegisteredUser,
    sim: Sim,  # noqa: F811
    clock: ManualClock,
) -> None:
    quote_id = await quoted(client, maria, "100.00")
    await pin(sim, "20.5")
    # Long enough for the cached rate to be forgotten, by the clock that decides it.
    clock.advance(seconds=16)
    await sim.advance(16)
    assert (await ask(client, maria, "100.00")).json()["buy_amount"] == "2039.75"

    conversion = await convert(client, maria, quote_id)

    assert conversion.status_code == 201, conversion.text
    assert (conversion.json()["buy_amount"], conversion.json()["rate"]) == ("1716.37", "17.16375")
    assert await available(client, maria, "MXN") == "1716.37"


@pytest.mark.usefixtures("rates")
async def test_the_rate_is_fetched_once_for_quotes_made_close_together(
    client: httpx.AsyncClient,
    maria: RegisteredUser,
    sim: Sim,  # noqa: F811
) -> None:
    await quoted(client, maria)
    await quoted(client, maria)

    assert len(sim.recorder.sent("GET", RATE_PATH)) == 1


@pytest.mark.usefixtures("rates")
async def test_a_stale_rate_is_a_503_and_no_quote(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, clock: ManualClock
) -> None:
    # The source has not moved; the application's clock has.
    clock.advance(seconds=16)

    assert_problem(await ask(client, maria), 503, "rate_unavailable")
    assert await count(db, "fx_quotes") == 0


async def test_without_a_rate_source_a_quote_is_a_503(
    client: httpx.AsyncClient, app: FastAPI, db: Database, maria: RegisteredUser
) -> None:
    assert app.state.container.rates is None

    assert_problem(await ask(client, maria), 503, "rate_unavailable")
    assert await count(db, "fx_quotes") == 0


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"sell_asset": "USD", "buy_asset": "USD", "sell_amount": "1.00"}, "same_asset"),
        ({"sell_asset": "EUR", "buy_asset": "USD", "sell_amount": "1.00"}, "unknown_asset"),
        ({"sell_asset": "USD", "buy_asset": "EUR", "sell_amount": "1.00"}, "unknown_asset"),
        ({"sell_asset": "USD", "buy_asset": "MXN", "sell_amount": "1.005"}, "invalid_amount"),
        ({"sell_asset": "USD", "buy_asset": "MXN", "sell_amount": "0"}, "invalid_amount"),
        ({"sell_asset": "USD", "buy_asset": "MXN", "sell_amount": "-1"}, "invalid_amount"),
    ],
)
async def test_a_quote_that_could_never_be_made_is_refused_before_a_rate_is_fetched(
    client: httpx.AsyncClient,
    maria: RegisteredUser,
    rates: SimRates,
    sim: Sim,  # noqa: F811
    body: dict[str, str],
    code: str,
) -> None:
    response = await client.post(QUOTES, json=body, headers=maria.headers)

    assert_problem(response, 422, code)
    assert sim.recorder.sent("GET", RATE_PATH) == []


@pytest.mark.usefixtures("rates")
async def test_an_amount_too_small_to_buy_anything_is_refused(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    sim: Sim,  # noqa: F811
) -> None:
    response = await client.post(
        QUOTES,
        json={"sell_asset": "MXN", "buy_asset": "USD", "sell_amount": "0.01"},
        headers=maria.headers,
    )

    assert_problem(response, 422, "amount_too_small")
    assert await count(db, "fx_quotes") == 0


@pytest.mark.usefixtures("rates")
@pytest.mark.parametrize(
    "body",
    [
        {"sell_asset": "USD", "buy_asset": "MXN", "sell_amount": 100},
        {"sell_asset": "USD", "buy_asset": "MXN", "sell_amount": 100.5},
        {"sell_asset": "USD", "buy_asset": "MXN"},
        {"sell_asset": "USD", "buy_asset": "MXN", "sell_amount": "1.00", "rate": "99"},
    ],
)
async def test_a_malformed_quote_request_is_refused(
    client: httpx.AsyncClient, maria: RegisteredUser, body: dict[str, Any]
) -> None:
    response = await client.post(QUOTES, json=body, headers=maria.headers)

    assert response.status_code == 422, response.text


async def test_fx_needs_a_credential(client: httpx.AsyncClient) -> None:
    body = {"sell_asset": "USD", "buy_asset": "MXN", "sell_amount": "1.00"}

    assert_problem(await client.post(QUOTES, json=body), 401, "unauthenticated")
    assert_problem(
        await client.post(CONVERSIONS, json={"quote_id": str(new_id())}), 401, "unauthenticated"
    )
    assert_problem(await client.get(f"{CONVERSIONS}/{new_id()}"), 401, "unauthenticated")


@pytest.mark.usefixtures("rates")
async def test_a_conversion_without_an_idempotency_key_is_refused(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    quote_id = await quoted(client, maria)

    assert_problem(
        await convert(client, maria, quote_id, key=None), 400, "idempotency_key_required"
    )
    assert await count(db, "fx_conversions") == 0
    assert await available(client, maria) == "250.00"


@pytest.mark.usefixtures("rates")
async def test_each_route_needs_its_scope(
    client: httpx.AsyncClient, app: FastAPI, maria: RegisteredUser
) -> None:
    quote_id = await quoted(client, maria)

    app.dependency_overrides[get_principal] = lambda: agent_for(maria, Scope.FX_CONVERT)
    assert (await ask(client, maria)).status_code == 201
    assert_problem(
        await client.get(f"{CONVERSIONS}/{new_id()}", headers=maria.headers),
        403,
        "insufficient_scope",
    )

    # Reading conversions lets an agent neither price one nor make one.
    app.dependency_overrides[get_principal] = lambda: agent_for(maria, Scope.FX_READ)
    assert_problem(await ask(client, maria), 403, "insufficient_scope")
    assert_problem(await convert(client, maria, quote_id), 403, "insufficient_scope")
    assert (await client.get(f"{CONVERSIONS}/{new_id()}", headers=maria.headers)).status_code == 404

    app.dependency_overrides[get_principal] = lambda: agent_for(maria, Scope.FX_CONVERT)
    assert (await convert(client, maria, quote_id, key="key-2")).status_code == 201


@pytest.mark.usefixtures("rates")
async def test_an_expired_quote_is_a_409(
    client: httpx.AsyncClient, maria: RegisteredUser, clock: ManualClock
) -> None:
    quote_id = await quoted(client, maria)
    clock.advance(seconds=31)

    assert_problem(await convert(client, maria, quote_id), 409, "quote_expired")
    assert await available(client, maria) == "250.00"


@pytest.mark.usefixtures("rates")
async def test_a_quote_converts_once_whatever_the_key(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    quote_id = await quoted(client, maria)

    first = await convert(client, maria, quote_id, key="key-1")
    second = await convert(client, maria, quote_id, key="key-2")

    assert first.status_code == 201, first.text
    assert_problem(second, 409, "quote_already_used")
    assert await available(client, maria) == "150.00"


@pytest.mark.usefixtures("rates")
async def test_another_users_quote_is_the_same_404_as_one_that_does_not_exist(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    await deposit(db, joao, 250_00)
    quote_id = await quoted(client, maria)

    theirs = await convert(client, joao, quote_id, key="key-1")
    missing = await convert(client, joao, str(new_id()), key="key-2")

    assert_problem(theirs, 404, "quote_not_found")
    without_id = {k: v for k, v in theirs.json().items() if k != "request_id"}
    assert without_id == {k: v for k, v in missing.json().items() if k != "request_id"}
    assert await available(client, joao) == "250.00"
    assert (await convert(client, maria, quote_id)).status_code == 201


@pytest.mark.usefixtures("rates")
async def test_a_conversion_the_balance_cannot_cover_is_a_402_and_moves_nothing(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    quote_id = await quoted(client, maria, "250.01")

    assert_problem(await convert(client, maria, quote_id), 402, "insufficient_funds")
    assert await available(client, maria) == "250.00"
    assert await available(client, maria, "MXN") == "0.00"
    assert await count(db, "fx_conversions") == 0
    # The refusal belongs to that key. The quote is still open for another.
    await deposit(db, maria, 1)
    assert (await convert(client, maria, quote_id, key="key-2")).status_code == 201


@pytest.mark.usefixtures("rates")
async def test_a_restricted_user_cannot_convert(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    quote_id = await quoted(client, maria)
    async with db.transaction() as session:
        await identity.restrict_user(session, uuid.UUID(maria.id), "review")

    assert_problem(await convert(client, maria, quote_id), 403, "user_restricted")
    assert await available(client, maria) == "250.00"


@pytest.mark.usefixtures("rates")
async def test_the_same_request_again_replays_the_first_answer(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    quote_id = await quoted(client, maria)

    first = await convert(client, maria, quote_id)
    again = await convert(client, maria, quote_id)

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert again.headers["idempotent-replayed"] == "true"
    assert await count(db, "fx_conversions") == 1


@pytest.mark.usefixtures("rates")
async def test_20_conversions_of_one_quote_with_different_keys_give_exactly_one(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    await deposit(db, maria, 5_000_00)
    quote_id = await quoted(client, maria)

    responses = await asyncio.gather(
        *(convert(client, maria, quote_id, key=f"key-{n}") for n in range(20))
    )

    assert sorted(response.status_code for response in responses) == [201] + [409] * 19
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {"quote_already_used"}
    assert await count(db, "fx_conversions") == 1
    assert await count(db, "outbox_events") == 1
    assert await available(client, maria, "USD") == "5150.00"
    assert await available(client, maria, "MXN") == "1716.37"


@pytest.mark.usefixtures("rates")
async def test_20_identical_conversions_at_once_make_one_and_get_one_answer(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser
) -> None:
    quote_id = await quoted(client, maria)

    responses = await asyncio.gather(*(convert(client, maria, quote_id) for _ in range(20)))

    assert [response.status_code for response in responses] == [201] * 20
    assert all(response.json() == responses[0].json() for response in responses)
    assert [r.headers.get("idempotent-replayed") for r in responses].count(None) == 1
    assert await count(db, "fx_conversions") == 1
    assert await count(db, "outbox_events") == 1
    assert await available(client, maria, "USD") == "150.00"
    assert await available(client, maria, "MXN") == "1716.37"


@pytest.mark.usefixtures("rates")
async def test_nobody_else_can_read_a_conversion(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    conversion = await convert(client, maria, await quoted(client, maria))
    url = f"{CONVERSIONS}/{conversion.json()['id']}"

    theirs = await client.get(url, headers=joao.headers)
    missing = await client.get(f"{CONVERSIONS}/{new_id()}", headers=joao.headers)

    assert_problem(theirs, 404, "conversion_not_found")
    assert_problem(missing, 404, "conversion_not_found")
    assert theirs.json()["detail"] == missing.json()["detail"]

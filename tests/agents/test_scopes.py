"""Scopes on routes: what an agent's key can reach, route by route.

Every route the app serves is in exactly one of three tables: the public routes, the
routes an agent can reach with one named scope, and the routes that are not for agents at
all. A route in none of them fails the first test here, so a route added later cannot be
left out of the others. For each route an agent can reach:

- no credential is a 401;
- a key with every scope but the one the route names is a 403;
- a key with only that scope succeeds on its owner's resources;
- and that same key gets nothing of another user's.
"""

import dataclasses
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from corridor import agents, payments
from corridor.api.deps import PUBLIC_ROUTES
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.identity import Scope
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import SimBank, SimCustody, SimRates
from tests.agents.support import AGENTS, create_agent, issue_key, key_headers
from tests.support.auth import RegisteredUser, register_user, served_routes
from tests.support.providers import (  # noqa: F401
    ACCOUNT_NUMBER,
    API_KEY,
    BASE_URL,
    ROUTING_NUMBER,
    Sim,
    bank,
    custody,
    provider_settings,
    sim,
    wire,
)

Route = tuple[str, str]


@dataclass(frozen=True)
class Call:
    """One request, less the credential it is made with."""

    method: str
    url: str
    json: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


@dataclass(frozen=True)
class Person:
    """A user and one of everything a user can have."""

    user: RegisteredUser
    deposit_id: str
    transfer_id: str
    beneficiary_id: str
    withdrawal_id: str
    # A quote that has not been converted, and a conversion of another.
    quote_id: str
    conversion_id: str

    @property
    def headers(self) -> dict[str, str]:
        return self.user.headers


@dataclass(frozen=True)
class World:
    """Three users with money and history. Maria owns the agent under test. Joao is the
    other user, whose resources her agent must not reach. Carla is whom both of them pay,
    so that neither is a party to the other's transfer."""

    api: httpx.AsyncClient
    maria: Person
    joao: Person
    carla: RegisteredUser
    # An agent of Maria's and a spare key of it, for the routes that manage agents.
    agent_id: str
    spare_key_id: str

    async def send(self, call: Call, headers: dict[str, str] | None = None) -> httpx.Response:
        sent = dict(headers or {})
        if call.method == "POST":
            # Required by the routes that move money, and ignored by the others.
            sent["Idempotency-Key"] = f"scopes-{new_id()}"
        return await self.api.request(
            call.method, call.url, json=call.json, params=call.params, headers=sent
        )

    async def key(self, *scopes: str) -> dict[str, str]:
        """The headers of a new key of Maria's agent with exactly these scopes."""
        issued = await issue_key(self.api, self.maria.user, self.agent_id, *scopes)
        return key_headers(issued["key"])


Owned = Callable[[World, httpx.Response], Awaitable[None]]


@dataclass(frozen=True)
class Reachable:
    """A route an agent can reach, and how each claim about it is shown."""

    scope: Scope
    # The request on the owner's own resources, and the status that means it worked.
    own: Callable[[World], Call]
    succeeds: int = 200
    # The same route aimed at another user's resource, where the request can name one.
    other: Callable[[World], Call] | None = None
    # Where it cannot, what shows that what was read or made is the owner's and nobody
    # else's. None means the answer is compared with what each user's own session gets.
    owned: Owned | None = field(default=None, repr=False)


def assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == code


# --- what shows that something made by an agent is its owner's -------------------------------


async def the_transfer_is_marias(world: World, response: httpx.Response) -> None:
    body = response.json()
    assert body["sender"]["id"] == world.maria.user.id
    url = f"/v1/transfers/{body['id']}"
    assert (await world.api.get(url, headers=world.maria.headers)).status_code == 200
    assert (await world.api.get(url, headers=world.joao.headers)).status_code == 404


async def the_beneficiary_is_marias(world: World, response: httpx.Response) -> None:
    async def saved(person: Person) -> set[str]:
        listed = await world.api.get("/v1/beneficiaries", headers=person.headers)
        return {item["id"] for item in listed.json()["items"]}

    created = response.json()["id"]
    assert created in await saved(world.maria)
    assert created not in await saved(world.joao)


async def the_quote_is_marias(world: World, response: httpx.Response) -> None:
    convert = Call("POST", "/v1/fx/conversions", json={"quote_id": response.json()["id"]})
    assert_problem(await world.send(convert, world.joao.headers), 404, "quote_not_found")
    assert (await world.send(convert, world.maria.headers)).status_code == 201


# --- the tables ------------------------------------------------------------------------------

TRANSFER: Final = {"asset": "USD", "amount": "5.00"}

AGENT_ROUTES: Final[dict[Route, Reachable]] = {
    ("/v1/wallets", "GET"): Reachable(Scope.WALLET_READ, lambda w: Call("GET", "/v1/wallets")),
    ("/v1/wallets/{asset}/entries", "GET"): Reachable(
        Scope.WALLET_READ, lambda w: Call("GET", "/v1/wallets/USD/entries")
    ),
    ("/v1/transfers", "POST"): Reachable(
        Scope.TRANSFERS_CREATE,
        lambda w: Call("POST", "/v1/transfers", json={**TRANSFER, "recipient": w.carla.id}),
        succeeds=201,
        owned=the_transfer_is_marias,
    ),
    ("/v1/transfers", "GET"): Reachable(
        Scope.TRANSFERS_READ, lambda w: Call("GET", "/v1/transfers")
    ),
    ("/v1/transfers/{transfer_id}", "GET"): Reachable(
        Scope.TRANSFERS_READ,
        lambda w: Call("GET", f"/v1/transfers/{w.maria.transfer_id}"),
        other=lambda w: Call("GET", f"/v1/transfers/{w.joao.transfer_id}"),
    ),
    ("/v1/deposit-instructions", "GET"): Reachable(
        Scope.DEPOSITS_READ,
        lambda w: Call("GET", "/v1/deposit-instructions", params={"asset": "USD"}),
    ),
    ("/v1/deposits", "GET"): Reachable(Scope.DEPOSITS_READ, lambda w: Call("GET", "/v1/deposits")),
    ("/v1/deposits/{deposit_id}", "GET"): Reachable(
        Scope.DEPOSITS_READ,
        lambda w: Call("GET", f"/v1/deposits/{w.maria.deposit_id}"),
        other=lambda w: Call("GET", f"/v1/deposits/{w.joao.deposit_id}"),
    ),
    ("/v1/beneficiaries", "POST"): Reachable(
        Scope.BENEFICIARIES_WRITE,
        lambda w: Call(
            "POST",
            "/v1/beneficiaries",
            json={
                "asset": "USD",
                "holder_name": "Maria Silva",
                "account_number": ACCOUNT_NUMBER,
                "routing_number": ROUTING_NUMBER,
            },
        ),
        succeeds=201,
        owned=the_beneficiary_is_marias,
    ),
    ("/v1/beneficiaries", "GET"): Reachable(
        Scope.BENEFICIARIES_READ, lambda w: Call("GET", "/v1/beneficiaries")
    ),
    ("/v1/withdrawals", "POST"): Reachable(
        Scope.WITHDRAWALS_CREATE,
        lambda w: Call(
            "POST",
            "/v1/withdrawals",
            json={"asset": "USD", "amount": "7.00", "beneficiary_id": w.maria.beneficiary_id},
        ),
        succeeds=202,
        # Maria's money to an account that Joao saved.
        other=lambda w: Call(
            "POST",
            "/v1/withdrawals",
            json={"asset": "USD", "amount": "7.00", "beneficiary_id": w.joao.beneficiary_id},
        ),
    ),
    ("/v1/withdrawals", "GET"): Reachable(
        Scope.WITHDRAWALS_READ, lambda w: Call("GET", "/v1/withdrawals")
    ),
    ("/v1/withdrawals/{withdrawal_id}", "GET"): Reachable(
        Scope.WITHDRAWALS_READ,
        lambda w: Call("GET", f"/v1/withdrawals/{w.maria.withdrawal_id}"),
        other=lambda w: Call("GET", f"/v1/withdrawals/{w.joao.withdrawal_id}"),
    ),
    ("/v1/withdrawals/{withdrawal_id}/cancel", "POST"): Reachable(
        Scope.WITHDRAWALS_CREATE,
        lambda w: Call("POST", f"/v1/withdrawals/{w.maria.withdrawal_id}/cancel"),
        other=lambda w: Call("POST", f"/v1/withdrawals/{w.joao.withdrawal_id}/cancel"),
    ),
    ("/v1/fx/quotes", "POST"): Reachable(
        Scope.FX_READ,
        lambda w: Call(
            "POST",
            "/v1/fx/quotes",
            json={"sell_asset": "USD", "buy_asset": "MXN", "sell_amount": "3.00"},
        ),
        succeeds=201,
        owned=the_quote_is_marias,
    ),
    ("/v1/fx/conversions", "POST"): Reachable(
        Scope.FX_CONVERT,
        lambda w: Call("POST", "/v1/fx/conversions", json={"quote_id": w.maria.quote_id}),
        succeeds=201,
        other=lambda w: Call("POST", "/v1/fx/conversions", json={"quote_id": w.joao.quote_id}),
    ),
    ("/v1/fx/conversions/{conversion_id}", "GET"): Reachable(
        Scope.FX_READ,
        lambda w: Call("GET", f"/v1/fx/conversions/{w.maria.conversion_id}"),
        other=lambda w: Call("GET", f"/v1/fx/conversions/{w.joao.conversion_id}"),
    ),
}

# Each with a request that the owner's own session makes successfully, and the status that
# says so: what is refused to the agent below is a request that was otherwise good.
NOT_FOR_AGENTS: Final[dict[Route, tuple[Callable[[World], Call], int]]] = {
    ("/v1/me", "GET"): (lambda w: Call("GET", "/v1/me"), 200),
    ("/v1/auth/logout", "POST"): (lambda w: Call("POST", "/v1/auth/logout"), 204),
    ("/v1/agents", "POST"): (lambda w: Call("POST", AGENTS, json={"name": "A helper"}), 201),
    ("/v1/agents", "GET"): (lambda w: Call("GET", AGENTS), 200),
    ("/v1/agents/{agent_id}/pause", "POST"): (
        lambda w: Call("POST", f"{AGENTS}/{w.agent_id}/pause"),
        200,
    ),
    ("/v1/agents/{agent_id}/resume", "POST"): (
        lambda w: Call("POST", f"{AGENTS}/{w.agent_id}/resume"),
        200,
    ),
    ("/v1/agents/{agent_id}/revoke", "POST"): (
        lambda w: Call("POST", f"{AGENTS}/{w.agent_id}/revoke"),
        200,
    ),
    ("/v1/agents/{agent_id}/keys", "POST"): (
        lambda w: Call(
            "POST", f"{AGENTS}/{w.agent_id}/keys", json={"scopes": [Scope.TRANSFERS_CREATE]}
        ),
        201,
    ),
    ("/v1/agents/{agent_id}/keys/{key_id}", "DELETE"): (
        lambda w: Call("DELETE", f"{AGENTS}/{w.agent_id}/keys/{w.spare_key_id}"),
        204,
    ),
}


def route_id(route: Route) -> str:
    path, method = route
    return f"{method} {path}"


reachable = pytest.mark.parametrize("route", sorted(AGENT_ROUTES), ids=route_id)
not_for_agents = pytest.mark.parametrize("route", sorted(NOT_FOR_AGENTS), ids=route_id)
by_id = pytest.mark.parametrize(
    "route", sorted(r for r, case in AGENT_ROUTES.items() if case.other is not None), ids=route_id
)
not_by_id = pytest.mark.parametrize(
    "route", sorted(r for r, case in AGENT_ROUTES.items() if case.other is None), ids=route_id
)


# --- the world -------------------------------------------------------------------------------


async def created(world_api: httpx.AsyncClient, user: RegisteredUser, call: Call) -> Any:
    response = await world_api.request(
        call.method,
        call.url,
        json=call.json,
        headers={**user.headers, "Idempotency-Key": f"setup-{new_id()}"},
    )
    assert response.status_code in (201, 202), response.text
    return response.json()


async def a_person(
    api: httpx.AsyncClient,
    db: Database,
    simulator: Sim,
    handle: str,
    funds: str,
    pays: RegisteredUser,
) -> Person:
    user = await register_user(api, handle=handle)
    # A real bank deposit: it is the user's money, and their one deposit on record.
    instructions = await api.get(
        "/v1/deposit-instructions", params={"asset": "USD"}, headers=user.headers
    )
    assert instructions.status_code == 200, instructions.text
    account = simulator.app.state.sim.bank._virtual_account_of[user.id, "USD"]
    await payments.apply_bank_deposit_received(db, await simulator.bank_deposit(account, funds))
    deposits = await api.get("/v1/deposits", headers=user.headers)

    transfer = await created(
        api, user, Call("POST", "/v1/transfers", json={**TRANSFER, "recipient": pays.id})
    )
    beneficiary = await created(
        api,
        user,
        Call(
            "POST",
            "/v1/beneficiaries",
            json={
                "asset": "USD",
                "holder_name": handle.title(),
                "account_number": ACCOUNT_NUMBER,
                "routing_number": ROUTING_NUMBER,
            },
        ),
    )
    withdrawal = await created(
        api,
        user,
        Call(
            "POST",
            "/v1/withdrawals",
            json={"asset": "USD", "amount": "20.00", "beneficiary_id": beneficiary["id"]},
        ),
    )
    quote = Call(
        "POST",
        "/v1/fx/quotes",
        json={"sell_asset": "USD", "buy_asset": "MXN", "sell_amount": "9.00"},
    )
    converted = await created(api, user, quote)
    conversion = await created(
        api, user, Call("POST", "/v1/fx/conversions", json={"quote_id": converted["id"]})
    )
    return Person(
        user=user,
        deposit_id=deposits.json()["items"][0]["id"],
        transfer_id=transfer["id"],
        beneficiary_id=beneficiary["id"],
        withdrawal_id=withdrawal["id"],
        quote_id=(await created(api, user, quote))["id"],
        conversion_id=conversion["id"],
    )


@pytest.fixture
async def world(
    clock: ManualClock,
    app: FastAPI,
    client: httpx.AsyncClient,
    settings: Settings,
    db: Database,
    sim: Sim,  # noqa: F811
    bank: SimBank,  # noqa: F811
    custody: SimCustody,  # noqa: F811
) -> World:
    wire(app, bank, custody)
    rates = SimRates(
        settings.model_copy(
            update={"fx_rates_url": BASE_URL, "fx_rates_api_key": SecretStr(API_KEY)}
        ),
        client=sim.http,
    )
    app.state.container = dataclasses.replace(app.state.container, rates=rates)
    await sim.control("POST", "/fx/rates", {"base": "USD", "quote": "MXN", "mid": "17.25"})

    carla = await register_user(client, handle="carla")
    # Different amounts, so that one user's balances can never pass for the other's.
    maria = await a_person(client, db, sim, "maria", "500.00", carla)
    joao = await a_person(client, db, sim, "joao", "300.00", carla)
    agent = await create_agent(client, maria.user)
    spare = await issue_key(client, maria.user, agent["id"], Scope.WALLET_READ)
    return World(
        api=client,
        maria=maria,
        joao=joao,
        carla=carla,
        agent_id=agent["id"],
        spare_key_id=spare["id"],
    )


# --- every route is accounted for ------------------------------------------------------------


def test_every_route_is_public_or_reachable_by_an_agent_or_not_for_agents(app: FastAPI) -> None:
    served = {(route.path, route.method) for route in served_routes(app)}

    assert served - PUBLIC_ROUTES - AGENT_ROUTES.keys() - NOT_FOR_AGENTS.keys() == set()
    # And no table lists a route that is gone, or one that another table also lists.
    assert (AGENT_ROUTES.keys() | NOT_FOR_AGENTS.keys()) - served == set()
    assert AGENT_ROUTES.keys() & NOT_FOR_AGENTS.keys() == set()
    assert (AGENT_ROUTES.keys() | NOT_FOR_AGENTS.keys()) & PUBLIC_ROUTES == set()


def test_the_check_notices_a_route_that_is_in_no_table(app: FastAPI) -> None:
    async def handler() -> dict[str, str]:
        return {}

    app.add_api_route("/v1/added-later", handler, methods=["GET"])
    served = {(route.path, route.method) for route in served_routes(app)}

    assert served - PUBLIC_ROUTES - AGENT_ROUTES.keys() - NOT_FOR_AGENTS.keys() == {
        ("/v1/added-later", "GET")
    }


def test_every_scope_an_agent_may_hold_opens_some_route() -> None:
    assert {case.scope for case in AGENT_ROUTES.values()} == agents.AGENT_SCOPES


# --- the routes an agent can reach -----------------------------------------------------------


@reachable
async def test_without_a_credential_the_route_answers_401(world: World, route: Route) -> None:
    response = await world.send(AGENT_ROUTES[route].own(world))

    assert_problem(response, 401, "unauthenticated")


@reachable
async def test_a_key_with_every_scope_but_the_routes_own_is_refused_with_403(
    world: World, route: Route
) -> None:
    case = AGENT_ROUTES[route]
    every_other = sorted(agents.AGENT_SCOPES - {case.scope})
    assert len(every_other) == len(agents.AGENT_SCOPES) - 1

    response = await world.send(case.own(world), await world.key(*every_other))

    assert_problem(response, 403, "insufficient_scope")


@reachable
async def test_a_key_with_only_the_routes_scope_succeeds_on_its_owners_resources(
    world: World, route: Route
) -> None:
    case = AGENT_ROUTES[route]

    response = await world.send(case.own(world), await world.key(case.scope))

    assert response.status_code == case.succeeds, response.text


@by_id
async def test_a_key_with_the_routes_scope_does_not_reach_another_users_resource(
    world: World, route: Route
) -> None:
    case = AGENT_ROUTES[route]
    assert case.other is not None
    # The control: the resource is there, and its own user's session reaches it.
    assert (await world.send(case.other(world), world.joao.headers)).status_code == case.succeeds

    response = await world.send(case.other(world), await world.key(case.scope))

    assert response.status_code == 404, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE


@not_by_id
async def test_where_a_request_names_no_resource_what_it_reads_or_makes_is_the_owners(
    world: World, route: Route
) -> None:
    case = AGENT_ROUTES[route]
    call = case.own(world)

    response = await world.send(call, await world.key(case.scope))

    assert response.status_code == case.succeeds, response.text
    if case.owned is not None:
        await case.owned(world, response)
    else:
        # A read: the agent sees what its owner sees, which is not what Joao sees.
        assert call.method == "GET"
        assert response.json() == (await world.send(call, world.maria.headers)).json()
        assert response.json() != (await world.send(call, world.joao.headers)).json()


# --- the routes that are not for agents ------------------------------------------------------


@not_for_agents
async def test_a_route_that_is_not_for_agents_answers_401_without_a_credential(
    world: World, route: Route
) -> None:
    call, _ = NOT_FOR_AGENTS[route]

    assert_problem(await world.send(call(world)), 401, "unauthenticated")


@not_for_agents
async def test_a_route_that_is_not_for_agents_refuses_a_key_holding_every_scope(
    world: World, route: Route
) -> None:
    call, succeeds = NOT_FOR_AGENTS[route]
    key = await world.key(*sorted(agents.AGENT_SCOPES))

    refused = await world.send(call(world), key)
    # The control, made after: the request was good, and the owner's own session makes it.
    allowed = await world.send(call(world), world.maria.headers)

    assert_problem(refused, 403, "insufficient_scope")
    assert allowed.status_code == succeeds, allowed.text

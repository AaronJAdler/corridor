"""Ownership, route by route: a resource that is one user's is not there for another.

Every route that names a resource, in its path or in its body, is in the matrix below. For
each, a second user sends the request that the owner could send, aimed at the owner's
resource, and is told it does not exist: a 404, never a 403, so that an id cannot be
probed to learn that something is there. The owner then sends the same request and it
succeeds, which shows both that the request was a good one and that the refused attempt
changed nothing.

The scope tests ask the same question of an agent's key. These ask it of a user's own
session, which holds every scope: what stops it here is ownership and nothing else. The
world, and the requests themselves, are the ones those tests build.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import pytest
from fastapi import FastAPI

from corridor.api.deps import PUBLIC_ROUTES
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from tests.agents.test_scopes import (  # noqa: F401
    ADMIN_ROUTES,
    AGENT_ROUTES,
    NOT_FOR_AGENTS,
    Call,
    Person,
    Route,
    World,
    route_id,
    world,
)
from tests.support.auth import served_routes

# The simulated providers the world is built on. Imported so that pytest finds them as
# fixtures of this module.
from tests.support.providers import bank, custody, provider_settings, sim  # noqa: F401


@dataclass(frozen=True)
class Owned:
    """A route that names a resource, as a request for Joao's and a request for Maria's."""

    # The request aimed at the resource of the person who does not own the session.
    theirs: Callable[[World], Call]
    # Whose resource that is, and so who can make the request.
    owner: Callable[[World], Person]
    # The intruder: the other of the two.
    intruder: Callable[[World], Person]
    # What the owner is answered.
    succeeds: int
    # The code of the 404 the intruder is answered.
    code: str


def _marias(call: Callable[[World], Call], succeeds: int, code: str) -> Owned:
    return Owned(
        call, owner=lambda w: w.maria, intruder=lambda w: w.joao, succeeds=succeeds, code=code
    )


def _joaos(route: Route, code: str) -> Owned:
    # The scope tests already build the request for Joao's resource, for Maria's agent.
    case = AGENT_ROUTES[route]
    assert case.other is not None
    return Owned(
        case.other,
        owner=lambda w: w.joao,
        intruder=lambda w: w.maria,
        succeeds=case.succeeds,
        code=code,
    )


def _owners(route: Route, code: str) -> Owned:
    call, succeeds = NOT_FOR_AGENTS[route]
    return _marias(call, succeeds, code)


MATRIX: Final[dict[Route, Owned]] = {
    ("/v1/transfers/{transfer_id}", "GET"): _joaos(
        ("/v1/transfers/{transfer_id}", "GET"), "transfer_not_found"
    ),
    ("/v1/deposits/{deposit_id}", "GET"): _joaos(
        ("/v1/deposits/{deposit_id}", "GET"), "deposit_not_found"
    ),
    ("/v1/withdrawals/{withdrawal_id}", "GET"): _joaos(
        ("/v1/withdrawals/{withdrawal_id}", "GET"), "withdrawal_not_found"
    ),
    ("/v1/withdrawals/{withdrawal_id}/cancel", "POST"): _joaos(
        ("/v1/withdrawals/{withdrawal_id}/cancel", "POST"), "withdrawal_not_found"
    ),
    ("/v1/fx/conversions/{conversion_id}", "GET"): _joaos(
        ("/v1/fx/conversions/{conversion_id}", "GET"), "conversion_not_found"
    ),
    # The two that name another user's resource in the body rather than the path.
    ("/v1/withdrawals", "POST"): _joaos(("/v1/withdrawals", "POST"), "beneficiary_not_found"),
    ("/v1/fx/conversions", "POST"): _joaos(("/v1/fx/conversions", "POST"), "quote_not_found"),
    ("/v1/agents/{agent_id}/pause", "POST"): _owners(
        ("/v1/agents/{agent_id}/pause", "POST"), "agent_not_found"
    ),
    ("/v1/agents/{agent_id}/resume", "POST"): _owners(
        ("/v1/agents/{agent_id}/resume", "POST"), "agent_not_found"
    ),
    ("/v1/agents/{agent_id}/revoke", "POST"): _owners(
        ("/v1/agents/{agent_id}/revoke", "POST"), "agent_not_found"
    ),
    ("/v1/agents/{agent_id}/keys", "POST"): _owners(
        ("/v1/agents/{agent_id}/keys", "POST"), "agent_not_found"
    ),
    ("/v1/agents/{agent_id}/keys/{key_id}", "DELETE"): _owners(
        ("/v1/agents/{agent_id}/keys/{key_id}", "DELETE"), "agent_not_found"
    ),
    ("/v1/agents/{agent_id}/policy", "PUT"): _owners(
        ("/v1/agents/{agent_id}/policy", "PUT"), "agent_not_found"
    ),
    ("/v1/agents/{agent_id}/policy", "GET"): _owners(
        ("/v1/agents/{agent_id}/policy", "GET"), "agent_not_found"
    ),
    ("/v1/approvals/{approval_id}/approve", "POST"): _owners(
        ("/v1/approvals/{approval_id}/approve", "POST"), "approval_not_found"
    ),
    ("/v1/approvals/{approval_id}/reject", "POST"): _owners(
        ("/v1/approvals/{approval_id}/reject", "POST"), "approval_not_found"
    ),
}

# The routes with something in braces that is not the id of anything a user owns: an asset
# code, which every user has a wallet in, and the name of a provider.
NAMES_NO_RESOURCE: Final[frozenset[Route]] = frozenset(
    {
        ("/v1/wallets/{asset}/entries", "GET"),
        ("/v1/webhooks/{provider}", "POST"),
    }
)

_PARAMETER: Final = re.compile(r"\{[a-z_]+\}")

owned = pytest.mark.parametrize("route", sorted(MATRIX), ids=route_id)


# --- every route that names a resource is in the matrix ----------------------------------------


def test_every_route_with_a_path_parameter_is_in_the_matrix_or_is_an_administrators(
    app: FastAPI,
) -> None:
    by_id = {
        (route.path, route.method)
        for route in served_routes(app)
        if _PARAMETER.search(route.path) is not None
    }

    assert by_id - MATRIX.keys() - ADMIN_ROUTES - NAMES_NO_RESOURCE == set()
    # And nothing is excused that is not a route with a parameter any more.
    assert NAMES_NO_RESOURCE - by_id == set()
    assert {route for route in ADMIN_ROUTES if _PARAMETER.search(route[0])} <= by_id


def test_every_route_the_scope_tests_aim_at_another_users_resource_is_in_the_matrix() -> None:
    aimed = {route for route, case in AGENT_ROUTES.items() if case.other is not None}

    assert aimed - MATRIX.keys() == set()


def test_no_route_in_the_matrix_is_public_or_gone(app: FastAPI) -> None:
    served = {(route.path, route.method) for route in served_routes(app)}

    assert MATRIX.keys() - served == set()
    assert MATRIX.keys() & PUBLIC_ROUTES == set()


def test_the_check_notices_a_by_id_route_that_is_in_no_table(app: FastAPI) -> None:
    async def handler(statement_id: str) -> dict[str, str]:
        return {}

    app.add_api_route("/v1/statements/{statement_id}", handler, methods=["GET"])
    by_id = {
        (route.path, route.method)
        for route in served_routes(app)
        if _PARAMETER.search(route.path) is not None
    }

    assert by_id - MATRIX.keys() - ADMIN_ROUTES - NAMES_NO_RESOURCE == {
        ("/v1/statements/{statement_id}", "GET")
    }


# --- the matrix ----------------------------------------------------------------------------------


@owned
async def test_another_users_resource_is_not_found_and_its_owner_still_reaches_it(
    world: World,  # noqa: F811
    route: Route,
) -> None:
    case = MATRIX[route]
    call = case.theirs(world)

    refused = await world.send(call, case.intruder(world).headers)
    # After, on purpose: what the intruder asked for was not done, so the owner can do it.
    allowed = await world.send(call, case.owner(world).headers)

    assert refused.status_code == 404, refused.text
    assert refused.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert refused.json()["code"] == case.code
    assert allowed.status_code == case.succeeds, allowed.text


@owned
async def test_a_resource_that_is_not_there_is_answered_exactly_as_another_users_is(
    world: World,  # noqa: F811
    route: Route,
) -> None:
    # The same request with every id replaced by one that names nothing. If the two
    # answers differed, the difference would say which ids are somebody's.
    case = MATRIX[route]
    call = case.theirs(world)
    intruder = case.intruder(world).headers

    theirs = await world.send(call, intruder)
    nobodys = await world.send(_aimed_at_nothing(call), intruder)

    assert (nobodys.status_code, _comparable(nobodys.json())) == (
        theirs.status_code,
        _comparable(theirs.json()),
    )


_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Well formed, a version 7 id like every other, and the id of nothing.
_NOTHING: Final = "01900000-0000-7000-8000-000000000000"


def _aimed_at_nothing(call: Call) -> Call:
    """The call with the first id it names replaced, in the path or else in the body: the
    id of the resource the route is about."""
    if _UUID.search(call.url) is not None:
        return Call(call.method, _UUID.sub(_NOTHING, call.url, count=1), call.json, call.params)
    assert call.json is not None
    named = {key: value for key, value in call.json.items() if key.endswith("_id")}
    assert len(named) == 1, "a body that names a resource names one"
    return Call(call.method, call.url, {**call.json, **dict.fromkeys(named, _NOTHING)}, call.params)


def _comparable(body: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in body.items() if key != "request_id"}

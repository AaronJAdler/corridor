"""Every route requires authentication unless it is on the short public list.

A route is protected by depending on ``get_principal``, directly or through another
dependency. Forgetting to is silent, so every route the app serves is walked here and the
routes that forgot are named.
"""

from fastapi import APIRouter, Depends, FastAPI

from corridor.api.deps import (
    PUBLIC_ROUTES,
    AdminPrincipal,
    CurrentPrincipal,
    get_principal,
    require,
)
from corridor.identity import Principal, Scope
from tests.support.auth import served_routes


def unprotected_routes(app: FastAPI) -> list[tuple[str, str]]:
    """The routes that are neither public nor behind ``get_principal``."""
    return [
        (route.path, route.method)
        for route in served_routes(app)
        if (route.path, route.method) not in PUBLIC_ROUTES and get_principal not in route.calls
    ]


async def _handler() -> dict[str, str]:
    return {}


async def test_every_route_outside_the_public_list_requires_a_principal(app: FastAPI) -> None:
    assert unprotected_routes(app) == []


async def test_the_checker_reports_a_route_that_forgot_to_require_a_principal(
    app: FastAPI,
) -> None:
    app.add_api_route("/v1/forgotten", _handler, methods=["GET", "POST"])

    assert unprotected_routes(app) == [("/v1/forgotten", "GET"), ("/v1/forgotten", "POST")]


async def test_the_checker_sees_into_an_included_router(app: FastAPI) -> None:
    # Where a forgotten route would really be: every router but the health one is included.
    router = APIRouter(prefix="/v1/later")
    router.add_api_route("/forgotten", _handler)
    app.include_router(router)

    assert unprotected_routes(app) == [("/v1/later/forgotten", "GET")]


async def test_the_checker_reports_a_route_that_fastapi_did_not_build(app: FastAPI) -> None:
    app.add_route("/v1/plain", _handler, methods=["GET"])  # type: ignore[arg-type]

    assert unprotected_routes(app) == [("/v1/plain", "GET")]


async def test_a_public_path_is_public_for_its_listed_method_only(app: FastAPI) -> None:
    app.add_api_route("/v1/auth/login", _handler, methods=["DELETE"])

    assert unprotected_routes(app) == [("/v1/auth/login", "DELETE")]


async def test_a_route_is_protected_however_it_comes_to_depend_on_the_principal(
    app: FastAPI,
) -> None:
    async def direct(principal: CurrentPrincipal) -> dict[str, str]:
        return {}

    async def admin(principal: AdminPrincipal) -> dict[str, str]:
        return {}

    app.add_api_route("/v1/direct", direct)
    app.add_api_route("/v1/admin", admin)
    app.add_api_route("/v1/scoped", _handler, dependencies=[Depends(require(Scope.WALLET_READ))])

    assert unprotected_routes(app) == []


async def test_the_public_list_is_exactly_the_routes_meant_to_be_public(app: FastAPI) -> None:
    # Written out in full: adding a path to the list is a decision that shows up here.
    assert (
        frozenset(
            {
                ("/healthz", "GET"),
                ("/readyz", "GET"),
                ("/metrics", "GET"),
                ("/docs", "GET"),
                ("/openapi.json", "GET"),
                ("/.well-known/jwks.json", "GET"),
                ("/v1/auth/register", "POST"),
                ("/v1/auth/login", "POST"),
                ("/v1/auth/refresh", "POST"),
            }
        )
        == PUBLIC_ROUTES
    )


async def test_every_public_entry_is_a_real_route(app: FastAPI) -> None:
    # An entry for a route that is gone would make the next route at that path public.
    served = {(route.path, route.method) for route in served_routes(app)}

    assert served >= PUBLIC_ROUTES


async def test_a_public_route_does_not_ask_for_a_principal(app: FastAPI) -> None:
    public = [r for r in served_routes(app) if (r.path, r.method) in PUBLIC_ROUTES]

    assert [route.path for route in public if get_principal in route.calls] == []


def test_the_principal_dependencies_resolve_to_a_principal() -> None:
    assert CurrentPrincipal.__origin__ is Principal  # type: ignore[attr-defined]
    assert AdminPrincipal.__origin__ is Principal  # type: ignore[attr-defined]

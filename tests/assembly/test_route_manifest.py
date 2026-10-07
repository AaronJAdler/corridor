"""The route table as the assembled app serves it: what is there, that nothing is there
twice, and that every route outside a short public list asks who is calling.

A route is protected by depending on ``get_principal``, directly or through another
dependency. Forgetting to is silent, so every route the app serves is walked here and the
routes that forgot are named.
"""

from collections import Counter

from fastapi import APIRouter, Depends, FastAPI

from corridor.api import deps
from corridor.api.deps import (
    PUBLIC_ROUTES,
    AdminPrincipal,
    CurrentPrincipal,
    get_principal,
    require,
)
from corridor.identity import Principal, Scope
from tests.support.auth import served_routes

# Every route the API serves, as (method, path). Written out in full: a route that is
# added, removed or moved is a decision, and it shows up here as a change to this list.
MANIFEST: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/.well-known/jwks.json"),
        ("GET", "/docs"),
        ("GET", "/healthz"),
        ("GET", "/metrics"),
        ("GET", "/openapi.json"),
        ("GET", "/readyz"),
        ("GET", "/v1/admin/adjustments"),
        ("POST", "/v1/admin/adjustments"),
        ("POST", "/v1/admin/adjustments/suspense-release"),
        ("POST", "/v1/admin/adjustments/suspense-return"),
        ("GET", "/v1/admin/adjustments/{adjustment_id}"),
        ("POST", "/v1/admin/adjustments/{adjustment_id}/approve"),
        ("POST", "/v1/admin/adjustments/{adjustment_id}/reject"),
        ("GET", "/v1/admin/outbox/dead"),
        ("POST", "/v1/admin/outbox/dead/{event_id}/requeue"),
        ("GET", "/v1/admin/recon/breaks"),
        ("POST", "/v1/admin/recon/breaks/{break_id}/resolve"),
        ("GET", "/v1/admin/recon/runs"),
        ("GET", "/v1/admin/reviews"),
        ("POST", "/v1/admin/reviews/{review_id}/clear"),
        ("POST", "/v1/admin/reviews/{review_id}/reject"),
        ("GET", "/v1/admin/risk/denylist"),
        ("POST", "/v1/admin/risk/denylist"),
        ("GET", "/v1/admin/risk/limits"),
        ("PUT", "/v1/admin/risk/limits"),
        ("PUT", "/v1/admin/users/{user_id}/kyc-tier"),
        ("GET", "/v1/agents"),
        ("POST", "/v1/agents"),
        ("POST", "/v1/agents/{agent_id}/keys"),
        ("DELETE", "/v1/agents/{agent_id}/keys/{key_id}"),
        ("POST", "/v1/agents/{agent_id}/pause"),
        ("GET", "/v1/agents/{agent_id}/policy"),
        ("PUT", "/v1/agents/{agent_id}/policy"),
        ("POST", "/v1/agents/{agent_id}/resume"),
        ("POST", "/v1/agents/{agent_id}/revoke"),
        ("GET", "/v1/approvals"),
        ("POST", "/v1/approvals/{approval_id}/approve"),
        ("POST", "/v1/approvals/{approval_id}/reject"),
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/auth/logout"),
        ("POST", "/v1/auth/refresh"),
        ("POST", "/v1/auth/register"),
        ("GET", "/v1/beneficiaries"),
        ("POST", "/v1/beneficiaries"),
        ("GET", "/v1/deposit-instructions"),
        ("GET", "/v1/deposits"),
        ("GET", "/v1/deposits/{deposit_id}"),
        ("POST", "/v1/fx/conversions"),
        ("GET", "/v1/fx/conversions/{conversion_id}"),
        ("POST", "/v1/fx/quotes"),
        ("GET", "/v1/me"),
        ("GET", "/v1/transfers"),
        ("POST", "/v1/transfers"),
        ("GET", "/v1/transfers/{transfer_id}"),
        ("GET", "/v1/wallets"),
        ("GET", "/v1/wallets/{asset}/entries"),
        ("POST", "/v1/webhooks/{provider}"),
        ("GET", "/v1/withdrawals"),
        ("POST", "/v1/withdrawals"),
        ("GET", "/v1/withdrawals/{withdrawal_id}"),
        ("POST", "/v1/withdrawals/{withdrawal_id}/cancel"),
    }
)


def duplicated_routes(app: FastAPI) -> list[tuple[str, str]]:
    """The routes the app was given more than once. Only the first of them ever answers,
    so the second is dead code that looks alive, or the wrong one is."""
    seen = Counter((route.method, route.path) for route in served_routes(app))
    return sorted(route for route, times in seen.items() if times > 1)


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
                ("/v1/webhooks/{provider}", "POST"),
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


# --- the manifest ------------------------------------------------------------------------------


async def test_the_app_serves_exactly_the_routes_in_the_manifest(app: FastAPI) -> None:
    served = {(route.method, route.path) for route in served_routes(app)}

    assert served - MANIFEST == set()
    assert MANIFEST - served == set()


async def test_no_route_is_served_twice(app: FastAPI) -> None:
    assert duplicated_routes(app) == []


async def test_the_duplicate_check_notices_a_route_that_was_added_a_second_time(
    app: FastAPI,
) -> None:
    app.add_api_route("/v1/me", _handler, methods=["GET"])
    router = APIRouter(prefix="/v1/transfers")
    router.add_api_route("", _handler, methods=["POST", "PATCH"])
    app.include_router(router)

    assert duplicated_routes(app) == [("GET", "/v1/me"), ("POST", "/v1/transfers")]


async def test_the_same_path_under_another_method_is_not_a_duplicate(app: FastAPI) -> None:
    app.add_api_route("/v1/me", _handler, methods=["PATCH"])

    assert duplicated_routes(app) == []


async def test_nothing_is_mounted_and_nothing_is_a_websocket(app: FastAPI) -> None:
    # Neither is walked for dependencies, so either could answer without asking who calls.
    assert [route.path for route in served_routes(app) if route.method == "*"] == []


async def test_every_route_is_under_v1_or_is_a_service_endpoint(app: FastAPI) -> None:
    service = {
        "/healthz",
        "/readyz",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/.well-known/jwks.json",
    }

    assert [
        route.path
        for route in served_routes(app)
        if not route.path.startswith("/v1/") and route.path not in service
    ] == []


async def test_every_admin_route_asks_for_an_administrator(app: FastAPI) -> None:
    admin = [route for route in served_routes(app) if route.path.startswith("/v1/admin/")]

    assert len(admin) == 20
    assert [
        (route.method, route.path) for route in admin if deps._require_admin not in route.calls
    ] == []


async def test_no_route_outside_admin_asks_for_an_administrator(app: FastAPI) -> None:
    # An admin check on a user's route would not be wrong, but it would be a surprise.
    assert [
        (route.method, route.path)
        for route in served_routes(app)
        if not route.path.startswith("/v1/admin/") and deps._require_admin in route.calls
    ] == []

"""The simulator as an application: settings, authentication, errors and start-up."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
import uvicorn
from fastapi import Depends, FastAPI
from pydantic import SecretStr, ValidationError

from corridor_sim import __main__ as entry_point
from corridor_sim.api.deps import require_api_key, require_control_token
from corridor_sim.app import create_app
from corridor_sim.settings import SimSettings
from tests.simulators.conftest import (
    API_KEY,
    BANK_WEBHOOK_SECRET,
    BASE_URL,
    CUSTODY_WEBHOOK_SECRET,
    Sim,
    sim_settings,
)
from tests.support.auth import served_routes

Launch = Callable[..., Awaitable[Sim]]

# Not secrets: they open a simulator that lives for one test.
CONTROL_TOKEN = "sim-test-control-token-of-32-characters"  # pragma: allowlist secret
ONE_SHORT = "not-thirty-two-characters-long!"  # pragma: allowlist secret

# --- settings ------------------------------------------------------------------------------


def test_the_simulator_refuses_to_start_without_its_secrets() -> None:
    with pytest.raises(ValidationError) as refusal:
        SimSettings(_env_file=None)

    assert {error["loc"][0] for error in refusal.value.errors()} == {
        "api_key",
        "bank_webhook_secret",
        "custody_webhook_secret",
    }


@pytest.mark.parametrize("name", ["api_key", "bank_webhook_secret", "custody_webhook_secret"])
def test_an_empty_secret_is_no_secret(name: str) -> None:
    with pytest.raises(ValidationError) as refusal:
        sim_settings(**{name: SecretStr("")})

    assert [error["loc"] for error in refusal.value.errors()] == [(name,)]


def test_the_defaults_are_the_contracts_schedule() -> None:
    settings = SimSettings(
        _env_file=None,
        api_key=SecretStr(API_KEY),
        bank_webhook_secret=SecretStr(BANK_WEBHOOK_SECRET),
        custody_webhook_secret=SecretStr(CUSTODY_WEBHOOK_SECRET),
    )

    assert (settings.clock_mode, settings.start_time, settings.seed) == ("realtime", None, 20260115)
    assert (settings.bank_webhook_url, settings.custody_webhook_url) == (None, None)
    assert (
        settings.ach_settle_seconds,
        settings.spei_settle_seconds,
        settings.pix_settle_seconds,
    ) == (30, 5, 1)
    assert (settings.block_seconds, settings.confirmations) == (2, 3)
    assert settings.webhook_timeout_seconds == 5
    assert (settings.host, settings.port) == ("127.0.0.1", 8100)
    assert (settings.environment, settings.control_token) == ("development", None)


def test_settings_are_read_from_the_environment_under_the_simulators_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORRIDOR_SIM_API_KEY", API_KEY)
    monkeypatch.setenv("CORRIDOR_SIM_BANK_WEBHOOK_SECRET", BANK_WEBHOOK_SECRET)
    monkeypatch.setenv("CORRIDOR_SIM_CUSTODY_WEBHOOK_SECRET", CUSTODY_WEBHOOK_SECRET)
    monkeypatch.setenv("CORRIDOR_SIM_BANK_WEBHOOK_URL", "http://api:8000/v1/webhooks/simbank")
    monkeypatch.setenv("CORRIDOR_SIM_CLOCK_MODE", "manual")
    monkeypatch.setenv("CORRIDOR_SIM_START_TIME", "2026-02-01T08:00:00+02:00")
    monkeypatch.setenv("CORRIDOR_SIM_PIX_SETTLE_SECONDS", "0.5")
    monkeypatch.setenv("CORRIDOR_SIM_PORT", "9100")
    # Corridor's own variables share the first half of the prefix and are not the simulator's.
    monkeypatch.setenv("CORRIDOR_SEED", "7")

    settings = SimSettings(_env_file=None)

    assert settings.api_key.get_secret_value() == API_KEY
    assert settings.bank_webhook_url == "http://api:8000/v1/webhooks/simbank"
    assert settings.clock_mode == "manual"
    assert settings.start_time == datetime(2026, 2, 1, 6, 0, tzinfo=UTC)
    assert settings.pix_settle_seconds == 0.5
    assert settings.port == 9100
    assert settings.seed == 20260115


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_time": "2026-01-15T12:00:00"},
        {"clock_mode": "frozen"},
        {"ach_settle_seconds": -1},
        {"block_seconds": 0},
        {"confirmations": 0},
        {"webhook_timeout_seconds": 0},
        {"port": 70000},
    ],
)
def test_a_setting_that_makes_no_sense_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        sim_settings(**overrides)


def test_secrets_are_not_shown_when_settings_are_printed() -> None:
    shown = repr(sim_settings(control_token=SecretStr(CONTROL_TOKEN)))

    for secret in (API_KEY, BANK_WEBHOOK_SECRET, CUSTODY_WEBHOOK_SECRET, CONTROL_TOKEN):
        assert secret not in shown


SECRETS = ["api_key", "bank_webhook_secret", "custody_webhook_secret", "control_token"]


@pytest.mark.parametrize("name", SECRETS)
def test_a_secret_shorter_than_32_characters_is_refused_and_not_repeated(name: str) -> None:
    assert len(ONE_SHORT) == 31
    # As it arrives from the environment: plain text, which an error could quote.
    with pytest.raises(ValidationError) as refusal:
        sim_settings(**{name: ONE_SHORT})

    assert [error["loc"] for error in refusal.value.errors()] == [(name,)]
    assert ONE_SHORT not in str(refusal.value)


@pytest.mark.parametrize("name", SECRETS)
def test_a_secret_of_32_characters_is_accepted(name: str) -> None:
    settings = sim_settings(**{name: SecretStr(ONE_SHORT + "x")})

    assert getattr(settings, name).get_secret_value() == ONE_SHORT + "x"


# --- where the simulator may run -----------------------------------------------------------


@pytest.mark.parametrize("environment", ["development", "test"])
def test_the_simulator_runs_in_development_and_in_test(environment: str) -> None:
    assert sim_settings(environment=environment).environment == environment


@pytest.mark.parametrize("environment", ["production", "staging", "prod", "", "Development"])
def test_the_simulator_refuses_to_start_anywhere_else(environment: str) -> None:
    with pytest.raises(ValidationError) as refusal:
        sim_settings(environment=environment)

    assert [error["loc"] for error in refusal.value.errors()] == [("environment",)]


def test_the_environment_is_read_from_the_simulators_own_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORRIDOR_SIM_ENVIRONMENT", "production")

    with pytest.raises(ValidationError):
        sim_settings()

    monkeypatch.setenv("CORRIDOR_SIM_ENVIRONMENT", "test")
    # Corridor's own environment is not the simulator's.
    monkeypatch.setenv("CORRIDOR_ENVIRONMENT", "production")
    assert sim_settings().environment == "test"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "localhost"])
def test_on_a_loopback_address_the_control_endpoints_need_no_token(host: str) -> None:
    assert sim_settings(host=host).control_token is None


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "10.0.0.7", "192.168.1.7", "sim.internal", "", "localhost."],  # noqa: S104
)
@pytest.mark.parametrize("environment", ["development", "test"])
def test_listening_beyond_loopback_without_a_control_token_is_refused(
    host: str, environment: str
) -> None:
    with pytest.raises(ValidationError) as refusal:
        sim_settings(host=host, environment=environment)

    assert "control_token" in str(refusal.value)
    assert (
        sim_settings(
            host=host, environment=environment, control_token=SecretStr(CONTROL_TOKEN)
        ).host
        == host
    )


# --- authentication ------------------------------------------------------------------------


@pytest.fixture
def guarded(sim: Sim) -> str:
    """A route that has nothing but the provider routes' authentication in front of it."""

    async def probe() -> dict[str, bool]:
        return {"reached": True}

    sim.app.add_api_route("/probe", probe, dependencies=[Depends(require_api_key)])
    return "/probe"


async def test_the_api_key_as_a_bearer_credential_is_accepted(sim: Sim, guarded: str) -> None:
    response = await sim.anonymous.get(guarded, headers={"Authorization": f"Bearer {API_KEY}"})

    assert (response.status_code, response.json()) == (200, {"reached": True})


async def test_the_scheme_is_matched_without_regard_to_case(sim: Sim, guarded: str) -> None:
    response = await sim.anonymous.get(guarded, headers={"Authorization": f"bearer {API_KEY}"})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Bearer not-the-key",
        f"Bearer {API_KEY}x",
        f"Bearer {API_KEY[:-1]}",
        f"Bearer  {API_KEY}",
        f"Basic {API_KEY}",
        f"{API_KEY}",
        "Bearer cl\u00e9",
    ],
)
async def test_a_missing_or_wrong_credential_is_refused_with_the_contracts_error(
    sim: Sim, guarded: str, authorization: str | None
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization.encode("latin-1")}

    response = await sim.anonymous.get(guarded, headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == "unauthorized"
    assert API_KEY not in response.text


# --- the control token ---------------------------------------------------------------------

# One request to every control route, valid or not: authentication comes before the body.
CONTROL_ROUTES = [
    ("POST", "/reset"),
    ("GET", "/clock"),
    ("POST", "/clock/advance"),
    ("POST", "/bank/deposits"),
    ("POST", "/bank/deposits/dep_1/return"),
    ("GET", "/bank/payouts"),
    ("GET", "/bank/deposits"),
    ("GET", "/bank/balances"),
    ("POST", "/custody/deposits"),
    ("POST", "/custody/deposits/dep_1/drop"),
    ("POST", "/chain/mine"),
    ("GET", "/custody/withdrawals"),
    ("GET", "/custody/deposits"),
    ("GET", "/custody/balances"),
    ("POST", "/fx/rates"),
    ("POST", "/fx/freeze"),
    ("POST", "/faults"),
    ("DELETE", "/faults"),
    ("POST", "/webhooks/behaviour"),
    ("POST", "/webhooks/deliver"),
    ("GET", "/webhooks/events"),
]


def test_the_list_of_control_routes_is_every_control_route(sim: Sim) -> None:
    served = {
        (route.method, route.path.removeprefix("/_control"))
        for route in served_routes(sim.app)
        if route.path.startswith("/_control")
    }

    assert served == {
        (method, path.replace("dep_1", "{deposit_id}")) for method, path in CONTROL_ROUTES
    }


def test_every_control_route_has_the_token_check_in_front_of_it(sim: Sim) -> None:
    unguarded = [
        (route.method, route.path)
        for route in served_routes(sim.app)
        if route.path.startswith("/_control") and require_control_token not in route.calls
    ]

    assert unguarded == []


@pytest.mark.parametrize(("method", "path"), CONTROL_ROUTES)
async def test_every_control_route_asks_for_the_token_when_one_is_configured(
    launch: Launch, method: str, path: str
) -> None:
    sim = await launch(host="0.0.0.0", control_token=SecretStr(CONTROL_TOKEN))  # noqa: S104

    response = await sim.anonymous.request(method, f"/_control{path}", json={})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Bearer not-the-token",
        f"Bearer {CONTROL_TOKEN}x",
        f"Bearer {CONTROL_TOKEN[:-1]}",
        f"Basic {CONTROL_TOKEN}",
        f"{CONTROL_TOKEN}",
        # The key of the provider API is not the key to the control endpoints.
        f"Bearer {API_KEY}",
        "Bearer cl\u00e9",
    ],
)
async def test_a_missing_or_wrong_control_token_is_refused_and_changes_nothing(
    launch: Launch, authorization: str | None
) -> None:
    sim = await launch(control_token=SecretStr(CONTROL_TOKEN))
    headers = {} if authorization is None else {"Authorization": authorization.encode("latin-1")}

    response = await sim.anonymous.post(
        "/_control/clock/advance", json={"seconds": 30}, headers=headers
    )

    assert response.status_code == 401
    assert CONTROL_TOKEN not in response.text
    clock = await sim.anonymous.get(
        "/_control/clock", headers={"Authorization": f"Bearer {CONTROL_TOKEN}"}
    )
    assert clock.json()["now"] == "2026-01-15T12:00:00Z"


async def test_the_control_token_opens_the_control_routes(launch: Launch) -> None:
    sim = await launch(control_token=SecretStr(CONTROL_TOKEN))

    response = await sim.anonymous.post(
        "/_control/clock/advance",
        json={"seconds": 30},
        headers={"Authorization": f"bearer {CONTROL_TOKEN}"},
    )

    assert (response.status_code, response.json()["now"]) == (200, "2026-01-15T12:00:30Z")


async def test_the_control_token_is_not_a_key_to_the_provider_api(launch: Launch) -> None:
    sim = await launch(control_token=SecretStr(CONTROL_TOKEN))

    response = await sim.anonymous.get(
        "/bank/v1/payouts", headers={"Authorization": f"Bearer {CONTROL_TOKEN}"}
    )

    assert response.status_code == 401


# --- the clock, through the control endpoints ----------------------------------------------


async def test_the_clock_is_read_without_credentials(sim: Sim) -> None:
    response = await sim.anonymous.get("/_control/clock")

    assert response.status_code == 200
    assert response.json() == {"now": "2026-01-15T12:00:00Z", "mode": "manual"}


async def test_advancing_moves_a_manual_clock_by_exactly_that_much(sim: Sim) -> None:
    first = await sim.anonymous.post("/_control/clock/advance", json={"seconds": 30})
    second = await sim.anonymous.post("/_control/clock/advance", json={"seconds": 0.25})

    assert first.status_code == 200
    assert first.json() == {"now": "2026-01-15T12:00:30Z", "mode": "manual"}
    assert second.json() == {"now": "2026-01-15T12:00:30.250000Z", "mode": "manual"}
    assert (await sim.anonymous.get("/_control/clock")).json() == second.json()


async def test_a_manual_clock_starts_at_the_configured_time(launch: object) -> None:
    sim = await launch(start_time=datetime(2026, 6, 1, 8, 0, tzinfo=UTC))  # type: ignore[operator]

    assert (await sim.control("GET", "/clock"))["now"] == "2026-06-01T08:00:00Z"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"seconds": -1},
        {"seconds": "30"},
        {"seconds": True},
        {"seconds": None},
        {"seconds": 32 * 86_400},
        [30],
        "30",
        30,
    ],
)
async def test_a_malformed_advance_is_refused_and_moves_nothing(sim: Sim, body: object) -> None:
    response = await sim.anonymous.post("/_control/clock/advance", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert (await sim.control("GET", "/clock"))["now"] == "2026-01-15T12:00:00Z"


@pytest.mark.parametrize("content", [b"", b"{not json", b'{"seconds": NaN}', b"\xff\xfe"])
async def test_a_body_that_is_not_json_is_refused(sim: Sim, content: bytes) -> None:
    response = await sim.anonymous.post(
        "/_control/clock/advance", content=content, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


# --- errors --------------------------------------------------------------------------------


async def test_an_unknown_route_is_an_error_in_the_contracts_shape(sim: Sim) -> None:
    response = await sim.api.get("/bank/v1/no-such-thing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert set(response.json()["error"]) == {"code", "message"}


async def test_a_wrong_method_is_an_error_in_the_contracts_shape(sim: Sim) -> None:
    response = await sim.anonymous.delete("/_control/clock")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.headers["allow"] == "GET"


async def test_an_unexpected_error_is_a_500_that_reveals_nothing(sim: Sim) -> None:
    async def crash() -> None:
        raise RuntimeError("internal detail: /var/lib/sim")

    sim.app.add_api_route("/probe/crash", crash)
    transport = httpx.ASGITransport(app=sim.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as tolerant:
        response = await tolerant.get("/probe/crash")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "internal detail" not in response.text
    assert "RuntimeError" not in response.text


async def test_a_request_the_framework_rejects_is_an_error_in_the_contracts_shape(
    sim: Sim,
) -> None:
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    sim.app.add_api_route("/probe/items/{item_id}", item)

    response = await sim.anonymous.get("/probe/items/not-a-number")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "not-a-number" not in response.text


async def test_interactive_documentation_is_not_served(sim: Sim) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await sim.anonymous.get(path)).status_code == 404


# --- start-up and shutdown -----------------------------------------------------------------


async def test_without_a_client_of_its_own_the_app_opens_one_and_closes_it() -> None:
    app = create_app(sim_settings(webhook_timeout_seconds=3))
    assert app.state.sender.client is None

    async with app.router.lifespan_context(app):
        opened = app.state.sender.client
        assert isinstance(opened, httpx.AsyncClient)
        assert not opened.is_closed
        assert opened.timeout == httpx.Timeout(3)

    assert opened.is_closed
    assert app.state.sender.client is None


async def test_a_client_that_was_passed_in_is_left_open() -> None:
    async with httpx.AsyncClient() as passed_in:
        app = create_app(sim_settings(), webhook_client=passed_in)
        async with app.router.lifespan_context(app):
            assert app.state.sender.client is passed_in

        assert not passed_in.is_closed
        assert app.state.sender.client is passed_in


def test_the_app_is_configured_from_the_environment_when_given_no_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORRIDOR_SIM_API_KEY", API_KEY)
    monkeypatch.setenv("CORRIDOR_SIM_BANK_WEBHOOK_SECRET", BANK_WEBHOOK_SECRET)
    monkeypatch.setenv("CORRIDOR_SIM_CUSTODY_WEBHOOK_SECRET", CUSTODY_WEBHOOK_SECRET)
    monkeypatch.setenv("CORRIDOR_SIM_CLOCK_MODE", "manual")
    monkeypatch.chdir("/")  # away from any .env file

    app = create_app()

    assert app.state.sim.clock.mode == "manual"
    assert app.state.sim.settings.api_key.get_secret_value() == API_KEY


def test_python_m_corridor_sim_serves_the_app_on_the_configured_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORRIDOR_SIM_API_KEY", API_KEY)
    monkeypatch.setenv("CORRIDOR_SIM_BANK_WEBHOOK_SECRET", BANK_WEBHOOK_SECRET)
    monkeypatch.setenv("CORRIDOR_SIM_CUSTODY_WEBHOOK_SECRET", CUSTODY_WEBHOOK_SECRET)
    monkeypatch.setenv("CORRIDOR_SIM_HOST", "127.0.0.9")
    monkeypatch.setenv("CORRIDOR_SIM_PORT", "9123")
    monkeypatch.chdir("/")
    served: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: served.append((app, options)))

    entry_point.main()

    ((app, options),) = served
    assert isinstance(app, FastAPI)
    assert (options["host"], options["port"]) == ("127.0.0.9", 9123)

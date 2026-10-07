from pathlib import Path

import pytest
from pydantic import ValidationError

from corridor.platform.config import (
    EnvironmentNotSet,
    MigrationSettings,
    ProcessRole,
    Settings,
    ToolSettings,
    WorkerSettings,
    load_settings,
    settings_for,
)
from corridor.platform.money import ASSETS

REQUIRED = {
    "database_url": "postgresql+asyncpg://app:example@db.invalid/corridor",  # pragma: allowlist secret
    "redis_url": "redis://cache.invalid:6379/0",
}
# The same two as a production server is given them: over TLS.
REQUIRED_OVER_TLS = {
    "database_url": REQUIRED["database_url"] + "?ssl=require",
    "redis_url": "rediss://cache.invalid:6379/0",
}


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CORRIDOR_DATABASE_URL", "CORRIDOR_REDIS_URL", "CORRIDOR_DATABASE_OWNER_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_a_connection_secret_has_no_default(missing: str) -> None:
    values = {name: value for name, value in REQUIRED.items() if name != missing}
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, **values)  # type: ignore[arg-type]
    assert [error["loc"] for error in failure.value.errors()] == [(missing,)]


def test_settings_load_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORRIDOR_DATABASE_URL", REQUIRED["database_url"])
    monkeypatch.setenv("CORRIDOR_REDIS_URL", REQUIRED["redis_url"])
    monkeypatch.setenv("CORRIDOR_DB_LOCK_TIMEOUT_MS", "1234")

    settings = Settings(_env_file=None)

    assert settings.db_lock_timeout_ms == 1234
    assert settings.database_url.get_secret_value() == REQUIRED["database_url"]


def test_secrets_do_not_appear_when_settings_are_printed() -> None:
    settings = Settings(_env_file=None, **REQUIRED)  # type: ignore[arg-type]
    rendered = f"{settings!r} {settings} {settings.model_dump()} {settings.model_dump_json()}"
    assert "example" not in rendered
    assert "cache.invalid" not in rendered


@pytest.mark.parametrize("role", ['app"; DROP ROLE x; --', "App", "", "a" * 64, "1app"])
def test_the_application_role_must_be_a_plain_identifier(role: str) -> None:
    with pytest.raises(ValidationError):
        MigrationSettings(_env_file=None, database_app_role=role)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_app_role=role, **REQUIRED)  # type: ignore[arg-type]


# Fake webhook secrets. They protect nothing.
LONG_ENOUGH = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
ONE_SHORT = "not-thirty-two-characters-long!"  # pragma: allowlist secret

WEBHOOK_SECRET_LISTS = ["bank_rail_webhook_secrets", "custody_webhook_secrets"]


@pytest.mark.parametrize("name", WEBHOOK_SECRET_LISTS)
@pytest.mark.parametrize(
    "secrets", [[""], [ONE_SHORT], [LONG_ENOUGH, ""], [LONG_ENOUGH, ONE_SHORT]]
)
def test_a_webhook_secret_shorter_than_32_characters_is_refused(
    name: str, secrets: list[str]
) -> None:
    assert len(ONE_SHORT) == 31
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, **{name: secrets}, **REQUIRED)  # type: ignore[arg-type]

    assert [error["loc"][0] for error in failure.value.errors()] == [name]


@pytest.mark.parametrize("name", WEBHOOK_SECRET_LISTS)
def test_webhook_secrets_of_32_characters_are_accepted_and_none_at_all_is_allowed(
    name: str,
) -> None:
    assert len(LONG_ENOUGH) == 32
    rotating = Settings(_env_file=None, **{name: [LONG_ENOUGH, LONG_ENOUGH + "x"]}, **REQUIRED)  # type: ignore[arg-type]

    assert [secret.get_secret_value() for secret in getattr(rotating, name)] == [
        LONG_ENOUGH,
        LONG_ENOUGH + "x",
    ]
    assert getattr(Settings(_env_file=None, **REQUIRED), name) == []  # type: ignore[arg-type]


def test_a_refused_setting_is_not_echoed_in_the_error() -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, bank_rail_webhook_secrets=[ONE_SHORT], **REQUIRED)  # type: ignore[arg-type]

    assert ONE_SHORT not in str(failure.value)
    assert "bank_rail_webhook_secrets" in str(failure.value)


def test_a_refused_migration_setting_is_not_echoed_in_the_error() -> None:
    with pytest.raises(ValidationError) as failure:
        MigrationSettings(_env_file=None, database_app_role=ONE_SHORT)

    assert ONE_SHORT not in str(failure.value)
    assert "database_app_role" in str(failure.value)


@pytest.mark.parametrize("value", ["*", "127.0.0.1,*", " * ", "10.0.0.0/8, *"])
def test_trusting_every_proxy_is_refused(value: str) -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, forwarded_allow_ips=value, **REQUIRED)  # type: ignore[arg-type]

    assert [error["loc"] for error in failure.value.errors()] == [("forwarded_allow_ips",)]


@pytest.mark.parametrize("value", ["127.0.0.1", "10.0.0.0/8,192.168.1.7", "::1"])
def test_named_proxies_are_accepted(value: str) -> None:
    settings = Settings(_env_file=None, forwarded_allow_ips=value, **REQUIRED)  # type: ignore[arg-type]

    assert settings.forwarded_allow_ips == value


# --- what production refuses -----------------------------------------------------------------

PRODUCTION: dict[str, object] = {
    **REQUIRED_OVER_TLS,
    "environment": "production",
    "bank_rail_url": "https://bank.example",
    "custody_url": "https://custody.example",
    "fx_rates_url": "https://rates.example",
    "bank_rail_webhook_secrets": [LONG_ENOUGH],
    "custody_webhook_secrets": [LONG_ENOUGH + "x"],
    "api_key_hash_key": LONG_ENOUGH + "y",
}

UNFIT = [
    ({"bank_rail_url": "http://bank.example"}, "bank_rail_url"),
    ({"custody_url": "http://custody.example"}, "custody_url"),
    ({"fx_rates_url": "http://rates.example"}, "fx_rates_url"),
    ({"fx_rates_url": "rates.example"}, "fx_rates_url"),
    ({"bank_rail_url": "httpsx://bank.example"}, "bank_rail_url"),
    ({"rate_limit_enabled": False}, "rate_limit_enabled"),
    ({"webhook_tolerance_seconds": 601}, "webhook_tolerance_seconds"),
    ({"bank_rail_webhook_secrets": []}, "bank_rail_webhook_secrets"),
    ({"custody_webhook_secrets": []}, "custody_webhook_secrets"),
    ({"log_level": "DEBUG"}, "log_level"),
    ({"api_key_hash_key": None}, "api_key_hash_key"),
]


def test_a_production_configuration_that_is_fit_for_it_is_accepted() -> None:
    settings = Settings(_env_file=None, **PRODUCTION)  # type: ignore[arg-type]

    assert settings.environment == "production"


def test_production_accepts_the_longest_webhook_tolerance_and_an_upper_case_scheme() -> None:
    Settings(
        _env_file=None,
        **{**PRODUCTION, "webhook_tolerance_seconds": 600, "bank_rail_url": "HTTPS://bank.example"},  # type: ignore[arg-type]
    )


def test_production_does_not_ask_for_a_provider_that_is_not_configured() -> None:
    bare = {
        name: value
        for name, value in PRODUCTION.items()
        if not name.endswith(("_url", "_secrets")) or name in REQUIRED
    }

    settings = Settings(_env_file=None, **bare)  # type: ignore[arg-type]

    assert (settings.bank_rail_url, settings.bank_rail_webhook_secrets) == (None, [])


@pytest.mark.parametrize(("change", "named"), UNFIT)
def test_production_refuses_a_configuration_fit_only_for_development(
    change: dict[str, object], named: str
) -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, **{**PRODUCTION, **change})  # type: ignore[arg-type]

    message = str(failure.value)
    assert "not a production configuration" in message
    assert named in message
    # Only what is wrong is listed: problems are separated by semicolons.
    assert ";" not in message
    assert "example" not in message
    assert LONG_ENOUGH not in message


def test_production_names_everything_that_is_wrong_at_once() -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(
            _env_file=None,
            **{**PRODUCTION, "log_level": "DEBUG", "rate_limit_enabled": False},  # type: ignore[arg-type]
        )

    assert "log_level" in str(failure.value)
    assert "rate_limit_enabled" in str(failure.value)


@pytest.mark.parametrize("environment", ["development", "test"])
@pytest.mark.parametrize(("change", "_named"), UNFIT)
def test_other_environments_are_not_held_to_what_production_is(
    environment: str, change: dict[str, object], _named: str
) -> None:
    settings = Settings(
        _env_file=None,
        **{**PRODUCTION, "environment": environment, **change},  # type: ignore[arg-type]
    )

    assert settings.environment == environment


# --- settings added for FX -------------------------------------------------------------------


def test_a_spread_of_nothing_is_refused() -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, fx_spread_bps=0, **REQUIRED)  # type: ignore[arg-type]

    assert [error["loc"] for error in failure.value.errors()] == [("fx_spread_bps",)]
    assert Settings(_env_file=None, fx_spread_bps=1, **REQUIRED).fx_spread_bps == 1  # type: ignore[arg-type]


def test_the_key_that_authenticates_cached_rates_is_optional_and_never_short() -> None:
    assert Settings(_env_file=None, **REQUIRED).fx_cache_mac_key is None  # type: ignore[arg-type]
    Settings(_env_file=None, fx_cache_mac_key=LONG_ENOUGH, **REQUIRED)  # type: ignore[arg-type]
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, fx_cache_mac_key=ONE_SHORT, **REQUIRED)  # type: ignore[arg-type]

    assert [error["loc"] for error in failure.value.errors()] == [("fx_cache_mac_key",)]
    assert ONE_SHORT not in str(failure.value)


def test_reconciliation_runs_every_five_minutes_over_an_hour_and_waits_two_minutes() -> None:
    settings = Settings(_env_file=None, **REQUIRED)  # type: ignore[arg-type]

    assert settings.reconciliation_interval_seconds == 300
    assert settings.reconciliation_window_seconds == 3600
    assert settings.reconciliation_grace_seconds == 120


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("reconciliation_interval_seconds", 0),
        ("reconciliation_window_seconds", 0),
        ("reconciliation_grace_seconds", -1),
    ],
)
def test_a_reconciliation_setting_that_could_not_work_is_refused(name: str, value: int) -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, **REQUIRED, **{name: value})  # type: ignore[arg-type]

    assert [error["loc"] for error in failure.value.errors()] == [(name,)]


# --- which process the settings are for ------------------------------------------------------

# What a worker task is given in production: no key for agent keys, and no webhook secret,
# because it answers no request and verifies no webhook.
WORKER_PRODUCTION: dict[str, object] = {
    name: value
    for name, value in PRODUCTION.items()
    if name not in ("api_key_hash_key", "bank_rail_webhook_secrets", "custody_webhook_secrets")
}


def test_settings_are_for_the_api_unless_another_process_is_named() -> None:
    assert Settings.process_role == "api"
    assert (settings_for("api"), settings_for("worker"), settings_for("tool")) == (
        Settings,
        WorkerSettings,
        ToolSettings,
    )
    assert (WorkerSettings.process_role, ToolSettings.process_role) == ("worker", "tool")


@pytest.mark.parametrize("role", ["worker", "tool"])
def test_a_process_that_serves_no_request_starts_in_production_without_the_apis_secrets(
    role: ProcessRole,
) -> None:
    settings = settings_for(role)(_env_file=None, **WORKER_PRODUCTION)  # type: ignore[arg-type]

    assert (settings.environment, settings.process_role) == ("production", role)


def test_the_api_does_not_start_in_production_with_what_a_worker_is_given() -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, **WORKER_PRODUCTION)  # type: ignore[arg-type]

    message = str(failure.value)
    assert "api_key_hash_key" in message
    assert "bank_rail_webhook_secrets" in message
    assert "custody_webhook_secrets" in message


def test_the_process_is_not_chosen_by_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORRIDOR_PROCESS_ROLE", "tool")

    assert Settings(_env_file=None, **REQUIRED).process_role == "api"  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **WORKER_PRODUCTION)  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["api", "worker"])
def test_a_server_process_refuses_to_start_when_the_environment_is_not_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role: ProcessRole
) -> None:
    # An empty directory, so that no .env file of a developer's is read.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CORRIDOR_ENVIRONMENT", raising=False)
    monkeypatch.setenv("CORRIDOR_DATABASE_URL", REQUIRED["database_url"])
    monkeypatch.setenv("CORRIDOR_REDIS_URL", REQUIRED["redis_url"])

    with pytest.raises(EnvironmentNotSet) as failure:
        load_settings(role)

    assert "CORRIDOR_ENVIRONMENT" in str(failure.value)
    monkeypatch.setenv("CORRIDOR_ENVIRONMENT", "development")
    assert load_settings(role).environment == "development"


def test_a_tool_runs_without_the_environment_being_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CORRIDOR_ENVIRONMENT", raising=False)
    monkeypatch.setenv("CORRIDOR_DATABASE_URL", REQUIRED["database_url"])
    monkeypatch.setenv("CORRIDOR_REDIS_URL", REQUIRED["redis_url"])

    settings = load_settings("tool")

    assert (settings.environment, settings.process_role) == ("development", "tool")


# --- what production refuses of a server process ---------------------------------------------

# Each is refused of the API and of the worker alike, and named in the refusal.
UNFIT_FOR_A_SERVER = [
    ({"argon2_time_cost": 2}, "argon2"),
    ({"argon2_memory_cost_kib": 65535}, "argon2"),
    ({"argon2_parallelism": 3}, "argon2"),
    ({"argon2_time_cost": 1, "argon2_memory_cost_kib": 8, "argon2_parallelism": 1}, "argon2"),
    ({"metrics_public": True}, "metrics_public"),
    ({"worker_metrics_host": "0.0.0.0"}, "worker_metrics_host"),  # noqa: S104
    ({"worker_metrics_host": "10.0.0.7"}, "worker_metrics_host"),
    ({"worker_metrics_host": "metrics.internal"}, "worker_metrics_host"),
    ({"forwarded_allow_ips": "0.0.0.0/0"}, "forwarded_allow_ips"),
    ({"forwarded_allow_ips": "10.0.0.0/8, ::/0"}, "forwarded_allow_ips"),
    ({"forwarded_allow_ips": "10.0.0.0/8,0.0.0.0/0"}, "forwarded_allow_ips"),
    ({"database_owner_url": REQUIRED["database_url"]}, "database_owner_url"),
    ({"db_statement_timeout_ms": 0}, "db_statement_timeout_ms"),
    ({"db_lock_timeout_ms": 0}, "db_lock_timeout_ms"),
    ({"db_idle_in_transaction_timeout_ms": 0}, "db_idle_in_transaction_timeout_ms"),
    ({"access_token_ttl_seconds": 3601}, "access_token_ttl_seconds"),
    ({"redis_url": REQUIRED["redis_url"]}, "redis_url"),
    ({"database_url": REQUIRED["database_url"]}, "database_url"),
    ({"database_url": REQUIRED["database_url"] + "?ssl=prefer"}, "database_url"),
    ({"database_url": REQUIRED["database_url"] + "?ssl=disable"}, "database_url"),
    ({"database_url": REQUIRED["database_url"] + "?ssl=require&ssl=disable"}, "database_url"),
]


@pytest.mark.parametrize("role", ["api", "worker"])
@pytest.mark.parametrize(("change", "named"), UNFIT_FOR_A_SERVER)
def test_production_refuses_a_server_process_that_is_configured_for_a_developers_machine(
    role: ProcessRole, change: dict[str, object], named: str
) -> None:
    with pytest.raises(ValidationError) as failure:
        settings_for(role)(_env_file=None, **{**PRODUCTION, **change})  # type: ignore[arg-type]

    message = str(failure.value)
    assert "not a production configuration" in message
    assert named in message
    assert ";" not in message
    assert "example" not in message
    assert "invalid" not in message


@pytest.mark.parametrize(("change", "_named"), UNFIT_FOR_A_SERVER)
def test_a_tool_is_not_held_to_what_a_server_process_is(
    change: dict[str, object], _named: str
) -> None:
    settings = ToolSettings(_env_file=None, **{**PRODUCTION, **change})  # type: ignore[arg-type]

    assert settings.environment == "production"


@pytest.mark.parametrize("environment", ["development", "test"])
@pytest.mark.parametrize(("change", "_named"), UNFIT_FOR_A_SERVER)
def test_other_environments_are_not_held_to_what_a_production_server_is(
    environment: str, change: dict[str, object], _named: str
) -> None:
    settings = Settings(
        _env_file=None,
        **{**PRODUCTION, "environment": environment, **change},  # type: ignore[arg-type]
    )

    assert settings.environment == environment


@pytest.mark.parametrize(
    "fit",
    [
        # The library's two profiles, and more than either.
        {"argon2_time_cost": 3, "argon2_memory_cost_kib": 65536, "argon2_parallelism": 4},
        {"argon2_time_cost": 1, "argon2_memory_cost_kib": 2097152, "argon2_parallelism": 4},
        {"argon2_time_cost": 4, "argon2_memory_cost_kib": 131072, "argon2_parallelism": 8},
        {"worker_metrics_host": "::1"},
        {"worker_metrics_host": "localhost"},
        {"worker_metrics_host": "0.0.0.0", "worker_metrics_public": True},  # noqa: S104
        {"forwarded_allow_ips": "10.0.0.0/24,10.0.1.0/24"},
        {"access_token_ttl_seconds": 3600},
        {"database_url": REQUIRED["database_url"] + "?ssl=verify-full"},
        {"database_url": REQUIRED["database_url"] + "?ssl=verify-ca"},
    ],
)
def test_production_accepts_a_server_process_that_is_fit_for_it(fit: dict[str, object]) -> None:
    for role in ("api", "worker"):
        settings_for(role)(_env_file=None, **{**PRODUCTION, **fit})  # type: ignore[arg-type]


# --- every asset that can be paid out costs something to pay out -----------------------------


def test_every_asset_has_a_least_withdrawal_fee_where_nothing_is_configured() -> None:
    settings = Settings(_env_file=None, **REQUIRED)  # type: ignore[arg-type]

    assert settings.withdrawal_fee_bps == 0
    assert set(settings.withdrawal_min_fee) == set(ASSETS)
    assert settings.withdrawal_min_fee["BRL"] == "0.50"


@pytest.mark.parametrize("environment", ["development", "test", "production"])
@pytest.mark.parametrize(
    "minimums",
    [
        {},
        {"USD": "0.25", "MXN": "5.00", "USDC": "0.15"},
        {"USD": "0.25", "MXN": "5.00", "USDC": "0.15", "BRL": "0"},
        {"USD": "0.25", "MXN": "5.00", "USDC": "0.15", "BRL": "0.00"},
    ],
)
def test_an_asset_that_would_be_paid_out_for_nothing_is_refused_in_every_environment(
    environment: str, minimums: dict[str, str]
) -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(
            _env_file=None,
            **{**PRODUCTION, "environment": environment, "withdrawal_min_fee": minimums},  # type: ignore[arg-type]
        )

    message = str(failure.value)
    assert "withdrawal_min_fee" in message
    assert "BRL" in message


def test_a_percentage_fee_stands_in_for_a_missing_minimum() -> None:
    settings = Settings(_env_file=None, withdrawal_fee_bps=1, withdrawal_min_fee={}, **REQUIRED)  # type: ignore[arg-type]

    assert settings.withdrawal_min_fee == {}

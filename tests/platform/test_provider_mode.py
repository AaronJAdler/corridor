"""Which implementation stands behind each provider port, and what keeps a sandbox mode from
reaching anything but the sandbox."""

import pytest
from pydantic import SecretStr, ValidationError

from corridor.platform.config import (
    INCREASE_SANDBOX_URL,
    ProcessRole,
    Settings,
    load_settings,
)

REQUIRED = {
    "database_url": "postgresql+asyncpg://app:example@db.invalid/corridor",  # pragma: allowlist secret
    "redis_url": "redis://cache.invalid:6379/0",
}
# A made-up key. It is not an Increase key and protects nothing.
FAKE_KEY = "placeholder-increase-key-" + "0" * 16  # pragma: allowlist secret
# Made up, and shaped like the prefix people assume marks an environment.
SANDBOXY_KEY = "sandbox_" + "0" * 24  # pragma: allowlist secret
PRODUCTIONY_KEY = "production_" + "0" * 24  # pragma: allowlist secret
# Shaped like the example account id in Increase's documentation. Not an account of anyone's.
ACCOUNT_ID = "account_in71c4amph0vgo2qllky"


def sandbox(**overrides: object) -> Settings:
    values: dict[str, object] = {
        **REQUIRED,
        "bank_rail_provider": "increase_sandbox",
        "increase_api_key": FAKE_KEY,
        "increase_account_id": ACCOUNT_ID,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CORRIDOR_DATABASE_URL",
        "CORRIDOR_REDIS_URL",
        "CORRIDOR_BANK_RAIL_PROVIDER",
        "CORRIDOR_INCREASE_BASE_URL",
        "CORRIDOR_INCREASE_API_KEY",
        "CORRIDOR_INCREASE_ACCOUNT_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_every_provider_is_the_simulator_unless_told_otherwise() -> None:
    settings = Settings(_env_file=None, **REQUIRED)  # type: ignore[arg-type]

    assert settings.bank_rail_provider == "sim"
    assert settings.custody_provider == "sim"
    assert settings.fx_rates_provider == "sim"


def test_the_sandbox_address_is_the_one_the_documentation_names() -> None:
    assert INCREASE_SANDBOX_URL == "https://sandbox.increase.com"


def test_the_sandbox_is_accepted_at_its_own_address_with_a_key() -> None:
    settings = sandbox()

    assert settings.bank_rail_provider == "increase_sandbox"
    assert settings.increase_base_url == INCREASE_SANDBOX_URL


@pytest.mark.parametrize(
    "address",
    [
        "https://api.increase.com",  # production
        "https://sandbox.increase.com/",  # a trailing slash
        "https://sandbox.increase.com/v1",
        "https://sandbox.increase.com:8443",
        "http://sandbox.increase.com",
        "https://SANDBOX.INCREASE.COM",
        " https://sandbox.increase.com",
        "https://sandbox.increase.com ",
        "https://sandbox.increase.com@api.increase.com",  # user-info: the host is the other one
        "https://sandbox.increase.com.evil.example",
        "https://evilsandbox.increase.com",
        "https://localhost:8100",
        "",
    ],
)
def test_the_sandbox_mode_refuses_any_address_but_the_sandbox(address: str) -> None:
    with pytest.raises(ValidationError) as failure:
        sandbox(increase_base_url=address)

    assert "increase_base_url" in str(failure.value)
    # The address is a setting, so it is named and not repeated.
    assert address.strip() == "" or address.strip() not in str(failure.value)


def test_the_sandbox_mode_does_not_care_what_the_key_looks_like() -> None:
    # Increase's documentation does not say that a key's prefix marks its environment, so
    # nothing here reads one. A key that looks like anything is accepted at the sandbox
    # address, and the address is what is checked.
    for key in (PRODUCTIONY_KEY, SANDBOXY_KEY, "x"):
        assert sandbox(increase_api_key=key).increase_base_url == INCREASE_SANDBOX_URL

    with pytest.raises(ValidationError):
        sandbox(increase_base_url="https://api.increase.com", increase_api_key=SANDBOXY_KEY)


def test_the_sandbox_mode_needs_a_key() -> None:
    with pytest.raises(ValidationError) as failure:
        sandbox(increase_api_key=None)

    assert "increase_api_key" in str(failure.value)


def test_the_sandbox_mode_loads_without_an_account_id_for_the_adapter_to_find() -> None:
    assert sandbox(increase_account_id=None).increase_account_id is None


@pytest.mark.parametrize("account", ["", "account in71", "account/../x", "a" * 201])
def test_an_account_id_that_is_not_a_plain_identifier_is_refused(account: str) -> None:
    with pytest.raises(ValidationError) as failure:
        sandbox(increase_account_id=account)

    assert "increase_account_id" in str(failure.value)


def test_the_increase_webhook_secrets_are_read_as_secrets_of_their_own() -> None:
    settings = sandbox(increase_webhook_secrets=["increase-webhook-signing-value-of-32+"])

    assert all(isinstance(secret, SecretStr) for secret in settings.increase_webhook_secrets)
    assert "signing-value" not in repr(settings)


def test_the_sandbox_mode_is_refused_in_production() -> None:
    with pytest.raises(ValidationError) as failure:
        sandbox(environment="production")

    assert "bank_rail_provider must be sim" in str(failure.value)


@pytest.mark.parametrize("environment", ["development", "test"])
def test_the_sandbox_mode_is_allowed_outside_production(environment: str) -> None:
    assert sandbox(environment=environment).environment == environment


def test_the_key_is_not_shown_when_the_settings_are_printed_or_refused() -> None:
    settings = sandbox()
    rendered = f"{settings!r} {settings} {settings.model_dump()} {settings.model_dump_json()}"
    assert FAKE_KEY not in rendered

    with pytest.raises(ValidationError) as failure:
        sandbox(increase_base_url="https://api.increase.com")
    assert FAKE_KEY not in str(failure.value)


@pytest.mark.parametrize("name", ["custody_provider", "fx_rates_provider"])
def test_custody_and_rates_have_no_sandbox_adapter(name: str) -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, **{name: "increase_sandbox"}, **REQUIRED)  # type: ignore[arg-type]

    assert [error["loc"] for error in failure.value.errors()] == [(name,)]


def test_an_unknown_bank_rail_provider_is_refused() -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, bank_rail_provider="increase", **REQUIRED)  # type: ignore[arg-type]

    assert [error["loc"] for error in failure.value.errors()] == [("bank_rail_provider",)]


@pytest.mark.parametrize("role", ["api", "worker", "tool"])
def test_a_process_of_every_kind_refuses_to_start_with_another_address(
    role: ProcessRole, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORRIDOR_ENVIRONMENT", "development")
    monkeypatch.setenv("CORRIDOR_DATABASE_URL", REQUIRED["database_url"])
    monkeypatch.setenv("CORRIDOR_REDIS_URL", REQUIRED["redis_url"])
    monkeypatch.setenv("CORRIDOR_BANK_RAIL_PROVIDER", "increase_sandbox")
    monkeypatch.setenv("CORRIDOR_INCREASE_API_KEY", FAKE_KEY)
    monkeypatch.setenv("CORRIDOR_INCREASE_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("CORRIDOR_INCREASE_BASE_URL", "https://api.increase.com")

    with pytest.raises(ValidationError):
        load_settings(role)

    monkeypatch.delenv("CORRIDOR_INCREASE_BASE_URL")
    assert load_settings(role).increase_base_url == INCREASE_SANDBOX_URL


def test_the_key_is_read_from_the_variable_the_secret_is_stored_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORRIDOR_INCREASE_API_KEY", FAKE_KEY)

    settings = Settings(_env_file=None, **REQUIRED)  # type: ignore[arg-type]

    assert isinstance(settings.increase_api_key, SecretStr)
    assert settings.increase_api_key.get_secret_value() == FAKE_KEY

import pytest
from pydantic import ValidationError

from corridor.platform.config import MigrationSettings, Settings

REQUIRED = {
    "database_url": "postgresql+asyncpg://app:example@db.invalid/corridor",  # pragma: allowlist secret
    "redis_url": "redis://cache.invalid:6379/0",
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

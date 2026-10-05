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

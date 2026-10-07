"""The setting that agent keys are hashed under."""

import pytest
from pydantic import ValidationError

from corridor.platform.config import Settings

REQUIRED = {
    "database_url": "postgresql+asyncpg://app:example@db.invalid/corridor",  # pragma: allowlist secret
    "redis_url": "redis://cache.invalid:6379/0",
}

# Fake keys. They protect nothing.
LONG_ENOUGH = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
ONE_SHORT = "not-thirty-two-characters-long!"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORRIDOR_API_KEY_HASH_KEY", raising=False)


@pytest.mark.parametrize("value", ["", ONE_SHORT])
def test_a_hash_key_shorter_than_32_characters_is_refused(value: str) -> None:
    assert len(ONE_SHORT) == 31
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, api_key_hash_key=value, **REQUIRED)  # type: ignore[arg-type]

    assert [error["loc"] for error in failure.value.errors()] == [("api_key_hash_key",)]
    assert ONE_SHORT not in str(failure.value)


def test_a_hash_key_of_32_characters_is_accepted() -> None:
    assert len(LONG_ENOUGH) == 32
    settings = Settings(_env_file=None, api_key_hash_key=LONG_ENOUGH, **REQUIRED)  # type: ignore[arg-type]

    assert settings.api_key_hash_key is not None
    assert settings.api_key_hash_key.get_secret_value() == LONG_ENOUGH


def test_the_hash_key_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORRIDOR_API_KEY_HASH_KEY", LONG_ENOUGH)

    settings = Settings(_env_file=None, **REQUIRED)  # type: ignore[arg-type]

    assert settings.api_key_hash_key is not None
    assert settings.api_key_hash_key.get_secret_value() == LONG_ENOUGH


def test_without_a_hash_key_agent_keys_are_simply_not_configured() -> None:
    assert Settings(_env_file=None, **REQUIRED).api_key_hash_key is None  # type: ignore[arg-type]


def test_the_hash_key_does_not_appear_when_settings_are_printed() -> None:
    settings = Settings(_env_file=None, api_key_hash_key=LONG_ENOUGH, **REQUIRED)  # type: ignore[arg-type]

    rendered = f"{settings!r} {settings} {settings.model_dump()} {settings.model_dump_json()}"

    assert LONG_ENOUGH not in rendered

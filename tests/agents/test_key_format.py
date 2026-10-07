"""An agent key as text: what one looks like, how it is hashed, and what is not one."""

import hashlib
import hmac
import re

import pytest
from pydantic import SecretStr

from corridor.agents import keys
from corridor.platform.config import Settings
from corridor.platform.logging import REDACTED, scrub
from tests.agents.support import HASH_KEY, OTHER_HASH_KEY

# Over TLS, as a production process must be given them: one test builds production settings.
REQUIRED = {
    "database_url": "postgresql+asyncpg://app:example@db.invalid/corridor?ssl=require",  # pragma: allowlist secret
    "redis_url": "rediss://cache.invalid:6379/0",
}


def configured(environment: str = "test", hash_key: str | None = HASH_KEY) -> Settings:
    return Settings(
        _env_file=None,
        environment=environment,  # type: ignore[arg-type]
        api_key_hash_key=None if hash_key is None else SecretStr(hash_key),
        **REQUIRED,  # type: ignore[arg-type]
    )


def secret_of(key: str) -> str:
    return key.split("_", 3)[3]


def test_a_key_names_its_environment_then_its_prefix_then_a_secret_of_32_bytes() -> None:
    generated = keys.generate(configured())

    matched = re.fullmatch(r"ck_test_([a-z0-9]{12})_([A-Za-z0-9_-]{43})", generated.key)
    assert matched is not None
    assert matched[1] == generated.prefix


@pytest.mark.parametrize(
    ("environment", "label"),
    [("development", "dev"), ("test", "test"), ("production", "live")],
)
def test_the_environment_a_key_belongs_to_can_be_read_off_it(environment: str, label: str) -> None:
    assert keys.generate(configured(environment)).key.startswith(f"ck_{label}_")


def test_no_two_keys_share_a_prefix_or_a_secret() -> None:
    generated = [keys.generate(configured()) for _ in range(50)]

    assert len({key.prefix for key in generated}) == 50
    assert len({secret_of(key.key) for key in generated}) == 50


def test_what_is_stored_is_the_hmac_sha256_of_the_secret_under_the_server_key() -> None:
    generated = keys.generate(configured())

    expected = hmac.new(
        HASH_KEY.encode(), secret_of(generated.key).encode(), hashlib.sha256
    ).hexdigest()
    assert generated.key_hash == expected
    assert secret_of(generated.key) not in generated.key_hash


def test_a_key_read_back_gives_its_prefix_and_the_digest_that_was_stored() -> None:
    settings = configured()
    generated = keys.generate(settings)

    presented = keys.read(generated.key, settings)

    assert presented is not None
    assert (presented.prefix, presented.digest) == (generated.prefix, generated.key_hash)


def test_under_another_server_key_the_same_key_gives_another_digest() -> None:
    generated = keys.generate(configured())

    presented = keys.read(generated.key, configured(hash_key=OTHER_HASH_KEY))

    assert presented is not None
    assert presented.digest != generated.key_hash


def test_a_key_of_another_environment_is_not_a_key_here() -> None:
    generated = keys.generate(configured("production"))

    assert keys.read(generated.key, configured("test")) is None
    assert keys.read(generated.key, configured("production")) is not None


PREFIX = "abcdefghijkl"
SECRET = "S" * 43


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ck_",
        f"ck_test_{PREFIX}",
        f"ck_test_{PREFIX}_",
        f"sk_test_{PREFIX}_{SECRET}",
        f"CK_test_{PREFIX}_{SECRET}",
        f"ck_test_{PREFIX[:-1]}_{SECRET}",
        f"ck_test_{PREFIX}m_{SECRET}",
        f"ck_test_{PREFIX.upper()}_{SECRET}",
        f"ck_test_abcdef-hijkl_{SECRET}",
        f"ck_test_{PREFIX}_{SECRET[:-1]}",
        f"ck_test_{PREFIX}_{SECRET[:-1]}!",
        f"ck_test_{PREFIX}_{SECRET[:-1]}é",
        f"ck_test_{PREFIX}_{SECRET}\n",
        f" ck_test_{PREFIX}_{SECRET}",
        f"ck_test_{PREFIX}_{'S' * 129}",
        f"ck__{PREFIX}_{SECRET}",
    ],
)
def test_text_that_is_not_shaped_like_a_key_is_not_read_as_one(text: str) -> None:
    assert keys.read(text, configured()) is None


def test_the_shape_that_is_refused_differs_from_one_that_is_read_by_that_much() -> None:
    # The control for the cases above: the same prefix and secret, untouched, are read.
    assert keys.read(f"ck_test_{PREFIX}_{SECRET}", configured()) is not None
    assert keys.read(f"ck_test_{PREFIX}_{'S' * 128}", configured()) is not None


def test_without_a_server_key_nothing_is_read_and_nothing_can_be_generated() -> None:
    generated = keys.generate(configured())
    unconfigured = configured(hash_key=None)

    assert keys.read(generated.key, unconfigured) is None
    with pytest.raises(keys.NotConfigured):
        keys.generate(unconfigured)


def test_a_key_does_not_show_when_what_holds_it_is_printed() -> None:
    settings = configured()
    generated = keys.generate(settings)
    presented = keys.read(generated.key, settings)

    rendered = f"{generated!r} {generated} {presented!r} {presented}"

    assert secret_of(generated.key) not in rendered
    assert generated.key_hash not in rendered


def test_a_key_that_reaches_a_log_line_or_an_audit_detail_is_redacted() -> None:
    generated = keys.generate(configured("production"))

    scrubbed = scrub({"note": f"sent {generated.key} by mistake", "bare": generated.key})

    assert scrubbed == {"note": f"sent {REDACTED} by mistake", "bare": REDACTED}

import json
import logging

import pytest

from corridor.platform.logging import REDACTED, configure_logging, get_logger, redact

# A value for sensitive-looking keys in these tests. It protects nothing.
NOT_A_SECRET = "hunter2-value"  # pragma: allowlist secret


def _redacted(**event: object) -> dict[str, object]:
    return dict(redact(None, "info", dict(event)))


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "new_password",
        "refresh_token",
        "access_token",
        "Authorization",
        "api_key",
        "apiKey",
        "private_key",
        "webhook_secret",
        "account_number",
        "routing_number",
        "clabe",
        "database_url",
    ],
)
def test_a_sensitive_key_never_keeps_its_value(key: str) -> None:
    assert _redacted(**{key: NOT_A_SECRET}) == {key: REDACTED}


def test_sensitive_keys_are_found_inside_nested_structures() -> None:
    event = _redacted(
        request={"headers": {"authorization": "x", "accept": "json"}, "items": [{"token": "y"}]}
    )
    assert event == {
        "request": {
            "headers": {"authorization": REDACTED, "accept": "json"},
            "items": [{"token": REDACTED}],
        }
    }


@pytest.mark.parametrize(
    "value",
    [
        "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLWJ5dGVz",  # pragma: allowlist secret
        "ck_ab12cd34_0123456789abcdefghijklmnop",  # pragma: allowlist secret
        "Bearer abcdefghijklmnop",
        "postgresql://corridor_app:not-a-real-password@db.internal:5432/corridor",  # pragma: allowlist secret
        "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEE\n-----END EC PRIVATE KEY-----",  # pragma: allowlist secret
    ],
)
def test_a_secret_shaped_value_is_removed_whatever_its_key(value: str) -> None:
    event = _redacted(event=f"request failed: {value}", note=value)
    assert value not in json.dumps(event)
    assert REDACTED in str(event["event"])


def test_ordinary_values_pass_through_untouched() -> None:
    event = {
        "event": "transfer.created",
        "amount": "12.50",
        "asset": "USD",
        "attempt": 3,
        "ok": True,
    }
    assert _redacted(**event) == event


def test_the_configured_pipeline_redacts_structlog_and_stdlib_records(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging("INFO", "json")
        get_logger("test").info("login.failed", password=NOT_A_SECRET, user="maria")
        logging.getLogger("third.party").warning("connecting with Bearer abcdefghijklmnop")
        get_logger("test").debug("below.the.level")
    finally:
        root.handlers, root.level = saved_handlers, saved_level

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["event"] for line in lines] == ["login.failed", f"connecting with {REDACTED}"]
    assert lines[0]["password"] == REDACTED
    assert lines[0]["user"] == "maria"
    assert lines[0]["level"] == "info"
    assert lines[0]["timestamp"].endswith("Z")

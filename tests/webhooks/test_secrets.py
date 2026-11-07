"""The start-up check on the webhook secrets."""

import pytest
from pydantic import SecretStr

from corridor import webhooks
from corridor.platform.config import Settings
from tests.webhooks.helpers import (
    BANK_NEXT_SECRET,
    BANK_SECRET,
    CUSTODY_SECRET,
    INCREASE_SECRET,
    with_secrets,
)


@pytest.fixture
def base() -> Settings:
    """Settings whose stores are never opened."""
    return Settings(
        _env_file=None,
        environment="test",
        database_url=SecretStr("postgresql://unused.invalid/unused"),
        redis_url=SecretStr("redis://unused.invalid/0"),
    )


def test_distinct_secrets_per_provider_pass(base: Settings) -> None:
    webhooks.validate_secrets(
        with_secrets(base, bank=(BANK_SECRET, BANK_NEXT_SECRET), custody=(CUSTODY_SECRET,))
    )


def test_no_secrets_at_all_pass(base: Settings) -> None:
    webhooks.validate_secrets(base)


def test_a_secret_shared_by_the_two_providers_is_refused(base: Settings) -> None:
    shared = with_secrets(
        base, bank=(BANK_SECRET, BANK_NEXT_SECRET), custody=(CUSTODY_SECRET, BANK_NEXT_SECRET)
    )

    with pytest.raises(ValueError, match="share") as refused:
        webhooks.validate_secrets(shared)

    assert BANK_NEXT_SECRET not in str(refused.value)


@pytest.mark.parametrize("other", ["bank", "custody"])
def test_increase_sharing_a_secret_with_a_simulator_is_refused(base: Settings, other: str) -> None:
    """Increase signs other bytes than the simulators do. A shared secret is refused all
    the same: no provider's secret is another's."""
    shared = with_secrets(
        base, increase=(INCREASE_SECRET, BANK_SECRET if other == "bank" else CUSTODY_SECRET)
    )

    with pytest.raises(ValueError, match="increase_webhook_secrets") as refused:
        webhooks.validate_secrets(shared)

    assert BANK_SECRET not in str(refused.value)
    assert CUSTODY_SECRET not in str(refused.value)

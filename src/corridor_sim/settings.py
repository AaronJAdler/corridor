"""Configuration.

Everything comes from environment variables prefixed ``CORRIDOR_SIM_``. The API key and the
webhook secrets have no default: the simulator refuses to start without them rather than
run with a guess.

The simulator hands out money that does not exist and lets anyone who reaches ``/_control``
decide what the outside world did. It therefore refuses to start anywhere but a development
or a test environment, and refuses to listen beyond the machine it runs on unless its
control endpoints are given a token.
"""

import ipaddress
from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A short secret verifies a forged request as readily as a real one.
MIN_SECRET_LENGTH: Final = 32
Secret = Annotated[SecretStr, Field(min_length=MIN_SECRET_LENGTH)]


class SimSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CORRIDOR_SIM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # A value that is refused is very often a secret pasted into the wrong variable.
        # The error names the setting and never repeats what it was given.
        hide_input_in_errors=True,
    )

    # Where this is running. There is no third value: a simulator has no place in a
    # deployment that moves real money, and one that is told it is there does not start.
    environment: Literal["development", "test"] = "development"

    # What Corridor's adapters present as ``Authorization: Bearer <api key>``.
    api_key: Secret

    # Where each provider's webhooks go, and the secret that signs them. A provider with no
    # URL records its events and delivers none.
    bank_webhook_url: str | None = None
    custody_webhook_url: str | None = None
    bank_webhook_secret: Secret
    custody_webhook_secret: Secret
    # How long one delivery may take before it counts as failed.
    webhook_timeout_seconds: float = Field(default=5.0, gt=0, le=300)

    # A manual clock starts at ``start_time`` (2026-01-15T12:00:00Z if unset) and moves only
    # through the control endpoint. A realtime clock follows the wall clock.
    clock_mode: Literal["manual", "realtime"] = "realtime"
    start_time: AwareDatetime | None = None

    # Seeds the identifiers and the rate walk, so that a run can be repeated exactly.
    seed: int = 20260115

    # How long after it is created a payout settles, per rail, in simulator seconds.
    ach_settle_seconds: float = Field(default=30, ge=0)
    spei_settle_seconds: float = Field(default=5, ge=0)
    pix_settle_seconds: float = Field(default=1, ge=0)

    # The chain: seconds between blocks, and the confirmations that make a movement final.
    block_seconds: float = Field(default=2, gt=0)
    confirmations: int = Field(default=3, ge=1)

    # Where ``python -m corridor_sim`` listens.
    host: str = "127.0.0.1"
    port: int = Field(default=8100, ge=0, le=65535)

    # What ``/_control`` asks for as ``Authorization: Bearer <control token>``. Without one
    # the control endpoints take anybody's word, which is only safe on a loopback address.
    control_token: Secret | None = None

    @model_validator(mode="after")
    def _control_is_not_open_to_the_network(self) -> Self:
        if self.control_token is None and not is_loopback(self.host):
            raise ValueError("set control_token to listen on an address that is not loopback")
        return self


def is_loopback(host: str) -> bool:
    """Whether only this machine can reach an address. A name other than ``localhost`` is
    taken not to be: what it resolves to is not known here."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_settings() -> SimSettings:
    return SimSettings()

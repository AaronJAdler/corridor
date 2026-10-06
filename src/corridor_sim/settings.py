"""Configuration.

Everything comes from environment variables prefixed ``CORRIDOR_SIM_``. The API key and the
webhook secrets have no default: the simulator refuses to start without them rather than
run with a guess.
"""

from typing import Literal

from pydantic import AwareDatetime, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SimSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CORRIDOR_SIM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # What Corridor's adapters present as ``Authorization: Bearer <api key>``.
    api_key: SecretStr = Field(min_length=1)

    # Where each provider's webhooks go, and the secret that signs them. A provider with no
    # URL records its events and delivers none.
    bank_webhook_url: str | None = None
    custody_webhook_url: str | None = None
    bank_webhook_secret: SecretStr = Field(min_length=1)
    custody_webhook_secret: SecretStr = Field(min_length=1)
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


def load_settings() -> SimSettings:
    return SimSettings()

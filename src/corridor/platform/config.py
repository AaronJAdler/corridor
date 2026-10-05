"""Configuration.

Everything comes from environment variables prefixed ``CORRIDOR_``. Values that are secret
have no default: the process refuses to start without them rather than run with a guess.
"""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# The role name is interpolated into GRANT statements, so it is restricted to a plain
# identifier.
APP_ROLE_PATTERN = r"^[a-z_][a-z0-9_]{0,62}$"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CORRIDOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # Addresses of the proxies whose X-Forwarded-For is believed. In deployment this is the
    # load balancer's network; the rate limiter keys on the client address it yields.
    forwarded_allow_ips: str = "127.0.0.1"

    # PostgreSQL. The application connects as the least-privileged role. The owner URL is
    # used only by migrations and is not set on the API or the worker.
    database_url: SecretStr
    database_owner_url: SecretStr | None = None
    database_app_role: str = Field(default="corridor_app", pattern=APP_ROLE_PATTERN)
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=10, ge=0, le=200)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    db_statement_timeout_ms: int = Field(default=10_000, ge=0)
    db_lock_timeout_ms: int = Field(default=5_000, ge=0)
    db_idle_in_transaction_timeout_ms: int = Field(default=15_000, ge=0)

    # Redis holds nothing that cannot be rebuilt, so its timeouts are short: a slow Redis is
    # treated as an absent one.
    redis_url: SecretStr
    redis_key_prefix: str = "corridor:"
    redis_timeout_seconds: float = Field(default=0.25, gt=0)


class MigrationSettings(BaseSettings):
    """What a migration run needs, and nothing else: the owner connection and the name of
    the application role that grants are issued to."""

    model_config = SettingsConfigDict(
        env_prefix="CORRIDOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    database_owner_url: SecretStr | None = None
    database_app_role: str = Field(default="corridor_app", pattern=APP_ROLE_PATTERN)


def load_settings() -> Settings:
    return Settings()

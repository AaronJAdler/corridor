"""Configuration.

Everything comes from environment variables prefixed ``CORRIDOR_``. Values that are secret
have no default: the process refuses to start without them rather than run with a guess.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from corridor.platform.errors import InvalidRequest
from corridor.platform.money import parse_amount

# The role name is interpolated into GRANT statements, so it is restricted to a plain
# identifier.
APP_ROLE_PATTERN = r"^[a-z_][a-z0-9_]{0,62}$"


MIN_WEBHOOK_SECRET_LENGTH = 32
WebhookSecret = Annotated[SecretStr, Field(min_length=MIN_WEBHOOK_SECRET_LENGTH)]

# The longest a production deployment lets a webhook's timestamp be from its own clock. A
# captured delivery can be replayed for that long.
MAX_PRODUCTION_WEBHOOK_TOLERANCE_SECONDS: Final = 600

# What a withdrawal costs at the least, per asset, as decimal strings in major units. A fee
# in basis points alone rounds to nothing on a small withdrawal, which still costs a payout.
DEFAULT_WITHDRAWAL_MIN_FEE: Final[Mapping[str, str]] = {
    "USD": "0.25",
    "MXN": "5.00",
    "USDC": "0.15",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CORRIDOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # A value that is refused is very often a secret pasted into the wrong variable.
        # The error names the setting and never repeats what it was given.
        hide_input_in_errors=True,
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

    # Passwords are hashed with Argon2id. None means the library's RFC 9106 parameters;
    # the test suite lowers them so a login does not cost a tenth of a second.
    argon2_time_cost: int | None = Field(default=None, ge=1)
    argon2_memory_cost_kib: int | None = Field(default=None, ge=8)
    argon2_parallelism: int | None = Field(default=None, ge=1)

    # Access tokens are ES256 JWTs. The private key is given inline (as it arrives from a
    # secret store) or as a file (as `corridor keys generate` writes it). The key id is
    # derived from the key, so there is nothing to keep in step. Public keys of retired
    # signing keys are listed so their tokens verify until they expire.
    jwt_issuer: str = "corridor"
    jwt_audience: str = "corridor-api"
    jwt_signing_key: SecretStr | None = None
    jwt_signing_key_file: Path | None = None
    jwt_additional_public_keys: list[str] = Field(default_factory=list)
    access_token_ttl_seconds: int = Field(default=900, ge=30)
    refresh_token_ttl_seconds: int = Field(default=30 * 24 * 3600, ge=60)

    # After this many consecutive failed logins an account is locked, for a period that
    # doubles with each further failure up to the maximum.
    login_lockout_threshold: int = Field(default=5, ge=1)
    login_lockout_base_seconds: int = Field(default=60, ge=1)
    login_lockout_max_seconds: int = Field(default=3600, ge=1)

    # Rate limits, per client address. They fail open when Redis is unavailable.
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = Field(default=600, ge=1)
    rate_limit_auth_per_minute: int = Field(default=10, ge=1)

    # The outbox dispatcher and the worker that runs it.
    outbox_batch_size: int = Field(default=20, ge=1, le=500)
    outbox_concurrency: int = Field(default=10, ge=1, le=100)
    outbox_claim_seconds: int = Field(default=60, ge=1)
    outbox_max_attempts: int = Field(default=8, ge=1)
    outbox_poll_seconds: float = Field(default=5.0, gt=0)
    outbox_retention_days: int = Field(default=7, ge=1)
    # The worker serves its metrics on this port, or not at all when it is 0, and only on
    # the loopback address unless told otherwise: the endpoint has no authentication.
    worker_metrics_port: int = Field(default=0, ge=0, le=65535)
    worker_metrics_host: str = "127.0.0.1"

    # The providers: where each one is, and the key presented to it. None means the
    # provider is not configured, and its adapter refuses to be built. One deadline covers
    # a whole call, from connecting to the last byte of the response.
    bank_rail_url: str | None = None
    custody_url: str | None = None
    fx_rates_url: str | None = None
    bank_rail_api_key: SecretStr | None = None
    custody_api_key: SecretStr | None = None
    fx_rates_api_key: SecretStr | None = None
    provider_timeout_seconds: float = Field(default=5.0, gt=0)

    # The secrets that sign each provider's webhooks. A list, so that a secret can be
    # rotated: the new one is added before the provider starts using it.
    # An empty or a short secret would verify a forged webhook as readily as a real one,
    # so each has a least length. No secrets at all means the provider is not configured.
    bank_rail_webhook_secrets: list[WebhookSecret] = Field(default_factory=list)
    custody_webhook_secrets: list[WebhookSecret] = Field(default_factory=list)

    # What a transfer between users costs the sender: basis points of the amount, rounded
    # down, and never less than the asset's minimum, a decimal string in major units. An
    # asset that is not named has no minimum.
    transfer_fee_bps: int = Field(default=0, ge=0, le=1000)
    transfer_min_fee: dict[str, str] = Field(default_factory=dict)

    # Agents. The key that agent API keys are hashed under: what is stored is the
    # HMAC-SHA256 of a key's secret part, so a copy of the table is of no use without it.
    # None means agent keys are not configured: none can be issued and none is accepted.
    # A short key could be searched for, so it has the least length a webhook secret has.
    api_key_hash_key: Annotated[SecretStr, Field(min_length=32)] | None = None

    @field_validator("forwarded_allow_ips")
    @classmethod
    def _no_wildcard_proxy(cls, value: str) -> str:
        # "*" believes X-Forwarded-For from anyone, so any client could choose the address
        # it is rate-limited and logged under.
        if any(entry.strip() == "*" for entry in value.split(",")):
            raise ValueError('name the proxies to trust; "*" trusts every client')
        return value

    # Webhooks: how far a signature's timestamp may be from our clock.
    webhook_tolerance_seconds: int = Field(default=300, ge=1)

    # FX. The customer rate is the mid rate less the spread. A mid rate older than
    # fx_rate_max_age_seconds is refused; a quote lives fx_quote_ttl_seconds.
    # A spread of nothing would convert at the mid rate, back and forth, for free.
    fx_spread_bps: int = Field(default=50, ge=1, le=1000)
    fx_rate_max_age_seconds: int = Field(default=15, ge=1)
    fx_quote_ttl_seconds: int = Field(default=30, ge=1)
    fx_rate_cache_seconds: int = Field(default=5, ge=1)
    # Authenticates the rates cached in Redis, so that whoever can write to Redis cannot
    # set a price. Without it rates are cached in each process and Redis is not used.
    fx_cache_mac_key: WebhookSecret | None = None

    # Withdrawals. Corridor's own fee: basis points of the amount, rounded down, and never
    # less than the asset's minimum, a decimal string in major units.
    withdrawal_fee_bps: int = Field(default=0, ge=0, le=1000)
    withdrawal_min_fee: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_WITHDRAWAL_MIN_FEE)
    )
    # A submitted payout with no settlement webhook after this long is checked by polling.
    payout_sweep_after_seconds: int = Field(default=120, ge=1)

    @field_validator("withdrawal_min_fee", "transfer_min_fee")
    @classmethod
    def _amounts_of_known_assets(cls, value: dict[str, str]) -> dict[str, str]:
        for asset, amount in value.items():
            try:
                parse_amount(amount, asset, allow_zero=True)
            except InvalidRequest:
                # How platform.money refuses an asset code or an amount.
                raise ValueError(
                    "each entry is an asset code and a decimal amount in that asset"
                ) from None
        return value

    @model_validator(mode="after")
    def _safe_in_production(self) -> Self:
        """Refuse a production configuration that is only fit for a developer's machine.

        Each message names settings and never repeats a value.
        """
        if self.environment != "production":
            return self
        problems: list[str] = []
        for name in ("bank_rail_url", "custody_url", "fx_rates_url"):
            url = getattr(self, name)
            if url is not None and not url.lower().startswith("https://"):
                problems.append(f"{name} must be an https URL")
        if not self.rate_limit_enabled:
            problems.append("rate_limit_enabled must be on")
        if self.webhook_tolerance_seconds > MAX_PRODUCTION_WEBHOOK_TOLERANCE_SECONDS:
            problems.append(
                "webhook_tolerance_seconds must be at most "
                f"{MAX_PRODUCTION_WEBHOOK_TOLERANCE_SECONDS}"
            )
        # A provider that is called and whose webhooks cannot be verified would have its
        # payouts settled by the sweeper alone, and its deposits never.
        for url_name, secrets_name in (
            ("bank_rail_url", "bank_rail_webhook_secrets"),
            ("custody_url", "custody_webhook_secrets"),
        ):
            if getattr(self, url_name) is not None and not getattr(self, secrets_name):
                problems.append(f"{secrets_name} must be set when {url_name} is")
        if self.log_level == "DEBUG":
            problems.append("log_level must not be DEBUG")
        if problems:
            raise ValueError("not a production configuration: " + "; ".join(problems))
        return self


class MigrationSettings(BaseSettings):
    """What a migration run needs, and nothing else: the owner connection and the name of
    the application role that grants are issued to."""

    model_config = SettingsConfigDict(
        env_prefix="CORRIDOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # A value that is refused is very often a secret pasted into the wrong variable.
        # The error names the setting and never repeats what it was given.
        hide_input_in_errors=True,
    )

    database_owner_url: SecretStr | None = None
    database_app_role: str = Field(default="corridor_app", pattern=APP_ROLE_PATTERN)


def load_settings() -> Settings:
    return Settings()

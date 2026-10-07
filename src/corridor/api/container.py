"""What a running API process holds: settings, its connections to the two data stores,
what it authenticates with, and its clients for the providers it has been told about."""

from dataclasses import dataclass

from corridor.identity import KeySet, PasswordHasher
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.redis import RedisStore
from corridor.providers import BankRail, Custodian, RateSource


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    db: Database
    redis: RedisStore
    keys: KeySet
    hasher: PasswordHasher
    # None when the provider has no address configured: what needs it answers 503.
    bank: BankRail | None = None
    custody: Custodian | None = None
    rates: RateSource | None = None

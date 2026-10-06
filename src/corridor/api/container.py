"""What a running API process holds: settings, its connections to the two data stores, and
what it authenticates with."""

from dataclasses import dataclass

from corridor.identity import KeySet, PasswordHasher
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.redis import RedisStore


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    db: Database
    redis: RedisStore
    keys: KeySet
    hasher: PasswordHasher

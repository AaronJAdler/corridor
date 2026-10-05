"""What a running API process holds: settings and its connections to the two data stores."""

from dataclasses import dataclass

from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.redis import RedisStore


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    db: Database
    redis: RedisStore

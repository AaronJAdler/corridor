"""Agent API keys as text: making one, reading one, and the hash that is kept of it.

A key is ``ck_<environment>_<prefix>_<secret>``. The prefix is public: it is stored as it
is, and it is how a key is found. The secret is 32 random bytes and is never stored. What
is kept is its HMAC-SHA256 under a key that only the server holds, so the table alone
neither yields a key nor lets one be checked.
"""

import hashlib
import hmac
import re
import secrets
import string
from dataclasses import dataclass, field
from typing import Final

from corridor.agents.types import PresentedKey
from corridor.identity import Scope
from corridor.platform.config import Settings

# What every key starts with. ``platform.logging`` redacts anything of this shape.
MARK: Final = "ck"

PREFIX_LENGTH: Final = 12
# Neither "_" nor "-", so that the prefix is one field of the key however it is split.
_PREFIX_ALPHABET: Final = string.ascii_lowercase + string.digits

SECRET_BYTES: Final = 32
# 32 bytes are 43 characters of URL-safe base64. The most bounds the hashing that a
# request can ask for with a string that was never a key.
_MIN_SECRET_LENGTH: Final = 43
_MAX_SECRET_LENGTH: Final = 128

# A key says where it belongs, so that one from a test system is recognised when it turns
# up somewhere it should not be.
_ENVIRONMENT_LABELS: Final = {"development": "dev", "test": "test", "production": "live"}

_KEY: Final = re.compile(
    rf"{MARK}_(?P<environment>[a-z]+)_(?P<prefix>[a-z0-9]{{{PREFIX_LENGTH}}})"
    rf"_(?P<secret>[A-Za-z0-9_-]{{{_MIN_SECRET_LENGTH},{_MAX_SECRET_LENGTH}}})"
)

# Every scope a key can be given. Written out rather than taken from ``Scope`` whole: a
# scope added later for what only the owner may do (managing agents and their keys,
# deciding an approval, anything of an administrator's) is not open to agents until it is
# put here on purpose.
AGENT_SCOPES: Final[frozenset[str]] = frozenset(
    {
        Scope.WALLET_READ,
        Scope.TRANSFERS_READ,
        Scope.TRANSFERS_CREATE,
        Scope.DEPOSITS_READ,
        Scope.WITHDRAWALS_READ,
        Scope.WITHDRAWALS_CREATE,
        Scope.BENEFICIARIES_READ,
        Scope.BENEFICIARIES_WRITE,
        Scope.FX_READ,
        Scope.FX_CONVERT,
    }
)


class NotConfigured(RuntimeError):
    """There is no server key to hash under. Callers check before they get this far."""


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """A new key, with the two parts of it that are stored."""

    key: str = field(repr=False)
    prefix: str
    key_hash: str = field(repr=False)


def is_configured(settings: Settings) -> bool:
    return settings.api_key_hash_key is not None


def generate(settings: Settings) -> GeneratedKey:
    hash_key = _hash_key(settings)
    if hash_key is None:
        raise NotConfigured("api_key_hash_key is not set")
    prefix = "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(PREFIX_LENGTH))
    secret = secrets.token_urlsafe(SECRET_BYTES)
    label = _ENVIRONMENT_LABELS[settings.environment]
    return GeneratedKey(
        key=f"{MARK}_{label}_{prefix}_{secret}", prefix=prefix, key_hash=_digest(secret, hash_key)
    )


def read(text: str, settings: Settings) -> PresentedKey | None:
    """What a presented string comes to as a key, or None if it cannot be one here.

    The digest is worked out now, from the string alone, and so before anyone knows
    whether the prefix exists: a key that names no row costs what a real one costs.
    """
    hash_key = _hash_key(settings)
    # fullmatch, because "$" alone would let a trailing newline through.
    matched = _KEY.fullmatch(text)
    if hash_key is None or matched is None:
        return None
    if matched["environment"] != _ENVIRONMENT_LABELS[settings.environment]:
        return None
    return PresentedKey(prefix=matched["prefix"], digest=_digest(matched["secret"], hash_key))


def _hash_key(settings: Settings) -> bytes | None:
    configured = settings.api_key_hash_key
    return None if configured is None else configured.get_secret_value().encode()


def _digest(secret: str, hash_key: bytes) -> str:
    return hmac.new(hash_key, secret.encode("ascii"), hashlib.sha256).hexdigest()

"""Tokens: signed access tokens, opaque refresh tokens, and the revocation hint.

An access token is a short-lived ES256 JWT that says who is calling and in which session.
An asymmetric signature means that a service split out later can verify a token while
holding nothing that could sign one. A refresh token is a random string; what it stands for
is in the ``refresh_tokens`` table, which ``service`` owns.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Final

import jwt

from corridor.identity.errors import InvalidToken
from corridor.identity.keys import ALGORITHM, KeySet
from corridor.identity.principal import ALL_SCOPES
from corridor.identity.types import AccessClaims, Role
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.ids import new_id
from corridor.platform.redis import RedisStore

_REQUIRED_CLAIMS: Final = ("iss", "aud", "sub", "sid", "role", "scope", "iat", "exp", "jti")
_ROLES: Final = ("user", "admin")

# How far ahead of this process's clock a token's issue time may be. Two instances do not
# agree to the second.
_MAX_CLOCK_SKEW_SECONDS: Final = 30


def mint_access_token(
    *, user_id: uuid.UUID, session_id: uuid.UUID, role: Role, keys: KeySet, settings: Settings
) -> tuple[str, datetime]:
    """Sign an access token for a user's own session. Returns it with its expiry."""
    # Whole seconds, rounded down: a token never claims to be issued in the future.
    issued_at = int(utcnow().timestamp())
    expires_at = issued_at + settings.access_token_ttl_seconds
    token = jwt.encode(
        {
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "sub": str(user_id),
            "sid": str(session_id),
            "role": role,
            "scope": ALL_SCOPES,
            "iat": issued_at,
            "exp": expires_at,
            "jti": str(new_id()),
        },
        keys.signing_key,
        algorithm=ALGORITHM,
        headers={"kid": keys.signing_kid},
    )
    return token, datetime.fromtimestamp(expires_at, UTC)


def verify_access_token(token: str, *, keys: KeySet, settings: Settings) -> AccessClaims:
    """Check a token and return what it says. Every failure is the same ``InvalidToken``."""
    if not token.isascii():
        raise InvalidToken
    try:
        # The token's kid picks the key, but only from the configured keys, and the one
        # algorithm is fixed here. Nothing else in a token has a say in how it is verified.
        kid = jwt.get_unverified_header(token).get("kid")
        key = keys.public_keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise InvalidToken
        claims = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={
                "require": list(_REQUIRED_CLAIMS),
                "strict_aud": True,
                # Time is checked below, against the application clock rather than the
                # library's: one clock for the whole system, and one that tests can move.
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except jwt.PyJWTError:
        raise InvalidToken from None

    now = utcnow().timestamp()
    issued_at, expires_at = _seconds(claims["iat"]), _seconds(claims["exp"])
    if expires_at <= now:
        raise InvalidToken
    if issued_at > now + _MAX_CLOCK_SKEW_SECONDS:
        raise InvalidToken

    role, scope = claims["role"], claims["scope"]
    if role not in _ROLES or not isinstance(scope, str):
        raise InvalidToken
    return AccessClaims(
        user_id=_uuid(claims["sub"]),
        session_id=_uuid(claims["sid"]),
        role=role,
        scope=scope,
        issued_at=_moment(issued_at),
        expires_at=_moment(expires_at),
        token_id=claims["jti"],
    )


def _seconds(value: object) -> int:
    # Exactly an int: a bool is one too, as far as isinstance is concerned.
    if type(value) is not int:
        raise InvalidToken
    return value


def _moment(seconds: int) -> datetime:
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except OverflowError, OSError, ValueError:
        raise InvalidToken from None


def _uuid(value: object) -> uuid.UUID:
    if not isinstance(value, str):
        raise InvalidToken
    try:
        return uuid.UUID(value)
    except ValueError:
        raise InvalidToken from None


def new_refresh_token() -> str:
    """An opaque refresh token: 256 random bits. It means nothing without the stored row."""
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """What is stored for a refresh token: its SHA-256, in hex.

    A fast hash is enough, because the token is random rather than chosen by a person, and
    it is looked up by this hash, so the token itself is never compared. Whatever a client
    presents gets a hash, including text that is not valid UTF-8.
    """
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


# --- session revocation hint -----------------------------------------------------------------
#
# An access token outlives a logout by up to its whole lifetime. A mark in Redis lets the API
# refuse it at once. It is a hint, not the record: the record is ``revoked_at`` in PostgreSQL,
# and if Redis is unavailable the hint is skipped and the token lasts until it expires.

_REDIS_USE: Final = "session_revocation"


async def mark_session_revoked(
    redis: RedisStore, session_id: uuid.UUID, *, ttl_seconds: int
) -> None:
    """Mark a session revoked for ``ttl_seconds``: the lifetime of its last access token."""
    if ttl_seconds < 1:
        # Redis refuses such an expiry, and ``attempt`` would take the refusal for Redis
        # being down: the mark would silently not be made.
        raise ValueError("ttl_seconds must be at least 1")
    await redis.attempt(
        _set(redis, _revocation_key(redis, session_id), ttl_seconds),
        default=None,
        use=_REDIS_USE,
    )


async def is_session_revoked(redis: RedisStore, session_id: uuid.UUID) -> bool:
    """Whether a session is marked revoked. False if Redis cannot say."""
    return await redis.attempt(
        _exists(redis, _revocation_key(redis, session_id)), default=False, use=_REDIS_USE
    )


def _revocation_key(redis: RedisStore, session_id: uuid.UUID) -> str:
    return redis.key("revoked", "sid", str(session_id))


async def _set(redis: RedisStore, key: str, ttl_seconds: int) -> None:
    await redis.client.set(key, "1", ex=ttl_seconds)


async def _exists(redis: RedisStore, key: str) -> bool:
    return bool(await redis.client.exists(key))

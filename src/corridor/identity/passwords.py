"""Passwords: the length rule, and Argon2id hashing.

Hashing is slow on purpose. It never runs while a database transaction is open and never on
the event loop's thread: a caller hashes or verifies first and opens its transaction after.
"""

import asyncio
import secrets
from typing import Final

import argon2
from argon2.exceptions import InvalidHashError, VerificationError

from corridor.identity.errors import WeakPassword
from corridor.platform.config import Settings

# Length is the only rule; no mix of characters is required. The maximum is there to bound
# the work one request can demand, not to limit strength.
MIN_PASSWORD_LENGTH: Final = 12
MAX_PASSWORD_LENGTH: Final = 128


def validate_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise WeakPassword(
            f"A password is {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters long."
        )


class PasswordHasher:
    """Argon2id with the configured parameters, or the library's where none is set."""

    def __init__(self, settings: Settings) -> None:
        self._argon2 = argon2.PasswordHasher(
            time_cost=settings.argon2_time_cost or argon2.DEFAULT_TIME_COST,
            memory_cost=settings.argon2_memory_cost_kib or argon2.DEFAULT_MEMORY_COST,
            parallelism=settings.argon2_parallelism or argon2.DEFAULT_PARALLELISM,
        )
        # What a login for an unknown email is verified against. It is made with the same
        # parameters, so it costs the same, and nobody knows the password it is a hash of.
        self._dummy_hash = self._argon2.hash(secrets.token_urlsafe(32))

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._argon2.hash, password)

    async def verify(self, password_hash: str | None, password: str) -> bool:
        """Whether ``password`` matches. Always costs exactly one Argon2 verification.

        With no hash, because there is no such user, the password is verified against a
        dummy and the answer is False. Answering at once would let the time a login takes
        say whether an email address is registered.
        """
        return await asyncio.to_thread(self._verify, password_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        """Whether a stored hash was made with other parameters than today's, and should be
        replaced the next time its password is seen."""
        try:
            return self._argon2.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True

    def _verify(self, password_hash: str | None, password: str) -> bool:
        # A stored value that is not an Argon2 hash is handled like a missing user. Argon2
        # would refuse it before doing any work, and that account would answer faster.
        stored = password_hash if password_hash and _is_argon2_hash(password_hash) else None
        try:
            self._argon2.verify(stored or self._dummy_hash, password)
        except VerificationError, ValueError:
            # The password does not match, or Argon2 could not take the hash or the password.
            return False
        return stored is not None


def _is_argon2_hash(value: str) -> bool:
    try:
        argon2.extract_parameters(value)
    except InvalidHashError:
        return False
    return True

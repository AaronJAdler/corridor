"""Database access: engine, sessions, the unit of work, locks and shared column types.

PostgreSQL is the only source of truth. Transactions are ``READ COMMITTED`` with explicit
locks; see section 5 of the architecture for the lock order this module helps enforce.
"""

import asyncio
import hashlib
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import DateTime, MetaData, Numeric, text
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from corridor.platform.config import Settings
from corridor.platform.metrics import DB_TRANSACTION_RETRIES

# PostgreSQL error codes this codebase branches on.
DEADLOCK_DETECTED: Final = "40P01"
SERIALIZATION_FAILURE: Final = "40001"
LOCK_NOT_AVAILABLE: Final = "55P03"
UNIQUE_VIOLATION: Final = "23505"
CHECK_VIOLATION: Final = "23514"

RETRYABLE: Final = frozenset({DEADLOCK_DETECTED, SERIALIZATION_FAILURE})
MAX_RETRIES: Final = 3

# Every constraint gets a predictable name, so a violation can be mapped to an error code.
NAMING_CONVENTION: Final = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}


class Base(DeclarativeBase):
    """Base for every table model. Modules own their models; nothing shares a table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: DateTime(timezone=True)}  # noqa: RUF012 - SQLAlchemy API


class MinorUnits(TypeDecorator[int]):
    """An amount in an asset's smallest unit: ``NUMERIC(38,0)`` in SQL, ``int`` in Python.

    Binding anything but an ``int`` is an error, which keeps floats out of the money path at
    the last point where they could get in.
    """

    impl = Numeric(38, 0)
    cache_ok = True

    def process_bind_param(self, value: int | None, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"an amount must be an int, not {type(value).__name__}")
        return Decimal(value)

    def process_result_value(self, value: Any | None, dialect: Dialect) -> int | None:
        if value is None:
            return None
        as_int = int(value)
        if as_int != value:
            raise ValueError("the database returned a fractional amount")
        return as_int


def sqlstate_of(error: BaseException) -> str | None:
    """The PostgreSQL error code carried by a SQLAlchemy error, if there is one."""
    if isinstance(error, DBAPIError):
        code = getattr(error.orig, "sqlstate", None)
        return code if isinstance(code, str) else None
    return None


def constraint_of(error: BaseException) -> str | None:
    """The name of the constraint an integrity error violated, if the driver reported it."""
    if isinstance(error, DBAPIError):
        driver_error = getattr(error.orig, "__cause__", None)
        name = getattr(driver_error, "constraint_name", None)
        return name if isinstance(name, str) else None
    return None


def create_engine(settings: Settings, *, application_name: str) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args=_connect_args(settings, application_name),
    )


def _connect_args(settings: Settings, application_name: str) -> dict[str, Any]:
    # Set on the connection itself, so they hold for every statement on it however the
    # connection is used.
    return {
        "timeout": settings.db_connect_timeout_seconds,
        "server_settings": {
            "application_name": application_name,
            "timezone": "UTC",
            "statement_timeout": str(settings.db_statement_timeout_ms),
            "lock_timeout": str(settings.db_lock_timeout_ms),
            "idle_in_transaction_session_timeout": str(settings.db_idle_in_transaction_timeout_ms),
        },
    }


class Database:
    """The engine and the two ways to get a transaction from it.

    Entry points (an HTTP handler, an outbox handler, a scheduled job) open the transaction
    here and pass the session down. Nothing below an entry point commits.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """One transaction: committed when the block ends, rolled back if it raises."""
        async with self._sessions() as session, session.begin():
            yield session

    async def run[T](self, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
        """Run ``work`` in a transaction, re-running it if PostgreSQL reports a deadlock.

        ``work`` may run more than once, each time in a new transaction, so it must have no
        effect outside the database. The lock order makes a deadlock unexpected; this is the
        seatbelt, and the metric counts every time it is used.
        """
        attempt = 0
        while True:
            try:
                async with self.transaction() as session:
                    return await work(session)
            except DBAPIError as error:
                sqlstate = sqlstate_of(error)
                if sqlstate not in RETRYABLE or attempt >= MAX_RETRIES:
                    raise
                attempt += 1
                DB_TRANSACTION_RETRIES.labels(sqlstate=sqlstate).inc()
                # Jitter, so the transactions that collided do not collide again in step.
                await asyncio.sleep(random.uniform(0, 0.02 * 2**attempt))  # noqa: S311

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()


def lock_key(namespace: str, value: uuid.UUID | str) -> int:
    """A stable 64-bit advisory-lock key for ``value`` within ``namespace``."""
    digest = hashlib.sha256(f"{namespace}:{value}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def advisory_xact_lock(session: AsyncSession, keys: Iterable[int]) -> None:
    """Take transaction-scoped advisory locks, always in ascending key order.

    Taking them in one global order is what makes two transactions that need the same pair
    of locks queue instead of deadlocking.
    """
    for key in sorted(set(keys)):
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def try_advisory_xact_lock(session: AsyncSession, key: int) -> bool:
    """Take one transaction-scoped advisory lock if it is free. Never waits."""
    result = await session.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
    return bool(result.scalar_one())

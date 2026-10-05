"""Alembic environment.

Migrations run as the owner role, never as the application. The connection comes from
``CORRIDOR_DATABASE_OWNER_URL``, or from a caller that already holds one (the test harness
shares its connection through ``config.attributes``).

Revisions are written by hand, because they carry triggers and grants that cannot be
generated. Each reads the application role's name from ``config.attributes["app_role"]``.
"""

import asyncio
import importlib
import importlib.util
import pkgutil
import re

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import corridor
from corridor.platform.config import APP_ROLE_PATTERN, MigrationSettings
from corridor.platform.db import Base

config = context.config


def _settings() -> MigrationSettings:
    return MigrationSettings()


if "app_role" not in config.attributes:
    config.attributes["app_role"] = _settings().database_app_role
if re.fullmatch(APP_ROLE_PATTERN, config.attributes["app_role"]) is None:
    # The name is interpolated into GRANT statements, which cannot take a bound parameter.
    raise ValueError("the application role must be a plain lower-case identifier")


def _load_models() -> None:
    """Import every module's models so ``alembic revision --autogenerate`` can draft from them."""
    for module in pkgutil.iter_modules(corridor.__path__):
        if module.ispkg and importlib.util.find_spec(f"corridor.{module.name}.models"):
            importlib.import_module(f"corridor.{module.name}.models")


_load_models()
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render the SQL instead of running it: ``alembic upgrade head --sql``."""
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = _settings().database_owner_url
    if url is None:
        raise RuntimeError(
            "CORRIDOR_DATABASE_OWNER_URL is not set; migrations need the owner role."
        )
    connectable = create_async_engine(url.get_secret_value(), poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        asyncio.run(run_async_migrations())
    else:
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

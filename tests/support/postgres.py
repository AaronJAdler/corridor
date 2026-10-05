"""Test databases.

A test session builds, or reuses, a template database with every migration applied. Each
test clones it with ``CREATE DATABASE ... TEMPLATE`` and drops the clone afterwards, so no
test ever sees another test's rows and there is nothing to clean up between tests.

Tests connect as three roles, as production does:

- the **owner** role runs migrations and owns the schema;
- the **application** role is what the code under test connects as, with the same reduced
  privileges it has in production;
- the **admin** role (the one in ``CORRIDOR_TEST_POSTGRES_ADMIN_URL``) creates databases, and
  is used by a handful of tests to corrupt data on purpose.
"""

import asyncio
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "migrations"

ADMIN_URL_ENV = "CORRIDOR_TEST_POSTGRES_ADMIN_URL"
OWNER_ROLE = "corridor_test_owner"
APP_ROLE = "corridor_test_app"

# Bump when a change here should invalidate templates that already exist.
_HARNESS_VERSION = "1"
_TEMPLATE_PREFIX = "corridor_tpl_"
_TEST_PREFIX = "corridor_t_"
# Serialises template builds between test sessions that share a server.
_BUILD_LOCK = 0x636F7272  # "corr"
_STALE_TEST_DB_SECONDS = 3600
_STALE_TEMPLATE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class TestDatabase:
    __test__ = False  # not a test class, whatever its name suggests to pytest

    name: str
    admin_url: URL

    @property
    def app_url(self) -> str:
        return _render(self.admin_url, username=APP_ROLE, database=self.name)

    @property
    def owner_url(self) -> str:
        return _render(self.admin_url, username=OWNER_ROLE, database=self.name)

    @property
    def superuser_url(self) -> str:
        return _render(self.admin_url, database=self.name)


def admin_url() -> URL:
    raw = os.environ.get(ADMIN_URL_ENV)
    if not raw:
        raise RuntimeError(
            f"{ADMIN_URL_ENV} is not set. Tests need a real PostgreSQL: point it at a role "
            "that can create databases, for example postgresql://postgres@127.0.0.1:5432/postgres"
        )
    return make_url(raw)


def _render(url: URL, *, database: str, username: str | None = None) -> str:
    # The test roles reuse the admin password, so no credential of their own is kept here.
    changed = url.set(drivername="postgresql+asyncpg", database=database)
    if username is not None:
        changed = changed.set(username=username)
    return changed.render_as_string(hide_password=False)


def _asyncpg_dsn(url: URL, database: str | None = None) -> str:
    plain = url.set(drivername="postgresql")
    if database is not None:
        plain = plain.set(database=database)
    return plain.render_as_string(hide_password=False)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def template_name() -> str:
    """A name that changes whenever the migrations, or this harness, change."""
    digest = hashlib.sha256(_HARNESS_VERSION.encode())
    for path in sorted(p for p in MIGRATIONS.rglob("*") if p.is_file() and p.suffix != ".pyc"):
        digest.update(path.relative_to(MIGRATIONS).as_posix().encode())
        digest.update(path.read_bytes())
    return _TEMPLATE_PREFIX + digest.hexdigest()[:16]


def alembic_config(*, app_role: str = APP_ROLE) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["app_role"] = app_role
    return config


def _upgrade(connection: Connection, config: Config, revision: str) -> None:
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def _downgrade(connection: Connection, config: Config, revision: str) -> None:
    config.attributes["connection"] = connection
    command.downgrade(config, revision)


async def migrate(owner_url: str, revision: str = "head", *, down: bool = False) -> None:
    """Run migrations as the owner role, sharing one connection with Alembic."""
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_downgrade if down else _upgrade, alembic_config(), revision)
    finally:
        await engine.dispose()


async def _ensure_roles(connection: asyncpg.Connection, password: str | None) -> None:
    for role in (OWNER_ROLE, APP_ROLE):
        exists = await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        if not exists:
            await connection.execute(f"CREATE ROLE {_quote(role)} LOGIN")
        if password is not None:
            statement = await connection.fetchval(
                "SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)", role, password
            )
            await connection.execute(statement)


async def _drop_stale(connection: asyncpg.Connection, keep: str) -> None:
    """Remove databases left behind by sessions that were killed before they could tidy up."""
    now = time.time()
    rows = await connection.fetch(
        "SELECT d.datname AS name, shobj_description(d.oid, 'pg_database') AS comment "
        "FROM pg_database d WHERE d.datname LIKE $1 OR d.datname LIKE $2",
        _TEMPLATE_PREFIX + "%",
        _TEST_PREFIX + "%",
    )
    for row in rows:
        name: str = row["name"]
        if name == keep:
            continue
        try:
            created = float(row["comment"] or 0)
        except ValueError:
            created = 0.0
        limit = (
            _STALE_TEMPLATE_SECONDS if name.startswith(_TEMPLATE_PREFIX) else _STALE_TEST_DB_SECONDS
        )
        if now - created > limit:
            await connection.execute(f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)")


async def _stamp(connection: asyncpg.Connection, database: str) -> None:
    await connection.execute(f"COMMENT ON DATABASE {_quote(database)} IS '{time.time():.0f}'")


async def _ensure_template(url: URL) -> str:
    name = template_name()
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        await connection.execute("SELECT pg_advisory_lock($1)", _BUILD_LOCK)
        await _ensure_roles(connection, url.password)
        await _drop_stale(connection, keep=name)
        if await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            return name

        # Build under a temporary name and rename at the end, so a build that dies half way
        # never leaves something that looks like a finished template.
        building = f"{name}_building"
        await connection.execute(f"DROP DATABASE IF EXISTS {_quote(building)} WITH (FORCE)")
        await connection.execute(f"CREATE DATABASE {_quote(building)} OWNER {_quote(OWNER_ROLE)}")
        await migrate(_render(url, username=OWNER_ROLE, database=building))
        await connection.execute(f"ALTER DATABASE {_quote(building)} RENAME TO {_quote(name)}")
        await _stamp(connection, name)
        return name
    finally:
        await connection.close()


async def _create(url: URL, template: str) -> str:
    name = f"{_TEST_PREFIX}{secrets.token_hex(8)}"
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        await connection.execute(
            f"CREATE DATABASE {_quote(name)} TEMPLATE {_quote(template)} OWNER {_quote(OWNER_ROLE)}"
        )
        await _stamp(connection, name)
    finally:
        await connection.close()
    return name


async def _drop(url: URL, name: str) -> None:
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        await connection.execute(f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)")
    finally:
        await connection.close()


def ensure_template() -> str:
    return asyncio.run(_ensure_template(admin_url()))


def create_database(template: str) -> TestDatabase:
    url = admin_url()
    return TestDatabase(name=asyncio.run(_create(url, template)), admin_url=url)


def drop_database(database: TestDatabase) -> None:
    asyncio.run(_drop(database.admin_url, database.name))

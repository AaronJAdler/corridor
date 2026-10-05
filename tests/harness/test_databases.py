"""The test harness is load-bearing, so it has tests of its own."""

import asyncio

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, Table, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from corridor.platform.db import Database
from tests.support import postgres
from tests.support.models import all_metadata


def _head() -> str:
    head = ScriptDirectory.from_config(postgres.alembic_config()).get_current_head()
    assert head is not None
    return head


async def _execute(url: str, statement: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _table_names(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return set(rows.scalars())
    finally:
        await engine.dispose()


def _diff(session: AsyncSession, metadata: MetaData) -> list[object]:
    def compare(sync_session: object) -> list[object]:
        connection = sync_session.connection()  # type: ignore[attr-defined]
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return list(compare_metadata(context, metadata))

    return compare  # type: ignore[return-value]


async def test_a_test_database_is_migrated_to_head(owner_db: Database) -> None:
    async with owner_db.transaction() as session:
        version = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    assert version == _head()


def test_each_test_gets_a_database_of_its_own(database: postgres.TestDatabase) -> None:
    other = postgres.create_database(postgres.template_name())
    try:
        assert other.name != database.name
        asyncio.run(_execute(database.owner_url, "CREATE TABLE only_here (id int)"))

        assert "only_here" in asyncio.run(_table_names(database.owner_url))
        assert "only_here" not in asyncio.run(_table_names(other.owner_url))
    finally:
        postgres.drop_database(other)


async def test_tests_run_as_the_application_role_not_the_owner(
    db: Database, owner_db: Database
) -> None:
    async with db.transaction() as session:
        row = (
            await session.execute(
                text("SELECT current_user, usesuper FROM pg_user WHERE usename = current_user")
            )
        ).one()
    assert tuple(row) == (postgres.APP_ROLE, False)

    async with owner_db.transaction() as session:
        row = (
            await session.execute(
                text("SELECT current_user, usesuper FROM pg_user WHERE usename = current_user")
            )
        ).one()
    assert tuple(row) == (postgres.OWNER_ROLE, False)


async def test_the_application_role_gets_row_access_to_new_tables_and_nothing_more(
    db: Database, owner_db: Database
) -> None:
    async with owner_db.transaction() as session:
        await session.execute(text("CREATE TABLE harness_probe (id int PRIMARY KEY)"))

    async with db.transaction() as session:
        granted = (
            await session.execute(
                text(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_name = 'harness_probe' AND grantee = current_user"
                )
            )
        ).scalars()
        assert set(granted) == {"SELECT", "INSERT", "UPDATE", "DELETE"}


async def test_migrations_and_models_agree(owner_db: Database) -> None:
    async with owner_db.transaction() as session:
        differences = await session.run_sync(_diff(session, all_metadata()))
    assert differences == []


async def test_the_drift_check_notices_a_model_without_a_migration(owner_db: Database) -> None:
    drifted = MetaData()
    for table in all_metadata().tables.values():
        table.to_metadata(drifted)
    Table("not_migrated", drifted, Column("id", Integer, primary_key=True))

    async with owner_db.transaction() as session:
        differences = await session.run_sync(_diff(session, drifted))

    assert [(change[0], change[1].name) for change in differences] == [
        ("add_table", "not_migrated")
    ]  # type: ignore[index]


async def test_the_drift_check_notices_a_table_without_a_model(owner_db: Database) -> None:
    async with owner_db.transaction() as session:
        await session.execute(text("CREATE TABLE not_modelled (id int PRIMARY KEY)"))
        differences = await session.run_sync(_diff(session, all_metadata()))

    assert [(change[0], change[1].name) for change in differences] == [
        ("remove_table", "not_modelled")
    ]  # type: ignore[index]


def test_migrations_run_down_to_nothing_and_back_up(database: postgres.TestDatabase) -> None:
    before = asyncio.run(_table_names(database.owner_url))

    asyncio.run(postgres.migrate(database.owner_url, "base", down=True))
    assert asyncio.run(_table_names(database.owner_url)) == {"alembic_version"}

    asyncio.run(postgres.migrate(database.owner_url, "head"))
    assert asyncio.run(_table_names(database.owner_url)) == before


def test_the_template_name_follows_the_migrations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    original = postgres.template_name()
    assert original == postgres.template_name()

    copy = tmp_path / "migrations"  # type: ignore[operator]
    copy.mkdir()
    for path in postgres.MIGRATIONS.rglob("*.py"):
        target = copy / path.relative_to(postgres.MIGRATIONS)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    monkeypatch.setattr(postgres, "MIGRATIONS", copy)
    unchanged = postgres.template_name()

    (copy / "versions" / "9999_new.py").write_text("# a new revision\n")
    assert postgres.template_name() != unchanged

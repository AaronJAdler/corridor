"""The commands an operator runs from a shell: making the first administrator, and
generating a signing key that somebody else's process can read."""

import asyncio
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from corridor import cli, identity
from corridor.platform.clock import ManualClock
from corridor.platform.config import EnvironmentNotSet, Settings
from corridor.platform.db import Database
from tests.identity.support import add_user, close_account
from tests.payments.support import rows
from tests.support import postgres

EMAIL = "maria@example.com"
# Not a password hash: a value of the column's type that protects nothing.
PLACEHOLDER_HASH = "not-a-hash"  # pragma: allowlist secret


async def role_changes(db: Database) -> list[dict[str, Any]]:
    return await rows(
        db,
        "SELECT actor_type, actor_id, principal_id, resource_type, resource_id, details"
        " FROM audit_events WHERE action = 'user.role_changed'",
    )


# --- making an administrator -----------------------------------------------------------------


async def test_make_admin_gives_the_role_ends_the_users_tokens_and_is_audited(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    async with db.transaction() as session:
        maria = await add_user(session, "maria")
    assert settings.database_owner_url is not None

    outcome = await cli.make_admin(settings.database_owner_url.get_secret_value(), EMAIL)

    assert outcome is not None
    user, changed = outcome
    assert (user.id, user.role, changed) == (maria.id, "admin", True)
    (stored,) = await rows(db, "SELECT role, tokens_valid_after FROM users")
    assert stored["role"] == "admin"
    # A token that says "user" is not left to go on saying it.
    assert stored["tokens_valid_after"] > clock.now()
    (event,) = await role_changes(db)
    assert (event["actor_type"], event["actor_id"]) == ("system", "cli.users.make_admin")
    assert (event["principal_id"], event["resource_type"], event["resource_id"]) == (
        maria.id,
        "user",
        str(maria.id),
    )
    assert event["details"] == {"old_role": "user", "new_role": "admin"}


async def test_make_admin_changes_nothing_for_someone_who_is_one_already(
    db: Database, settings: Settings
) -> None:
    async with db.transaction() as session:
        await add_user(session, "maria", role="admin")
    assert settings.database_owner_url is not None

    outcome = await cli.make_admin(settings.database_owner_url.get_secret_value(), EMAIL)

    assert outcome is not None
    assert (outcome[0].role, outcome[1]) == ("admin", False)
    assert await role_changes(db) == []
    assert await rows(db, "SELECT tokens_valid_after FROM users") == [{"tokens_valid_after": None}]


@pytest.mark.parametrize("email", ["nobody@example.com", "maria", "@maria", ""])
async def test_make_admin_finds_an_account_by_its_email_address_and_by_nothing_else(
    db: Database, settings: Settings, email: str
) -> None:
    async with db.transaction() as session:
        await add_user(session, "maria")
    assert settings.database_owner_url is not None

    outcome = await cli.make_admin(settings.database_owner_url.get_secret_value(), email)

    assert outcome is None
    assert await rows(db, "SELECT role FROM users") == [{"role": "user"}]
    assert await role_changes(db) == []


async def test_make_admin_does_not_promote_a_closed_account(
    db: Database, settings: Settings
) -> None:
    async with db.transaction() as session:
        maria = await add_user(session, "maria")
    async with db.transaction() as session:
        await close_account(session, maria.id)
    assert settings.database_owner_url is not None

    outcome = await cli.make_admin(settings.database_owner_url.get_secret_value(), EMAIL)

    assert outcome is None
    assert await rows(db, "SELECT role FROM users") == [{"role": "user"}]


async def _roles(url: str) -> list[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return list((await connection.execute(text("SELECT role FROM users"))).scalars())
    finally:
        await engine.dispose()


async def _a_user(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, email, handle, display_name, password_hash, role,"
                    " kyc_tier, status, created_at, updated_at) VALUES (gen_random_uuid(),"
                    " :email, 'maria', 'Maria', :hash, 'user', 0, 'active', now(), now())"
                ),
                {"email": EMAIL, "hash": PLACEHOLDER_HASH},
            )
    finally:
        await engine.dispose()


def test_the_command_refuses_without_yes_and_changes_nothing(
    database: postgres.TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_a_user(database.owner_url))
    monkeypatch.setenv("CORRIDOR_DATABASE_OWNER_URL", database.owner_url)

    result = CliRunner().invoke(cli.app, ["users", "make-admin", "--email", EMAIL])

    assert result.exit_code == 1
    assert "--yes" in result.output
    assert asyncio.run(_roles(database.owner_url)) == ["user"]


def test_the_command_with_yes_makes_the_administrator(
    database: postgres.TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_a_user(database.owner_url))
    monkeypatch.setenv("CORRIDOR_DATABASE_OWNER_URL", database.owner_url)

    result = CliRunner().invoke(cli.app, ["users", "make-admin", "--email", EMAIL, "--yes"])
    again = CliRunner().invoke(cli.app, ["users", "make-admin", "--email", EMAIL, "--yes"])

    assert result.exit_code == 0, result.output
    assert "is now an administrator" in result.output
    assert asyncio.run(_roles(database.owner_url)) == ["admin"]
    assert again.exit_code == 0, again.output
    assert "already" in again.output


def test_the_command_says_so_when_there_is_no_such_account(
    database: postgres.TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORRIDOR_DATABASE_OWNER_URL", database.owner_url)

    result = CliRunner().invoke(cli.app, ["users", "make-admin", "--email", EMAIL, "--yes"])

    assert result.exit_code == 1
    assert "no open account" in result.output


def test_the_command_needs_the_owner_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CORRIDOR_DATABASE_OWNER_URL", raising=False)
    # Away from any .env of the working directory.
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.app, ["users", "make-admin", "--email", EMAIL, "--yes"])

    assert result.exit_code == 1
    assert "CORRIDOR_DATABASE_OWNER_URL" in result.output


# --- the mode of a generated key -------------------------------------------------------------

posix_only = pytest.mark.skipif(os.name != "posix", reason="permission bits are POSIX's")


def _generated_mode(directory: Path) -> int:
    (private,) = [path for path in directory.iterdir() if not path.name.endswith(".pub.pem")]
    return stat.S_IMODE(private.stat().st_mode)


@posix_only
def test_a_key_is_generated_for_its_owner_alone_unless_a_mode_is_given(tmp_path: Path) -> None:
    by_default = CliRunner().invoke(cli.app, ["keys", "generate", "--out", str(tmp_path / "a")])
    for_a_container = CliRunner().invoke(
        cli.app, ["keys", "generate", "--out", str(tmp_path / "b"), "--mode", "644"]
    )

    assert (by_default.exit_code, for_a_container.exit_code) == (0, 0)
    assert _generated_mode(tmp_path / "a") == 0o600
    assert _generated_mode(tmp_path / "b") == 0o644


@posix_only
def test_the_mode_asked_for_is_the_mode_given_whatever_the_umask(tmp_path: Path) -> None:
    previous = os.umask(0o077)
    try:
        identity.write_keypair(tmp_path, mode=0o640)
    finally:
        os.umask(previous)

    assert _generated_mode(tmp_path) == 0o640


@pytest.mark.parametrize("mode", ["666", "755", "200", "044", "rw", "1644", ""])
def test_a_mode_that_would_let_others_change_the_key_or_is_not_one_is_refused(
    tmp_path: Path, mode: str
) -> None:
    result = CliRunner().invoke(
        cli.app, ["keys", "generate", "--out", str(tmp_path / "keys"), "--mode", mode]
    )

    assert result.exit_code == 2
    assert not (tmp_path / "keys").exists()


# --- which settings a server command loads ---------------------------------------------------


@pytest.fixture
def bare_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """Only the two connection settings, and no .env file to read the rest from."""
    for name in list(os.environ):
        if name.startswith("CORRIDOR_") and not name.startswith("CORRIDOR_TEST_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CORRIDOR_DATABASE_URL", "postgresql+asyncpg://app@db.invalid/corridor")
    monkeypatch.setenv("CORRIDOR_REDIS_URL", "redis://cache.invalid:6379/0")
    return monkeypatch


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """What `corridor serve` and `corridor worker` would have run, instead of running it."""
    import uvicorn

    from corridor.worker import main as worker_main

    calls: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(("serve", kwargs)))
    monkeypatch.setattr(worker_main, "run", lambda settings: calls.append(("worker", settings)))
    return calls


@pytest.mark.parametrize("command", ["serve", "worker"])
def test_a_server_command_refuses_to_start_without_being_told_its_environment(
    bare_environment: pytest.MonkeyPatch, started: list[Any], command: str
) -> None:
    result = CliRunner().invoke(cli.app, [command])

    assert result.exit_code != 0
    assert isinstance(result.exception, EnvironmentNotSet)
    assert started == []


def test_the_worker_command_loads_the_workers_settings(
    bare_environment: pytest.MonkeyPatch, started: list[Any]
) -> None:
    bare_environment.setenv("CORRIDOR_ENVIRONMENT", "development")

    result = CliRunner().invoke(cli.app, ["worker"])

    assert result.exit_code == 0, result.output
    ((name, settings),) = started
    assert (name, settings.process_role, settings.environment) == (
        "worker",
        "worker",
        "development",
    )


def test_the_serve_command_checks_the_apis_settings_before_it_binds_a_port(
    bare_environment: pytest.MonkeyPatch, started: list[Any]
) -> None:
    # A production API without what a production API needs: refused before uvicorn runs.
    bare_environment.setenv("CORRIDOR_ENVIRONMENT", "production")

    refused = CliRunner().invoke(cli.app, ["serve"])

    assert refused.exit_code != 0
    assert "api_key_hash_key" in str(refused.exception)
    assert started == []
    bare_environment.setenv("CORRIDOR_ENVIRONMENT", "development")
    accepted = CliRunner().invoke(cli.app, ["serve"])
    assert accepted.exit_code == 0, accepted.output
    assert [name for name, _ in started] == ["serve"]


def test_the_app_built_without_settings_loads_the_apis_and_asks_for_the_environment(
    bare_environment: pytest.MonkeyPatch,
) -> None:
    from corridor.api.app import create_app

    # How uvicorn builds it for `corridor serve`: with nothing passed in.
    with pytest.raises(EnvironmentNotSet):
        create_app()
    bare_environment.setenv("CORRIDOR_ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="api_key_hash_key"):
        create_app()

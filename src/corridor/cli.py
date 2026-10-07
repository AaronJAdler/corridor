"""Command line: ``corridor serve``, ``corridor worker``, ``corridor db migrate``,
``corridor keys generate``, ``corridor users make-admin``, ``corridor verify-ledger`` and
``corridor demo``."""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from corridor import identity

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Corridor wallet backend.")
db_app = typer.Typer(no_args_is_help=True, help="Database administration.")
app.add_typer(db_app, name="db")
keys_app = typer.Typer(no_args_is_help=True, help="Signing keys for access tokens.")
app.add_typer(keys_app, name="keys")
users_app = typer.Typer(no_args_is_help=True, help="Accounts, from the operator's own shell.")
app.add_typer(users_app, name="users")

# What a private key file may be made readable to: its owner, and at the most everyone
# for reading. Nobody but the owner is ever given a way to change it.
_WIDEST_KEY_MODE = 0o644
_OWNER_READ = 0o400


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Restart on code changes (development).")] = False,
) -> None:
    """Run the HTTP API."""
    import uvicorn

    from corridor.platform.config import load_settings
    from corridor.platform.logging import configure_logging

    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format)
    uvicorn.run(
        "corridor.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        # Logging is configured above; uvicorn's own configuration and access log are off.
        log_config=None,
        access_log=False,
        server_header=False,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )


@db_app.command("migrate")
def db_migrate(
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "head",
    config: Annotated[Path, typer.Option(help="Path to alembic.ini.")] = Path("alembic.ini"),
) -> None:
    """Apply migrations as the owner role (CORRIDOR_DATABASE_OWNER_URL)."""
    from alembic import command
    from alembic.config import Config

    if not config.is_file():
        raise typer.BadParameter(f"{config} not found; run from the project root or pass --config.")
    command.upgrade(Config(str(config)), revision)
    typer.echo(f"Database is at {revision}.")


@keys_app.command("generate")
def keys_generate(
    out: Annotated[Path, typer.Option(help="Directory to write the key pair into.")],
    mode: Annotated[
        str,
        typer.Option(
            help="Permission bits of the private key, in octal. 644 lets a container that"
            " runs as another user read a key mounted from this machine."
        ),
    ] = "600",
) -> None:
    """Generate a signing key and print the settings that use it.

    The first line makes an instance sign with the new key. The second lets an instance
    verify the new key's tokens without holding it: set it on the instances that still sign
    with another key while this one is rolled out, and when this one is retired.
    """
    import json
    import shlex

    from corridor import identity

    kid, private_path = identity.write_keypair(out, mode=_key_mode(mode))
    public_pem = (out / f"{kid}.pub.pem").read_text(encoding="ascii")
    # Only the path of the private key is printed, never the key.
    typer.echo(f"CORRIDOR_JWT_SIGNING_KEY_FILE={shlex.quote(str(private_path))}")
    typer.echo(f"CORRIDOR_JWT_ADDITIONAL_PUBLIC_KEYS={shlex.quote(json.dumps([public_pem]))}")


def _key_mode(text: str) -> int:
    try:
        mode = int(text, 8)
    except ValueError:
        raise typer.BadParameter("give the mode in octal, such as 600 or 644.") from None
    if not mode & _OWNER_READ or mode & ~_WIDEST_KEY_MODE:
        raise typer.BadParameter(
            "a private key is readable by its owner, and at the most readable by others:"
            " 400, 600, 640 or 644."
        )
    return mode


@users_app.command("make-admin")
def users_make_admin(
    email: Annotated[str, typer.Option(help="The email address of a registered user.")],
    yes: Annotated[
        bool, typer.Option("--yes", help="Do it. Without this nothing is changed.")
    ] = False,
) -> None:
    """Make a registered user an administrator, as the owner role
    (CORRIDOR_DATABASE_OWNER_URL).

    This is how the first administrator is made: the API gives the role only at the word
    of someone who has it. It is written to the audit log, and the access tokens the user
    holds are ended, so they log in again to act as an administrator.
    """
    import asyncio

    from corridor.platform.config import MigrationSettings

    if not yes:
        typer.echo(
            f"This would make {email} an administrator. Nothing was changed: run it again"
            " with --yes.",
            err=True,
        )
        raise typer.Exit(code=1)
    owner_url = MigrationSettings().database_owner_url
    if owner_url is None:
        typer.echo("CORRIDOR_DATABASE_OWNER_URL is not set; this needs the owner role.", err=True)
        raise typer.Exit(code=1)

    outcome = asyncio.run(make_admin(owner_url.get_secret_value(), email))
    if outcome is None:
        typer.echo(f"There is no open account with the email address {email}.", err=True)
        raise typer.Exit(code=1)
    user, changed = outcome
    if changed:
        typer.echo(f"{user.email} ({user.id}) is now an administrator.")
    else:
        typer.echo(f"{user.email} ({user.id}) is an administrator already. Nothing was changed.")


async def make_admin(owner_url: str, email: str) -> tuple[identity.User, bool] | None:
    """Give the user with this email address the administrator's role, over a connection
    of the owner role. Returns the user and whether anything changed, or None if there is
    no such open account.

    The change and its audit event are one transaction. The actor is the system, by the
    name of this command: whoever ran it is known to the machine it ran on, not to
    Corridor.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from corridor import audit, identity
    from corridor.platform.db import Database

    # Bound values are left out of error messages, as they are for the application.
    db = Database(create_async_engine(owner_url, hide_parameters=True))
    try:
        async with db.transaction() as session:
            # Only an address is looked up: a handle or an id is not what was asked for.
            user = await identity.find_user(session, email) if "@" in email[1:] else None
            if user is None:
                return None
            if user.role == "admin":
                return user, False
            promoted = await identity.set_role(session, user.id, "admin")
            await audit.record(
                session,
                actor=audit.Actor.system("cli.users.make_admin"),
                action="user.role_changed",
                principal_id=user.id,
                resource_type="user",
                resource_id=user.id,
                details={"old_role": user.role, "new_role": promoted.role},
            )
            return promoted, True
    finally:
        await db.dispose()


def _report_ledger() -> None:
    """Recompute the ledger's invariants and say what was found. Exits 1 if any is violated."""
    import asyncio

    from sqlalchemy import text

    from corridor import ledger
    from corridor.platform.config import load_settings
    from corridor.platform.db import Database, create_engine

    async def run() -> list[ledger.Finding]:
        db = Database(create_engine(load_settings(), application_name="corridor-verify"))
        try:
            async with db.transaction() as session:
                # One snapshot for the whole report, and no time limit: this reads every
                # posting.
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                await session.execute(text("SET LOCAL statement_timeout = 0"))
                return await ledger.verify(session)
        finally:
            await db.dispose()

    findings = asyncio.run(run())
    for finding in findings:
        typer.echo(str(finding))
    if findings:
        typer.echo(f"Ledger verification FAILED: {len(findings)} finding(s).", err=True)
        raise typer.Exit(code=1)
    typer.echo("Ledger verification passed: no findings.")


@app.command("verify-ledger")
def verify_ledger() -> None:
    """Recompute the ledger's invariants. Exits 1 if any is violated."""
    _report_ledger()


@app.command()
def demo(  # pragma: no cover - a client of a running stack, run as a process of its own
    base_url: Annotated[str, typer.Option(help="Where the API listens.")] = "http://127.0.0.1:8000",
    sim_url: Annotated[
        str, typer.Option(help="Where the provider simulator listens.")
    ] = "http://127.0.0.1:8100",
    sim_control_token: Annotated[
        str,
        typer.Option(
            envvar="CORRIDOR_SIM_CONTROL_TOKEN",
            show_envvar=True,
            help="The simulator's control token, if it was started with one.",
        ),
    ] = "",
    verify: Annotated[
        bool,
        typer.Option(help="End by verifying the ledger (needs the stack's database settings)."),
    ] = True,
) -> None:
    """Walk a deposit, a conversion, a transfer and a withdrawal through a running stack.

    The stack must be running against the provider simulator: the demo plays the bank.
    """
    from corridor import demo as story

    typer.echo(f"Corridor demo: the API at {base_url}, the simulated providers at {sim_url}.\n")
    stack = story.Stack(base_url, sim_url, sim_control_token=sim_control_token or None)
    try:
        story.run(stack)
    except story.DemoError as refusal:
        typer.echo(f"The demo stopped: {refusal}.", err=True)
        raise typer.Exit(code=1) from None
    finally:
        stack.close()
    if verify:
        typer.echo("6. Every balance is recomputed from the ledger's postings.")
        _report_ledger()


@app.command()
def worker() -> None:
    """Run the worker: the outbox dispatcher and the scheduled jobs."""
    from corridor.platform.config import load_settings
    from corridor.worker.main import run

    run(load_settings())

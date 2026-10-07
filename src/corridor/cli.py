"""Command line: ``corridor serve``, ``corridor worker``, ``corridor db migrate``,
``corridor keys generate``, ``corridor verify-ledger`` and ``corridor demo``."""

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Corridor wallet backend.")
db_app = typer.Typer(no_args_is_help=True, help="Database administration.")
app.add_typer(db_app, name="db")
keys_app = typer.Typer(no_args_is_help=True, help="Signing keys for access tokens.")
app.add_typer(keys_app, name="keys")


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
) -> None:
    """Generate a signing key and print the settings that use it.

    The first line makes an instance sign with the new key. The second lets an instance
    verify the new key's tokens without holding it: set it on the instances that still sign
    with another key while this one is rolled out, and when this one is retired.
    """
    import json
    import shlex

    from corridor import identity

    kid, private_path = identity.write_keypair(out)
    public_pem = (out / f"{kid}.pub.pem").read_text(encoding="ascii")
    # Only the path of the private key is printed, never the key.
    typer.echo(f"CORRIDOR_JWT_SIGNING_KEY_FILE={shlex.quote(str(private_path))}")
    typer.echo(f"CORRIDOR_JWT_ADDITIONAL_PUBLIC_KEYS={shlex.quote(json.dumps([public_pem]))}")


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

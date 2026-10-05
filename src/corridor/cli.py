"""Command line: ``corridor serve``, ``corridor db migrate`` and, later, the rest."""

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Corridor wallet backend.")
db_app = typer.Typer(no_args_is_help=True, help="Database administration.")
app.add_typer(db_app, name="db")


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

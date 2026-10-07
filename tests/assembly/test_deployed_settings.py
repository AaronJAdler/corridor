"""What the Terraform gives each kind of task is enough for that task to start.

The environment of a task is read from ``infra/main.tf`` and its secrets from the identity
module, as text: there is no Terraform here to evaluate them, and none is needed to see
which variables a task kind is given. Each variable is then set to a value of the right
shape and the settings are loaded as the task's command loads them.
"""

import json
import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from corridor.platform.config import MigrationSettings, ProcessRole, load_settings

INFRA = Path(__file__).resolve().parents[2] / "infra"
MAIN = INFRA / "main.tf"
IDENTITY = INFRA / "modules" / "identity" / "main.tf"

KINDS = ("api", "worker", "migrate")
VARIABLE = re.compile(r"\bCORRIDOR_[A-Z0-9_]+\b")

# A value no real service would accept, long enough for every least length.
LONG = "placeholder-" + "0" * 36  # pragma: allowlist secret

# What each variable is set to. A literal in the Terraform is used as it stands; these are
# for the ones whose value is a variable, a module output or a secret set by hand. The
# connection strings are in the form infra/README.md tells the operator to store.
SHAPES = {
    "CORRIDOR_LOG_LEVEL": "INFO",
    "CORRIDOR_WEBHOOK_TOLERANCE_SECONDS": "300",
    "CORRIDOR_FORWARDED_ALLOW_IPS": "10.0.0.0/24,10.0.1.0/24",
    "CORRIDOR_BANK_RAIL_URL": "https://bank.invalid",
    "CORRIDOR_CUSTODY_URL": "https://custody.invalid",
    "CORRIDOR_FX_RATES_URL": "https://rates.invalid",
    "CORRIDOR_DATABASE_URL": f"postgresql+asyncpg://corridor_app:{LONG}@db.invalid:5432/corridor?ssl=require",
    "CORRIDOR_DATABASE_OWNER_URL": f"postgresql+asyncpg://corridor_owner:{LONG}@db.invalid:5432/corridor?ssl=require",
    "CORRIDOR_REDIS_URL": f"rediss://:{LONG}@cache.invalid:6379/0",
    "CORRIDOR_JWT_SIGNING_KEY": LONG,
    "CORRIDOR_API_KEY_HASH_KEY": LONG + "a",
    "CORRIDOR_FX_CACHE_MAC_KEY": LONG + "b",
    "CORRIDOR_BANK_RAIL_API_KEY": LONG + "c",
    "CORRIDOR_CUSTODY_API_KEY": LONG + "d",
    "CORRIDOR_FX_RATES_API_KEY": LONG + "e",
    "CORRIDOR_BANK_RAIL_WEBHOOK_SECRETS": json.dumps([LONG + "f"]),
    "CORRIDOR_CUSTODY_WEBHOOK_SECRETS": json.dumps([LONG + "g"]),
}


def _block(text: str, name: str) -> str:
    """The text of one ``<name> = ...`` local, up to the next blank line at its depth: the
    locals are written one after another with a blank line between them."""
    start = re.search(rf"^  {name}\s*=", text, flags=re.MULTILINE)
    assert start is not None, f"{name} is not defined in {MAIN.name}"
    rest = text[start.start() :]
    end = re.search(r"\n\n  (?=\S)|\n}\n", rest)
    assert end is not None
    return rest[: end.start()]


def _literals(block: str) -> dict[str, str | None]:
    """Each variable a block names, with its value where that is a quoted literal."""
    found: dict[str, str | None] = {}
    for name in VARIABLE.findall(block):
        literal = re.search(rf'\b{name}\s*=\s*"([^"$]*)"', block)
        found[name] = literal.group(1) if literal is not None else None
    return found


def plain_environment() -> dict[str, dict[str, str | None]]:
    """What each task kind's container is given as plain environment, from ``main.tf``."""
    text = MAIN.read_text(encoding="utf-8")
    common = _literals(_block(text, "common_environment"))
    environment: dict[str, dict[str, str | None]] = {}
    for kind in KINDS:
        block = _block(text, f"{kind}_environment")
        environment[kind] = {
            **(common if "local.common_environment" in block else {}),
            **_literals(block),
        }
    return environment


def secret_kinds() -> dict[str, tuple[str, ...]]:
    """Each secret's variable and the task kinds that receive it, from the identity module."""
    text = IDENTITY.read_text(encoding="utf-8")
    found = {
        entry.group("env"): tuple(re.findall(r'"([a-z]+)"', entry.group("kinds")))
        for entry in re.finditer(
            r'env\s*=\s*"(?P<env>CORRIDOR_[A-Z0-9_]+)"[^}]*?kinds\s*=\s*\[(?P<kinds>[^\]]*)\]',
            text,
        )
    }
    assert found, "no secret was found in the identity module"
    return found


def environment_of(kind: str) -> dict[str, str]:
    """Every variable a task of this kind starts with, each set to a value of its shape."""
    names: dict[str, str | None] = dict(plain_environment()[kind])
    names.update({name: None for name, kinds in secret_kinds().items() if kind in kinds})
    missing = sorted(
        name for name, literal in names.items() if literal is None and name not in SHAPES
    )
    assert not missing, f"give {missing} a value in SHAPES"
    return {
        name: literal if literal is not None else SHAPES[name] for name, literal in names.items()
    }


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """Nothing of this machine's configuration: no CORRIDOR_ variable and no .env file."""
    for name in list(os.environ):
        if name.startswith("CORRIDOR_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def test_the_terraform_is_read_as_it_is_written() -> None:
    environment = plain_environment()

    assert environment["api"]["CORRIDOR_ENVIRONMENT"] == "production"
    assert environment["worker"]["CORRIDOR_ENVIRONMENT"] == "production"
    assert "CORRIDOR_FORWARDED_ALLOW_IPS" in environment["api"]
    assert "CORRIDOR_FORWARDED_ALLOW_IPS" not in environment["worker"]
    assert set(environment["migrate"]) == {"CORRIDOR_DATABASE_APP_ROLE"}
    secrets = secret_kinds()
    assert secrets["CORRIDOR_DATABASE_OWNER_URL"] == ("migrate",)
    assert secrets["CORRIDOR_API_KEY_HASH_KEY"] == ("api",)
    assert set(secrets["CORRIDOR_DATABASE_URL"]) == {"api", "worker"}


def test_every_variable_the_terraform_names_reaches_a_task() -> None:
    """A variable added to the Terraform outside the blocks read here would be one this
    test knows nothing of."""
    named = set(VARIABLE.findall(MAIN.read_text(encoding="utf-8")))
    named |= set(VARIABLE.findall(IDENTITY.read_text(encoding="utf-8")))

    read = {name for kind in KINDS for name in environment_of(kind)}

    assert named == read


@pytest.mark.parametrize("role", ["api", "worker"])
def test_a_task_starts_in_production_with_what_the_terraform_gives_it(
    deployed: pytest.MonkeyPatch, role: ProcessRole
) -> None:
    for name, value in environment_of(role).items():
        deployed.setenv(name, value)

    settings = load_settings(role)

    assert (settings.environment, settings.process_role) == ("production", role)
    # Neither is given the connection that can change the schema.
    assert settings.database_owner_url is None


def test_the_worker_is_given_neither_a_webhook_secret_nor_the_apis_keys(
    deployed: pytest.MonkeyPatch,
) -> None:
    given = environment_of("worker")

    assert "CORRIDOR_API_KEY_HASH_KEY" not in given
    assert "CORRIDOR_BANK_RAIL_WEBHOOK_SECRETS" not in given
    assert "CORRIDOR_CUSTODY_WEBHOOK_SECRETS" not in given
    # Which is why its settings are not the API's: the API refuses to start with this.
    for name, value in given.items():
        deployed.setenv(name, value)
    with pytest.raises(ValidationError) as failure:
        load_settings("api")
    assert "api_key_hash_key" in str(failure.value)


def test_the_migration_task_starts_with_what_the_terraform_gives_it(
    deployed: pytest.MonkeyPatch,
) -> None:
    given = environment_of("migrate")
    for name, value in given.items():
        deployed.setenv(name, value)

    # `corridor db migrate` reads these, and not the application's settings.
    settings = MigrationSettings()

    assert set(given) == {"CORRIDOR_DATABASE_APP_ROLE", "CORRIDOR_DATABASE_OWNER_URL"}
    assert settings.database_owner_url is not None
    assert settings.database_app_role == "corridor_app"

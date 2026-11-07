"""Every caller depends on the provider ports and not on an adapter.

Outside ``corridor.providers``, no module of ``corridor`` may name an adapter or import the
module of one. The API and the worker get their adapters from ``build_*``, which chooses
by the ``*_provider`` settings, so that choosing a different provider changes no caller.
"""

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src" / "corridor"
ADAPTER_CLASSES = frozenset({"SimBank", "SimCustody", "SimRates", "IncreaseBank"})
ADAPTER_MODULES = frozenset(
    {
        "corridor.providers.bank_rail",
        "corridor.providers.custody",
        "corridor.providers.fx_rates",
        "corridor.providers.http",
        "corridor.providers.increase",
    }
)


def callers() -> list[Path]:
    return sorted(
        path
        for path in SOURCE.rglob("*.py")
        if "providers" not in path.relative_to(SOURCE).parts[:1]
    )


def tie_to_an_adapter(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in ADAPTER_MODULES:
                found.append(f"imports from {module}")
            found.extend(
                f"imports {alias.name}" for alias in node.names if alias.name in ADAPTER_CLASSES
            )
        elif isinstance(node, ast.Import):
            found.extend(
                f"imports {alias.name}" for alias in node.names if alias.name in ADAPTER_MODULES
            )
        elif isinstance(node, ast.Name) and node.id in ADAPTER_CLASSES:
            found.append(f"names {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in ADAPTER_CLASSES:
            found.append(f"names {node.attr}")
    return found


def test_the_scan_sees_the_callers_it_is_meant_to() -> None:
    names = {path.name for path in callers()}

    assert {"app.py", "main.py", "service.py", "withdrawals.py"} <= names
    assert not any("providers" in path.relative_to(SOURCE).parts[:1] for path in callers())


@pytest.mark.parametrize(
    "source",
    [
        "from corridor.providers import SimBank",
        "from corridor.providers.bank_rail import SimBank",
        "import corridor.providers.custody",
        "import corridor.providers as p\nx = p.SimRates",
        "bank = SimBank(settings)",
    ],
)
def test_the_scan_catches_each_way_of_tying_a_caller_to_an_adapter(source: str) -> None:
    assert tie_to_an_adapter(ast.parse(source))


def test_no_caller_is_tied_to_an_adapter() -> None:
    ties = {
        str(path.relative_to(SOURCE)): found
        for path in callers()
        if (found := tie_to_an_adapter(ast.parse(path.read_text(encoding="utf-8"))))
    }

    assert ties == {}

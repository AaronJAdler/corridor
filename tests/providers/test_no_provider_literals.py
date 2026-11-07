"""A provider's name is spelled out once, in ``corridor.providers.names``.

Anything else in ``corridor`` that needs it takes it from the adapter, the event's path or the
row, or imports the constant. A literal elsewhere would tie that module to one provider, as the
payments and webhooks modules once were."""

import ast
from pathlib import Path

from corridor.providers import INCREASE, SIM_BANK, SIM_CUSTODY, SIM_RATES

SOURCE = Path(__file__).resolve().parents[2] / "src" / "corridor"
NAMES = frozenset({SIM_BANK, SIM_CUSTODY, SIM_RATES, INCREASE})
HOME = SOURCE / "providers" / "names.py"


def spelled_out(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in NAMES
    ]


def test_no_provider_name_is_written_out_but_where_it_is_defined() -> None:
    found = {
        str(path.relative_to(SOURCE)): lines
        for path in sorted(SOURCE.rglob("*.py"))
        if path != HOME and (lines := spelled_out(path))
    }

    assert found == {}


def test_the_scan_finds_a_name_where_one_is_written() -> None:
    assert spelled_out(HOME) != []

"""The end-to-end test starts three processes and waits on real time, so it runs only when
it is asked for: ``uv run pytest -m e2e``. The marker is registered in ``pyproject.toml``."""

import pytest

MARKER = "e2e"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if MARKER in config.getoption("markexpr"):
        return
    skip = pytest.mark.skip(reason=f"end to end: run with -m {MARKER}, or uv run poe e2e")
    for item in items:
        if item.get_closest_marker(MARKER) is not None:
            item.add_marker(skip)

"""The end-to-end test starts three processes and waits on real time, so it runs only when
it is asked for: ``uv run pytest -m e2e``."""

import pytest

MARKER = "e2e"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", f"{MARKER}: starts the whole stack as real processes; run with -m {MARKER}"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if MARKER in config.getoption("markexpr"):
        return
    skip = pytest.mark.skip(reason=f"end to end: run with -m {MARKER}, or uv run poe e2e")
    for item in items:
        if item.get_closest_marker(MARKER) is not None:
            item.add_marker(skip)

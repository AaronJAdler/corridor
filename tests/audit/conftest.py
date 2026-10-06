"""Fixtures for the audit tests."""

from collections.abc import Iterator

import pytest

from corridor.platform.logging import clear_context


@pytest.fixture(autouse=True)
def clean_log_context() -> Iterator[None]:
    """Start and end every test with nothing bound to the log context.

    ``audit.record`` reads the request id from that context. A value bound by a synchronous
    test or fixture stays bound for the tests that follow, and would turn up in their events.
    """
    clear_context()
    yield
    clear_context()

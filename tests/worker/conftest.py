"""Fixtures for the worker tests."""

from collections.abc import Iterator

import pytest

from tests.outbox.helpers import LogReader, json_logs

# The simulated providers and the adapters wired to them, for the tests of what the worker
# is built from. Imported so that pytest finds them as fixtures of this directory.
from tests.support.providers import bank, custody, provider_settings, sim  # noqa: F401


@pytest.fixture
def logs(capsys: pytest.CaptureFixture[str]) -> Iterator[LogReader]:
    """What the code under test logs, as the parsed JSON lines an operator would read."""
    with json_logs(capsys) as read:
        yield read

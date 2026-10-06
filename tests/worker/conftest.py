"""Fixtures for the worker tests."""

from collections.abc import Iterator

import pytest

from tests.outbox.helpers import LogReader, json_logs


@pytest.fixture
def logs(capsys: pytest.CaptureFixture[str]) -> Iterator[LogReader]:
    """What the code under test logs, as the parsed JSON lines an operator would read."""
    with json_logs(capsys) as read:
        yield read

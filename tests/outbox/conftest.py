"""Fixtures for the outbox tests."""

from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest

from corridor import outbox
from tests.outbox.helpers import LogReader, NotifyProbe, json_logs
from tests.support import postgres


@pytest.fixture
async def probe(database: postgres.TestDatabase) -> AsyncIterator[NotifyProbe]:
    """A second connection, listening for what the outbox trigger announces."""
    dsn = database.app_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(dsn)
    try:
        listening = NotifyProbe(connection, outbox.NOTIFY_CHANNEL)
        await listening.start()
        yield listening
    finally:
        await connection.close()


@pytest.fixture
def logs(capsys: pytest.CaptureFixture[str]) -> Iterator[LogReader]:
    """What the code under test logs, as the parsed JSON lines an operator would read."""
    with json_logs(capsys) as read:
        yield read

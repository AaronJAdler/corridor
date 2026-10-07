"""Fixtures for payment tests."""

from collections.abc import AsyncIterator

import pytest

from corridor import payments
from corridor.identity import User
from corridor.platform.config import Settings
from corridor.platform.db import Database, create_engine
from tests.payments.support import add_person

# The simulated providers and the adapters wired to them, for the money-in and money-out
# tests. Imported so that pytest finds them as fixtures of this directory.
from tests.support.providers import bank, custody, provider_settings, sim  # noqa: F401


@pytest.fixture
async def maria(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "maria")


@pytest.fixture
async def joao(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "joao")


@pytest.fixture
def with_fee(settings: Settings) -> Settings:
    """The test settings with a 1% transfer fee."""
    return settings.model_copy(update={"transfer_fee_bps": 100})


@pytest.fixture
async def impatient_db(settings: Settings) -> AsyncIterator[Database]:
    """The same database through connections that give up on a lock after 100 ms: how a
    test shows that an entry point which opens its own transactions waits for a lock."""
    impatient = Database(
        create_engine(
            settings.model_copy(update={"db_lock_timeout_ms": 100}),
            application_name="corridor-test-impatient",
        )
    )
    try:
        yield impatient
    finally:
        await impatient.dispose()


@pytest.fixture
async def db(db: Database, request: pytest.FixtureRequest) -> AsyncIterator[Database]:
    """The suite's database, with one more look at it when a payments test ends: the
    payments verifier runs against what the test left behind, as the ledger's does for
    every test. Whatever path the test took, every held balance is some withdrawal's and
    everything in suspense is some deposit's."""
    yield db
    if request.node.get_closest_marker("corrupts_ledger") is not None:
        return
    if "written_by_hand" in request.fixturenames:
        return
    async with db.transaction() as session:
        findings = await payments.verify(session)
    if findings:
        pytest.fail("the payments verifier found:\n" + "\n".join(map(str, findings)), pytrace=False)


@pytest.fixture
def written_by_hand() -> None:
    """Asked for by a test that writes withdrawals or deposits with plain SQL, or moves a
    held or suspense balance without one: what it leaves behind is not what the payments
    verifier expects of the service, so that verifier is not run after it. The ledger's
    still is."""

"""The providers a webhook may come from are written down twice, in the ``Provider``
enumeration and in the table's check constraint. The two must say the same."""

import re

from sqlalchemy import text

from corridor.platform.db import Database
from corridor.providers import BANK_RAILS, CUSTODIANS
from corridor.webhooks import Provider


async def allowed_by_the_table(db: Database) -> set[str]:
    async with db.transaction() as session:
        definition = (
            await session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conname = 'ck_webhook_events_provider'"
                )
            )
        ).scalar_one()
    return set(re.findall(r"'([^']+)'", definition))


async def test_the_table_allows_exactly_the_providers_the_enumeration_names(db: Database) -> None:
    assert await allowed_by_the_table(db) == {provider.value for provider in Provider}


def test_every_provider_a_webhook_may_come_from_is_a_known_kind_of_provider() -> None:
    assert {provider.value for provider in Provider} <= BANK_RAILS | CUSTODIANS

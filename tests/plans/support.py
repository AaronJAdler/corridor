"""Query plans as tests.

A hot path is run as the application runs it, every statement it issues is captured at the
driver, and each captured statement is handed back to PostgreSQL under ``EXPLAIN``. The test
then reads the plan: which relations are scanned, how, and through which index. The
statements under test are the ones the code issues, not copies kept in the test, so a change
to a service function changes what is explained.

Plans are read, never timed. A planner choice depends on statistics, so the fixture seeds
enough rows for the planner to have a choice and runs ``ANALYZE`` first; on an empty table a
sequential scan is the right plan and says nothing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.platform.db import Database


@dataclass(frozen=True)
class Captured:
    """One statement as the driver saw it: asyncpg's ``$n`` placeholders and its parameters."""

    sql: str
    parameters: tuple[Any, ...]

    def mentions(self, relation: str) -> bool:
        return relation in self.sql


# A Bitmap Heap Scan reads the relation; the BitmapOr/BitmapAnd and Bitmap Index Scan nodes
# below it read its indexes and name no relation of their own.
_BITMAP_NODES = frozenset({"Bitmap Heap Scan", "BitmapOr", "BitmapAnd"})


@dataclass
class Plan:
    """An ``EXPLAIN (FORMAT JSON)`` result, with the questions a test asks of it."""

    statement: Captured
    root: dict[str, Any]

    def nodes(self) -> Iterator[dict[str, Any]]:
        for node, _ in self._walk():
            yield node

    def _walk(self) -> Iterator[tuple[dict[str, Any], str | None]]:
        """Every node with the relation it reads. A ``Bitmap Index Scan`` names no relation
        itself; it reads for the nearest ``Bitmap Heap Scan`` above it, so it is given that
        one's relation."""
        stack: list[tuple[dict[str, Any], str | None]] = [(self.root, None)]
        while stack:
            node, inherited = stack.pop()
            relation = node.get("Relation Name") or inherited
            yield node, relation
            carried = relation if node["Node Type"] in _BITMAP_NODES else None
            stack.extend((child, carried) for child in node.get("Plans", []))

    def scans_of(self, relation: str) -> list[dict[str, Any]]:
        return [n for n, r in self._walk() if r == relation and "Scan" in n["Node Type"]]

    def seq_scans_of(self, relation: str) -> list[dict[str, Any]]:
        return [n for n in self.scans_of(relation) if n["Node Type"] == "Seq Scan"]

    def indexes_used(self, relation: str) -> set[str]:
        return {n["Index Name"] for n in self.scans_of(relation) if "Index Name" in n}

    def __str__(self) -> str:
        return json.dumps(self.root, indent=2)


@dataclass
class Recorder:
    statements: list[Captured] = field(default_factory=list)

    def __call__(
        self, conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        params = parameters if isinstance(parameters, tuple) else tuple(parameters or ())
        self.statements.append(Captured(statement, params))

    def about(self, relation: str) -> list[Captured]:
        return [s for s in self.statements if s.mentions(relation)]


@asynccontextmanager
async def recording(database: Database) -> AsyncIterator[Recorder]:
    """Capture every statement the database issues while the block runs."""
    recorder = Recorder()
    sync_engine = database.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", recorder)
    try:
        yield recorder
    finally:
        event.remove(sync_engine, "before_cursor_execute", recorder)


async def explain(session: AsyncSession, statement: Captured) -> Plan:
    """Ask PostgreSQL how it would run the statement, with the parameters it was run with.

    ``EXPLAIN`` without ``ANALYZE`` plans and does not execute, so an ``UPDATE`` or a
    ``FOR UPDATE`` is safe to explain. The raw asyncpg connection is used because the captured
    statement is already in asyncpg's form.
    """
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    driver = raw.driver_connection
    rows = await driver.fetch(f"EXPLAIN (FORMAT JSON) {statement.sql}", *statement.parameters)
    document = rows[0][0]
    if isinstance(document, str):
        document = json.loads(document)
    return Plan(statement, document[0]["Plan"])


async def explained(
    database: Database,
    relation: str,
    run: Callable[[AsyncSession], Awaitable[Any]],
) -> list[Plan]:
    """Run ``run`` inside a transaction, then explain every statement it issued about
    ``relation``, in the same transaction so the plans see the same statistics and settings."""
    async with database.transaction() as session:
        async with recording(database) as recorder:
            await run(session)
        statements = recorder.about(relation)
        assert statements, f"the path issued no statement about {relation}"
        return [await explain(session, s) for s in statements]

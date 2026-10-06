"""The audit log: what the service records and reads, and what the database refuses."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import MetaData, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit
from corridor.audit import Actor, AuditEvent, Outcome
from corridor.platform.clock import ManualClock, use_clock
from corridor.platform.db import (
    CHECK_VIOLATION,
    NAMING_CONVENTION,
    UNIQUE_VIOLATION,
    Database,
    constraint_of,
    sqlstate_of,
)
from corridor.platform.ids import new_id
from corridor.platform.logging import REDACTED, bind_context
from tests.support import postgres
from tests.support.models import all_metadata

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
APPEND_ONLY = "CR001"
INSUFFICIENT_PRIVILEGE = "42501"
NOT_NULL_VIOLATION = "23502"

USER = uuid.UUID("0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10")
AGENT = uuid.UUID("0199b7c2-7a31-7c44-8e02-5b9d3f6a1c27")
SWEEPER = Actor.system("payout_sweeper")

# Values for sensitive-looking places in these tests. They protect nothing.
NOT_A_SECRET = "hunter2-value"  # pragma: allowlist secret
BEARER = "Bearer abcdefghijklmnop"  # pragma: allowlist secret

# Every way SQL has of changing or removing a row that is already there.
EDITS = [
    pytest.param("UPDATE audit_events SET outcome = 'failed'", id="update"),
    pytest.param("DELETE FROM audit_events", id="delete"),
    pytest.param("TRUNCATE audit_events", id="truncate"),
    pytest.param(
        "INSERT INTO audit_events SELECT * FROM audit_events"
        " ON CONFLICT (id) DO UPDATE SET outcome = 'failed'",
        id="upsert",
    ),
    pytest.param(
        "MERGE INTO audit_events AS stored USING audit_events AS again ON stored.id = again.id"
        " WHEN MATCHED THEN UPDATE SET outcome = 'failed'",
        id="merge",
    ),
]

# Names that are not dotted lower-case segments. The service and the database must both
# refuse every one of them.
MALFORMED_ACTIONS = [
    pytest.param("", id="empty"),
    pytest.param("transfer", id="one-segment"),
    pytest.param("transfer.", id="trailing-dot"),
    pytest.param(".created", id="leading-dot"),
    pytest.param("transfer..created", id="empty-segment"),
    pytest.param("Transfer.created", id="upper-case-first-segment"),
    pytest.param("transfer.Created", id="upper-case-last-segment"),
    pytest.param("1transfer.created", id="first-segment-starts-with-a-digit"),
    pytest.param("transfer.1created", id="last-segment-starts-with-a-digit"),
    pytest.param("transfer created", id="space"),
    pytest.param("transfer-created.done", id="hyphen"),
    pytest.param("transfer.created ", id="trailing-space"),
    # A pattern anchored with "$" alone lets this through in Python.
    pytest.param("transfer.created\n", id="trailing-newline"),
    pytest.param("transf\u00e9r.created", id="non-ascii-letter"),
]


async def add_event(
    session: AsyncSession, *, omit: str | None = None, **values: object
) -> uuid.UUID:
    """Insert a row with plain SQL, the way a buggy or hostile caller would.

    Only the columns the table requires are given, unless ``values`` adds more or ``omit``
    leaves one out.
    """
    event = new_id()
    row: dict[str, object] = {
        "id": event,
        "occurred_at": NOW,
        "actor_type": "system",
        "action": "test.happened",
        "outcome": "success",
        "details": "{}",
    } | values
    if omit is not None:
        del row[omit]
    columns = ", ".join(row)
    placeholders = ", ".join(
        "CAST(:details AS jsonb)" if column == "details" else f":{column}" for column in row
    )
    await session.execute(
        text(f"INSERT INTO audit_events ({columns}) VALUES ({placeholders})"),  # noqa: S608
        row,
    )
    return event


async def stored_rows(db: Database) -> list[tuple[uuid.UUID, str]]:
    """Every row's id and outcome, read with plain SQL."""
    async with db.transaction() as session:
        rows = await session.execute(text("SELECT id, outcome FROM audit_events ORDER BY id"))
        return [(row.id, row.outcome) for row in rows]


async def stored_details(session: AsyncSession) -> str:
    """The details of the only event, as the text PostgreSQL holds."""
    stored = await session.execute(text("SELECT CAST(details AS text) FROM audit_events"))
    return str(stored.scalar_one())


def ids(events: Sequence[AuditEvent]) -> list[uuid.UUID]:
    return [event.id for event in events]


# --- recording -------------------------------------------------------------------------------


def test_each_kind_of_actor_has_a_constructor() -> None:
    assert Actor.user(USER) == Actor("user", str(USER))
    assert Actor.agent(AGENT) == Actor("agent", str(AGENT))
    assert Actor.admin(USER) == Actor("admin", str(USER))
    assert Actor.system("payout_sweeper") == Actor("system", "payout_sweeper")
    assert Actor.provider("simbank") == Actor("provider", "simbank")


async def test_an_event_round_trips_with_every_field(db: Database, clock: ManualClock) -> None:
    owner, transfer = new_id(), new_id()
    details = {
        "amount": "12.50",
        "asset": "USD",
        "limit": {"per_transaction": 1000, "remaining": [250, 0]},
        "approved": False,
        "note": None,
    }

    async with db.transaction() as session:
        event_id = await audit.record(
            session,
            actor=Actor.agent(AGENT),
            action="transfer.created",
            outcome="denied",
            principal_id=owner,
            resource_type="transfer",
            resource_id=transfer,
            details=details,
            request_id="req-7f3a9c21",
        )

    async with db.transaction() as session:
        events = await audit.list_events(session)
        row = (await session.execute(text("SELECT * FROM audit_events"))).mappings().one()

    expected = {
        "id": event_id,
        "occurred_at": clock.now(),
        "actor_type": "agent",
        "actor_id": str(AGENT),
        "principal_id": owner,
        "action": "transfer.created",
        "resource_type": "transfer",
        "resource_id": str(transfer),
        "outcome": "denied",
        "request_id": "req-7f3a9c21",
        "details": details,
    }
    assert events == [AuditEvent(**expected)]
    # Read with plain SQL as well, so that a column the service wrote and read back under
    # the wrong name would not pass unnoticed.
    assert dict(row) == expected


async def test_an_event_needs_only_an_actor_and_an_action(db: Database, clock: ManualClock) -> None:
    async with db.transaction() as session:
        event_id = await audit.record(session, actor=SWEEPER, action="payout.swept")
        events = await audit.list_events(session)

    assert events == [
        AuditEvent(
            id=event_id,
            occurred_at=clock.now(),
            actor_type="system",
            actor_id="payout_sweeper",
            principal_id=None,
            action="payout.swept",
            resource_type=None,
            resource_id=None,
            outcome="success",
            request_id=None,
            details={},
        )
    ]


@pytest.mark.parametrize(
    "actor",
    [
        Actor.user(USER),
        Actor.agent(AGENT),
        Actor.admin(USER),
        Actor.system("payout_sweeper"),
        Actor.provider("simbank"),
        # Someone the system could not identify: a login for an unknown email, say.
        Actor("user", None),
    ],
    ids=["user", "agent", "admin", "system", "provider", "unidentified-user"],
)
async def test_every_kind_of_actor_is_recorded_as_given(db: Database, actor: Actor) -> None:
    async with db.transaction() as session:
        await audit.record(session, actor=actor, action="test.happened")
        (event,) = await audit.list_events(session)

    assert (event.actor_type, event.actor_id) == (actor.type, actor.id)


@pytest.mark.parametrize("outcome", ["success", "denied", "failed"])
async def test_every_outcome_is_recorded_as_given(db: Database, outcome: Outcome) -> None:
    async with db.transaction() as session:
        await audit.record(session, actor=SWEEPER, action="test.happened", outcome=outcome)
        (event,) = await audit.list_events(session)

    assert event.outcome == outcome


async def test_an_event_is_stamped_by_the_application_clock(
    db: Database, clock: ManualClock
) -> None:
    started = clock.now()

    async with db.transaction() as session:
        first = await audit.record(session, actor=SWEEPER, action="test.happened")
        clock.advance(minutes=5)
        second = await audit.record(session, actor=SWEEPER, action="test.happened")
        occurred_at = {event.id: event.occurred_at for event in await audit.list_events(session)}

    assert occurred_at == {first: started, second: started + timedelta(minutes=5)}


# --- the request id --------------------------------------------------------------------------


async def test_the_request_id_is_taken_from_the_log_context(db: Database) -> None:
    # What the request middleware does at the start of every request.
    bind_context(request_id="req-from-context")

    async with db.transaction() as session:
        await audit.record(session, actor=SWEEPER, action="test.happened")
        (event,) = await audit.list_events(session)

    assert event.request_id == "req-from-context"


async def test_an_explicit_request_id_wins_over_the_log_context(db: Database) -> None:
    bind_context(request_id="req-from-context")

    async with db.transaction() as session:
        await audit.record(
            session, actor=SWEEPER, action="test.happened", request_id="req-explicit"
        )
        (event,) = await audit.list_events(session)

    assert event.request_id == "req-explicit"


async def test_a_request_id_bound_as_something_other_than_text_is_stored_as_text(
    db: Database,
) -> None:
    request_id = new_id()
    bind_context(request_id=request_id)

    async with db.transaction() as session:
        await audit.record(session, actor=SWEEPER, action="test.happened")
        (event,) = await audit.list_events(session)

    assert event.request_id == str(request_id)


async def test_an_event_recorded_outside_a_request_has_no_request_id(db: Database) -> None:
    # A scheduled job binds other things to the log context, and no request id.
    bind_context(job="payout_sweeper")

    async with db.transaction() as session:
        await audit.record(session, actor=SWEEPER, action="test.happened")
        (event,) = await audit.list_events(session)

    assert event.request_id is None


# --- the caller's transaction ----------------------------------------------------------------


async def test_an_event_is_rolled_back_with_the_callers_transaction(db: Database) -> None:
    with pytest.raises(RuntimeError, match="later step failed"):
        async with db.transaction() as session:
            await audit.record(session, actor=SWEEPER, action="test.happened")
            assert len(await audit.list_events(session)) == 1
            raise RuntimeError("later step failed")

    async with db.transaction() as session:
        assert await audit.list_events(session) == []


async def test_an_event_is_committed_with_the_callers_transaction(db: Database) -> None:
    async with db.transaction() as session:
        event_id = await audit.record(session, actor=SWEEPER, action="test.happened")
        # Recording does not commit: until the caller does, no other transaction sees it.
        async with db.transaction() as other:
            assert await audit.list_events(other) == []

    async with db.transaction() as session:
        assert ids(await audit.list_events(session)) == [event_id]


# --- secrets ---------------------------------------------------------------------------------


async def test_a_secret_in_details_is_stored_redacted(db: Database) -> None:
    async with db.transaction() as session:
        await audit.record(
            session,
            actor=Actor("user", None),
            action="auth.login_failed",
            outcome="failed",
            details={
                "email": "maria@example.com",
                # Removed for the name of its key.
                "password": NOT_A_SECRET,
                # Removed for the shape of its value, whatever the key.
                "error": f"upstream refused {BEARER}",
            },
        )
        (event,) = await audit.list_events(session)
        stored = await stored_details(session)

    assert event.details == {
        "email": "maria@example.com",
        "password": REDACTED,
        "error": f"upstream refused {REDACTED}",
    }
    assert NOT_A_SECRET not in stored
    assert BEARER not in stored


async def test_secrets_nested_inside_details_are_redacted_too(db: Database) -> None:
    async with db.transaction() as session:
        await audit.record(
            session,
            actor=Actor.provider("simbank"),
            action="webhook.rejected",
            outcome="denied",
            details={
                "request": {
                    "headers": {"authorization": NOT_A_SECRET, "accept": "application/json"},
                    "attempts": [
                        {"refresh_token": NOT_A_SECRET, "number": 1},
                        f"retried with {BEARER}",
                    ],
                }
            },
        )
        (event,) = await audit.list_events(session)
        stored = await stored_details(session)

    assert event.details == {
        "request": {
            "headers": {"authorization": REDACTED, "accept": "application/json"},
            "attempts": [{"refresh_token": REDACTED, "number": 1}, f"retried with {REDACTED}"],
        }
    }
    assert NOT_A_SECRET not in stored
    assert BEARER not in stored


# --- action names ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    ["transfer.created", "auth.login_failed", "ops.adjustment.approved", "a.b", "kyc2.tier_2_set"],
)
async def test_a_dotted_lower_case_action_is_recorded(db: Database, action: str) -> None:
    async with db.transaction() as session:
        await audit.record(session, actor=SWEEPER, action=action)
        (event,) = await audit.list_events(session)

    assert event.action == action


@pytest.mark.parametrize("action", MALFORMED_ACTIONS)
async def test_a_malformed_action_is_refused_before_anything_is_written(
    db: Database, action: str
) -> None:
    async with db.transaction() as session:
        with pytest.raises(ValueError, match="not an audit action"):
            await audit.record(session, actor=SWEEPER, action=action)
        # Refused before any SQL ran, so the caller's transaction is still usable. Had the
        # database been the one to refuse, this next statement would fail too.
        kept = await audit.record(session, actor=SWEEPER, action="test.happened")

    async with db.transaction() as session:
        assert ids(await audit.list_events(session)) == [kept]


# --- reading ---------------------------------------------------------------------------------


async def test_events_are_listed_newest_first_by_id_whatever_their_clocks_said(
    db: Database,
) -> None:
    async with db.transaction() as session:
        with use_clock(ManualClock(NOW)):
            first = await audit.record(session, actor=SWEEPER, action="test.happened")
        # Written later by a host whose clock runs an hour behind.
        with use_clock(ManualClock(NOW - timedelta(hours=1))):
            second = await audit.record(session, actor=SWEEPER, action="test.happened")

        events = await audit.list_events(session)

    assert ids(events) == [second, first]
    assert events[0].occurred_at < events[1].occurred_at


async def test_events_are_filtered_by_principal(db: Database) -> None:
    maria, joao = new_id(), new_id()

    async with db.transaction() as session:
        first = await audit.record(
            session, actor=Actor.user(maria), action="transfer.created", principal_id=maria
        )
        await audit.record(
            session, actor=Actor.user(joao), action="transfer.created", principal_id=joao
        )
        await audit.record(session, actor=SWEEPER, action="payout.swept")
        last = await audit.record(
            session, actor=Actor.agent(AGENT), action="transfer.created", principal_id=maria
        )

        listed = await audit.list_events(session, principal_id=maria)

    assert ids(listed) == [last, first]


async def test_events_are_filtered_by_resource(db: Database) -> None:
    transfer, another = new_id(), new_id()

    async with db.transaction() as session:
        created = await audit.record(
            session,
            actor=SWEEPER,
            action="transfer.created",
            resource_type="transfer",
            resource_id=transfer,
        )
        other_transfer = await audit.record(
            session,
            actor=SWEEPER,
            action="transfer.created",
            resource_type="transfer",
            resource_id=another,
        )
        # The same id under another type is another resource.
        withdrawal = await audit.record(
            session,
            actor=SWEEPER,
            action="withdrawal.requested",
            resource_type="withdrawal",
            resource_id=transfer,
        )
        await audit.record(session, actor=SWEEPER, action="payout.swept")
        # A resource id is text in the log, so the id may be given either way.
        reversed_ = await audit.record(
            session,
            actor=SWEEPER,
            action="transfer.reversed",
            resource_type="transfer",
            resource_id=str(transfer),
        )

        one_resource = await audit.list_events(
            session, resource_type="transfer", resource_id=transfer
        )
        same_as_text = await audit.list_events(
            session, resource_type="transfer", resource_id=str(transfer)
        )
        one_type = await audit.list_events(session, resource_type="transfer")
        one_id = await audit.list_events(session, resource_id=transfer)

    assert ids(one_resource) == [reversed_, created]
    assert same_as_text == one_resource
    assert ids(one_type) == [reversed_, other_transfer, created]
    assert ids(one_id) == [reversed_, withdrawal, created]


async def test_events_are_filtered_by_action(db: Database) -> None:
    async with db.transaction() as session:
        first = await audit.record(session, actor=SWEEPER, action="auth.login_failed")
        await audit.record(session, actor=SWEEPER, action="auth.login")
        await audit.record(session, actor=SWEEPER, action="auth.login_failed.locked")
        last = await audit.record(session, actor=SWEEPER, action="auth.login_failed")

        listed = await audit.list_events(session, action="auth.login_failed")

    assert ids(listed) == [last, first]


async def test_an_event_must_match_every_filter_given(db: Database) -> None:
    maria, joao, transfer = new_id(), new_id(), new_id()

    async def add(
        session: AsyncSession, principal: uuid.UUID, action: str, resource: uuid.UUID
    ) -> uuid.UUID:
        return await audit.record(
            session,
            actor=Actor.user(principal),
            action=action,
            principal_id=principal,
            resource_type="transfer",
            resource_id=resource,
        )

    async with db.transaction() as session:
        wanted = await add(session, maria, "transfer.created", transfer)
        # Each of these misses exactly one of the filters.
        await add(session, joao, "transfer.created", transfer)
        await add(session, maria, "transfer.reversed", transfer)
        await add(session, maria, "transfer.created", new_id())

        listed = await audit.list_events(
            session,
            principal_id=maria,
            action="transfer.created",
            resource_type="transfer",
            resource_id=transfer,
        )

    assert ids(listed) == [wanted]


async def test_three_pages_by_cursor_hold_every_event_exactly_once(db: Database) -> None:
    async with db.transaction() as session:
        recorded = [
            await audit.record(session, actor=SWEEPER, action="test.happened") for _ in range(7)
        ]

        first = await audit.list_events(session, limit=3)
        second = await audit.list_events(session, before=first[-1].id, limit=3)
        third = await audit.list_events(session, before=second[-1].id, limit=3)
        beyond = await audit.list_events(session, before=third[-1].id, limit=3)

    assert [len(page) for page in (first, second, third, beyond)] == [3, 3, 1, 0]
    # Newest first with no event repeated and none skipped.
    assert ids(first + second + third) == recorded[::-1]


async def test_paging_stays_within_the_filters(db: Database) -> None:
    maria = new_id()

    async with db.transaction() as session:
        hers = []
        for _ in range(5):
            hers.append(
                await audit.record(
                    session, actor=Actor.user(maria), action="test.happened", principal_id=maria
                )
            )
            # Someone else's event in between, which no page of hers may hold.
            await audit.record(session, actor=SWEEPER, action="test.happened")

        first = await audit.list_events(session, principal_id=maria, limit=2)
        second = await audit.list_events(session, principal_id=maria, before=first[-1].id, limit=2)
        third = await audit.list_events(session, principal_id=maria, before=second[-1].id, limit=2)

    assert ids(first + second + third) == hers[::-1]


async def test_a_page_holds_50_events_unless_asked_otherwise(db: Database) -> None:
    async with db.transaction() as session:
        for _ in range(51):
            await audit.record(session, actor=SWEEPER, action="test.happened")

        assert len(await audit.list_events(session)) == 50
        assert len(await audit.list_events(session, limit=51)) == 51


async def test_a_page_never_holds_more_than_200_events(db: Database) -> None:
    async with db.transaction() as session:
        for _ in range(201):
            await audit.record(session, actor=SWEEPER, action="test.happened")

        assert len(await audit.list_events(session, limit=201)) == 200
        assert len(await audit.list_events(session, limit=10_000)) == 200


@pytest.mark.parametrize("limit", [0, -1])
async def test_a_page_of_no_events_is_refused_before_any_query(db: Database, limit: int) -> None:
    async with db.transaction() as session:
        event_id = await audit.record(session, actor=SWEEPER, action="test.happened")

        with pytest.raises(ValueError, match="at least one event"):
            await audit.list_events(session, limit=limit)
        # Refused before any SQL ran, so the caller's transaction is still usable.
        assert ids(await audit.list_events(session, limit=1)) == [event_id]


# --- what the database refuses on its own ----------------------------------------------------


@pytest.mark.parametrize("statement", EDITS)
async def test_the_application_role_has_no_privilege_to_edit_the_log(
    db: Database, statement: str
) -> None:
    async with db.transaction() as session:
        event = await add_event(session)

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == INSUFFICIENT_PRIVILEGE
    assert await stored_rows(db) == [(event, "success")]


@pytest.mark.parametrize("statement", EDITS)
async def test_even_the_owner_cannot_edit_the_log(
    db: Database, owner_db: Database, statement: str
) -> None:
    # The owner holds every privilege on the table. The trigger is what stops it.
    async with db.transaction() as session:
        event = await add_event(session)

    with pytest.raises(DBAPIError) as failure:
        async with owner_db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == APPEND_ONLY
    assert "append-only" in str(failure.value)
    assert await stored_rows(db) == [(event, "success")]


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("UPDATE audit_events SET outcome = 'failed' WHERE false", id="update"),
        pytest.param("DELETE FROM audit_events WHERE false", id="delete"),
    ],
)
async def test_the_owner_is_refused_even_a_statement_that_matches_no_rows(
    owner_db: Database, statement: str
) -> None:
    # The table is empty and the statement could match nothing anyway. A row-level trigger
    # would never fire here.
    with pytest.raises(DBAPIError) as failure:
        async with owner_db.transaction() as session:
            await session.execute(text(statement))

    assert sqlstate_of(failure.value) == APPEND_ONLY


async def test_the_application_role_may_read_the_log_and_add_to_it_and_nothing_else(
    db: Database,
) -> None:
    async with db.transaction() as session:
        granted = await session.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants"
                " WHERE grantee = :role AND table_schema = 'public'"
                " AND table_name = 'audit_events'"
            ),
            {"role": postgres.APP_ROLE},
        )
        privileges = sorted(granted.scalars())

    assert privileges == ["INSERT", "SELECT"]


@pytest.mark.parametrize(
    "column", ["id", "occurred_at", "actor_type", "action", "outcome", "details"]
)
async def test_the_database_fills_in_nothing_the_application_left_out(
    db: Database, column: str
) -> None:
    # No column has a default, so a value the application did not supply is simply missing.
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_event(session, omit=column)

    assert sqlstate_of(failure.value) == NOT_NULL_VIOLATION
    assert await stored_rows(db) == []


async def test_two_events_cannot_share_an_id(db: Database) -> None:
    async with db.transaction() as session:
        event = await add_event(session)

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_event(session, id=event, outcome="failed")

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == "pk_audit_events"
    assert await stored_rows(db) == [(event, "success")]


async def test_the_database_refuses_a_kind_of_actor_it_does_not_know(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_event(session, actor_type="robot")

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == "ck_audit_events_actor_type"


async def test_the_database_refuses_an_outcome_it_does_not_know(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_event(session, outcome="maybe")

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == "ck_audit_events_outcome"


@pytest.mark.parametrize("action", MALFORMED_ACTIONS)
async def test_the_database_refuses_a_malformed_action(db: Database, action: str) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_event(session, action=action)

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == "ck_audit_events_action"


async def test_the_model_states_the_same_checks_as_the_migration(owner_db: Database) -> None:
    # The comparison of models with migrations that the harness runs does not look at check
    # constraints. So build the table from the model in a schema of its own, and let
    # PostgreSQL say how it reads each definition.
    modelled = MetaData(naming_convention=NAMING_CONVENTION)
    all_metadata().tables["audit_events"].to_metadata(modelled, schema="from_model")

    async with owner_db.transaction() as session:
        await session.execute(text("CREATE SCHEMA from_model"))
        await session.run_sync(lambda sync: modelled.create_all(sync.connection()))
        rows = await session.execute(
            text(
                "SELECT n.nspname AS schema, c.conname AS name,"
                " pg_get_constraintdef(c.oid) AS definition"
                " FROM pg_constraint c"
                " JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE t.relname = 'audit_events' AND c.contype = 'c'"
            )
        )
        checks: dict[str, dict[str, str]] = {"public": {}, "from_model": {}}
        for row in rows:
            checks[row.schema][row.name] = row.definition

    assert set(checks["public"]) == {
        "ck_audit_events_actor_type",
        "ck_audit_events_action",
        "ck_audit_events_outcome",
    }
    assert checks["from_model"] == checks["public"]

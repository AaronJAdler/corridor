"""Users: what the database refuses on its own, then the service on top of it."""

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import identity
from corridor.identity import EmailTaken, HandleTaken, InvalidHandle, User, UserNotFound
from corridor.platform.clock import ManualClock
from corridor.platform.db import (
    CHECK_VIOLATION,
    UNIQUE_VIOLATION,
    Database,
    constraint_of,
    sqlstate_of,
)
from corridor.platform.errors import Conflict, InvalidRequest
from corridor.platform.ids import new_id
from tests.identity.support import PASSWORD_HASH, add_user, close_account, count, user_row
from tests.support import postgres

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
INSUFFICIENT_PRIVILEGE = "42501"

# What a row written by hand carries for a password hash. It matches no password.
NOT_A_HASH = "not-a-hash"  # pragma: allowlist secret


async def insert_user(session: AsyncSession, **overrides: object) -> uuid.UUID:
    """Write a users row with plain SQL, the way a buggy caller would, bypassing the service."""
    user_id = new_id()
    values: dict[str, object] = {
        "id": user_id,
        "email": f"{user_id}@example.com",
        # The tail of a UUIDv7 is its random part; the head is a timestamp.
        "handle": f"u{user_id.hex[-12:]}",
        "display_name": "By Hand",
        "password_hash": NOT_A_HASH,
        "role": "user",
        "kyc_tier": 0,
        "status": "active",
        "restricted_reason": None,
        "created_at": NOW,
        "updated_at": NOW,
        "tokens_valid_after": None,
    }
    values.update(overrides)
    await session.execute(
        text(
            "INSERT INTO users (id, email, handle, display_name, password_hash, role, kyc_tier,"
            " status, restricted_reason, created_at, updated_at, tokens_valid_after)"
            " VALUES (:id, :email, :handle, :display_name, :password_hash, :role, :kyc_tier,"
            " :status, :restricted_reason, :created_at, :updated_at, :tokens_valid_after)"
        ),
        values,
    )
    return user_id


# --- what the database refuses on its own ----------------------------------------------------


async def test_a_well_formed_user_row_is_stored(db: Database) -> None:
    async with db.transaction() as session:
        user_id = await insert_user(session, handle="maria_01", kyc_tier=2, status="restricted")
        stored = (
            await session.execute(
                text("SELECT handle, kyc_tier, status, created_at FROM users WHERE id = :id"),
                {"id": user_id},
            )
        ).one()
    assert tuple(stored) == ("maria_01", 2, "restricted", NOW)


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("email", "Maria@example.com", "ck_users_email_lowercase"),
        ("handle", "ab", "ck_users_handle"),
        ("handle", "a" * 31, "ck_users_handle"),
        ("handle", "Maria", "ck_users_handle"),
        ("handle", "maria.s", "ck_users_handle"),
        ("handle", "maria\n", "ck_users_handle"),
        ("role", "root", "ck_users_role"),
        ("kyc_tier", -1, "ck_users_kyc_tier"),
        ("kyc_tier", 3, "ck_users_kyc_tier"),
        ("status", "banned", "ck_users_status"),
    ],
)
async def test_the_database_refuses_a_malformed_user(
    db: Database, column: str, value: object, constraint: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await insert_user(session, **{column: value})

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == constraint


@pytest.mark.parametrize(
    ("column", "constraint"), [("email", "uq_users_email"), ("handle", "uq_users_handle")]
)
async def test_the_database_refuses_a_second_user_with_the_same_email_or_handle(
    db: Database, column: str, constraint: str
) -> None:
    taken = {"email": "maria@example.com", "handle": "maria"}[column]
    async with db.transaction() as session:
        await insert_user(session, **{column: taken})

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await insert_user(session, **{column: taken})

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == constraint


async def test_no_identity_column_has_a_default(db: Database) -> None:
    # The application supplies every value, so a forgotten one is an error, not a guess.
    async with db.transaction() as session:
        defaulted = (
            await session.execute(
                text(
                    "SELECT table_name || '.' || column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name IN"
                    " ('users', 'refresh_tokens', 'login_lockouts', 'login_throttles')"
                    " AND column_default IS NOT NULL"
                )
            )
        ).scalars()
        assert list(defaulted) == []
        counted = (
            await session.execute(
                text(
                    "SELECT table_name, count(*) FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name IN"
                    " ('users', 'refresh_tokens', 'login_lockouts', 'login_throttles')"
                    " GROUP BY table_name"
                )
            )
        ).all()
    assert dict(tuple(row) for row in counted) == {
        "users": 12,
        "refresh_tokens": 8,
        "login_lockouts": 5,
        "login_throttles": 3,
    }


async def test_a_user_row_has_no_login_counters_for_a_stranger_to_write_to(db: Database) -> None:
    # Failed logins are counted in tables of their own, by the address that was typed.
    async with db.transaction() as session:
        columns = (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'users'"
                )
            )
        ).scalars()
        assert {"failed_logins", "locked_until"} & set(columns) == set()


@pytest.mark.parametrize(
    ("table", "statement"),
    [
        (
            "login_lockouts",
            "INSERT INTO login_lockouts (email_hash, client, failed_logins, locked_until,"
            " updated_at) VALUES ('h', 'c', 0, NULL, :now)",
        ),
        (
            "login_throttles",
            "INSERT INTO login_throttles (email_hash, failed_logins, last_failed_at)"
            " VALUES ('h', 0, :now)",
        ),
    ],
)
async def test_the_database_refuses_a_count_of_no_failures(
    db: Database, table: str, statement: str
) -> None:
    # A row is there because something failed. The first failure makes it, with a count of one.
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text(statement), {"now": NOW})

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == f"ck_{table}_failed_logins_positive"


async def test_one_client_has_one_count_for_one_address(db: Database) -> None:
    insert = text(
        "INSERT INTO login_lockouts (email_hash, client, failed_logins, locked_until, updated_at)"
        " VALUES ('h', :client, 1, NULL, :now)"
    )
    async with db.transaction() as session:
        await session.execute(insert, {"client": "203.0.113.7", "now": NOW})
        await session.execute(insert, {"client": "203.0.113.8", "now": NOW})

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(insert, {"client": "203.0.113.7", "now": NOW})

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == "pk_login_lockouts"


async def test_the_application_role_cannot_delete_a_user(db: Database) -> None:
    async with db.transaction() as session:
        user_id = await insert_user(session)

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    assert sqlstate_of(failure.value) == INSUFFICIENT_PRIVILEGE
    async with db.transaction() as session:
        assert (await session.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1


async def test_the_application_role_holds_exactly_the_privileges_it_needs(db: Database) -> None:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type) AS privileges"
                " FROM information_schema.role_table_grants"
                " WHERE grantee = :role AND table_schema = 'public'"
                " AND table_name IN ('users', 'refresh_tokens', 'login_lockouts',"
                " 'login_throttles') GROUP BY table_name"
            ),
            {"role": postgres.APP_ROLE},
        )
        granted = {row.table_name: row.privileges for row in rows}

    assert granted == {
        "users": "INSERT,SELECT,UPDATE",
        # DELETE stays, so that expired tokens can be pruned.
        "refresh_tokens": "DELETE,INSERT,SELECT,UPDATE",
        # And here, so that a count is dropped when its owner logs in or it has gone stale.
        "login_lockouts": "DELETE,INSERT,SELECT,UPDATE",
        "login_throttles": "DELETE,INSERT,SELECT,UPDATE",
    }


# --- registration ----------------------------------------------------------------------------


async def test_registration_normalises_and_stores(db: Database, clock: ManualClock) -> None:
    async with db.transaction() as session:
        user = await identity.register(
            session,
            email="  Maria.Souza@Example.COM ",
            handle=" @Maria_01 ",
            display_name="Maria Souza",
            password_hash=PASSWORD_HASH,
        )

    assert user == User(
        id=user.id,
        email="maria.souza@example.com",
        handle="maria_01",
        display_name="Maria Souza",
        role="user",
        kyc_tier=0,
        status="active",
        created_at=clock.now(),
    )
    assert user.id.version == 7
    async with db.transaction() as session:
        assert await identity.get_user(session, user.id) == user
        assert await user_row(session, user.id) == {
            "id": user.id,
            "email": "maria.souza@example.com",
            "handle": "maria_01",
            "display_name": "Maria Souza",
            "password_hash": PASSWORD_HASH,
            "role": "user",
            "kyc_tier": 0,
            "status": "active",
            "restricted_reason": None,
            "created_at": clock.now(),
            "updated_at": clock.now(),
            "tokens_valid_after": None,
        }


async def test_an_admin_is_registered_with_the_admin_role(db: Database) -> None:
    async with db.transaction() as session:
        admin = await add_user(session, "root_admin", role="admin")
        assert (await identity.get_user(session, admin.id)).role == "admin"


async def test_a_user_never_carries_the_password_hash(db: Database) -> None:
    async with db.transaction() as session:
        user = await add_user(session)

    assert "password_hash" not in {field.name for field in dataclasses.fields(User)}
    assert PASSWORD_HASH not in repr(user)


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("abc", "abc"),
        ("a" * 30, "a" * 30),
        ("_x_", "_x_"),
        ("007", "007"),
        ("@Maria", "maria"),
        ("  joao_99\n", "joao_99"),
    ],
)
async def test_a_handle_of_3_to_30_letters_digits_and_underscores_is_accepted(
    db: Database, given: str, stored: str
) -> None:
    async with db.transaction() as session:
        user = await identity.register(
            session,
            email="someone@example.com",
            handle=given,
            display_name="Someone",
            password_hash=PASSWORD_HASH,
        )
    assert user.handle == stored


@pytest.mark.parametrize(
    "handle",
    [
        "ab",
        "a" * 31,
        "maria.souza",
        "maria-souza",
        "maria souza",
        "mar\nia",
        "maría",
        "maria!",
        "",
        "@",
        "@@maria",
    ],
)
async def test_a_bad_handle_is_refused(db: Database, handle: str) -> None:
    async with db.transaction() as session:
        with pytest.raises(InvalidHandle) as refusal:
            await identity.register(
                session,
                email="maria@example.com",
                handle=handle,
                display_name="Maria",
                password_hash=PASSWORD_HASH,
            )

        assert (refusal.value.status, refusal.value.code) == (422, "invalid_handle")
        assert await count(session, "users") == 0


async def test_an_email_already_registered_in_another_case_is_refused(db: Database) -> None:
    async with db.transaction() as session:
        await add_user(session, "maria")

        with pytest.raises(EmailTaken) as refusal:
            await identity.register(
                session,
                email="MARIA@Example.com",
                handle="someone_else",
                display_name="Someone Else",
                password_hash=PASSWORD_HASH,
            )

        assert (refusal.value.status, refusal.value.code) == (409, "email_taken")
        # The refusal wrote nothing and left the transaction usable: the caller carries on.
        await add_user(session, "joao")

    async with db.transaction() as session:
        assert await count(session, "users") == 2


async def test_a_handle_already_taken_is_refused(db: Database) -> None:
    async with db.transaction() as session:
        await add_user(session, "maria")

        with pytest.raises(HandleTaken) as refusal:
            await identity.register(
                session,
                email="another.maria@example.com",
                handle="@Maria",
                display_name="Another Maria",
                password_hash=PASSWORD_HASH,
            )

        assert (refusal.value.status, refusal.value.code) == (409, "handle_taken")
        await add_user(session, "joao")

    async with db.transaction() as session:
        assert await count(session, "users") == 2


async def test_when_both_are_taken_the_handle_is_reported(db: Database) -> None:
    # A handle is public and an address is not. Reporting the address first would let a
    # handle known to be taken be used to ask whether any address is registered.
    async with db.transaction() as session:
        await add_user(session, "maria")
        await add_user(session, "joao")

        with pytest.raises(HandleTaken):
            await identity.register(
                session,
                email="maria@example.com",
                handle="joao",
                display_name="Both",
                password_hash=PASSWORD_HASH,
            )


async def test_20_concurrent_registrations_of_one_email_create_one_user(db: Database) -> None:
    async def attempt(index: int) -> str:
        async def work(session: AsyncSession) -> None:
            await identity.register(
                session,
                email="Maria@Example.com" if index % 2 else "maria@example.com",
                handle=f"maria_{index}",
                display_name="Maria",
                password_hash=PASSWORD_HASH,
            )

        try:
            await db.run(work)
        except EmailTaken:
            return "taken"
        return "registered"

    outcomes = await asyncio.gather(*(attempt(index) for index in range(20)))

    assert sorted(outcomes) == ["registered"] + ["taken"] * 19
    async with db.transaction() as session:
        assert await count(session, "users") == 1


async def test_20_concurrent_registrations_of_one_handle_create_one_user(db: Database) -> None:
    async def attempt(index: int) -> str:
        async def work(session: AsyncSession) -> None:
            await identity.register(
                session,
                email=f"maria{index}@example.com",
                handle="Maria" if index % 2 else "maria",
                display_name="Maria",
                password_hash=PASSWORD_HASH,
            )

        try:
            await db.run(work)
        except HandleTaken:
            return "taken"
        return "registered"

    outcomes = await asyncio.gather(*(attempt(index) for index in range(20)))

    assert sorted(outcomes) == ["registered"] + ["taken"] * 19
    async with db.transaction() as session:
        assert await count(session, "users") == 1


# --- lookup ----------------------------------------------------------------------------------


async def test_a_user_is_fetched_by_id(db: Database) -> None:
    async with db.transaction() as session:
        maria = await add_user(session, "maria")
        joao = await add_user(session, "joao")

        assert await identity.get_user(session, maria.id) == maria
        assert await identity.get_users(session, [joao.id, maria.id, new_id()]) == {
            maria.id: maria,
            joao.id: joao,
        }
        assert await identity.get_users(session, []) == {}


async def test_fetching_a_user_who_does_not_exist_is_not_found(db: Database) -> None:
    async with db.transaction() as session:
        await add_user(session)

        with pytest.raises(UserNotFound) as refusal:
            await identity.get_user(session, new_id())

    assert (refusal.value.status, refusal.value.code) == (404, "user_not_found")


async def test_a_user_is_found_by_handle_email_or_id(db: Database) -> None:
    async with db.transaction() as session:
        maria = await add_user(session, "maria")
        await add_user(session, "joao")

        for identifier in (
            "@maria",
            "@Maria",
            "maria",
            " MARIA ",
            "maria@example.com",
            " Maria@Example.COM ",
            str(maria.id),
            str(maria.id).upper(),
        ):
            assert await identity.find_user(session, identifier) == maria, identifier


@pytest.mark.parametrize(
    "identifier",
    [
        "@nobody",
        "nobody",
        "nobody@example.com",
        "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10",
        "",
        "   ",
        "@",
        "@@maria",
        "not a handle",
        "maria@",
        "maria!",
    ],
)
async def test_an_identifier_that_matches_nobody_finds_nothing(
    db: Database, identifier: str
) -> None:
    async with db.transaction() as session:
        await add_user(session, "maria")

        assert await identity.find_user(session, identifier) is None


async def test_a_closed_user_is_not_found_but_can_still_be_fetched(db: Database) -> None:
    async with db.transaction() as session:
        maria = await add_user(session, "maria")
        await close_account(session, maria.id)

        for identifier in ("@maria", "maria", "maria@example.com", str(maria.id)):
            assert await identity.find_user(session, identifier) is None, identifier
        # Whoever already holds the id still gets the user, and sees that it is closed.
        assert (await identity.get_user(session, maria.id)).status == "closed"


# --- KYC tier --------------------------------------------------------------------------------


@pytest.mark.parametrize("tier", [0, 1, 2])
async def test_a_kyc_tier_from_0_to_2_is_set(db: Database, clock: ManualClock, tier: int) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)
        clock.advance(hours=1)

        changed = await identity.set_kyc_tier(session, maria.id, tier)

        assert changed == dataclasses.replace(maria, kyc_tier=tier)
        row = await user_row(session, maria.id)
        assert (row["kyc_tier"], row["updated_at"]) == (tier, clock.now())


@pytest.mark.parametrize("tier", [-1, 3, True, 1.5, "2", None])
async def test_a_kyc_tier_outside_0_to_2_is_refused(db: Database, tier: object) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)
        await identity.set_kyc_tier(session, maria.id, 1)

        with pytest.raises(InvalidRequest):
            await identity.set_kyc_tier(session, maria.id, tier)  # type: ignore[arg-type]

        assert (await identity.get_user(session, maria.id)).kyc_tier == 1


async def test_setting_the_kyc_tier_of_nobody_is_not_found(db: Database) -> None:
    async with db.transaction() as session:
        with pytest.raises(UserNotFound):
            await identity.set_kyc_tier(session, new_id(), 1)


# --- restrictions ----------------------------------------------------------------------------


async def test_a_user_is_restricted_and_the_restriction_lifted(
    db: Database, clock: ManualClock
) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)
        clock.advance(hours=1)

        restricted = await identity.restrict_user(session, maria.id, "deposit returned")

        assert restricted == dataclasses.replace(maria, status="restricted")
        row = await user_row(session, maria.id)
        assert (row["status"], row["restricted_reason"], row["updated_at"]) == (
            "restricted",
            "deposit returned",
            clock.now(),
        )

        clock.advance(hours=1)
        lifted = await identity.lift_restriction(session, maria.id)

        assert lifted == maria
        row = await user_row(session, maria.id)
        assert (row["status"], row["restricted_reason"], row["updated_at"]) == (
            "active",
            None,
            clock.now(),
        )


async def test_restricting_again_replaces_the_reason(db: Database) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)
        await identity.restrict_user(session, maria.id, "deposit returned")

        again = await identity.restrict_user(session, maria.id, "under review")

        assert again.status == "restricted"
        assert (await user_row(session, maria.id))["restricted_reason"] == "under review"


async def test_lifting_a_restriction_that_is_not_there_changes_nothing(db: Database) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)

        assert await identity.lift_restriction(session, maria.id) == maria


async def test_a_closed_user_cannot_be_restricted_or_unrestricted(db: Database) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)
        await close_account(session, maria.id)

        with pytest.raises(Conflict) as restricting:
            await identity.restrict_user(session, maria.id, "deposit returned")
        with pytest.raises(Conflict) as lifting:
            await identity.lift_restriction(session, maria.id)

        assert (restricting.value.status, lifting.value.status) == (409, 409)
        row = await user_row(session, maria.id)
        assert (row["status"], row["restricted_reason"]) == ("closed", None)


async def test_restricting_nobody_is_not_found(db: Database) -> None:
    async with db.transaction() as session:
        with pytest.raises(UserNotFound):
            await identity.restrict_user(session, new_id(), "deposit returned")
        with pytest.raises(UserNotFound):
            await identity.lift_restriction(session, new_id())


# --- what registering would have returned ----------------------------------------------------


def test_an_unregistered_user_is_what_registering_those_details_would_have_returned(
    clock: ManualClock,
) -> None:
    stand_in = identity.unregistered_user(
        email="  Maria.Souza@Example.COM ", handle=" @Maria_01 ", display_name="Maria Souza"
    )

    assert stand_in == User(
        id=stand_in.id,
        email="maria.souza@example.com",
        handle="maria_01",
        display_name="Maria Souza",
        role="user",
        kyc_tier=0,
        status="active",
        created_at=clock.now(),
    )
    assert stand_in.id.version == 7


async def test_an_unregistered_user_is_nobody(db: Database) -> None:
    stand_in = identity.unregistered_user(email="a@example.com", handle="abc", display_name="A")
    other = identity.unregistered_user(email="a@example.com", handle="abc", display_name="A")

    assert stand_in.id != other.id
    async with db.transaction() as session:
        with pytest.raises(UserNotFound):
            await identity.get_user(session, stand_in.id)


def test_an_unregistered_user_with_a_handle_that_could_not_be_registered_is_a_mistake() -> None:
    # Registration refuses such a handle before it could come to this.
    with pytest.raises(ValueError, match="bad handle"):
        identity.unregistered_user(email="a@example.com", handle="a b", display_name="A")


# --- roles -----------------------------------------------------------------------------------


async def test_a_role_is_changed_and_the_users_tokens_are_ended(
    db: Database, clock: ManualClock
) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)
    clock.advance(seconds=90.25)

    async with db.transaction() as session:
        promoted = await identity.set_role(session, maria.id, "admin")
        row = await user_row(session, maria.id)

    assert promoted == dataclasses.replace(maria, role="admin")
    # The first whole second after the change: see ``test_tokens`` for why not the instant.
    assert row["tokens_valid_after"] == clock.now().replace(microsecond=0) + timedelta(seconds=1)
    assert row["updated_at"] == clock.now()


async def test_changing_a_role_keeps_the_users_sessions(db: Database) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)
        await session.execute(
            text(
                "INSERT INTO refresh_tokens (id, user_id, family_id, token_hash, issued_at,"
                " expires_at) VALUES (:id, :user, :family, 'h', :now, :now)"
            ),
            {"id": new_id(), "user": maria.id, "family": new_id(), "now": NOW},
        )

        await identity.set_role(session, maria.id, "admin")

        revoked = await session.execute(text("SELECT revoked_at FROM refresh_tokens"))
        assert revoked.scalar_one() is None


@pytest.mark.parametrize("role", ["root", "", "ADMIN", None])
async def test_a_role_that_is_not_one_is_refused(db: Database, role: object) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)

        with pytest.raises(InvalidRequest):
            await identity.set_role(session, maria.id, role)  # type: ignore[arg-type]

        assert (await user_row(session, maria.id))["tokens_valid_after"] is None


async def test_changing_the_role_of_nobody_is_not_found(db: Database) -> None:
    async with db.transaction() as session:
        with pytest.raises(UserNotFound):
            await identity.set_role(session, new_id(), "admin")


# --- closing ---------------------------------------------------------------------------------


async def test_closing_an_account_ends_its_tokens_and_its_sessions(
    db: Database, clock: ManualClock
) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)
        joao = await add_user(session, "joao")
        for owner in (maria, maria, joao):
            await session.execute(
                text(
                    "INSERT INTO refresh_tokens (id, user_id, family_id, token_hash, issued_at,"
                    " expires_at) VALUES (:id, :user, :family, :hash, :now, :now)"
                ),
                {
                    "id": new_id(),
                    "user": owner.id,
                    "family": new_id(),
                    "hash": str(new_id()),
                    "now": NOW,
                },
            )
    clock.advance(seconds=30)

    async with db.transaction() as session:
        closed = await identity.close_user(session, maria.id)
        row = await user_row(session, maria.id)
        revoked = (
            await session.execute(
                text("SELECT user_id, revoked_at FROM refresh_tokens ORDER BY user_id, id")
            )
        ).all()

    assert closed == dataclasses.replace(maria, status="closed")
    assert row["tokens_valid_after"] == clock.now() + timedelta(seconds=1)
    assert sorted((owner, at) for owner, at in revoked if owner == maria.id) == [
        (maria.id, clock.now()),
        (maria.id, clock.now()),
    ]
    # Nobody else's session is touched.
    assert [at for owner, at in revoked if owner == joao.id] == [None]


async def test_a_closed_account_is_not_found_by_what_people_type(db: Database) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)

        await identity.close_user(session, maria.id)

        assert await identity.find_user(session, "@maria") is None
        assert (await identity.get_user(session, maria.id)).status == "closed"


async def test_closing_a_closed_account_changes_nothing(db: Database, clock: ManualClock) -> None:
    async with db.transaction() as session:
        maria = await add_user(session)
        await identity.close_user(session, maria.id)
        before = await user_row(session, maria.id)
    clock.advance(seconds=3600)

    async with db.transaction() as session:
        again = await identity.close_user(session, maria.id)

        assert again.status == "closed"
        assert await user_row(session, maria.id) == before


async def test_closing_nobody_is_not_found(db: Database) -> None:
    async with db.transaction() as session:
        with pytest.raises(UserNotFound):
            await identity.close_user(session, new_id())

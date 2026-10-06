"""Users, logins and sessions.

Every function takes the caller's session and runs inside the caller's transaction. Nothing
here commits, and nothing here hashes a password: Argon2 is slow on purpose, so it runs
before the transaction opens (see ``passwords``).
"""

import re
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Final, cast

from sqlalchemy import ColumnElement, RowMapping, Table, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.identity import models
from corridor.identity.errors import EmailTaken, HandleTaken, InvalidHandle, UserNotFound
from corridor.identity.keys import KeySet
from corridor.identity.tokens import hash_refresh_token, mint_access_token, new_refresh_token
from corridor.identity.types import (
    LoginCandidate,
    LoginOutcome,
    RefreshOutcome,
    Role,
    TokenPair,
    User,
)
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.errors import Conflict, InvalidRequest
from corridor.platform.ids import new_id

# Core tables. Identity writes with explicit statements, never through the ORM's unit of work.
_users = cast(Table, models.User.__table__)
_tokens = cast(Table, models.RefreshToken.__table__)

# What a ``User`` is built from. The password hash and the login counters stay behind.
_USER_COLUMNS: Final = (
    _users.c.id,
    _users.c.email,
    _users.c.handle,
    _users.c.display_name,
    _users.c.role,
    _users.c.kyc_tier,
    _users.c.status,
    _users.c.created_at,
)

# The same rule as the table's CHECK. Matched with ``fullmatch``: ``$`` alone would let a
# trailing newline through.
_HANDLE: Final = re.compile(r"[a-z0-9_]{3,30}")

_KYC_TIERS: Final = range(3)


# --- users ---------------------------------------------------------------------------------


async def register(
    session: AsyncSession,
    *,
    email: str,
    handle: str,
    display_name: str,
    password_hash: str,
    role: Role = "user",
) -> User:
    """Create a user. The caller has already hashed the password, outside any transaction.

    A taken email or handle is found by ``ON CONFLICT DO NOTHING`` rather than by catching
    the unique violation: a violation would abort the caller's transaction, and a refusal
    must leave it usable.
    """
    email = _normalise_email(email)
    normalised = _normalise_handle(handle)
    if normalised is None:
        raise InvalidHandle(
            "A handle is 3 to 30 characters: lower-case letters, digits and underscores."
        )

    now = utcnow()
    inserted = await session.execute(
        pg_insert(_users)
        .values(
            id=new_id(),
            email=email,
            handle=normalised,
            display_name=display_name,
            password_hash=password_hash,
            role=role,
            kyc_tier=0,
            status="active",
            restricted_reason=None,
            failed_logins=0,
            locked_until=None,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing()
        .returning(*_USER_COLUMNS)
    )
    row = inserted.mappings().one_or_none()
    if row is not None:
        return _user(row)

    # The insert waited for whoever holds the email or the handle, and that transaction has
    # committed, so the row that is in the way is visible now.
    taken = (
        await session.execute(
            select(_users.c.email, _users.c.handle).where(
                or_(_users.c.email == email, _users.c.handle == normalised)
            )
        )
    ).all()
    if any(other.email == email for other in taken):
        raise EmailTaken("That email address is already registered.")
    if any(other.handle == normalised for other in taken):
        raise HandleTaken("That handle is already taken.")
    raise RuntimeError(  # pragma: no cover - the conflicting row is committed
        "a user conflicted on insert and then could not be found"
    )


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    rows = await session.execute(select(*_USER_COLUMNS).where(_users.c.id == user_id))
    row = rows.mappings().one_or_none()
    if row is None:
        raise UserNotFound("There is no such user.")
    return _user(row)


async def get_users(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, User]:
    """The users with these ids. An id that matches nobody is simply absent."""
    rows = await session.execute(select(*_USER_COLUMNS).where(_users.c.id.in_(set(ids))))
    return {row["id"]: _user(row) for row in rows.mappings()}


async def find_user(session: AsyncSession, identifier: str) -> User | None:
    """Resolve what a person types to address someone: ``@handle``, a bare handle, an email
    address or a user id. A closed account is not found."""
    identifier = identifier.strip()
    if "@" in identifier[1:]:
        return await _find_open_user(session, _users.c.email == _normalise_email(identifier))
    # An id and a handle cannot be mistaken for each other: an id has 32 hex digits and a
    # handle at most 30 characters.
    user_id = None if identifier.startswith("@") else _as_uuid(identifier)
    if user_id is not None:
        return await _find_open_user(session, _users.c.id == user_id)
    handle = _normalise_handle(identifier)
    if handle is None:
        return None
    return await _find_open_user(session, _users.c.handle == handle)


async def set_kyc_tier(session: AsyncSession, user_id: uuid.UUID, tier: int) -> User:
    if isinstance(tier, bool) or not isinstance(tier, int) or tier not in _KYC_TIERS:
        raise InvalidRequest("A KYC tier is 0, 1 or 2.")
    updated = await session.execute(
        update(_users)
        .where(_users.c.id == user_id)
        .values(kyc_tier=tier, updated_at=utcnow())
        .returning(*_USER_COLUMNS)
    )
    row = updated.mappings().one_or_none()
    if row is None:
        raise UserNotFound("There is no such user.")
    return _user(row)


async def restrict_user(session: AsyncSession, user_id: uuid.UUID, reason: str) -> User:
    """Mark a user restricted, which stops money moving out. Restricting a user who is
    already restricted replaces the reason."""
    return await _set_status(
        session,
        user_id,
        status="restricted",
        restricted_reason=reason,
        refusal="A closed account cannot be restricted.",
    )


async def lift_restriction(session: AsyncSession, user_id: uuid.UUID) -> User:
    return await _set_status(
        session,
        user_id,
        status="active",
        restricted_reason=None,
        refusal="A closed account cannot have a restriction lifted.",
    )


async def _set_status(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: str,
    restricted_reason: str | None,
    refusal: str,
) -> User:
    # One statement, so the check and the change cannot be separated by a concurrent close.
    updated = await session.execute(
        update(_users)
        .where(_users.c.id == user_id, _users.c.status != "closed")
        .values(status=status, restricted_reason=restricted_reason, updated_at=utcnow())
        .returning(*_USER_COLUMNS)
    )
    row = updated.mappings().one_or_none()
    if row is None:
        await get_user(session, user_id)  # raises if there is no such user at all
        raise Conflict(refusal)
    return _user(row)


async def _find_open_user(session: AsyncSession, condition: ColumnElement[bool]) -> User | None:
    rows = await session.execute(
        select(*_USER_COLUMNS).where(condition, _users.c.status != "closed")
    )
    row = rows.mappings().one_or_none()
    return _user(row) if row is not None else None


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def _normalise_handle(handle: str) -> str | None:
    """A handle as it is stored, or None if it cannot be one."""
    cleaned = handle.strip().removeprefix("@").lower()
    return cleaned if _HANDLE.fullmatch(cleaned) else None


def _as_uuid(text: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _user(row: RowMapping) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        handle=row["handle"],
        display_name=row["display_name"],
        role=row["role"],
        kyc_tier=row["kyc_tier"],
        status=row["status"],
        created_at=row["created_at"],
    )


# --- login ---------------------------------------------------------------------------------
#
# A login is three steps, so that no transaction is open while Argon2 runs: find the
# candidate (a short read), verify the password (no transaction), apply the result.


async def find_login_candidate(session: AsyncSession, email: str) -> LoginCandidate | None:
    """Step one: whose password is about to be checked. A closed account is still found, so
    that its password is checked like anyone's and timing gives nothing away."""
    rows = await session.execute(
        select(_users.c.id, _users.c.password_hash).where(_users.c.email == _normalise_email(email))
    )
    row = rows.one_or_none()
    if row is None:
        return None
    return LoginCandidate(user_id=row.id, password_hash=row.password_hash)


async def complete_login(
    session: AsyncSession,
    candidate: LoginCandidate | None,
    password_ok: bool,
    *,
    settings: Settings,
) -> LoginOutcome:
    """Step three: apply the result of the password check, and say how the login ended.

    The outcome is returned, never raised. A failed login has to be counted, and raising
    would roll the count back along with the caller's transaction.
    """
    if candidate is None:
        return LoginOutcome(user=None, user_id=None, reason="unknown_email", locked_until=None)

    # The row lock makes concurrent attempts queue, so each one sees the count the one
    # before it left and none is lost.
    rows = await session.execute(
        select(*_USER_COLUMNS, _users.c.failed_logins, _users.c.locked_until)
        .where(_users.c.id == candidate.user_id)
        .with_for_update()
    )
    row = rows.mappings().one_or_none()
    if row is None:
        return LoginOutcome(user=None, user_id=None, reason="unknown_email", locked_until=None)
    if row["status"] == "closed":
        return LoginOutcome(
            user=None, user_id=candidate.user_id, reason="closed", locked_until=None
        )

    now = utcnow()
    locked_until: datetime | None = row["locked_until"]
    if locked_until is not None and locked_until > now:
        # Whether or not the password was right. A lock that the right password opened
        # would not slow down guessing at all: the one correct guess would still get in.
        return LoginOutcome(
            user=None, user_id=candidate.user_id, reason="locked", locked_until=locked_until
        )

    if not password_ok:
        failed_logins = row["failed_logins"] + 1
        locked_until = None
        if failed_logins >= settings.login_lockout_threshold:
            # The base period at the threshold, doubling with each failure after it.
            doublings = failed_logins - settings.login_lockout_threshold
            locked_until = now + timedelta(
                seconds=min(
                    settings.login_lockout_base_seconds * 2**doublings,
                    settings.login_lockout_max_seconds,
                )
            )
        await session.execute(
            update(_users)
            .where(_users.c.id == candidate.user_id)
            .values(failed_logins=failed_logins, locked_until=locked_until, updated_at=now)
        )
        return LoginOutcome(
            user=None, user_id=candidate.user_id, reason="bad_password", locked_until=locked_until
        )

    if row["failed_logins"] != 0 or locked_until is not None:
        await session.execute(
            update(_users)
            .where(_users.c.id == candidate.user_id)
            .values(failed_logins=0, locked_until=None, updated_at=now)
        )
    return LoginOutcome(user=_user(row), user_id=candidate.user_id, reason=None, locked_until=None)


# --- sessions ------------------------------------------------------------------------------
#
# A session is a family of refresh tokens: the one issued at login and every token it has
# been rotated into. The family id is the session id that access tokens carry as ``sid``.


async def issue_session(
    session: AsyncSession, user: User, *, keys: KeySet, settings: Settings
) -> TokenPair:
    """Start a session for a user who has just logged in."""
    return await _issue(
        session, user_id=user.id, role=user.role, family_id=new_id(), keys=keys, settings=settings
    )


async def rotate_refresh_token(
    session: AsyncSession, presented: str, *, keys: KeySet, settings: Settings
) -> RefreshOutcome:
    """Exchange a refresh token for a new pair in the same session.

    A token is good for one exchange. Presenting one that was already exchanged means it
    was copied, and nobody can tell which holder is the thief, so the whole session ends.

    The outcome is returned, never raised: ending the session has to be committed, and
    raising would roll it back along with the caller's transaction.
    """
    now = utcnow()
    # The row lock makes two exchanges of one token queue: the second sees what the first did.
    rows = await session.execute(
        select(_tokens)
        .where(_tokens.c.token_hash == hash_refresh_token(presented))
        .with_for_update()
    )
    token = rows.mappings().one_or_none()
    if token is None:
        return RefreshOutcome(tokens=None, user_id=None, session_id=None, reason="unknown")

    user_id, family_id = token["user_id"], token["family_id"]
    # Asked of the family, not of this token alone: see ``_family_is_revoked``.
    if await _family_is_revoked(session, family_id):
        return RefreshOutcome(tokens=None, user_id=user_id, session_id=family_id, reason="revoked")
    if token["expires_at"] <= now:
        return RefreshOutcome(tokens=None, user_id=user_id, session_id=family_id, reason="expired")
    owner = (
        await session.execute(select(_users.c.role, _users.c.status).where(_users.c.id == user_id))
    ).one()
    if owner.status == "closed":
        return RefreshOutcome(tokens=None, user_id=user_id, session_id=family_id, reason="closed")
    if token["used_at"] is not None:
        await _revoke(session, _tokens.c.family_id == family_id)
        return RefreshOutcome(
            tokens=None, user_id=user_id, session_id=family_id, reason="reuse_detected"
        )

    await session.execute(update(_tokens).where(_tokens.c.id == token["id"]).values(used_at=now))
    # The role is read now, not copied from the old token: a change takes effect at the
    # next refresh.
    pair = await _issue(
        session, user_id=user_id, role=owner.role, family_id=family_id, keys=keys, settings=settings
    )
    return RefreshOutcome(tokens=pair, user_id=user_id, session_id=family_id, reason=None)


async def revoke_session(session: AsyncSession, session_id: uuid.UUID) -> int:
    """End one session. Returns how many of its tokens were not revoked before."""
    return await _revoke(session, _tokens.c.family_id == session_id)


async def revoke_all_sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    """End every session of a user. Returns how many tokens were not revoked before."""
    return await _revoke(session, _tokens.c.user_id == user_id)


async def _issue(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    role: Role,
    family_id: uuid.UUID,
    keys: KeySet,
    settings: Settings,
) -> TokenPair:
    now = utcnow()
    refresh_token = new_refresh_token()
    await session.execute(
        insert(_tokens).values(
            id=new_id(),
            user_id=user_id,
            family_id=family_id,
            token_hash=hash_refresh_token(refresh_token),
            issued_at=now,
            expires_at=now + timedelta(seconds=settings.refresh_token_ttl_seconds),
            used_at=None,
            revoked_at=None,
        )
    )
    access_token, _ = mint_access_token(
        user_id=user_id, session_id=family_id, role=role, keys=keys, settings=settings
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_seconds,
        session_id=family_id,
    )


async def _revoke(session: AsyncSession, condition: ColumnElement[bool]) -> int:
    revoked = await session.execute(
        update(_tokens)
        .where(condition, _tokens.c.revoked_at.is_(None))
        .values(revoked_at=utcnow())
        .returning(_tokens.c.id)
    )
    return len(revoked.all())


async def _family_is_revoked(session: AsyncSession, family_id: uuid.UUID) -> bool:
    """Whether any token of the family is revoked, which condemns all of them.

    Revoking a family is one UPDATE, and under READ COMMITTED it cannot see a token that a
    rotation running at the same moment has yet to commit. That token is left unrevoked.
    Without this check it would keep the session alive after the session was ended.
    """
    rows = await session.execute(
        select(_tokens.c.id)
        .where(_tokens.c.family_id == family_id, _tokens.c.revoked_at.is_not(None))
        .limit(1)
    )
    return rows.first() is not None

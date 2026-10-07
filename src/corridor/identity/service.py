"""Users, logins and sessions.

Every function takes the caller's session and runs inside the caller's transaction. Nothing
here commits, and nothing here hashes a password: Argon2 is slow on purpose, so it runs
before the transaction opens (see ``passwords``).
"""

import hashlib
import re
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Final, cast

from sqlalchemy import ColumnElement, RowMapping, Table, delete, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.identity import models
from corridor.identity.errors import (
    EmailTaken,
    HandleTaken,
    InvalidHandle,
    InvalidToken,
    UserNotFound,
)
from corridor.identity.keys import KeySet
from corridor.identity.tokens import hash_refresh_token, mint_access_token, new_refresh_token
from corridor.identity.types import (
    AccessClaims,
    LoginCandidate,
    LoginOutcome,
    LoginRefusal,
    RefreshOutcome,
    Role,
    TokenPair,
    User,
)
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import advisory_xact_lock, lock_key
from corridor.platform.errors import Conflict, InvalidRequest
from corridor.platform.ids import new_id

# Core tables. Identity writes with explicit statements, never through the ORM's unit of work.
_users = cast(Table, models.User.__table__)
_tokens = cast(Table, models.RefreshToken.__table__)
_lockouts = cast(Table, models.LoginLockout.__table__)
_throttles = cast(Table, models.LoginThrottle.__table__)

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

# The namespace of the advisory lock that makes login attempts on one address queue.
_LOGIN_LOCK: Final = "login"
# How often a period is doubled at most. Past this the maximum has long been reached.
_MAX_DOUBLINGS: Final = 32


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
            created_at=now,
            updated_at=now,
            tokens_valid_after=None,
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
    # The handle first. A handle is public, and whether one is taken is no secret; whether
    # an address is registered is. Asked in the other order, registering an address with
    # a handle known to be taken would say which of the two was in the way.
    if any(other.handle == normalised for other in taken):
        raise HandleTaken("That handle is already taken.")
    if any(other.email == email for other in taken):
        raise EmailTaken("That email address is already registered.")
    raise RuntimeError(  # pragma: no cover - the conflicting row is committed
        "a user conflicted on insert and then could not be found"
    )


def unregistered_user(*, email: str, handle: str, display_name: str) -> User:
    """What registering these details would have returned, for a registration that was
    not made because the address is somebody's already.

    Whoever asked is answered with this, so that the answer does not say the address is
    registered. It is nobody: the id is new and names no row.
    """
    normalised = _normalise_handle(handle)
    if normalised is None:
        raise ValueError("a registration with a bad handle is refused before this")
    return User(
        id=new_id(),
        email=_normalise_email(email),
        handle=normalised,
        display_name=display_name,
        role="user",
        kyc_tier=0,
        status="active",
        created_at=utcnow(),
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


async def set_role(session: AsyncSession, user_id: uuid.UUID, role: Role) -> User:
    """Make a user an administrator, or stop them being one.

    Every access token the user holds is ended, so that none goes on carrying the role
    they had. Their sessions are kept: the next refresh issues a token with the new role.
    """
    if role not in ("user", "admin"):
        raise InvalidRequest("A role is user or admin.")
    now = utcnow()
    updated = await session.execute(
        update(_users)
        .where(_users.c.id == user_id)
        .values(role=role, tokens_valid_after=_after(now), updated_at=now)
        .returning(*_USER_COLUMNS)
    )
    row = updated.mappings().one_or_none()
    if row is None:
        raise UserNotFound("There is no such user.")
    return _user(row)


async def close_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    """Close an account, for good. The row stays: other modules go on referring to it.

    Every access token is ended and every session revoked, so nothing the user holds works
    after this commits. Closing a closed account changes nothing.
    """
    now = utcnow()
    updated = await session.execute(
        update(_users)
        .where(_users.c.id == user_id, _users.c.status != "closed")
        .values(status="closed", tokens_valid_after=_after(now), updated_at=now)
        .returning(*_USER_COLUMNS)
    )
    row = updated.mappings().one_or_none()
    if row is None:
        return await get_user(session, user_id)  # raises if there is no such user at all
    await revoke_all_sessions(session, user_id)
    return _user(row)


def _after(now: datetime) -> datetime:
    """The first whole second after ``now``.

    A token's issue time is in whole seconds, rounded down, so one issued earlier in this
    very second says a time before ``now``. Ending tokens "before now" would let it
    through; this ends it, at the price of refusing a token issued in what is left of the
    second.
    """
    return now.replace(microsecond=0) + timedelta(seconds=1)


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
#
# Failures are counted against the address that was typed, not against a user, in two
# places. One count is per client: enough failures from one client lock that client out
# of that address, and nobody else. The other is across all clients: enough of them slow
# every answer about the address down, whoever asks, and refuse nobody. So a stranger
# who hammers on an account cannot keep its owner out, and a guesser who spreads over
# many clients is still slowed. Both are kept the same way for an address nobody
# registered, so neither says whether an account exists.


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


async def login_delay_seconds(session: AsyncSession, email: str, *, settings: Settings) -> float:
    """How long the answer to a login for this address is to be held back, in seconds.

    Zero until the address has failed ``login_throttle_threshold`` times within the
    window, then the base delay, doubling with each further failure up to the maximum.
    The caller waits with no transaction open.
    """
    rows = await session.execute(
        select(_throttles.c.failed_logins, _throttles.c.last_failed_at).where(
            _throttles.c.email_hash == _email_hash(email)
        )
    )
    row = rows.one_or_none()
    if row is None or _window_has_passed(row.last_failed_at, utcnow(), settings):
        return 0.0
    over = row.failed_logins - settings.login_throttle_threshold
    if over < 0:
        return 0.0
    # The exponent is bounded so that a long attack does not compute an enormous number
    # only to have the maximum taken of it.
    return float(
        min(
            settings.login_throttle_base_seconds * 2 ** min(over, _MAX_DOUBLINGS),
            settings.login_throttle_max_seconds,
        )
    )


async def complete_login(
    session: AsyncSession,
    candidate: LoginCandidate | None,
    password_ok: bool,
    *,
    email: str,
    client: str,
    settings: Settings,
) -> LoginOutcome:
    """Step three: apply the result of the password check, and say how the login ended.

    ``email`` is the address as it was typed and ``client`` is where the attempt came
    from, as the caller wants clients told apart.

    The outcome is returned, never raised. A failed login has to be counted, and raising
    would roll the count back along with the caller's transaction.
    """
    email_hash = _email_hash(email)
    user_id = candidate.user_id if candidate is not None else None
    # Attempts on one address queue here, so each sees the counts the one before it left
    # and none is lost.
    await advisory_xact_lock(session, [lock_key(_LOGIN_LOCK, email_hash)])

    now = utcnow()
    lockout = (
        await session.execute(
            select(_lockouts.c.failed_logins, _lockouts.c.locked_until).where(
                _lockouts.c.email_hash == email_hash, _lockouts.c.client == client
            )
        )
    ).one_or_none()
    locked_until: datetime | None = lockout.locked_until if lockout is not None else None
    if locked_until is not None and locked_until > now:
        # Whether or not the password was right. A lock that the right password opened
        # would not slow down guessing at all: the one correct guess would still get in.
        return LoginOutcome(user=None, user_id=user_id, reason="locked", locked_until=locked_until)

    user: RowMapping | None = None
    if candidate is not None:
        rows = await session.execute(select(*_USER_COLUMNS).where(_users.c.id == candidate.user_id))
        user = rows.mappings().one_or_none()

    if user is None or user["status"] == "closed" or not password_ok:
        reason: LoginRefusal = (
            "unknown_email"
            if user is None
            else "closed"
            if user["status"] == "closed"
            else "bad_password"
        )
        # Every failure is counted alike, whatever its reason, so that the counts and the
        # delay they lead to are the same for an address with no account behind it.
        failed_before = lockout.failed_logins if lockout is not None else 0
        locked_until = await _count_failure(
            session, email_hash, client, failed_before + 1, now, settings
        )
        return LoginOutcome(
            user=None,
            user_id=None if user is None else user_id,
            reason=reason,
            locked_until=locked_until,
        )

    if lockout is not None:
        await session.execute(
            delete(_lockouts).where(
                _lockouts.c.email_hash == email_hash, _lockouts.c.client == client
            )
        )
    # The owner is in, so what was counted across clients starts again from nothing.
    await session.execute(delete(_throttles).where(_throttles.c.email_hash == email_hash))
    return LoginOutcome(user=_user(user), user_id=user["id"], reason=None, locked_until=None)


async def purge_login_failures(session: AsyncSession, *, older_than: datetime) -> int:
    """Delete the failure counts nothing has added to since ``older_than``, and say how
    many rows went. A count that old locks nobody out and slows nothing down."""
    lockouts = await session.execute(
        delete(_lockouts)
        .where(
            _lockouts.c.updated_at < older_than,
            or_(_lockouts.c.locked_until.is_(None), _lockouts.c.locked_until < older_than),
        )
        .returning(_lockouts.c.email_hash)
    )
    throttles = await session.execute(
        delete(_throttles)
        .where(_throttles.c.last_failed_at < older_than)
        .returning(_throttles.c.email_hash)
    )
    return len(lockouts.all()) + len(throttles.all())


async def _count_failure(
    session: AsyncSession,
    email_hash: str,
    client: str,
    failed_logins: int,
    now: datetime,
    settings: Settings,
) -> datetime | None:
    """Record one more failure in both counts. Returns until when the client is locked out."""
    locked_until: datetime | None = None
    if failed_logins >= settings.login_lockout_threshold:
        # The base period at the threshold, doubling with each failure after it.
        doublings = min(failed_logins - settings.login_lockout_threshold, _MAX_DOUBLINGS)
        locked_until = now + timedelta(
            seconds=min(
                settings.login_lockout_base_seconds * 2**doublings,
                settings.login_lockout_max_seconds,
            )
        )
    counted = {"failed_logins": failed_logins, "locked_until": locked_until, "updated_at": now}
    await session.execute(
        pg_insert(_lockouts)
        .values(email_hash=email_hash, client=client, **counted)
        .on_conflict_do_update(
            index_elements=[_lockouts.c.email_hash, _lockouts.c.client], set_=counted
        )
    )

    throttle = (
        await session.execute(
            select(_throttles.c.failed_logins, _throttles.c.last_failed_at).where(
                _throttles.c.email_hash == email_hash
            )
        )
    ).one_or_none()
    # Failures older than the window are forgotten: the count starts again.
    recent = (
        0
        if throttle is None or _window_has_passed(throttle.last_failed_at, now, settings)
        else throttle.failed_logins
    )
    slowed = {"failed_logins": recent + 1, "last_failed_at": now}
    await session.execute(
        pg_insert(_throttles)
        .values(email_hash=email_hash, **slowed)
        .on_conflict_do_update(index_elements=[_throttles.c.email_hash], set_=slowed)
    )
    return locked_until


def _window_has_passed(last_failed_at: datetime, now: datetime, settings: Settings) -> bool:
    return last_failed_at <= now - timedelta(seconds=settings.login_throttle_window_seconds)


def _email_hash(email: str) -> str:
    """What failures are counted under: the SHA-256 of the address as it would be stored.

    A digest, so that whatever was typed makes a key of one fixed length, and so that the
    table is not a list of addresses people tried. "surrogatepass" lets text that is not
    valid Unicode be hashed instead of failing.
    """
    return hashlib.sha256(_normalise_email(email).encode("utf-8", "surrogatepass")).hexdigest()


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


async def check_access(session: AsyncSession, claims: AccessClaims) -> None:
    """Refuse an access token whose user may no longer use it. One read, by primary key.

    A token is signed once and says what was true then. This asks what is true now: the
    account is not closed, the role is still the one the token carries, and nothing has
    ended the user's tokens since it was issued. The refusal is the one every bad token
    gets, and does not say which of these it was.
    """
    rows = await session.execute(
        select(_users.c.status, _users.c.role, _users.c.tokens_valid_after).where(
            _users.c.id == claims.user_id
        )
    )
    user = rows.one_or_none()
    if user is None or user.status == "closed" or user.role != claims.role:
        raise InvalidToken
    if user.tokens_valid_after is not None and claims.issued_at < user.tokens_valid_after:
        raise InvalidToken


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

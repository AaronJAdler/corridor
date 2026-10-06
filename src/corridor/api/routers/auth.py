"""Authentication endpoints: register, log in, refresh, log out, and who am I.

Each handler owns its transactions. Argon2 never runs inside one, and a refusal that has to
leave a record (a failed login, a reused refresh token) is raised only after the transaction
that recorded it has committed.
"""

from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity
from corridor.api.deps import CurrentPrincipal, Db, Hasher, Keys, Redis, SettingsDep
from corridor.api.ratelimit import rate_limit
from corridor.api.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from corridor.platform.errors import Unauthenticated
from corridor.platform.logging import get_logger

log = get_logger(__name__)

# One bucket per client address for the whole group: guessing passwords and probing for
# registered addresses are the same activity spread over different endpoints.
auth_rate_limit: Final = rate_limit("auth", per_minute=lambda s: s.rate_limit_auth_per_minute)

router = APIRouter(prefix="/v1/auth", tags=["auth"], dependencies=[Depends(auth_rate_limit)])
account_router = APIRouter(tags=["auth"])

# What else has to exist for a new user, such as their wallets. Each hook runs inside the
# transaction that creates the user, so a user never exists without it, and a hook that
# fails undoes the registration.
on_user_registered: list[Callable[[AsyncSession, identity.User], Awaitable[None]]] = []


class InvalidCredentials(Unauthenticated):
    """A login was refused. It never says why: an unknown address, a wrong password and a
    locked account must be indistinguishable to whoever is guessing."""

    code = "invalid_credentials"
    title = "Invalid credentials"

    def __init__(self) -> None:
        super().__init__("The email address or the password is not correct.")


class InvalidRefreshToken(Unauthenticated):
    """A refresh token was refused. As with an access token, it never says which check
    failed, and it shares that refusal's code: to a client both mean "log in again"."""

    code = "invalid_token"
    title = "Invalid token"

    def __init__(self) -> None:
        super().__init__("The refresh token is not valid.")


def _no_store(response: Response) -> None:
    # Tokens are for the client that asked and for nobody on the way (RFC 6749, 5.1).
    response.headers["Cache-Control"] = "no-store"


@router.post("/register", status_code=201, summary="Create a user")
async def register(body: RegisterRequest, db: Db, hasher: Hasher) -> UserResponse:
    password = body.password.get_secret_value()
    identity.validate_password(password)
    # Before the transaction opens: Argon2 is slow on purpose.
    password_hash = await hasher.hash(password)

    async def work(session: AsyncSession) -> identity.User:
        user = await identity.register(
            session,
            email=body.email,
            handle=body.handle,
            display_name=body.display_name,
            password_hash=password_hash,
        )
        for hook in on_user_registered:
            await hook(session, user)
        await audit.record(
            session,
            actor=audit.Actor.user(user.id),
            action="auth.registered",
            principal_id=user.id,
            resource_type="user",
            resource_id=user.id,
        )
        return user

    user = await db.run(work)
    log.info("auth.registered", user_id=str(user.id))
    return UserResponse.of(user)


@router.post("/login", summary="Exchange an email address and a password for tokens")
async def login(
    body: LoginRequest,
    response: Response,
    db: Db,
    hasher: Hasher,
    keys: Keys,
    settings: SettingsDep,
) -> TokenResponse:
    # Three steps, so that no transaction is open while Argon2 runs.
    candidate = await db.run(lambda session: identity.find_login_candidate(session, body.email))
    password_ok = await hasher.verify(
        candidate.password_hash if candidate is not None else None,
        body.password.get_secret_value(),
    )

    async def work(
        session: AsyncSession,
    ) -> tuple[identity.LoginOutcome, identity.TokenPair | None]:
        outcome = await identity.complete_login(session, candidate, password_ok, settings=settings)
        if outcome.user is None:
            await audit.record(
                session,
                actor=_actor(outcome.user_id),
                action="auth.login_failed",
                outcome="denied",
                principal_id=outcome.user_id,
                resource_type="user" if outcome.user_id is not None else None,
                resource_id=outcome.user_id,
                details={"reason": outcome.reason},
            )
            # Returned, not raised: the failure count and this event have to be committed.
            return outcome, None
        pair = await identity.issue_session(session, outcome.user, keys=keys, settings=settings)
        await audit.record(
            session,
            actor=audit.Actor.user(outcome.user.id),
            action="auth.logged_in",
            principal_id=outcome.user.id,
            resource_type="session",
            resource_id=pair.session_id,
        )
        return outcome, pair

    outcome, pair = await db.run(work)
    if pair is None:
        log.info("auth.login_failed", reason=outcome.reason, user_id=_text(outcome.user_id))
        raise InvalidCredentials
    log.info("auth.logged_in", user_id=_text(outcome.user_id), session_id=str(pair.session_id))
    _no_store(response)
    return TokenResponse.of(pair)


@router.post("/refresh", summary="Exchange a refresh token for a new pair of tokens")
async def refresh(
    body: RefreshRequest,
    response: Response,
    db: Db,
    redis: Redis,
    keys: Keys,
    settings: SettingsDep,
) -> TokenResponse:
    async def work(session: AsyncSession) -> identity.RefreshOutcome:
        outcome = await identity.rotate_refresh_token(
            session, body.refresh_token.get_secret_value(), keys=keys, settings=settings
        )
        if outcome.reason == "reuse_detected":
            await audit.record(
                session,
                actor=_actor(outcome.user_id),
                action="auth.refresh_reuse_detected",
                outcome="denied",
                principal_id=outcome.user_id,
                resource_type="session",
                resource_id=outcome.session_id,
            )
        elif outcome.tokens is not None:
            await audit.record(
                session,
                actor=_actor(outcome.user_id),
                action="auth.token_refreshed",
                principal_id=outcome.user_id,
                resource_type="session",
                resource_id=outcome.session_id,
            )
        # Returned, not raised: ending the session and the event have to be committed.
        return outcome

    outcome = await db.run(work)
    if outcome.reason == "reuse_detected" and outcome.session_id is not None:
        # After the commit, and outside any transaction: Redis is a network call. The
        # access tokens of the session are refused from now on, not when they expire.
        await identity.mark_session_revoked(
            redis, outcome.session_id, ttl_seconds=settings.access_token_ttl_seconds
        )
        log.warning(
            "auth.refresh_reuse_detected",
            user_id=_text(outcome.user_id),
            session_id=str(outcome.session_id),
        )
    if outcome.tokens is None:
        log.info(
            "auth.refresh_failed",
            reason=outcome.reason,
            user_id=_text(outcome.user_id),
            session_id=_text(outcome.session_id),
        )
        raise InvalidRefreshToken
    _no_store(response)
    return TokenResponse.of(outcome.tokens)


@router.post("/logout", status_code=204, summary="End the session this credential belongs to")
async def logout(principal: CurrentPrincipal, db: Db, redis: Redis, settings: SettingsDep) -> None:
    session_id = principal.session_id
    if session_id is None:
        # An agent's key is not a session, and is not ended by its bearer.
        raise identity.InsufficientScope("Only a user's own session can be logged out.")

    async def work(session: AsyncSession) -> None:
        await identity.revoke_session(session, session_id)
        await audit.record(
            session,
            actor=audit.Actor.user(principal.actor_id),
            action="auth.logged_out",
            principal_id=principal.user_id,
            resource_type="session",
            resource_id=session_id,
        )

    await db.run(work)
    # After the commit. The mark outlives the last access token the session can hold.
    await identity.mark_session_revoked(
        redis, session_id, ttl_seconds=settings.access_token_ttl_seconds
    )
    log.info("auth.logged_out", session_id=str(session_id))


@account_router.get("/v1/me", summary="The user this credential acts for")
async def me(principal: CurrentPrincipal, db: Db) -> UserResponse:
    user = await db.run(lambda session: identity.get_user(session, principal.user_id))
    return UserResponse.of(user)


@account_router.get("/.well-known/jwks.json", summary="The public keys that verify access tokens")
async def jwks(keys: Keys) -> dict[str, list[dict[str, str]]]:
    return keys.jwks()


def _actor(user_id: object) -> audit.Actor:
    """The user who acted, or nobody in particular when the request named no known user."""
    return audit.Actor("user", _text(user_id))


def _text(value: object) -> str | None:
    return None if value is None else str(value)

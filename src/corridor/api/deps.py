"""FastAPI dependencies shared by the routers.

Authentication lives here. A route is protected by depending on ``get_principal``, usually
through ``CurrentPrincipal``, ``AdminPrincipal`` or ``require``. A route that does not, and
is not in ``PUBLIC_ROUTES``, fails the route-table test.

Only ``require`` admits an agent's API key. ``CurrentPrincipal`` and ``AdminPrincipal``
admit a user's own session and nothing else, so a route that names no scope is not one an
agent can reach.
"""

from collections.abc import Callable
from typing import Annotated, Final, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from corridor import agents, identity
from corridor.api.container import Container
from corridor.identity import KeySet, PasswordHasher, Principal
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.errors import Unauthenticated
from corridor.platform.logging import bind_context, get_logger
from corridor.platform.redis import RedisStore

# The routes that answer without a credential, as (path, method). Every other route must
# depend on ``get_principal``. Keep this list short: each entry is a decision.
PUBLIC_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("/healthz", "GET"),
        ("/readyz", "GET"),
        ("/metrics", "GET"),
        ("/docs", "GET"),
        ("/openapi.json", "GET"),
        ("/.well-known/jwks.json", "GET"),
        ("/v1/auth/register", "POST"),
        ("/v1/auth/login", "POST"),
        ("/v1/auth/refresh", "POST"),
        # A provider cannot hold a bearer credential. Its deliveries are authenticated by
        # an HMAC signature over the raw body, checked before anything else is done.
        ("/v1/webhooks/{provider}", "POST"),
    }
)

log = get_logger(__name__)

# What every agent API key starts with. No access token can: a JWT starts "eyJ".
_API_KEY_PREFIX: Final = "ck_"

# What is said to a key that is not accepted, whatever was wrong with it: a key nobody
# issued, a wrong secret, a revoked or expired key, a paused agent and an owner who may not
# act are one answer, so that the answer tells a guesser nothing.
_KEY_NOT_ACCEPTED: Final = "This credential is not accepted."

# Reads the Authorization header and describes the scheme in the OpenAPI document. It does
# not refuse anything itself, so that every refusal is this module's own problem document.
_bearer: Final = HTTPBearer(
    auto_error=False, description="An access token from /v1/auth/login, or an agent API key."
)


def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


def get_settings(container: Annotated[Container, Depends(get_container)]) -> Settings:
    return container.settings


def get_db(container: Annotated[Container, Depends(get_container)]) -> Database:
    return container.db


def get_redis(container: Annotated[Container, Depends(get_container)]) -> RedisStore:
    return container.redis


def get_keys(container: Annotated[Container, Depends(get_container)]) -> KeySet:
    return container.keys


def get_hasher(container: Annotated[Container, Depends(get_container)]) -> PasswordHasher:
    return container.hasher


SettingsDep = Annotated[Settings, Depends(get_settings)]
Db = Annotated[Database, Depends(get_db)]
Redis = Annotated[RedisStore, Depends(get_redis)]
Keys = Annotated[KeySet, Depends(get_keys)]
Hasher = Annotated[PasswordHasher, Depends(get_hasher)]


async def get_principal(
    container: Annotated[Container, Depends(get_container)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Who this request acts for. Refuses the request if it does not say, or cannot prove it."""
    if credentials is None or not credentials.credentials:
        raise Unauthenticated("This endpoint needs a bearer credential.")
    token = credentials.credentials
    if token.startswith(_API_KEY_PREFIX):
        principal = await _authenticate_api_key(token, container)
    else:
        principal = await _authenticate_access_token(token, container)
    bind_context(
        principal_id=str(principal.user_id),
        actor_type=principal.actor_type,
        actor_id=str(principal.actor_id),
    )
    return principal


async def _authenticate_access_token(token: str, container: Container) -> Principal:
    claims = identity.verify_access_token(token, keys=container.keys, settings=container.settings)
    # A session that was logged out, or ended because its refresh token was reused, is
    # refused here although its access token has not expired. If Redis cannot say, the
    # token is taken at its word and lasts until it expires.
    if await identity.is_session_revoked(container.redis, claims.session_id):
        raise identity.InvalidToken
    return Principal.for_user(claims.user_id, claims.role, claims.session_id)


async def _authenticate_api_key(token: str, container: Container) -> Principal:
    """Authenticate an agent by its API key."""
    # Hashed here, before the transaction and before anything is known about the key.
    presented = agents.read_key(token, container.settings)
    if presented is None:
        raise Unauthenticated(_KEY_NOT_ACCEPTED)
    # A transaction of its own, committed before the handler opens one: recording when the
    # key was last used locks the key's row, and that lock is gone before any lock on
    # money is taken.
    outcome = await container.db.run(lambda session: agents.authenticate(session, presented))
    if outcome.reason is not None or outcome.agent_id is None or outcome.owner_user_id is None:
        log.info(
            "auth.api_key_refused",
            reason=outcome.reason,
            agent_id=None if outcome.agent_id is None else str(outcome.agent_id),
        )
        raise Unauthenticated(_KEY_NOT_ACCEPTED)
    return Principal.for_agent(outcome.owner_user_id, outcome.agent_id, outcome.scopes)


def _require_user_session(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    identity.require_user_session(principal)
    return principal


# The user, in their own session. An agent's key is refused, whatever its scopes: a route
# is open to an agent only by naming, with ``require``, the scope that opens it.
CurrentPrincipal = Annotated[Principal, Depends(_require_user_session)]


def require(scope: str) -> Callable[[Principal], Principal]:
    """A dependency that admits only a credential holding ``scope``, and yields its principal::

    principal: Annotated[Principal, Depends(require(Scope.TRANSFERS_CREATE))]

    A user's own session holds every scope. An agent's key holds the ones it was given.
    """

    def require_scope(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        identity.require_scope(principal, scope)
        return principal

    return require_scope


def _require_admin(principal: CurrentPrincipal) -> Principal:
    identity.require_admin(principal)
    return principal


AdminPrincipal = Annotated[Principal, Depends(_require_admin)]

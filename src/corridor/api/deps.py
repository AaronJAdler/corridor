"""FastAPI dependencies shared by the routers.

Authentication lives here. A route is protected by depending on ``get_principal``, usually
through ``CurrentPrincipal``, ``AdminPrincipal`` or ``require``. A route that does not, and
is not in ``PUBLIC_ROUTES``, fails the route-table test.
"""

from collections.abc import Callable
from typing import Annotated, Final, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from corridor import identity
from corridor.api.container import Container
from corridor.identity import KeySet, PasswordHasher, Principal
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.errors import Unauthenticated
from corridor.platform.logging import bind_context
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
    }
)

# What every agent API key starts with. No access token can: a JWT starts "eyJ".
_API_KEY_PREFIX: Final = "ck_"

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
        principal = await _authenticate_api_key(token)
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


async def _authenticate_api_key(token: str) -> Principal:
    """Authenticate an agent by its API key. There are no agents yet, so no key is good."""
    raise Unauthenticated("This credential is not accepted.")


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require(scope: str) -> Callable[[Principal], Principal]:
    """A dependency that admits only a credential holding ``scope``, and yields its principal::

    principal: Annotated[Principal, Depends(require(Scope.TRANSFERS_CREATE))]
    """

    def require_scope(principal: CurrentPrincipal) -> Principal:
        identity.require_scope(principal, scope)
        return principal

    return require_scope


def _require_admin(principal: CurrentPrincipal) -> Principal:
    identity.require_admin(principal)
    return principal


AdminPrincipal = Annotated[Principal, Depends(_require_admin)]

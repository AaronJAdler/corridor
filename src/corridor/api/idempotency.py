"""Idempotency: a request that carries a key is performed at most once.

The key row and the effect of the request commit in one transaction, so there is no moment
at which one exists without the other. A later request with the same key gets the stored
response of the first, including a stored refusal. See section 6 of the architecture.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, cast

from fastapi import Depends, Header, Request
from sqlalchemy import Table, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from corridor.api.errors import PROBLEM_CONTENT_TYPE, PROBLEM_TYPE_BASE, request_id_of
from corridor.api.models import IdempotencyKeyRow
from corridor.platform.clock import utcnow
from corridor.platform.db import (
    LOCK_NOT_AVAILABLE,
    Database,
    advisory_xact_lock,
    lock_key,
    sqlstate_of,
)
from corridor.platform.errors import Conflict, DomainError, InvalidRequest

IDEMPOTENCY_KEY_HEADER: Final = "Idempotency-Key"
REPLAYED_HEADER: Final = "Idempotent-Replayed"

_LOCK_NAMESPACE: Final = "idem"

# One to 255 visible ASCII characters: no spaces, no control characters, nothing that could
# be read differently by two systems on the way here.
_KEY: Final = re.compile(r"[\x21-\x7e]{1,255}")

# A Core table: every statement against it is written out below.
_keys = cast(Table, IdempotencyKeyRow.__table__)


class IdempotencyKeyRequired(DomainError):
    status = 400
    code = "idempotency_key_required"
    title = "Idempotency key required"


class InvalidIdempotencyKey(InvalidRequest):
    code = "invalid_idempotency_key"
    title = "Invalid idempotency key"


class IdempotencyKeyReused(InvalidRequest):
    code = "idempotency_key_reused"
    title = "Idempotency key reused"


class RequestInProgress(Conflict):
    code = "request_in_progress"
    title = "Request in progress"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail, headers={"Retry-After": "1"})


@dataclass(frozen=True)
class StoredResponse:
    """What a request was answered with, in the form it is kept on the key row."""

    status_code: int
    body: Any
    headers: Mapping[str, str]


def fingerprint(
    method: str, route: str, body: bytes | Mapping[str, Any], path: str | None = None
) -> str:
    """SHA-256, in hex, of the method, the route template, the canonical body and the path.

    ``route`` is the template (``/v1/transfers/{transfer_id}``) and ``path`` what was
    asked for (``/v1/transfers/0190...``). The path is what tells two resources of one
    route apart: without it, a key used to approve one adjustment would answer for
    another, with the first one's stored response. It is left out where it says nothing
    the template does not, so a route with no parameters has the fingerprint it always had.

    Each part is hashed with its length in front, so no two different sets of parts run
    together into the same bytes.
    """
    parts = [method.upper().encode(), route.encode(), _canonical(body)]
    if path is not None and path != route:
        parts.append(path.encode())
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _canonical(body: bytes | Mapping[str, Any]) -> bytes:
    """The body as JSON with sorted keys and no whitespace, so that two bodies that say the
    same thing give the same bytes. A body that is not JSON stands for itself."""
    if isinstance(body, bytes):
        try:
            parsed = json.loads(body)
        except ValueError:
            return body
    else:
        parsed = body
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


async def run_idempotent(
    db: Database,
    *,
    actor_id: uuid.UUID,
    key: str,
    method: str,
    route: str,
    body: bytes | Mapping[str, Any],
    work: Callable[[AsyncSession], Awaitable[StoredResponse]],
    path: str | None = None,
    lock_timeout_ms: int | None = None,
) -> tuple[StoredResponse, bool]:
    """Perform ``work`` once for this actor and key, and say whether this call replayed it.

    Returns the response and True if it is a stored one. ``work`` runs in the transaction
    that records the key, so it must not commit, and it may run again if PostgreSQL reports
    a deadlock, as with any ``Database.run``.

    A ``DomainError`` raised by ``work`` is not raised here: what ``work`` wrote is undone,
    the refusal is stored as the response and returned, and every retry gets it again. Any
    other exception passes through and takes the key with it, so the client can retry.

    ``path`` is the path that was asked for. A key that was used for one resource and
    comes back for another of the same route is refused as reused, like any other key
    that comes back with a different request.

    ``lock_timeout_ms`` is how long to wait for another request holding the same key. Left
    out, the wait is the connection's configured ``lock_timeout``.
    """
    wanted = fingerprint(method, route, body, path)

    async def attempt(session: AsyncSession) -> tuple[StoredResponse, bool]:
        # First lock of the transaction, as the lock order requires. The primary key alone
        # would keep a second copy of the work from committing, but its loser gets a unique
        # violation; with the lock, a concurrent duplicate waits and then replays.
        await _lock_key(session, actor_id, key, lock_timeout_ms)

        found = (
            await session.execute(
                select(
                    _keys.c.fingerprint,
                    _keys.c.status_code,
                    _keys.c.response_body,
                    _keys.c.response_headers,
                    _keys.c.completed_at,
                ).where(_keys.c.actor_id == actor_id, _keys.c.key == key)
            )
        ).one_or_none()
        if found is not None:
            if found.fingerprint != wanted:
                raise IdempotencyKeyReused(
                    "This idempotency key was already used for a different request."
                )
            if found.completed_at is None:
                raise RequestInProgress()
            return (
                StoredResponse(found.status_code, found.response_body, found.response_headers),
                True,
            )

        await session.execute(
            insert(_keys).values(
                actor_id=actor_id, key=key, fingerprint=wanted, created_at=utcnow()
            )
        )
        try:
            # A savepoint, so a refusal undoes what the work wrote and nothing else: the
            # key row above stays, to carry the refusal.
            async with session.begin_nested():
                response = await work(session)
        except DomainError as error:
            response = _refusal(error)
        await session.execute(
            update(_keys)
            .where(_keys.c.actor_id == actor_id, _keys.c.key == key)
            .values(
                status_code=response.status_code,
                response_body=response.body,
                response_headers=dict(response.headers),
                completed_at=utcnow(),
            )
        )
        return response, False

    return await db.run(attempt)


async def _lock_key(
    session: AsyncSession, actor_id: uuid.UUID, key: str, lock_timeout_ms: int | None
) -> None:
    """Take the key's advisory lock for the rest of the transaction.

    A wait that runs out means another request with this key is still in flight; the client
    is told to come back, and by then that request's response is stored.
    """
    previous: str | None = None
    if lock_timeout_ms is not None:
        previous = (await session.execute(text("SHOW lock_timeout"))).scalar_one()
        await _set_local_lock_timeout(session, f"{lock_timeout_ms}ms")
    try:
        await advisory_xact_lock(session, [lock_key(_LOCK_NAMESPACE, f"{actor_id}:{key}")])
    except DBAPIError as error:
        if sqlstate_of(error) == LOCK_NOT_AVAILABLE:
            raise RequestInProgress(
                "A request with this idempotency key is still being processed."
            ) from error
        raise
    if previous is not None:
        # The shorter wait was for the key only. The work's own locks wait as configured.
        await _set_local_lock_timeout(session, previous)


async def _set_local_lock_timeout(session: AsyncSession, value: str) -> None:
    # SET LOCAL takes no bound parameter; this is the same thing as a function call.
    await session.execute(text("SELECT set_config('lock_timeout', :value, true)"), {"value": value})


def _refusal(error: DomainError) -> StoredResponse:
    """A domain error as the problem document it is answered with, less the request id:
    that belongs to the request being answered and is added when the response is built."""
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_BASE + error.code.replace("_", "-"),
        "title": error.title,
        "status": error.status,
        "code": error.code,
    }
    if error.detail is not None:
        body["detail"] = error.detail
    # Extension members never replace the standard ones.
    body.update({name: value for name, value in error.extra.items() if name not in body})
    return StoredResponse(
        error.status, body, {**error.headers, "Content-Type": PROBLEM_CONTENT_TYPE}
    )


def require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_KEY_HEADER)] = None,
) -> str:
    if idempotency_key is None:
        raise IdempotencyKeyRequired(f"This request needs an {IDEMPOTENCY_KEY_HEADER} header.")
    if _KEY.fullmatch(idempotency_key) is None:
        raise InvalidIdempotencyKey(
            f"{IDEMPOTENCY_KEY_HEADER} must be 1 to 255 visible ASCII characters."
        )
    return idempotency_key


IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]


def to_response(
    stored: StoredResponse, replayed: bool, request: Request | None = None
) -> JSONResponse:
    """The HTTP response for a stored one, marked as a replay if it is one.

    Given the request, a stored refusal carries that request's id in its body, as every
    other problem document does.
    """
    headers = dict(stored.headers)
    body = stored.body
    is_problem = any(
        name.lower() == "content-type" and value == PROBLEM_CONTENT_TYPE
        for name, value in headers.items()
    )
    request_id = request_id_of(request) if request is not None else None
    if is_problem and request_id is not None and isinstance(body, dict):
        body = {**body, "request_id": request_id}
    if replayed:
        headers[REPLAYED_HEADER] = "true"
    return JSONResponse(body, status_code=stored.status_code, headers=headers)

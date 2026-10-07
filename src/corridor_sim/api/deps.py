"""What the routers share: the state, authentication, and reading and writing JSON."""

import hmac
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, NoReturn, cast

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.responses import JSONResponse

from corridor_sim.clock import parse_time
from corridor_sim.errors import ApiError
from corridor_sim.idempotency import IdempotentRequest, canonical
from corridor_sim.state import SimState


def get_sim(request: Request) -> SimState:
    return cast(SimState, request.app.state.sim)


Sim = Annotated[SimState, Depends(get_sim)]


def require_api_key(request: Request, sim: Sim) -> None:
    """Every provider route sits behind this: ``Authorization: Bearer <api key>``."""
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    expected = sim.settings.api_key.get_secret_value()
    # Compared as bytes, because a header may hold characters that a str comparison in
    # constant time refuses.
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        credential.encode(), expected.encode()
    ):
        raise ApiError(
            401,
            "unauthorized",
            "Send the API key as 'Authorization: Bearer <api key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_control_token(request: Request, sim: Sim) -> None:
    """Every control route sits behind this. With no token configured the simulator only
    listens on a loopback address, and the route is open; with one, the caller presents it
    as ``Authorization: Bearer <control token>``."""
    if sim.settings.control_token is None:
        return
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    expected = sim.settings.control_token.get_secret_value()
    # As bytes and in constant time, as the API key is.
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        credential.encode(), expected.encode()
    ):
        raise ApiError(
            401,
            "unauthorized",
            "Send the control token as 'Authorization: Bearer <control token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class Body(BaseModel):
    """The shape of a request body. Nothing is coerced: a string is not a number here.

    An amount is declared as ``object`` and judged later by ``money.parse_amount``, so that
    a number where a string belongs is refused as an invalid amount rather than as a
    malformed request.
    """

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)


def document(content: object, status: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status)


def created_or_found(content: object, created: bool) -> JSONResponse:
    """The first response to a ``POST`` is ``201``; a repeat that finds the resource is ``200``."""
    return JSONResponse(content, status_code=201 if created else 200)


async def read_object(request: Request) -> dict[str, object]:
    """The request body as a JSON object, or a refusal."""
    try:
        parsed = json.loads(await request.body(), parse_constant=_not_json)
    except ValueError, RecursionError:
        raise invalid_request("The request body must be a JSON object.") from None
    if not isinstance(parsed, dict):
        raise invalid_request("The request body must be a JSON object.")
    return parsed


def parse[Model: BaseModel](model: type[Model], payload: Mapping[str, object]) -> Model:
    """Check a body's shape. The message names fields and never repeats what was sent."""
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
        raise invalid_request(problems) from None


def required_idempotency_key(request: Request) -> str:
    key = _idempotency_key(request)
    if key is None:
        raise ApiError(
            400, "idempotency_key_required", "This request needs an Idempotency-Key header."
        )
    return key


def optional_idempotency(
    request: Request, payload: Mapping[str, object]
) -> IdempotentRequest | None:
    """For the requests that accept a key without requiring one."""
    key = _idempotency_key(request)
    return IdempotentRequest(key, canonical(payload)) if key is not None else None


def _idempotency_key(request: Request) -> str | None:
    return request.headers.get("idempotency-key", "").strip() or None


def query(request: Request, name: str) -> str:
    value = request.query_params.get(name)
    if not value:
        raise invalid_request(f"The query parameter '{name}' is required.")
    return value


def query_time(request: Request, name: str) -> datetime:
    try:
        return parse_time(query(request, name))
    except ValueError:
        raise invalid_request(
            f"The query parameter '{name}' must be a time with a UTC offset, "
            "such as 2026-01-15T12:00:00Z."
        ) from None


def query_window(request: Request) -> tuple[datetime, datetime]:
    """A statement's half-open window, ``[from, to)``."""
    start, end = query_time(request, "from"), query_time(request, "to")
    if start > end:
        raise invalid_request("'from' must not be later than 'to'.")
    return start, end


def invalid_request(message: str) -> ApiError:
    return ApiError(422, "invalid_request", message)


def _not_json(constant: str) -> NoReturn:
    # NaN and Infinity are not JSON, though Python's parser accepts them by default.
    raise ValueError(f"{constant} is not JSON")

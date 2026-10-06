"""The application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from http import HTTPStatus
from typing import cast

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from corridor_sim.api import bank, control, custody, fx
from corridor_sim.errors import ApiError
from corridor_sim.settings import SimSettings, load_settings
from corridor_sim.state import SimState
from corridor_sim.webhooks import WebhookSender

log = logging.getLogger(__name__)

# How often a realtime simulator looks at the clock.
TICK_SECONDS = 0.25

_STATUS_CODES = {404: "not_found", 405: "method_not_allowed"}


def create_app(
    settings: SimSettings | None = None, *, webhook_client: httpx.AsyncClient | None = None
) -> FastAPI:
    """Build the simulators as one application.

    ``webhook_client`` is how a test wires webhook deliveries straight into another
    in-process application. Without it the simulator opens a client of its own when it
    starts and closes it when it stops.
    """
    resolved = settings if settings is not None else load_settings()
    sender = WebhookSender(webhook_client, resolved.webhook_timeout_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own_client: httpx.AsyncClient | None = None
        if webhook_client is None:
            own_client = httpx.AsyncClient(timeout=resolved.webhook_timeout_seconds)
            sender.client = own_client
        ticker = (
            asyncio.create_task(_tick_forever(app)) if resolved.clock_mode == "realtime" else None
        )
        try:
            yield
        finally:
            # The ticker goes first: it may be in the middle of a delivery on the client.
            if ticker is not None:
                ticker.cancel()
                with suppress(asyncio.CancelledError):
                    await ticker
            if own_client is not None:
                sender.client = None
                await own_client.aclose()

    app = FastAPI(
        title="Corridor provider simulators",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.sender = sender
    app.state.sim = SimState(resolved, sender)

    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected_error)
    app.include_router(bank.router)
    app.include_router(custody.router)
    app.include_router(fx.router)
    app.include_router(control.router)
    return app


async def _tick_forever(app: FastAPI) -> None:
    """In realtime mode nobody advances the clock, so time is noticed by looking at it."""
    while True:
        try:
            # Read from the app each time: a reset replaces the state.
            await cast(SimState, app.state.sim).tick()
        except Exception:
            log.exception("sim.tick_failed")
        await asyncio.sleep(TICK_SECONDS)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(ApiError(status, code, message).body(), status_code=status)


async def _api_error(_request: Request, exc: Exception) -> JSONResponse:
    error = cast(ApiError, exc)
    return JSONResponse(error.body(), status_code=error.status, headers=error.headers or None)


async def _validation_error(_request: Request, _exc: Exception) -> JSONResponse:
    return _error(422, "invalid_request", "The request did not match this endpoint.")


async def _http_error(_request: Request, exc: Exception) -> JSONResponse:
    error = cast(HTTPException, exc)
    response = _error(
        error.status_code,
        _STATUS_CODES.get(error.status_code, "http_error"),
        HTTPStatus(error.status_code).phrase,
    )
    response.headers.update(error.headers or {})
    return response


async def _unexpected_error(_request: Request, error: Exception) -> JSONResponse:
    log.error("sim.unhandled_error", exc_info=error)
    return _error(500, "internal_error", "The simulator failed to handle the request.")

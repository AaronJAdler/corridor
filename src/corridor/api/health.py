"""Service endpoints: liveness, readiness and metrics. None of them is under ``/v1``."""

import asyncio

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException

from corridor.api.deps import Db, Redis, SettingsDep
from corridor.platform.logging import get_logger

router = APIRouter(tags=["service"])
log = get_logger(__name__)

_CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/healthz", summary="Liveness: the process is running")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness: the process can serve requests")
async def readyz(db: Db, redis: Redis, response: Response) -> dict[str, object]:
    """PostgreSQL is required. Redis is not: without it the service is degraded, not down."""
    postgres_ok = await _postgres_ok(db)
    redis_ok = await redis.ping()

    if not postgres_ok:
        response.status_code = 503
        status = "unavailable"
    elif not redis_ok:
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "checks": {
            "postgres": "ok" if postgres_ok else "down",
            "redis": "ok" if redis_ok else "down",
        },
    }


async def _postgres_ok(db: Db) -> bool:
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            await db.ping()
    except Exception as error:
        # Whatever went wrong, the answer to "can this instance serve?" is no.
        log.warning("readiness.postgres_down", error_type=type(error).__name__)
        return False
    return True


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics(settings: SettingsDep) -> Response:
    """The API's own metrics, where a deployment has chosen to serve them here.

    The endpoint has no authentication and this is the port clients reach, so unless
    ``metrics_public`` is set it answers as a path that does not exist.
    """
    if not settings.metrics_public:
        # The same exception an unknown path raises, so the two answers are one.
        raise HTTPException(status_code=404)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

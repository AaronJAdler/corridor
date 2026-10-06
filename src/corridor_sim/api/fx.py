"""The rate source's API, ``/fx/v1``, exactly as the provider contract describes it."""

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse

from corridor_sim.api.deps import Sim, document, require_api_key
from corridor_sim.fx import rate_document

router = APIRouter(prefix="/fx/v1", dependencies=[Depends(require_api_key)])


@router.get("/rates/{base}/{quote}")
async def get_rate(base: str, quote: str, sim: Sim) -> JSONResponse:
    await sim.faults.before("fx.get_rate")
    response = document(rate_document(sim.fx.rate(base, quote)))
    await sim.faults.after("fx.get_rate")
    return response

"""The custodian's API, ``/custody/v1``, exactly as the provider contract describes it."""

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from starlette.responses import JSONResponse

from corridor_sim.api.deps import (
    Body,
    Sim,
    created_or_found,
    document,
    optional_idempotency,
    parse,
    query,
    query_window,
    read_object,
    require_api_key,
    required_idempotency_key,
)
from corridor_sim.books import statement_document
from corridor_sim.custody import address_document
from corridor_sim.idempotency import IdempotentRequest, canonical

router = APIRouter(prefix="/custody/v1", dependencies=[Depends(require_api_key)])


class AddressBody(Body):
    customer_reference: str = Field(min_length=1)
    asset: str


class WithdrawalBody(Body):
    asset: str
    amount: object = None
    # Judged by the address rule, so anything unsuitable is an invalid address.
    to_address: object = None
    reference: str = Field(min_length=1)


@router.post("/addresses")
async def create_address(request: Request, sim: Sim) -> JSONResponse:
    await sim.faults.before("custody.create_address")
    payload = await read_object(request)
    body = parse(AddressBody, payload)
    address, created = sim.custody.create_address(
        body.customer_reference, body.asset, optional_idempotency(request, payload)
    )
    response = created_or_found(address_document(address), created)
    await sim.faults.after("custody.create_address")
    return response


@router.post("/withdrawals")
async def create_withdrawal(request: Request, sim: Sim) -> JSONResponse:
    await sim.faults.before("custody.create_withdrawal")
    key = required_idempotency_key(request)
    payload = await read_object(request)
    body = parse(WithdrawalBody, payload)
    withdrawal, created = sim.custody.create_withdrawal(
        asset=body.asset,
        amount=body.amount,
        to_address=body.to_address,
        reference=body.reference,
        idempotent=IdempotentRequest(key, canonical(payload)),
    )
    response = created_or_found(sim.custody.withdrawal_document(withdrawal), created)
    await sim.faults.after("custody.create_withdrawal")
    return response


@router.get("/withdrawals/{withdrawal_id}")
async def get_withdrawal(withdrawal_id: str, sim: Sim) -> JSONResponse:
    await sim.faults.before("custody.get_withdrawal")
    withdrawal = sim.custody.get_withdrawal(withdrawal_id)
    response = document(sim.custody.withdrawal_document(withdrawal))
    await sim.faults.after("custody.get_withdrawal")
    return response


@router.get("/withdrawals")
async def list_withdrawals(request: Request, sim: Sim) -> JSONResponse:
    # The list form is the same read by another key, so the same faults reach it.
    await sim.faults.before("custody.get_withdrawal")
    withdrawals = sim.custody.withdrawals_by_reference(query(request, "reference"))
    response = document(
        {"withdrawals": [sim.custody.withdrawal_document(withdrawal) for withdrawal in withdrawals]}
    )
    await sim.faults.after("custody.get_withdrawal")
    return response


@router.get("/transactions")
async def list_transactions(request: Request, sim: Sim) -> JSONResponse:
    await sim.faults.before("custody.list_transactions")
    asset = query(request, "asset")
    start, end = query_window(request)
    response = document(
        statement_document(sim.custody.statement(asset, start, end), with_tx_hash=True)
    )
    await sim.faults.after("custody.list_transactions")
    return response

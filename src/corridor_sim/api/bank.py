"""The bank rail's API, ``/bank/v1``, exactly as the provider contract describes it."""

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
from corridor_sim.bank import beneficiary_document, payout_document, virtual_account_document
from corridor_sim.books import statement_document
from corridor_sim.idempotency import IdempotentRequest, canonical

router = APIRouter(prefix="/bank/v1", dependencies=[Depends(require_api_key)])


class VirtualAccountBody(Body):
    customer_reference: str = Field(min_length=1)
    asset: str


class BeneficiaryBody(Body):
    customer_reference: str = Field(min_length=1)
    asset: str
    holder_name: str = Field(min_length=1)
    # Judged against the rail's shape, so anything unsuitable is an invalid account.
    account_number: object = None
    routing_number: object = None


class PayoutBody(Body):
    beneficiary_id: str
    asset: str
    amount: object = None
    reference: str = Field(min_length=1)


@router.post("/virtual-accounts")
async def create_virtual_account(request: Request, sim: Sim) -> JSONResponse:
    await sim.faults.before("bank.create_virtual_account")
    payload = await read_object(request)
    body = parse(VirtualAccountBody, payload)
    account, created = sim.bank.create_virtual_account(
        body.customer_reference, body.asset, optional_idempotency(request, payload)
    )
    response = created_or_found(virtual_account_document(account), created)
    await sim.faults.after("bank.create_virtual_account")
    return response


@router.post("/beneficiaries")
async def create_beneficiary(request: Request, sim: Sim) -> JSONResponse:
    await sim.faults.before("bank.create_beneficiary")
    payload = await read_object(request)
    body = parse(BeneficiaryBody, payload)
    beneficiary, created = sim.bank.create_beneficiary(
        customer_reference=body.customer_reference,
        asset=body.asset,
        holder_name=body.holder_name,
        account_number=body.account_number,
        routing_number=body.routing_number,
        idempotent=optional_idempotency(request, payload),
    )
    response = created_or_found(beneficiary_document(beneficiary), created)
    await sim.faults.after("bank.create_beneficiary")
    return response


@router.post("/payouts")
async def create_payout(request: Request, sim: Sim) -> JSONResponse:
    await sim.faults.before("bank.create_payout")
    key = required_idempotency_key(request)
    payload = await read_object(request)
    body = parse(PayoutBody, payload)
    payout, created = sim.bank.create_payout(
        beneficiary_id=body.beneficiary_id,
        asset=body.asset,
        amount=body.amount,
        reference=body.reference,
        idempotent=IdempotentRequest(key, canonical(payload)),
    )
    response = created_or_found(payout_document(payout), created)
    await sim.faults.after("bank.create_payout")
    return response


@router.get("/payouts/{payout_id}")
async def get_payout(payout_id: str, sim: Sim) -> JSONResponse:
    await sim.faults.before("bank.get_payout")
    response = document(payout_document(sim.bank.get_payout(payout_id)))
    await sim.faults.after("bank.get_payout")
    return response


@router.get("/payouts")
async def list_payouts(request: Request, sim: Sim) -> JSONResponse:
    # The list form is the same read by another key, so the same faults reach it.
    await sim.faults.before("bank.get_payout")
    payouts = sim.bank.payouts_by_reference(query(request, "reference"))
    response = document({"payouts": [payout_document(payout) for payout in payouts]})
    await sim.faults.after("bank.get_payout")
    return response


@router.get("/transactions")
async def list_transactions(request: Request, sim: Sim) -> JSONResponse:
    await sim.faults.before("bank.list_transactions")
    asset = query(request, "asset")
    start, end = query_window(request)
    response = document(statement_document(sim.bank.statement(asset, start, end)))
    await sim.faults.after("bank.list_transactions")
    return response

"""``/_control``: how a test or the demo makes the outside world do something.

No part of any provider's API, and unauthenticated. Never reachable from outside a
development or test environment.
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field
from starlette.responses import JSONResponse

from corridor_sim import money
from corridor_sim.api.deps import Body, Sim, document, parse, read_object
from corridor_sim.bank import deposit_document, payout_inspection
from corridor_sim.chaos import Fault
from corridor_sim.clock import format_time
from corridor_sim.fx import rate_document
from corridor_sim.state import SimState
from corridor_sim.webhooks import Behaviour, delivery_document, event_document

router = APIRouter(prefix="/_control")

# One call moves the clock at most this far, which bounds the work a single request can ask
# for. A test that needs longer calls again.
MAX_ADVANCE_SECONDS = 31 * 86_400
MAX_MINED_BLOCKS = 100_000
MAX_FAULT_TIMES = 10_000
MAX_HANG_SECONDS = 3_600
MAX_DUPLICATES = 20


class AdvanceBody(Body):
    seconds: float = Field(ge=0, le=MAX_ADVANCE_SECONDS)


class BankDepositBody(Body):
    virtual_account_id: str = Field(min_length=1)
    amount: object = None
    sender_name: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    # Not in the contract's table. A deposit takes its asset from its virtual account; one
    # that names an account the bank never issued has nowhere to take it from.
    asset: str | None = None


class ReturnBody(Body):
    reason: str = Field(min_length=1)


class ChainDepositBody(Body):
    address: str
    amount: object = None
    from_address: object = None


class MineBody(Body):
    blocks: int = Field(ge=1, le=MAX_MINED_BLOCKS)


class PinBody(Body):
    base: str
    quote: str
    mid: object = None


class FreezeBody(Body):
    frozen: bool


class FaultBody(Body):
    operation: str
    mode: Literal["error", "timeout", "error_after_effect", "timeout_after_effect"]
    times: int = Field(default=1, ge=1, le=MAX_FAULT_TIMES)
    # What an ``error`` answers with. A fault is a refusal or a failure, never a success.
    status: int = Field(default=503, ge=400, le=599)
    # How long a ``timeout`` keeps the caller waiting, in real seconds.
    hang_seconds: float = Field(default=30, ge=0, le=MAX_HANG_SECONDS)


class BehaviourBody(Body):
    """Each switch is optional: one that is left out stays as it was."""

    duplicates: int | None = Field(default=None, ge=0, le=MAX_DUPLICATES)
    drop_types: list[str] | None = None
    hold: bool | None = None
    reverse: bool | None = None


def _clock(sim: SimState) -> dict[str, object]:
    return {"now": format_time(sim.clock.now()), "mode": sim.clock.mode}


@router.post("/reset")
async def reset(request: Request, sim: Sim) -> JSONResponse:
    """Forget everything: the books, the events, the faults, the pins, and the time a
    manual clock had reached."""
    fresh = sim.fresh()
    request.app.state.sim = fresh
    return document(_clock(fresh))


@router.get("/clock")
async def read_clock(sim: Sim) -> JSONResponse:
    return document(_clock(sim))


@router.post("/clock/advance")
async def advance_clock(request: Request, sim: Sim) -> JSONResponse:
    body = parse(AdvanceBody, await read_object(request))
    sim.clock.advance(body.seconds)
    await sim.tick()
    return document(_clock(sim))


# --- the bank ------------------------------------------------------------------------------


@router.post("/bank/deposits")
async def receive_bank_deposit(request: Request, sim: Sim) -> JSONResponse:
    body = parse(BankDepositBody, await read_object(request))
    deposit = sim.bank.receive_deposit(
        virtual_account_id=body.virtual_account_id,
        amount=body.amount,
        sender_name=body.sender_name,
        reference=body.reference,
        asset=body.asset,
    )
    return document(deposit_document(deposit), 201)


@router.post("/bank/deposits/{deposit_id}/return")
async def return_bank_deposit(deposit_id: str, request: Request, sim: Sim) -> JSONResponse:
    body = parse(ReturnBody, await read_object(request))
    return document(deposit_document(sim.bank.return_deposit(deposit_id, body.reason)))


@router.get("/bank/payouts")
async def inspect_bank_payouts(sim: Sim) -> JSONResponse:
    return document(
        {
            "payouts": [
                payout_inspection(payout, sim.bank.beneficiary(payout.beneficiary_id))
                for payout in sim.bank.payouts()
            ]
        }
    )


@router.get("/bank/deposits")
async def inspect_bank_deposits(sim: Sim) -> JSONResponse:
    return document({"deposits": [deposit_document(deposit) for deposit in sim.bank.deposits()]})


@router.get("/bank/balances")
async def inspect_bank_balances(sim: Sim) -> JSONResponse:
    return document(
        {
            "balances": {
                asset: money.format_amount(balance, asset)
                for asset, balance in sim.bank.balances().items()
            }
        }
    )


# --- the custodian and its chain -------------------------------------------------------------


@router.post("/custody/deposits")
async def detect_chain_deposit(request: Request, sim: Sim) -> JSONResponse:
    body = parse(ChainDepositBody, await read_object(request))
    deposit = sim.custody.detect_deposit(
        address=body.address, amount=body.amount, from_address=body.from_address
    )
    return document(sim.custody.deposit_document(deposit), 201)


@router.post("/custody/deposits/{deposit_id}/drop")
async def drop_chain_deposit(deposit_id: str, sim: Sim) -> JSONResponse:
    return document(sim.custody.deposit_document(sim.custody.drop_deposit(deposit_id)))


@router.post("/chain/mine")
async def mine_blocks(request: Request, sim: Sim) -> JSONResponse:
    body = parse(MineBody, await read_object(request))
    sim.custody.mine(body.blocks)
    await sim.tick()
    return document({"height": sim.custody.height})


@router.get("/custody/withdrawals")
async def inspect_custody_withdrawals(sim: Sim) -> JSONResponse:
    return document(
        {
            "withdrawals": [
                sim.custody.withdrawal_inspection(withdrawal)
                for withdrawal in sim.custody.withdrawals()
            ]
        }
    )


@router.get("/custody/deposits")
async def inspect_custody_deposits(sim: Sim) -> JSONResponse:
    return document(
        {"deposits": [sim.custody.deposit_document(deposit) for deposit in sim.custody.deposits()]}
    )


@router.get("/custody/balances")
async def inspect_custody_balances(sim: Sim) -> JSONResponse:
    return document(
        {
            "balances": {
                asset: money.format_amount(balance, asset)
                for asset, balance in sim.custody.balances().items()
            }
        }
    )


# --- the rate source -------------------------------------------------------------------------


@router.post("/fx/rates")
async def pin_rate(request: Request, sim: Sim) -> JSONResponse:
    body = parse(PinBody, await read_object(request))
    return document(rate_document(sim.fx.pin(body.base, body.quote, body.mid)))


@router.post("/fx/freeze")
async def freeze_rates(request: Request, sim: Sim) -> JSONResponse:
    body = parse(FreezeBody, await read_object(request))
    sim.fx.freeze(body.frozen, sim.clock.now())
    return document({"frozen": sim.fx.frozen, "as_of": format_time(sim.fx.as_of)})


# --- faults ----------------------------------------------------------------------------------


def _fault(fault: Fault) -> dict[str, object]:
    return {
        "operation": fault.operation,
        "mode": fault.mode,
        "times": fault.times,
        "status": fault.status,
        "hang_seconds": fault.hang_seconds,
    }


@router.post("/faults")
async def inject_fault(request: Request, sim: Sim) -> JSONResponse:
    body = parse(FaultBody, await read_object(request))
    sim.faults.add(
        body.operation,
        body.mode,
        times=body.times,
        status=body.status,
        hang_seconds=body.hang_seconds,
    )
    return document({"faults": [_fault(fault) for fault in sim.faults.queued()]}, 201)


@router.delete("/faults")
async def clear_faults(sim: Sim) -> JSONResponse:
    sim.faults.clear()
    return document({"faults": []})


# --- webhooks --------------------------------------------------------------------------------


def _behaviour(behaviour: Behaviour) -> dict[str, object]:
    return {
        "duplicates": behaviour.duplicates,
        "drop_types": sorted(behaviour.drop_types),
        "hold": behaviour.hold,
        "reverse": behaviour.reverse,
    }


@router.post("/webhooks/behaviour")
async def set_webhook_behaviour(request: Request, sim: Sim) -> JSONResponse:
    body = parse(BehaviourBody, await read_object(request))
    behaviour = sim.webhooks.behaviour
    if body.duplicates is not None:
        behaviour.duplicates = body.duplicates
    if body.drop_types is not None:
        behaviour.drop_types = frozenset(body.drop_types)
    if body.hold is not None:
        behaviour.hold = body.hold
    if body.reverse is not None:
        behaviour.reverse = body.reverse
    return document(_behaviour(behaviour))


@router.post("/webhooks/deliver")
async def deliver_webhooks(sim: Sim) -> JSONResponse:
    deliveries = await sim.deliver()
    return document({"deliveries": [delivery_document(delivery) for delivery in deliveries]})


@router.get("/webhooks/events")
async def inspect_webhook_events(sim: Sim) -> JSONResponse:
    return document({"events": [event_document(event) for event in sim.webhooks.events]})

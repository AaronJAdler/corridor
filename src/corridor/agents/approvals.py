"""Approval requests: what an agent asked to move above its threshold, and its owner's answer.

Asking moves nothing. The request records what the agent wanted and the id the movement
will have if it is ever made. The owner decides from their own session; no agent's key
decides anything here, whatever its scopes.

Approving makes the movement in the transaction that records the approval, as the agent
acting for its owner, so that it is limited, counted and audited as the agent's. Three
things keep it from being made twice. The request's row is locked and must still be
pending. Its status changes in the transaction that moves the money. And the movement is
made under the id chosen when the request was, which the ledger posts once and never again.

Every function takes the caller's session and runs inside the caller's transaction.
Nothing here commits.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import RowMapping, Table, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, payments, risk
from corridor.agents.errors import (
    AgentNotActive,
    ApprovalAlreadyDecided,
    ApprovalExpired,
    ApprovalLimitReached,
    ApprovalNotFound,
)
from corridor.agents.models import AgentApprovalRequestRow
from corridor.agents.policy import check_policy
from corridor.agents.service import status_of
from corridor.agents.types import (
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStatus,
    TransferIntent,
    WithdrawalIntent,
)
from corridor.identity import Principal, Scope
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import advisory_xact_lock, lock_key
from corridor.platform.errors import DomainError
from corridor.platform.ids import new_id
from corridor.platform.pagination import (
    DEFAULT_LIMIT,
    InvalidCursor,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)

_requests = cast(Table, AgentApprovalRequestRow.__table__)

CURSOR_KIND: Final = "approvals"

# The namespace of the per-agent lock that makes the count of an agent's waiting requests
# and the insert of one more a single step.
_COUNT_LOCK: Final = "approval_requests"

# How long an owner has to decide. After it the request can no longer be approved.
APPROVAL_TTL: Final = timedelta(hours=24)

# How each ending is written to the audit log. A request that failed was approved: the
# owner said yes, and the movement was refused.
_ACTION_OF: Final[dict[str, str]] = {
    "executed": "agent.approval_approved",
    "failed": "agent.approval_approved",
    "rejected": "agent.approval_rejected",
    "expired": "agent.approval_expired",
}
_OUTCOME_OF: Final[dict[str, audit.Outcome]] = {
    "executed": "success",
    "failed": "failed",
    "rejected": "success",
    "expired": "denied",
}

_SCOPE_OF: Final = {
    "transfer": Scope.TRANSFERS_CREATE,
    "withdrawal": Scope.WITHDRAWALS_CREATE,
}


async def request_approval(
    session: AsyncSession,
    principal: Principal,
    intent: TransferIntent | WithdrawalIntent,
    *,
    settings: Settings,
) -> ApprovalRequest:
    """Record that an agent asked for a movement its owner has to approve. Nothing moves.

    The caller has asked ``check_policy`` and been told the movement requires approval.
    An agent has at most ``max_pending_approvals_per_agent`` requests waiting at once, so
    that one cannot bury its owner in questions.
    """
    agent_id = principal.agent_id
    if agent_id is None:
        raise ValueError("only an agent's movement waits for approval")
    identity.require_scope(principal, _SCOPE_OF[intent.kind])
    # One at a time for an agent, so that two requests cannot both take the last place.
    await advisory_xact_lock(session, [lock_key(_COUNT_LOCK, agent_id)])
    waiting = await session.execute(
        select(func.count())
        .select_from(_requests)
        .where(
            _requests.c.agent_id == agent_id,
            _requests.c.status == "pending",
            # One past its time can no longer be approved, and is not waiting for anybody.
            _requests.c.expires_at > utcnow(),
        )
    )
    if waiting.scalar_one() >= settings.max_pending_approvals_per_agent:
        raise ApprovalLimitReached(settings.max_pending_approvals_per_agent)
    if isinstance(intent, TransferIntent):
        # Whoever the agent named is found now and kept by id. Nobody there is told as it
        # would be for a transfer, and no owner is asked to approve paying nobody.
        payee = await identity.find_user(session, intent.recipient)
        if payee is None:
            raise payments.RecipientNotFound
        intent = TransferIntent(
            recipient=str(payee.id), asset=intent.asset, amount=intent.amount, memo=intent.memo
        )

    now = utcnow()
    approval_id = new_id()
    row = {
        "id": approval_id,
        "agent_id": agent_id,
        "owner_user_id": principal.user_id,
        "kind": intent.kind,
        "request": _stored(intent),
        "status": "pending",
        "movement_id": new_id(),
        "failure_code": None,
        "expires_at": now + APPROVAL_TTL,
        "decided_at": None,
        "created_at": now,
    }
    await session.execute(insert(_requests).values(row))
    await audit.record(
        session,
        actor=audit.Actor.agent(agent_id),
        action="agent.approval_requested",
        principal_id=principal.user_id,
        resource_type="approval_request",
        resource_id=approval_id,
        details=_details(row),
    )
    return _request(row, now)


async def list_approvals(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[ApprovalRequest]:
    """One page of what the agents of the principal's user asked for, newest first."""
    identity.require_user_session(principal)
    limit = clamp_limit(limit)
    scope = str(principal.user_id)
    query = select(_requests).where(_requests.c.owner_user_id == principal.user_id)
    if cursor is not None:
        query = query.where(_requests.c.id < _position_of(cursor, scope))
    rows = await session.execute(query.order_by(_requests.c.id.desc()).limit(limit + 1))
    found = list(rows.mappings())
    shown = found[:limit]
    now = utcnow()
    return Page(
        items=tuple(_request(row, now) for row in shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=scope, position=str(shown[-1]["id"]))
            if len(found) > limit
            else None
        ),
    )


async def approve(
    session: AsyncSession, principal: Principal, approval_id: uuid.UUID, *, settings: Settings
) -> ApprovalOutcome:
    """Approve a request and make its movement, both in the caller's one transaction.

    A request that is another user's, or already decided, is refused by raising: there is
    nothing to record. One that has expired, or whose movement is refused now, is recorded
    as that and the refusal is returned in the outcome, for the caller to raise once the
    record has committed.

    The policy is asked again, because it may have changed since the agent asked, and so
    are the limits, by the movement itself. Only the threshold is not: this is the
    approval it asked for.
    """
    identity.require_user_session(principal)
    # Before the request's row, as the lock order has it: the movement takes these locks
    # again, and by then the row is held. The owner's, and for a transfer the recipient's
    # too, which the transfer takes so that it cannot pay into an account being closed.
    await advisory_xact_lock(
        session,
        [
            lock_key(risk.MONEY_OUT_LOCK, user_id)
            for user_id in (principal.user_id, await _payee_of(session, principal, approval_id))
            if user_id is not None
        ],
    )
    row = await _lock_pending(session, principal, approval_id)
    now = utcnow()
    if row["expires_at"] <= now:
        expired = await _decide(session, principal, row, "expired", now)
        return ApprovalOutcome(request=expired, refusal=ApprovalExpired())

    intent = _intent(row)
    # The agent acting for its owner, with the one scope this movement needs: what it
    # moves is limited and recorded as the agent's, as if it had been allowed at once.
    agent = Principal.for_agent(row["owner_user_id"], row["agent_id"], {_SCOPE_OF[intent.kind]})
    try:
        # A savepoint, so that a refusal undoes whatever the movement wrote before it was
        # refused and leaves the request to be recorded as failed.
        async with session.begin_nested():
            if await status_of(session, row["agent_id"]) != "active":
                raise AgentNotActive
            # Asked for what it refuses. That the amount is above the threshold is why
            # the request exists, and the owner has now answered that.
            await check_policy(
                session, agent, intent.kind, intent.asset, intent.amount, intent.destination
            )
            await _move(session, agent, intent, row["movement_id"], settings)
    except DomainError as refusal:
        failed = await _decide(session, principal, row, "failed", now, failure_code=refusal.code)
        return ApprovalOutcome(request=failed, refusal=refusal)
    return ApprovalOutcome(request=await _decide(session, principal, row, "executed", now))


async def reject(
    session: AsyncSession, principal: Principal, approval_id: uuid.UUID
) -> ApprovalRequest:
    """Refuse a request for good. Nothing was moved, and now nothing will be."""
    identity.require_user_session(principal)
    row = await _lock_pending(session, principal, approval_id)
    return await _decide(session, principal, row, "rejected", utcnow())


async def _move(
    session: AsyncSession,
    agent: Principal,
    intent: TransferIntent | WithdrawalIntent,
    movement_id: uuid.UUID,
    settings: Settings,
) -> None:
    if isinstance(intent, TransferIntent):
        await payments.create_transfer(
            session,
            agent,
            transfer_id=movement_id,
            recipient=intent.recipient,
            asset=intent.asset,
            amount=intent.amount,
            memo=intent.memo,
            settings=settings,
        )
    else:
        await payments.request_withdrawal(
            session,
            agent,
            withdrawal_id=movement_id,
            asset=intent.asset,
            amount=intent.amount,
            beneficiary_id=intent.beneficiary_id,
            to_address=intent.to_address,
            settings=settings,
        )


async def _payee_of(
    session: AsyncSession, principal: Principal, approval_id: uuid.UUID
) -> uuid.UUID | None:
    """The user a requested transfer would pay, if the request is the principal's and is
    for a transfer to someone who can be found.

    Read without a lock, only to learn whose money-out lock comes before the request's
    row. What a request asks for is written once, with the request.
    """
    found = await session.execute(
        select(_requests.c.kind, _requests.c.request).where(
            _requests.c.id == approval_id, _requests.c.owner_user_id == principal.user_id
        )
    )
    row = found.mappings().one_or_none()
    if row is None or row["kind"] != "transfer":
        return None
    payee = await identity.find_user(session, row["request"]["recipient"])
    return payee.id if payee is not None else None


async def _lock_pending(
    session: AsyncSession, principal: Principal, approval_id: uuid.UUID
) -> RowMapping:
    """The principal's user's request, locked for the rest of the transaction and still to
    be decided. Locked only if it is theirs: nobody holds a lock on someone else's row."""
    found = await session.execute(
        select(_requests)
        .where(_requests.c.id == approval_id, _requests.c.owner_user_id == principal.user_id)
        .with_for_update()
    )
    row = found.mappings().one_or_none()
    if row is None:
        raise ApprovalNotFound
    if row["status"] != "pending":
        raise ApprovalAlreadyDecided
    return row


async def _decide(
    session: AsyncSession,
    principal: Principal,
    row: RowMapping,
    status: ApprovalStatus,
    now: datetime,
    *,
    failure_code: str | None = None,
) -> ApprovalRequest:
    """Record what became of a request whose row the caller has locked."""
    updated = await session.execute(
        update(_requests)
        .where(_requests.c.id == row["id"])
        .values(status=status, decided_at=now, failure_code=failure_code)
        .returning(_requests)
    )
    decided = updated.mappings().one()
    details = _details(decided)
    if failure_code is not None:
        details["failure_code"] = failure_code
    await audit.record(
        session,
        actor=audit.Actor.user(principal.actor_id),
        action=_ACTION_OF[status],
        outcome=_OUTCOME_OF[status],
        principal_id=principal.user_id,
        resource_type="approval_request",
        resource_id=decided["id"],
        details=details,
    )
    return _request(decided, now)


def _details(row: RowMapping | dict[str, Any]) -> dict[str, Any]:
    """What every audit event about a request says: whose agent, what it would move, and
    to whom."""
    stored = row["request"]
    # Where the money would go, under the name the request itself has for it: the user a
    # transfer pays, or the beneficiary or the address a withdrawal is sent to.
    destination = {
        name: stored[key]
        for name, key in (
            ("recipient_id", "recipient"),
            ("beneficiary_id", "beneficiary_id"),
            ("to_address", "to_address"),
        )
        if stored.get(key) is not None
    }
    return {
        "agent_id": str(row["agent_id"]),
        "owner_user_id": str(row["owner_user_id"]),
        "kind": row["kind"],
        "movement_id": str(row["movement_id"]),
        "asset": stored["asset"],
        "amount": stored["amount"],
        **destination,
    }


def _stored(intent: TransferIntent | WithdrawalIntent) -> dict[str, Any]:
    """An intent as it is kept. Minor units as a string: a JSON number would lose
    precision above 2^53."""
    if isinstance(intent, TransferIntent):
        return {
            "recipient": intent.recipient,
            "asset": intent.asset,
            "amount": str(intent.amount),
            "memo": intent.memo,
        }
    return {
        "asset": intent.asset,
        "amount": str(intent.amount),
        "beneficiary_id": None if intent.beneficiary_id is None else str(intent.beneficiary_id),
        "to_address": intent.to_address,
    }


def _intent(row: RowMapping | dict[str, Any]) -> TransferIntent | WithdrawalIntent:
    stored = row["request"]
    if row["kind"] == "transfer":
        return TransferIntent(
            recipient=stored["recipient"],
            asset=stored["asset"],
            amount=int(stored["amount"]),
            memo=stored["memo"],
        )
    beneficiary = stored["beneficiary_id"]
    return WithdrawalIntent(
        asset=stored["asset"],
        amount=int(stored["amount"]),
        beneficiary_id=None if beneficiary is None else uuid.UUID(beneficiary),
        to_address=stored["to_address"],
    )


def _request(row: RowMapping | dict[str, Any], now: datetime) -> ApprovalRequest:
    status: ApprovalStatus = row["status"]
    if status == "pending" and row["expires_at"] <= now:
        # Nothing has to run for a request to expire: the time passing is enough.
        status = "expired"
    return ApprovalRequest(
        id=row["id"],
        agent_id=row["agent_id"],
        owner_user_id=row["owner_user_id"],
        intent=_intent(row),
        status=status,
        movement_id=row["movement_id"],
        failure_code=row["failure_code"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        created_at=row["created_at"],
    )


def _position_of(cursor: str, scope: str) -> uuid.UUID:
    position = decode_cursor(cursor, kind=CURSOR_KIND, scope=scope)
    if not isinstance(position, str):
        raise InvalidCursor
    try:
        return uuid.UUID(position)
    except ValueError:
        raise InvalidCursor from None

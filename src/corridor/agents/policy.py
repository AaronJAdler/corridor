"""Spend policy: what an agent may move, whom it may pay, and what must wait for its owner.

A policy is its owner's alone to set, from their own session. It is read from the database
on every request an agent makes, so a change takes effect with the next one.

The two caps are enforced where the money moves: setting a policy hands them to ``risk``
as the agent's limit, and ``risk.authorize`` counts every movement of the agent against
them under the owner's money-out lock. What is decided here is what ``risk`` has no way to
know: whether the destination is one the owner named, and whether the amount is one the
owner wants to see first.

Every function takes the caller's session and runs inside the caller's transaction.
Nothing here commits.
"""

import uuid
from collections.abc import Iterable
from typing import Any, Final, cast

from sqlalchemy import RowMapping, Table, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, risk
from corridor.agents.errors import AgentRevoked, InvalidPolicy, RecipientNotAllowed
from corridor.agents.models import AgentAllowedRecipientRow, AgentPolicyRow
from corridor.agents.service import own_agent_status
from corridor.agents.types import (
    AllowedRecipient,
    Policy,
    PolicyDecision,
    PolicyKind,
    Recipient,
)
from corridor.identity import Principal
from corridor.platform.clock import utcnow
from corridor.platform.money import MAX_MINOR_UNITS, format_amount

_policies = cast(Table, AgentPolicyRow.__table__)
_recipients = cast(Table, AgentAllowedRecipientRow.__table__)

# Far more than a person lists by hand. It bounds what one policy can make every request
# of its agent read.
MAX_ALLOWED_RECIPIENTS: Final = 100

_ALLOW: Final = PolicyDecision(outcome="allow")
_REQUIRES_APPROVAL: Final = PolicyDecision(outcome="requires_approval")


async def set_policy(
    session: AsyncSession,
    principal: Principal,
    agent_id: uuid.UUID,
    *,
    per_tx_usd: int | None,
    daily_usd: int | None,
    approval_threshold_usd: int | None,
    any_recipient: bool = False,
    allowed_recipients: Iterable[AllowedRecipient] = (),
) -> Policy:
    """Replace an agent's policy with this one, whole.

    The amounts are whole US cents. ``None`` sets no limit of that sort, which is a
    decision and not a default: all three have to be given. Whoever is not named in
    ``allowed_recipients`` cannot be paid, unless ``any_recipient`` says anyone can.
    """
    identity.require_user_session(principal)
    for name, amount in (
        ("per_tx_usd", per_tx_usd),
        ("daily_usd", daily_usd),
        ("approval_threshold_usd", approval_threshold_usd),
    ):
        if amount is not None and (
            isinstance(amount, bool)
            or not isinstance(amount, int)
            or not 0 <= amount <= MAX_MINOR_UNITS
        ):
            raise InvalidPolicy(f"{name} is an amount in US dollars, zero or more.", field=name)
    # In a fixed order, so that the same list is always stored and shown the same way.
    allowed = sorted(set(allowed_recipients), key=lambda r: (r.kind, r.target_id))
    if len(allowed) > MAX_ALLOWED_RECIPIENTS:
        raise InvalidPolicy(
            f"A policy names at most {MAX_ALLOWED_RECIPIENTS} recipients.",
            field="allowed_recipients",
        )

    # The agent's row is locked first, so that two changes of one policy are made one
    # after the other and the list of recipients is never a mixture of both.
    if await own_agent_status(session, principal, agent_id, lock=True) == "revoked":
        raise AgentRevoked

    now = utcnow()
    values = {
        "per_tx_usd": per_tx_usd,
        "daily_usd": daily_usd,
        "approval_threshold_usd": approval_threshold_usd,
        "any_recipient": any_recipient,
        "updated_at": now,
    }
    await session.execute(
        insert(_policies)
        .values(agent_id=agent_id, **values)
        .on_conflict_do_update(index_elements=[_policies.c.agent_id], set_=values)
    )
    await session.execute(delete(_recipients).where(_recipients.c.agent_id == agent_id))
    if allowed:
        await session.execute(
            insert(_recipients),
            [
                {"agent_id": agent_id, "kind": recipient.kind, "target_id": recipient.target_id}
                for recipient in allowed
            ],
        )
    # The caps go to risk, which enforces them where the money moves. A rule for every
    # kind of movement, so that a conversion counts against the day as a payment does.
    await risk.set_limit(
        session,
        scope="agent",
        agent_id=agent_id,
        kind=None,
        per_tx_usd=per_tx_usd,
        daily_usd=daily_usd,
    )
    await audit.record(
        session,
        actor=audit.Actor.user(principal.actor_id),
        action="agent.policy_changed",
        principal_id=principal.user_id,
        resource_type="agent",
        resource_id=agent_id,
        details={
            "agent_id": str(agent_id),
            "per_tx_usd": _dollars(per_tx_usd),
            "daily_usd": _dollars(daily_usd),
            "approval_threshold_usd": _dollars(approval_threshold_usd),
            "any_recipient": any_recipient,
            "allowed_recipients": [
                {"kind": recipient.kind, "id": str(recipient.target_id)} for recipient in allowed
            ],
        },
    )
    return Policy(
        agent_id=agent_id,
        per_tx_usd=per_tx_usd,
        daily_usd=daily_usd,
        approval_threshold_usd=approval_threshold_usd,
        any_recipient=any_recipient,
        allowed_recipients=tuple(allowed),
        updated_at=now,
    )


async def get_policy(session: AsyncSession, principal: Principal, agent_id: uuid.UUID) -> Policy:
    """The policy of one of the principal's user's agents, as it stands."""
    identity.require_user_session(principal)
    await own_agent_status(session, principal, agent_id)
    return await _policy_of(session, agent_id)


async def check_policy(
    session: AsyncSession,
    principal: Principal,
    kind: PolicyKind,
    asset: str,
    amount: int,
    recipient: Recipient | None = None,
) -> PolicyDecision:
    """Say whether an agent's policy lets this movement go ahead now.

    It raises the refusal if the destination is not one the agent may pay, or the amount
    is more than its policy allows at once. Otherwise it answers ``allow``, or
    ``requires_approval`` for an amount above the owner's threshold, in which case the
    caller moves nothing and asks for approval instead. For a movement the owner has
    approved, the caller asks again for the refusals and has no more use for the answer.

    A user acting for themselves has no policy and is always allowed. The agent's limit
    over 24 hours is not checked here: ``risk.authorize`` does that, under the lock that
    makes the sum exact, when the movement is made.
    """
    agent_id = principal.agent_id
    if agent_id is None:
        return _ALLOW
    policy = await _policy_of(session, agent_id)

    # A conversion is between the owner's own wallets: there is nobody to pay.
    if (
        kind != "conversion"
        and not policy.any_recipient
        and not await _may_pay(session, policy, recipient)
    ):
        raise RecipientNotAllowed

    value = await risk.usd_value(session, asset, amount)
    if policy.per_tx_usd is not None and value > policy.per_tx_usd:
        # Risk would refuse it too when the movement is made. Refused here as well, so
        # that the owner is never asked to approve what could not be carried out.
        raise risk.LimitExceeded(
            limit="per_transaction", scope="agent", usd_cents=policy.per_tx_usd
        )
    if policy.approval_threshold_usd is not None and value > policy.approval_threshold_usd:
        return _REQUIRES_APPROVAL
    return _ALLOW


async def _may_pay(session: AsyncSession, policy: Policy, recipient: Recipient | None) -> bool:
    """Whether the destination is on the policy's list."""
    if recipient is None or not policy.allowed_recipients:
        return False
    target: uuid.UUID | None = None
    if recipient.kind == "user":
        # Found as the transfer itself will find them, so that a handle, an email address
        # and an id that name one person are one recipient.
        user = await identity.find_user(session, recipient.value)
        target = None if user is None else user.id
    elif recipient.kind == "beneficiary":
        try:
            target = uuid.UUID(recipient.value)
        except ValueError:
            target = None
    if target is None:
        return False
    return any(
        allowed.kind == recipient.kind and allowed.target_id == target
        for allowed in policy.allowed_recipients
    )


async def _policy_of(session: AsyncSession, agent_id: uuid.UUID) -> Policy:
    """An agent's policy. One that was never set allows nobody to be paid."""
    found = await session.execute(select(_policies).where(_policies.c.agent_id == agent_id))
    row: RowMapping | dict[str, Any] | None = found.mappings().one_or_none()
    if row is None:
        return Policy(
            agent_id=agent_id,
            per_tx_usd=None,
            daily_usd=None,
            approval_threshold_usd=None,
            any_recipient=False,
            allowed_recipients=(),
            updated_at=None,
        )
    listed = await session.execute(
        select(_recipients.c.kind, _recipients.c.target_id)
        .where(_recipients.c.agent_id == agent_id)
        .order_by(_recipients.c.kind, _recipients.c.target_id)
    )
    return Policy(
        agent_id=agent_id,
        per_tx_usd=row["per_tx_usd"],
        daily_usd=row["daily_usd"],
        approval_threshold_usd=row["approval_threshold_usd"],
        any_recipient=row["any_recipient"],
        allowed_recipients=tuple(
            AllowedRecipient(kind=kind, target_id=target_id) for kind, target_id in listed
        ),
        updated_at=row["updated_at"],
    )


def _dollars(cents: int | None) -> str | None:
    return None if cents is None else format_amount(cents, "USD")

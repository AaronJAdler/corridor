# 0013. Agents get scoped keys, a deny-by-default spend policy and an approval threshold

**Status:** accepted

## Context

Users want software, including AI agents, to act on their wallet. Giving the software the
user's password or session gives it everything, and makes its actions indistinguishable
from the user's in the audit log. Telling the agent in its instructions what it may spend
is not a control: instructions can be ignored or overridden.

## Decision

An agent is a separate principal owned by a user.

- It authenticates with its own API key. Only an HMAC of the key's secret is stored.
- A key carries scopes from a fixed list. No key can hold the scope of a user session, and
  an agent is never an administrator.
- The owner sets a policy: a per-transaction cap, a 24-hour cap, an approval threshold, and
  who the agent may pay. The policy is read from the database on every request.
- **Deny by default.** An agent with no policy can pay nobody and cannot convert. A policy
  with no recipients and without "any recipient" can pay nobody.
- An amount above the threshold moves nothing. The API records an approval request and the
  owner decides from their own session. Approval makes the movement as the agent, under an
  id chosen when the request was made, in the transaction that records the approval.
- The caps are copied into `risk_limits` and enforced by `risk` with the owner's own limits.
- Managing agents, keys and policies, and deciding approvals, needs the owner's own session.

## Consequences

- What an agent may do is enforced by the server on every request, whatever the agent was
  told.
- A new agent can do nothing with money until its owner has made a decision.
- Every movement is attributed to the agent and to the user it acted for.
- A leaked key is bounded by its scopes and its policy, and can be revoked at once.
- An approved request cannot be executed twice: the row is locked, its status changes with
  the movement, and the ledger posts a movement id once.
- Each agent request costs an authentication transaction and a policy read.
- A conversion is checked against the per-transaction cap only and is never sent for
  approval, because it has no recipient. That is an accepted limit.

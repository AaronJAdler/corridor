# 0016. Simulated providers in a separate package, behind a written contract

**Status:** accepted

## Context

No real bank or custodian is available to this build, and the interesting behaviour of a
payments system is in the failure cases: a timeout after the provider acted, a webhook
that arrives twice, late or never. Mocks at the function level cannot produce those,
because they do not have state, time or a network between them and the caller.

## Decision

- Corridor reaches providers through ports (`Protocol` classes) with HTTP adapters.
- The providers are simulated by `corridor_sim`, a separate package and a separate FastAPI
  application with its own books, clock, webhook delivery and fault injection.
- The contract between the two is a document, [provider-api.md](../provider-api.md).
  `corridor` and `corridor_sim` are forbidden from importing each other by an
  `import-linter` contract, so neither can lean on the other's code.
- The simulator has control endpoints that make the outside world act: receive a deposit,
  drop a webhook, fail after the effect, advance time.
- It refuses to start outside a development or test environment, and refuses to listen
  beyond the local machine unless its control endpoints have a token.

## Consequences

- The failure-injection suite runs real HTTP exchanges against a provider that keeps its
  own count of payouts, so "exactly one payout" is checked on the provider's side.
- A real provider is a new adapter against the same ports. Nothing above the adapter
  changes.
- The contract can drift from either side. Tests exist for both sides, and the document
  has to be kept in step by hand.
- The simulator is shipped in the same wheel and image as Corridor. It is inert unless
  started, and it cannot start in production.
- The adapters have never met a real provider's quirks. That remains unproved.

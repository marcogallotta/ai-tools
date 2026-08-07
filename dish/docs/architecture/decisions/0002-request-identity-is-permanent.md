# ADR: Request identity is permanent

Status: Accepted

## Read this when

Read this when changing admission, replay, retries, lost-response recovery, request storage, or mutation target selection.

## Scope

A mutation request ID identifies one admitted consequential request and remains bound to that request for its lifetime.

## Authoritative implementation

Current implementation anchors live in [Request replay and idempotency](../request-replay-and-idempotency.md). Exact module locations may change without changing this decision.

## Actors, processes, and stores

The binding covers the caller, admitted command, canonical arguments, authenticated owner/run identity, and the original mutation target selected for that request.

## Authority and data ownership

The durable request record owns request identity and replay outcome. Nearby current state does not replace the target bound by the admitted request.

## Invariants

- Exact reuse replays the same request/outcome; incompatible reuse conflicts.
- Replay cannot silently retarget a later operation, cycle, lease, proposal, or other nearby object.
- Lost-response and uncertainty recovery preserve the same request ID.
- Minting a new request ID is not a way to escape uncertainty about an already-admitted mutation.

## Process and transaction boundaries

Any process or transaction boundary that admits, executes, persists, or reconstructs a request must preserve the same identity and bound target.

## Normal flow

Admit and reserve the request identity, bind its target, execute the mutation once, persist its authoritative outcome, and reconstruct that outcome on exact replay.

## Failure, replay, recovery, and concurrency

Concurrent duplicates converge on the durable request. Failure or uncertainty does not grant permission to choose a new identity or a fresh target for the same mutation.

## Change routing

Refactors may move request/replay code while preserving these semantics. Changes to what a request ID binds or how retargeting works require explicit architectural review.

## Proving tests

Tests should prove exact replay, incompatible reuse conflict, lost-response recovery, concurrency convergence, and no silent retargeting. Structural tests are appropriate where structure itself protects these guarantees.

## Current debt and temporary compatibility

Legacy and PostgreSQL adapters may currently represent the request lifecycle differently; compatibility must preserve this identity contract until those paths converge.

## Related documents

- [Request replay and idempotency](../request-replay-and-idempotency.md)
- [Operations, leases, and fencing](../operations-leases-and-fencing.md)

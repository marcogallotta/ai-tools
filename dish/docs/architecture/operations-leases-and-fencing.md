# Operations, leases, and fencing

## Read this when

Read this when changing execution ownership, leases, reclaim, abandonment, stale workers, process locks, or recovery from interrupted operations.

## Scope

This document records the safety goals of execution ownership. It does not elevate every current reclaim/abandonment branch into permanent product semantics.

## Authoritative implementation

Current anchors include `dish_service/leases.py`, `dish_service/lease_requests.py`, `dish_tool/operation_execution.py`, abandonment/recovery modules, and PostgreSQL claim/fencing code under `dish_pg/`.

## Actors, processes, and stores

Service processes, worker processes, operations, leases, and claims cooperate to ensure one valid executor for consequential work.

## Authority and data ownership

Durable claim/lease identity determines which executor may continue a mutation. Recovery may transfer execution ownership only through a recorded, fenced mechanism.

## Invariants

- A stale or superseded executor cannot continue writing authoritative outcomes.
- Reclaim/recovery does not silently duplicate an uncertain external effect.
- Recovery targets durable identity/evidence rather than an ambiguous "nearby" operation.
- Terminal evidence is preserved even if later cleanup fails.

## Process and transaction boundaries

Lease/claim mechanisms differ across SQLite/process locks/PostgreSQL rows. The architecture requires enforceable fencing at the actual concurrency boundary; it does not require one universal lease implementation.

## Normal flow

Acquire/validate execution ownership, perform bounded work, renew when appropriate, persist completion, release/expire ownership, and recover explicitly if the executor dies.

## Failure, replay, recovery, and concurrency

Ordinary lease reclaim, abandonment, succession, and reconciliation are current mechanisms. Their exact division is implementation behavior unless an explicit ADR promotes a rule to a durable decision.

## Change routing

Change the mechanism where execution ownership is actually enforced. User-facing recovery guidance may live in admin/agent/frontend surfaces as long as those surfaces do not bypass fencing.

## Proving tests

Relevant evidence includes lease/reclaim, database concurrency, operation execution, process-failure, and native PostgreSQL fencing tests.

## Current debt and temporary compatibility

Legacy and PostgreSQL ownership mechanisms coexist. Stage B is expected to reduce overlap. Avoid documenting a temporary recovery branch as timeless architecture solely because it exists today.

## Related documents

- [Request replay and idempotency](request-replay-and-idempotency.md)
- [Workflow and human review](workflow-and-human-review.md)
- [PostgreSQL runtime](postgresql-runtime.md)

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
- Same-run expired-lease recovery and different-run ownership transfer are distinct operations.
- Different-run safe reclaim is allowed only from a mechanically clean inactive frontier; it fences
  the source and creates an exact linked successor rather than retargeting the old operation.
- Recovery targets durable identity/evidence rather than an ambiguous "nearby" operation.
- Terminal evidence is preserved even if later cleanup fails.
- A recovery path that reports a usable continuation must also settle any recoverable execution evidence that the same fresh inspection has mechanically proved terminal; a subsequent inspect must not require the same recovery again.

## Process and transaction boundaries

Lease/claim mechanisms differ across SQLite/process locks/PostgreSQL rows. The architecture requires enforceable fencing at the actual concurrency boundary; it does not require one universal lease implementation.

## Normal flow

Acquire/validate execution ownership, perform bounded work, renew when appropriate, persist completion, release/expire ownership, and recover explicitly if the executor dies.

## Failure, replay, recovery, and concurrency

Current SQLite/service behavior distinguishes three paths: `recover-lease` for the same durable run,
agent-callable `safe-reclaim` for a different run only when the committed clean-frontier predicate
passes, and formal abandonment/reconciliation for genuine recovery risk. Safe reclaim records the
source lease/owner/run and successor lineage durably and forbids the replaced run from claiming that
successor.

For `safe-reclaim`, the first eligibility result is advisory. The writer transaction reruns the complete
predicate while holding the SQLite writer lock, including stage/frontier, Verification-cycle, lease
context, unresolved-work, confirmed-baseline, and live task identity/placement checks. Any mismatch
observed by that commit-time pass rolls back before the source operation is terminalized or a successor
is created. Because Asana and SQLite cannot share a transaction and Asana exposes no conditional-read
lock, an external task edit can still race in the narrow interval after the final Asana read returns and
before the SQLite commit completes. The prepared successor remains fail-closed on live drift at claim;
this residual cross-store race must not be described as eliminated.

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

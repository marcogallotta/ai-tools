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
- Marco kill/replace is an explicit durable revocation of one exact `(operation_id, owner_id, run_id)`. Historical lease state may identify which exact run Marco is killing after its lease was normally released, but history never implies revocation by itself. Ordinary lease loss remains recoverable until an explicit revocation exists; after revocation, that exact run may never acquire, reacquire, renew, or otherwise re-establish mutation authority for that operation.
- Revocation is checked at the canonical mutation-claim writer boundary. Approved mechanical proposal application uses that same operation-execution claim/fence as connected `apply-proposal`; durable human approval substitutes for the actor-lease prerequisite only, not for execution fencing or exact-principal revocation.
- Reclaim/recovery does not silently duplicate an uncertain external effect.
- Same-run expired-lease recovery and different-run ownership transfer are distinct operations.
- Different-run safe reclaim is allowed only from a mechanically clean inactive frontier; it fences
  the source and creates an exact linked successor rather than retargeting the old operation.
- Recovery targets durable identity/evidence rather than an ambiguous "nearby" operation.
- Terminal evidence is preserved even if later cleanup fails.
- A recovery path that reports a usable continuation must also settle any recoverable execution evidence that the same fresh inspection has mechanically proved terminal; a subsequent inspect must not require the same recovery again.
- Recovery must not bypass an unresolved service-request journal entry: when its exact request-bound execution has already been durably resolved, recovery settles the request through request-replay authority before reclaim becomes legal.

## Process and transaction boundaries

Lease/claim mechanisms differ across SQLite/process locks/PostgreSQL rows. The architecture requires enforceable fencing at the actual concurrency boundary; it does not require one universal lease implementation.

## Normal flow

Acquire/validate execution ownership, perform bounded work, renew when appropriate, persist completion, release/expire ownership, and recover explicitly if the executor dies.

## Failure, replay, recovery, and concurrency

Current SQLite/service behavior distinguishes four authority paths: ordinary same-run lease recovery,
explicit Marco revocation for `kill`, agent-callable `safe-reclaim` for a different run only when the
committed clean-frontier predicate passes, and formal abandonment/reconciliation for genuine recovery
risk. Revocation is checked at lease acquisition/reacquisition and renewal and is not inferred from
release reasons, timestamps, missing leases, or proposal status. Historical lease evidence may select
the exact owner/run targeted by an explicit kill. `kill-all-expired` and `kill-all` are bulk operator
frontends over that same exact-run revocation path, not lease-release primitives: they snapshot exact
lease/owner/run identity, require explicit confirmation, and apply kill per target. Snapshot mismatch,
renewal, or successor replacement conflicts that target rather than broadening revocation to the new
principal; partial results are reported per target and no atomic-all claim is made. Connected-agent mutation execution revalidates that
same owner/run, active actor lease, and non-revoked status atomically when the operation-execution claim
is created. Mechanical application of an already-approved proposal enters the same claim transaction with
its exact `dish-mechanical` owner/run principal; approval removes only the need for a connected actor lease.
Therefore either the execution claim wins and kill refuses as mutation-in-progress, or kill wins and the
application claim rejects the revoked exact principal before any external proposal write. Safe reclaim also
rechecks the requesting owner/run for explicit
revocation inside its writer transaction before it fences the source or creates a successor. Read-only
principal-aware views suppress mutation continuations for a revoked run rather than advertising an
action that the authority boundary will reject. Safe reclaim records the source lease/owner/run and
successor lineage durably and forbids the replaced run from claiming that successor.

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

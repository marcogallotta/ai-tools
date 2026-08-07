# Operations, leases, and fencing

## Read this when

Read this for process locks, service-owned databases, leases, lease expiry/recovery, operation execution claims, stale workers, task fences, legacy writer fencing, mutation admission, or shutdown/drain behavior.

## Scope

This document owns concurrency ownership and stale-actor exclusion. It does not own workflow meaning or external-effect semantics.

## Authoritative implementation

- Process/database ownership: `dish_service/process_lock.py`, `dish_service/database_ownership.py`.
- Service leases: `dish_service/leases.py`, `dish_service/lease_requests.py`.
- Operation execution claims: `dish_tool/operation_execution.py` and durable schema in `dish_tool/database_schema.py`.
- Request coordinators and admission: `dish_service/request_coordinators.py`, `dish_service/application.py`, `dish_service/http.py`.
- Abandonment task fences: `dish_tool/abandonment.py`, `dish_tool/database.py`.
- Legacy writer fence: `dish_service/legacy_writer_fence.py`.
- PostgreSQL workflow/worker fencing: `dish_pg/workflow.py`, `dish_pg/transition.py`, `dish_pg/stage3_models.py`, `dish_pg/stage5_models.py`.
- Cutover admission: `dish_pg/stage6_models.py`, `dish_pg/cutover_control.py`.

## Actors, processes, and stores

A service process owns a database process lock. A service principal owns a renewable operation/task lease. A command execution owns an exact execution claim. PostgreSQL workers own revisioned claims and attempt identities. Marco/admin may expire or recover narrowly defined leases but does not erase workflow evidence.

## Authority and data ownership

The OS process lock is process-exclusion authority. The service-owned marker is durable policy evidence. Lease rows own temporary actor/run access. Execution-claim rows own one active command execution. Abandonment attempts own task-level recovery fences. PostgreSQL generation, epoch, row revisions, claim tokens, and mutation-admission rows fence stale target work.

## Invariants

- A process lock is not an operation lease; a lease is not an execution claim; a run ID is not a content/signoff binding.
- One active service lease exists per task/operation under current constraints.
- A stale or expired actor cannot settle another execution or effect.
- Terminal workflow authority ends before cleanup tails; a leftover terminal lease is non-authoritative only after all work/effects are resolved.
- Admin lease expiry releases only the exact lease or exact current task lease and never changes workflow facts.
- Legacy writer fencing must be mechanically observable; network failure or authentication failure is not proof.
- PostgreSQL mutation admission is closed until exact cutover/first-admission evidence opens it.

## Process and transaction boundaries

Lease acquisition/renewal/recovery uses SQLite writer transactions. Operation execution claims wrap a workflow command and carry recovery evidence across crashes. The default HTTP server closes a shared admission gate before shutdown. PostgreSQL services lock in declared orders (generation/epoch, event/operation, attempt/evidence) and recheck revision/claim identity under lock.

## Normal flow

1. Service startup acquires the environment process lock and marks database ownership.
2. A mutation request crosses service admission and request replay.
3. The service resolves/acquires the exact actor lease.
4. `CurrentWorkflowService` claims an operation execution and performs one command.
5. The execution is completed or marked uncertain with recovery evidence.
6. Safe terminal cleanup releases only the exact stale lease.
7. At cutover, the legacy writer fence and PostgreSQL mutation-admission control replace legacy write eligibility in a fixed order.

## Failure, replay, recovery, and concurrency

A crashed process leaves durable lease/execution evidence. Exact replay or admin recovery reclaims the recorded execution; it does not create a chain of guesses. Expired-lease recovery fails if work is still live or unresolved. Abandonment is used only when permanent actor/run loss intersects real workflow risk. PostgreSQL workers use claim expiry, higher revisions, immutable attempts, and fencing tokens so old workers cannot settle current work.

## Change routing

- Change process exclusion in `process_lock.py`; do not rely on markers as locks.
- Change lease policy in `leases.py`/`lease_requests.py` and preserve request-result atomicity.
- Change command execution fencing in `operation_execution.py` and workflow service integration.
- Change legacy writer fence format/verification in `legacy_writer_fence.py` and cutover controls together.
- Do not add a “force unlock” that deletes evidence or bypasses exact target checks.

## Proving tests

- `tests/test_service_leases.py`, `tests/test_lease_authority.py`, and `tests/test_lease_request_atomicity.py` prove lease ownership and atomicity.
- `tests/test_actor_lease_attempt_context.py` proves actor-attempt binding.
- `tests/test_dish_admin_expire_lease_authority.py` proves narrow admin expiry.
- `tests/test_abandonment_fencing_and_reconciliation.py` proves task-level recovery fences.
- `tests/postgresql/test_projection_attempt_lifecycle.py` and native concurrency tests prove stale-worker exclusion.
- `tests/postgresql/test_stage6_legacy_writer_fence.py` and `tests/postgresql/test_stage6_cutover_first_admission.py` prove cutover fencing/admission.

## Current debt and temporary compatibility

SQLite has a global writer serialization point even though task/operation semantics are narrower. PostgreSQL removes that global point with row-level contention but is not current production authority. The service-owned marker remains for compatibility and safety after copies/replacements; it is deliberately not the lock. Some historical executions without complete request identity remain blocked rather than automatically reclaimed.

## Related documents

- [System context](system-context.md)
- [Request replay and idempotency](request-replay-and-idempotency.md)
- [External effects and Asana](external-effects-and-asana.md)
- [PostgreSQL runtime](postgresql-runtime.md)

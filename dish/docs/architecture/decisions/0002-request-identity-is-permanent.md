# ADR-0002: Request identity is permanent

Status: Accepted

## Read this when
Changing request IDs, retries, duplicate handling, or lost-response recovery.

## Scope
This decision owns the meaning of a mutation request UUID.

## Authoritative implementation
`dish_service/request_replay.py`, `dish_service/request_coordinators.py`, `dish_tool/database_schema.py`, `dish_pg/workflow.py`.

## Actors, processes, and stores
Authenticated owner/run supplies the UUID; current or target authority stores its first result.

## Authority and data ownership
The first admitted identity binds command, canonical arguments, owner, and run permanently.

## Invariants
Exact replay returns the stored result; changed reuse conflicts; pending/uncertain work is not reissued.

## Process and transaction boundaries
Identity admission precedes execution and completion follows the authoritative outcome, atomically where coupling requires.

## Normal flow
Begin, execute once, complete, replay exact result.

## Failure, replay, recovery, and concurrency
Concurrent duplicates join one identity; lost responses reconstruct from durable evidence.

## Change routing
Do not create transport-local idempotency keys or retry with a new mutation identity.

## Proving tests
`tests/test_request_identity.py`, `tests/test_request_completion_race.py`, `tests/test_action_replay_contract.py`.

## Current debt and temporary compatibility
Historical incomplete rows without sufficient evidence remain fail-closed.

## Related documents
[Request replay and idempotency](../request-replay-and-idempotency.md).

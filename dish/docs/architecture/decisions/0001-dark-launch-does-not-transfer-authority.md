# ADR-0001: Dark launch does not transfer authority

Status: Accepted

## Read this when
Changing capture, shadow execution, status reporting, routing, or deciding whether PostgreSQL shadow state may affect live behavior.

## Scope
This decision owns authority during dark launch.

## Authoritative implementation
`dish_service/shadow_capture.py`, `dish_shadow/policy.py`, `dish_pg/shadow_worker.py`, `dish_pg/dark_launch.py`.

## Actors, processes, and stores
Legacy service/SQLite/Asana remain live; spool and PostgreSQL are evidence stores.

## Authority and data ownership
Asana plus SQLite remain authoritative until explicit PostgreSQL cutover activation. Shadow results are comparisons only.

## Invariants
Routing, capture volume, parity, or a running worker cannot transfer authority.

## Process and transaction boundaries
Legacy command completion precedes supplemental capture; target execution occurs in separate worker transactions.

## Normal flow
Capture, execute/capture-only, compare, report; never read shadow state to authorize the legacy command.

## Failure, replay, recovery, and concurrency
Capture/worker failures do not change the already-authoritative legacy result and are represented as backlog/gap evidence.

## Change routing
Change authority only through cutover controls, not dark-launch configuration.

## Proving tests
`tests/postgresql/test_dark_launch_authority_regressions.py`, `tests/test_shadow_capture.py`.

## Current debt and temporary compatibility
Dark launch is temporary rollout evidence and host enablement remains gated.

## Related documents
[Dark launch](../dark-launch.md), [PostgreSQL runtime](../postgresql-runtime.md).

# Testing boundaries

## Read this when

Read this when an architectural claim depends on a particular execution boundary (unit, SQLite, PGlite, native PostgreSQL, subprocess/process, or external-effect simulation).

## Scope

This document explains what different evidence can and cannot prove. Test commands, selection procedure, rerun policy, ownership CSV maintenance, and delivery ritual belong in `docs/testing.md` / contributor instructions, not architecture.

## Authoritative implementation

Current test infrastructure includes `test_selection/`, pytest configuration, and PostgreSQL lane runners under `scripts/`.

## Actors, processes, and stores

Tests exercise different mechanisms: pure policy, SQLite/database boundaries, PGlite, native PostgreSQL, subprocess/process behavior, and external adapters.

## Authority and data ownership

No test becomes runtime authority. Evidence is only as strong as the boundary actually exercised.

## Invariants

- A test claim must match the mechanism it exercised.
- SQLite/PGlite/mocks/in-process tests do not certify PostgreSQL lock/server/process behavior.
- Native PostgreSQL is final certification for claims that specifically depend on PostgreSQL runtime semantics, while smaller tests remain useful evidence.
- Structural/source-layout tests should not substitute for behavioral proof unless topology itself is the safety property.
- Browser tests can prove frontend lifecycle and presentation behavior only at the HTTP/browser
  boundary they exercise. They do not certify native PostgreSQL locking, production HTTPS/proxy
  configuration, destructive restore behavior, or live deployment identity.

## Process and transaction boundaries

Test boundaries intentionally differ. Architecture cares about accurate claims, not a universal mandatory command sequence for every change.

## Normal flow

Choose evidence proportionate to the changed invariant, use the smallest useful tests for diagnosis, and include real-boundary evidence when the guarantee depends on that boundary.

## Failure, replay, recovery, and concurrency

Concurrency claims require a boundary capable of exercising the actual lock/claim mechanism. Unavailable infrastructure is an evidence gap, not a pass.

## Change routing

Operational test-selection policy belongs in `docs/testing.md`. Architecture documents should name representative proving tests only when they illuminate an invariant, not ossify test organization.

## Proving tests

Current lane/selector contracts include `tests/test_dish_test_plan.py`, `tests/test_database_boundary_lane.py`, and PostgreSQL lane contract tests under `tests/postgresql/`.

## Current debt and temporary compatibility

Some architecture tests currently enforce documentation structure and some test governance remains more rigid than the desired long-term system. Code-quality work should continue reducing structural bureaucracy without weakening real-boundary evidence.

## Related documents

- [`../testing.md`](../testing.md)
- [PostgreSQL runtime](postgresql-runtime.md)
- [Extension rules](extension-rules.md)

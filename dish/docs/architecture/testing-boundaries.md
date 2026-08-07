# Testing boundaries

## Read this when

Read this when selecting tests for a change, adding a test or new path, changing fixtures/runners/markers, proving database or process behavior, or claiming PostgreSQL/cutover evidence.

## Scope

This document owns what each test layer can prove and how changed paths route to evidence. Exact commands, flake operations, quarantine, and artifact policy remain in `docs/testing.md`.

## Authoritative implementation

- Path ownership map: `test_selection/ownership.csv`.
- Planner and validator: `test_selection/planner.py`, `test_selection/validator.py`.
- Entry scripts: `scripts/dish-test-plan`, `scripts/dish-test-lane`.
- Pytest policy/markers: `pytest.ini`, `tests/conftest.py`.
- Flake/mutation runners: `tests/flake_runner.py`, `tests/flake_policy.py`, `tests/mutation_runner.py`, `tests/mutation_cases.py`.
- PostgreSQL runners: `scripts/dish-pg-pglite`, `scripts/dish-pg-native-certification`, `scripts/dish-pg-acceptance`.
- Documentation contract: `tests/test_architecture_knowledge_base.py`.

## Actors, processes, and stores

The test planner consumes Git changed paths and the ownership CSV. Pytest lanes run in repository-local environments. PGlite uses a fast embedded PostgreSQL-compatible engine for development evidence. Native tests use a real disposable PostgreSQL database and separate processes/connections where required. Test artifacts are evidence outputs, not runtime authority.

## Authority and data ownership

The ownership CSV is the current-HEAD classification prior. The actual changed invariant remains the final selector authority: agents add semantically required lanes beyond the CSV. Exact tests prove focused contracts; governed lanes prove database/process classes; the ordinary full suite proves integration at merge/delivery boundaries. Native PostgreSQL is the only authority for native lock, server DDL/default, process, and worker behavior.

## Invariants

- Every in-scope repository path has one ownership row; new paths are classified in the same change.
- The planner union covers all changed paths and semantic escalation predicates.
- Smoke remains a curated representative gate, not “all fast tests.”
- First-attempt authoritative lanes do not auto-rerun failures.
- PGlite is useful but never substitutes for native PostgreSQL certification.
- Test doubles at the Asana SDK boundary fake the low-level transport while calling real generated methods.
- Documentation claims name exact proving tests and do not overstate what static/unit evidence proves.
- A complete delivery runs the ordinary full suite because this change alters global architecture guidance and test-selection policy data.

## Process and transaction boundaries

Focused pytest tests run in one process unless explicitly subprocess/native. Database-boundary tests disable fast schema cloning and durability overrides. Native PostgreSQL concurrency tests use independent connections and real row locks. Runtime wiring/process-failure rehearsals cross OS-process boundaries. Source acceptance and cutover evidence are higher-order bundles; they do not replace focused failures.

## Normal flow

1. Run `scripts/dish-test-plan` with the complete changed-path set or base revision.
2. Review the ownership prior and add lanes required by the actual invariant.
3. Run documentation/source-contract tests first.
4. Run focused owners and policy validation.
5. Run governed lanes selected by the plan.
6. Run ordinary collection/full suite at final integration/delivery.
7. Run native PostgreSQL or release/cutover lanes only when their predicate is actually triggered; report unavailable infrastructure as blocked, not passed.
8. Run `git diff --check` and inspect changed documentation for stale/speculative claims.

## Failure, replay, recovery, and concurrency

A failed first attempt remains failure evidence even if a later diagnostic rerun passes. Flake qualification preserves seeds and artifacts. Native infrastructure absence is a blocked result. Test selection failures or unclassified paths are change blockers because they make evidence routing ambiguous. Concurrency claims require real independent connections/processes where the mechanism depends on them.

## Change routing

- Add a focused owner test beside the invariant and map both path and test in `ownership.csv`.
- Change selector/runner policy only with its own contract tests and ordinary full-suite execution.
- Put SQL/constraint/lock claims in native/PGlite lanes as appropriate; do not certify from SQLite-rendered tests alone.
- Keep runbook commands in `docs/testing.md`; keep ownership/invariant explanations here.
- Do not mark a document complete when its source/test anchors were inferred rather than verified.

## Proving tests

- `tests/test_dish_test_map_validation.py` proves map completeness and structural policy.
- `tests/test_dish_test_plan.py` proves changed-path routing and escalation.
- `tests/test_database_boundary_lane.py` proves the SQLite boundary lane contract.
- `tests/postgresql/pglite/test_pglite_lane_contract.py` proves PGlite runner policy.
- `tests/postgresql/test_native_postgresql_certification_lane.py` proves native inventory policy.
- `tests/test_architecture_knowledge_base.py` proves architecture index/links/sections/source anchors and redirect rules.

## Current debt and temporary compatibility

The CSV is exact-path based and therefore requires explicit rows for every architecture document. Some older tests span multiple authority domains because they predate the current selector map. The full suite is still required for final integration even when the planner classifies a documentation-only change narrowly.

## Related documents

- [Extension rules](extension-rules.md)
- [PostgreSQL runtime](postgresql-runtime.md)
- [`../testing.md`](../testing.md)

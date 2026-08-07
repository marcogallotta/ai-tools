# Extension rules

## Read this when

Read this before implementing any new feature, compatibility path, command, durable fact, worker, recovery route, or architecture refactor.

## Scope

This document owns change-routing rules: where changes belong, where they must not be implemented, which hidden consumers fan out, and how temporary compatibility is retired.

## Authoritative implementation

The authoritative owner for each subsystem is listed in [the canonical index](index.md). Cross-cutting enforcement lives in `tests/test_action_authority_structure.py`, `tests/test_legacy_mutation_surface.py`, `test_selection/ownership.csv`, and `tests/test_architecture_knowledge_base.py`.

## Actors, processes, and stores

Fresh agents are the primary audience. Runtime actors/stores are described in [System context](system-context.md). This document treats architecture files as routing maps, not runtime authority.

## Authority and data ownership

A change belongs to the component that owns the durable fact or decision. Callers may validate shape and map errors, but they do not gain ownership by consuming a result. Generated artifacts are derived from shared specifications. Runbooks own operational sequences, not semantics. Product/future documents own approved intent, not current implementation.

## Invariants

- Name the invariant and owning layer before editing.
- Read the complete owning module and every recovery/persistence/external-effect path sharing the rule.
- Change the shared authority once; do not patch each transport or caller.
- Persist intent before effects and preserve exact reread confirmation.
- Keep agent and admin surfaces distinct.
- Preserve completed evidence across retry, recovery, restart, migration, and cleanup failure.
- Do not preserve an impossible state only because a test constructs it.
- Do not add compatibility aliases or forwarding facades without a real producer/database-preservation requirement and deletion condition.
- Update architecture in the same commit when authority, process, store, workflow, command surface, or proving-test ownership changes.

## Process and transaction boundaries

Every extension must state which process executes it, who opens/commits the transaction, what becomes durable before external work, what identity fences retries/concurrency, and how a crash is recovered. If those answers are missing, the design is not ready to implement.

## Normal flow

1. Route from `index.md` to the owning domain document.
2. Confirm current implementation against code and tests; treat planning/runbook claims as non-authoritative when they conflict.
3. Identify readers, writers, derived views, recovery paths, generated contracts, and hidden consumers.
4. Implement at the shared owner and update callers only for adaptation.
5. Add adversarial tests for lifecycle, drift, actor/run, effect outcome, restart, migration, and concurrency dimensions that changed.
6. Classify every new/changed path in `test_selection/ownership.csv`.
7. Update architecture and retire superseded claims/compatibility.

## Failure, replay, recovery, and concurrency

A locally reasonable change is incomplete if it creates a second legal-action matrix, replay identity, writer, effect retry loop, lease authority, or target-selection rule. Recovery must target exact durable evidence. Concurrency must be proved at the mechanism boundary: SQLite writer transactions, PostgreSQL row locks, process locks, or worker claim tokens.

## Change routing

| Need | Change here | Do not implement here |
|---|---|---|
| New workflow transition | Owning `stepN.py`, shared domain helper, snapshot/policy as needed | HTTP, CLI, Asana backend |
| New legal action predicate | `dish_tool/workflow_policy.py` and `application_service.py` snapshot | Result renderer or persisted phase-candidate query |
| New Action command | Shared command spec, service dispatch, application, OpenAPI | Route-only conditional or GPT template alone |
| New admin command | `admin_command_spec.py`, admin application, lease classification | One-off CLI-only shortcut |
| New durable SQLite fact | `database_schema.py`, persistence API, semantic validation/migration | Ad hoc SQL in a use case |
| New PostgreSQL fact | ORM + Alembic + repository/service + native/PGlite tests | Runtime script-only storage |
| New external effect | Intent/attempt schema + exact gateway + recovery/adjudication | Blind adapter call |
| New sidecar | Only replacement-surviving ownership/restore fact | Convenience cache or normal workflow state |
| New worker | Domain service owns state machine; worker drives transactions/I/O | State-machine decisions duplicated in loop code |
| Generated schema change | Shared specification + regenerate checked-in artifact | Hand-edit generated JSON only |

Hidden consumers commonly include CLI and Action schemas, private/admin HTTP, result views, audit repair, recovery/startup, migrations/upgrade fixtures, test-selection ownership, deployment templates, and live GPT instruction templates outside this repository.

## Proving tests

- `tests/test_action_authority_structure.py` detects duplicated action authority.
- `tests/test_legacy_mutation_surface.py` detects a revived legacy workflow engine.
- `tests/test_admin_command_spec.py` detects disconnected admin classifications.
- `tests/test_database_initialization_layers.py` detects duplicated startup/database initialization.
- `tests/test_architecture_knowledge_base.py` detects stale architecture routing/anchors.
- `tests/test_dish_test_map_validation.py` detects unclassified paths.

## Current debt and temporary compatibility

Current production and PostgreSQL target coexist, so some semantic bridges are temporary. `docs/architecture.md` remains only as an external compatibility redirect and should be deleted once no external automation/bookmark references it. Historical design and implementation documents may remain for provenance but must link to this index for current behavior.

## Related documents

- [Packages and dependencies](packages-and-dependencies.md)
- [Testing boundaries](testing-boundaries.md)
- [Authority and data ownership](authority-and-data-ownership.md)

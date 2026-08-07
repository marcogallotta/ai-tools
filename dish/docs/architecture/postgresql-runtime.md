# PostgreSQL runtime

## Read this when

Read this for SQLAlchemy models, Alembic migrations, PostgreSQL command semantics, validation-failure replay, target workflow transactions, projection/reconciliation workers, release candidates, cutover, rollback burn, first admission, or PostgreSQL backup/recovery evidence.

## Scope

This document owns the implemented PostgreSQL target architecture and its authority gates. It explicitly distinguishes implemented target behavior from current production authority. Operator sequencing, rehearsal results, host defects, and rollout status remain in the migration/runbook/status documents.

## Authoritative implementation

- Database/session layer: `dish_pg/database.py`, `alembic.ini`, `dish_pg/migrations/`.
- Core authority: `dish_pg/models.py`, `dish_pg/repositories.py`, `dish_pg/services.py`.
- Workflow/request authority: `dish_pg/stage3_models.py`, `dish_pg/workflow.py`.
- Command contract/port/read model: `dish_pg/command_contract.py`, `dish_pg/command_port.py`, `dish_pg/planner.py`, `dish_pg/read_model.py`, `dish_pg/protocol.py`.
- PostgreSQL service adapter, including durable pre-execution validation failures: `dish_pg/postgres_service.py`.
- Transition/projection/reconciliation authority: `dish_pg/stage5_models.py`, `dish_pg/transition.py`.
- Projection/reconciliation processes: `dish_pg/projection_worker.py`, `dish_pg/reconciliation_worker.py`.
- Release/cutover authority: `dish_pg/stage6_models.py`, `dish_pg/release.py`, `dish_pg/release_evidence.py`, `dish_pg/release_status.py`, `dish_pg/cutover_control.py`.
- Current migration head includes `dish_pg/migrations/versions/0030_validation_failure_admission.py`; migration history is append-only.
- Other runnable target processes: `dish_pg/shadow_worker.py`.
- TEST runtime entry: `dish_service/__main__.py`.

## Actors, processes, and stores

PostgreSQL actors are generation-bound service runs, request principals, verification actors, projection/reconciliation/shadow workers, Marco approval/cutover authority, and external projection adapters. Stores include PostgreSQL authority tables, Alembic history, request/outcome and execution rows, projection attempts/outbox, reconciliation runs/items, release/cutover evidence, and downstream Asana mappings. Runnable processes each own their session lifecycle; service/domain helpers consume caller-owned sessions.

## Authority and data ownership

Core models own generations, activations, contract bindings, registries, stable Dish task identity, content versions/activations, membership, placement, and completion. Stage 3 owns generation-bound request identity/outcomes, executions, fences, operations, actors, leases, planning challenges, authorizations, verification, holds, abandonment, audit, and causality. Stage 5 owns import/shadow/projection/reconciliation evidence. Stage 6 owns candidate/evidence/rehearsal/cutover/admission authority.

Pre-execution command validation failures are part of Stage 3 request/outcome authority, not command-execution authority. `WorkflowAuthorityService.record_validation_failure` persists an exact validation-only request identity and immutable rule-error outcome with audit/invocation evidence but no `CommandExecution`. `PostgresRuntimeService.record_replay_validation_failure` is the HTTP/runtime adapter for that path. Migration `0030_validation_failure_admission` changes the admission guard only enough to recognize that exact validation-only identity; ordinary mutation admission remains fail-closed and the reserved first production request is not consumed.

Reconciliation authority is corpus-scoped. `ProjectionService.start_reconciliation`, `record_reconciliation_item`, and `complete_reconciliation` own the durable run and item identities; `reconciliation_worker.py` fetches the complete external corpus before opening the authoritative transaction and drives those services without owning their state transitions. Replaying the same generation/epoch/corpus identity must converge on the compatible existing run/items; changed immutable corpus inputs or changed item evidence conflict rather than silently rewriting history.

Current production does **not** read these PostgreSQL tables as live authority. SQLite/Asana remains the production authority during dark launch. Authority can transfer only through the explicit cutover sequence and mutation-admission controls.

## Invariants

- Services participate in caller-owned transactions and do not commit independently.
- One active authority generation and one current activation exist under database constraints.
- Requests, actors, operations, mappings, workers, and evidence are generation/epoch-bound.
- Same-task contention is fenced without a global cross-task serialization point.
- Authoritative command state and projection intent commit in one PostgreSQL transaction; `command_effect_runtime.py` verifies that the handler produced the declared projection and covered mutation effects before that caller-owned transaction can succeed.
- Projection effects are disabled until an active epoch explicitly allows them.
- A `reproject` event is confirmed against its own recorded whole-state identity (the `authoritative_snapshot` payload field, compared through `_reproject_state_identity`), never through the shared `identity_field` mapping used for `update_task_document`/`move_task`/`set_completion`; that mapping never included `reproject` and treating it as covered left reproject work permanently unconfirmable as applied.
- Release candidates bind exact source, schema, generation, registry, import, reconciliation, closure, and runtime evidence.
- Rollback to legacy authority is forbidden after rollback-burn evidence/first admitted PostgreSQL mutation.
- Restores create a new authority generation and invalidate prior run/worker capability.
- A validation-only failure may persist request/outcome/audit evidence without creating a command execution, opening ordinary mutation admission, or consuming the isolated first-request reservation.
- Exact replay of a validation-only request returns the first stored error outcome; reuse of that UUID for different command/arguments/owner/run/error identity conflicts.
- Reconciliation external I/O completes before the authoritative reconciliation transaction. The worker does not hold a PostgreSQL transaction open while fetching the external corpus.
- One generation/epoch/corpus identity owns one reconciliation run. Compatible replay resumes/returns it; incompatible expected counts, item identities/kinds, outcomes, mappings, or evidence fail closed.
- Reconciliation can complete only after exactly the expected corpus is recorded. `unknown_external` or `blocked` items make the governed run blocked rather than importing external state as authority.

## Process and transaction boundaries

The protocol/runtime opens a SQLAlchemy session for a command and delegates to `PostgresCommandPort`. Repositories use the provided session. Validation failures take a separate pre-execution path: the runtime adapter opens one session transaction, binds the validation-only `ServiceRequest`, durable rule-error outcome, audit event, and invocation obligation, then returns the stored first outcome; no command execution is created.

Projection workers split claim, attempt creation, external call, and settlement into separately committed transactions so crash boundaries retain durable evidence. The reconciliation worker performs the inverse I/O ordering appropriate to whole-corpus comparison: it fetches the complete corpus first, then uses one caller-owned transaction for `start_reconciliation`, any missing item records, and completion. Replayed terminal reconciliation runs are returned only when the fetched immutable corpus still matches their stored item set.

Cutover/release services use locked, revisioned rows and immutable evidence. Alembic revisions are append-only; frozen historical DDL is not regenerated from live ORM metadata.

## Normal flow

1. Bootstrap/import creates a generation, contract binding, registry, and exact imported authority evidence.
2. Shadow execution and reconciliation accumulate non-authoritative evidence.
3. A reconciliation worker fetches one complete corpus, starts/reuses the corpus-scoped run, records only missing compatible items, and completes or blocks it according to exact item outcomes.
4. Ordinary command requests go through mutation admission and create command executions; pre-execution validation failures instead persist the validation-only request/outcome path and stop before command execution.
5. Release tooling constructs and validates a candidate and evidence bundle from current authoritative tables/artifacts, including selected reconciliation evidence.
6. Marco approval binds the exact candidate/evidence.
7. Cutover closes legacy admission, proves the writer fence, activates PostgreSQL, commits rollback burn, and performs bounded first admission.
8. Normal authoritative commands commit task/workflow/audit/outbox state in PostgreSQL.
9. Projection workers maintain downstream Asana; reconciliation reports drift/unknown corpus without importing it as authority.

## Failure, replay, recovery, and concurrency

Row locks and revisions reject stale claims. Exact request replay is generation-bound. Validation-only request insertion uses conflict-safe identity binding; concurrent exact callers converge on the same immutable outcome, while mismatched reuse conflicts. Because the validation request kind is distinguished from ordinary execution admission, a closed cutover control remains closed and the first-request reservation remains untouched.

Projection dispatch/recovery preserves immutable original dispatch identity. Reconciliation replay preserves immutable corpus/run/item identity and refuses partial or contradictory corpus replays. Release/cutover checkpoints are resumable and fail closed on stale evidence. Restore invalidates prior capabilities. Native PostgreSQL tests are required for lock ordering, server constraints/defaults, worker takeover, native validation replay/admission behavior, and process-failure behavior; PGlite remains fast compatibility/development evidence where designated.

## Change routing

- Add persistence in a new Alembic revision and matching ORM/service/repository tests; do not edit frozen historical revisions.
- Change request/validation replay identity in `dish_pg/workflow.py` and `dish_pg/postgres_service.py`, with migration/admission guards when the database gate changes; do not smuggle validation errors through ordinary command execution.
- Put command semantics in the command port/planner/workflow services, not protocol transport.
- Put branch-sensitive effect declarations in `command_effects.py` and persistence-facing command-effect recording/verification in `command_effect_runtime.py`; do not duplicate either inside command handlers or workers.
- Put projection and reconciliation lifecycle authority in `transition.py`; workers only fetch/drive owned service transitions and process boundaries.
- Change release/cutover gates in the typed evidence/status/control modules and exact gate tests together.
- Do not wire PostgreSQL into production default startup by changing a flag alone; authority activation, fencing, routing, credentials, workers, recovery, and evidence must converge.
- Do not encode current rehearsal pass/fail status here. Update the operational/test-plan documents that own execution evidence instead.

## Proving tests

- `tests/postgresql/test_stage2_core_authority.py`, `tests/postgresql/test_stage3_workflow_authority.py`, and `tests/postgresql/test_stage4_command_port.py` prove the model/command layers.
- `tests/postgresql/test_postgres_runtime_validation_replay.py` and `tests/postgresql/test_postgres_runtime_validation_http.py` prove runtime persistence/envelope replay for pre-execution validation failures.
- `tests/postgresql/pglite/test_validation_failure_authority.py` proves the validation-only path leaves closed admission and the first-request reservation unchanged at the PostgreSQL-like boundary.
- `tests/postgresql/native/test_validation_replay.py` is the native certification boundary for validation-failure persistence, concurrent exact replay, identity conflict, and admission isolation.
- `tests/postgresql/test_validation_failure_admission_migration.py` proves upgrade/downgrade installation of the `0030_validation_failure_admission` guard behavior.
- `tests/postgresql/test_stage5_transition_projection.py` and projection lifecycle tests prove outbox/effects.
- `tests/postgresql/test_reconciliation_worker.py`, `tests/postgresql/native/test_reconciliation_worker.py`, and process-failure reconciliation tests prove whole-corpus transaction ordering, restart/resume, and exact reconciliation ownership.
- `tests/postgresql/test_stage6_release_cutover.py`, `tests/postgresql/test_stage6_rollback_burn_gate.py`, and `tests/postgresql/test_stage8_cutover_evidence_gates.py` prove release/cutover authority.
- `tests/postgresql/pglite/test_pglite_migrations.py` proves fast migration compatibility.
- `tests/postgresql/native/test_stage_a_concurrency.py`, native worker tests, and `tests/postgresql/test_native_postgresql_certification_lane.py` prove native boundaries.
- `tests/postgresql/test_production_shaped_runtime_contracts.py` proves static/local runtime contracts only; it is not evidence that the current §3 or §4 rehearsal has passed.

## Current debt and temporary compatibility

The target is substantially implemented but current production startup remains SQLite/Asana. The checked-in PostgreSQL service path is TEST-only. Production authority, live projection/reconciliation deployment, and cutover require operational evidence outside this document. Some target modules intentionally reuse current workflow policy and legacy document helpers to preserve semantics.

Do not infer rehearsal completion from code or unit/contract tests. Current §3 runtime-wiring and §4 production-shaped rehearsal status, including any open defect or rerun result, is transient operational evidence owned by `../database-backend-postgresql-test-plan.md`, `../database-backend-imp.md`, and `../ops-issues.md`. Those documents may lag or change independently of this stable ownership description; when status claims conflict, verify the current code/tests and update the status owner rather than hard-coding the temporary result here.

## Related documents

- [Request replay and idempotency](request-replay-and-idempotency.md)
- [Dark launch](dark-launch.md)
- [Operations, leases, and fencing](operations-leases-and-fencing.md)
- [Testing boundaries](testing-boundaries.md)
- [`../database-backend.md`](../database-backend.md)
- [`../postgresql-cutover.md`](../postgresql-cutover.md)
- [`../database-backend-postgresql-test-plan.md`](../database-backend-postgresql-test-plan.md)
- [`../database-backend-imp.md`](../database-backend-imp.md)
- [`../ops-issues.md`](../ops-issues.md)

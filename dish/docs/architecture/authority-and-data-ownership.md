# Authority and data ownership

## Read this when

Read this before changing a durable field, adding a table or sidecar, changing an external read/write, moving a rule between layers, or deciding whether Asana, SQLite, PostgreSQL, or an Honest asset owns a fact.

## Scope

This document owns the current writer/reader map and the distinction between authoritative, derived, temporary, historical, and intended future state. It does not prescribe operator procedures.

## Authoritative implementation

- Honest release resolution: `dish_tool/releases.py`.
- Current task document parsing and validation: `dish_tool/task_document.py`, `dish_tool/schema_validation.py`, `dish_tool/governed_diff.py`.
- Current SQLite schema and invariants: `dish_tool/database_schema.py`, `dish_tool/database_initialization.py`, `dish_tool/database.py`.
- Current live-task boundary: `dish_tool/task_gateway.py`, `dish_tool/task_store.py`, `dish_tool/backend.py`.
- Current action snapshot: `dish_tool/application_service.py`, `dish_tool/workflow_policy.py`.
- Replacement-surviving state: `dish_service/database_ownership.py`, `dish_service/restore_request_journal.py`, `dish_service/restore_fault.py`.
- Dark-launch production observation/export: `dish_pg/location_manifest.py`, `dish_pg/legacy_source.py`, `dish_pg/dark_launch_readiness.py`.
- PostgreSQL target authority: `dish_pg/models.py`, `dish_pg/stage3_models.py`, `dish_pg/stage5_models.py`, `dish_pg/stage6_models.py`.

## Actors, processes, and stores

The principal writer is the default `dish-service` process. It writes SQLite and calls Asana. Honest assets are read-only inputs. Sidecars are written only for ownership or database-replacement facts. Dark-launch PostgreSQL receives copied and shadow-derived evidence but is not read by the default production workflow as authority.

## Authority and data ownership

| Fact | Authoritative writer | Authoritative reader | Derived/projected state | Temporary or legacy state |
|---|---|---|---|---|
| Supported protocol/schema release | Maintained Honest assets | `dish_tool/releases.py` | Parsed schemas and validation findings | Historical operation bindings remain immutable |
| Live title and notes | Governed Asana effect through `ExactTaskGateway` | Exact reread through `dish_tool/task_gateway.py` | Content identity and confirmed content versions in SQLite | Candidate files are ephemeral inputs |
| Cooking-project placement and completion | Asana | Exact gateway reread | Confirmed movement/completion evidence | First-membership assumptions are prohibited legacy behavior |
| Workflow operation and phase | SQLite workflow use cases | `CurrentWorkflowService` and repositories | Result envelopes and `allowed_actions` | `submissions` storage is read-only compatibility only |
| Legal actions | `workflow_policy.legal_actions` over one current snapshot | Command applications and response rendering | Nested authoritative views | Persisted phase candidates are not legal-action authority |
| Request identity and first result | `dish_service/request_replay.py` in SQLite | Request coordinators | `data.request_replayed` response decoration | Incomplete historical rows without evidence remain blocking |
| Service lease and execution claim | Lease/operation coordinators in SQLite | Request gates and workflow mutation authority | Lease guidance in responses | A terminal-operation lease may remain as a safe cleanup tail |
| External-effect intent and outcome | SQLite attempt journals | Recovery and workflow code | Audit and recovery guidance | `uncertain` is unresolved evidence, not permission to retry |
| Backup/restore replacement facts | Backup metadata plus restore sidecars | Startup and restore recovery | Reports and health status | Sidecars exist only because database replacement can erase in-DB records |
| Dark-launch production source observation | None: strictly read-only capture from fixed production SQLite/Asana identity | `dish_pg/location_manifest.py` | Location manifest used by export/import evidence | Observation only; never a writer or replacement authority |
| Dark-launch legacy export | None: deterministic read-only join of SQLite content heads and complete location manifest | `dish_pg/legacy_source.py` / importer | NDJSON importer input | Derived migration input; no Asana access |
| Dark-launch readiness | None: read-only inspection of artifacts, existing spool, PostgreSQL, paths/environment, and stopped unit | `dish_pg/dark_launch_readiness.py` | Machine-readable readiness report | Evidence only; cannot create authority/effects/unit state |
| Dark-launch capture and comparison | Legacy capture/spool and shadow worker | Dark-launch status/evidence tools | PostgreSQL shadow results and parity classifications | Evidence only; never current authority |
| PostgreSQL task/workflow authority | `dish_pg` services after explicit activated generation and admission | PostgreSQL command port/runtime | Asana projection intents and external mappings | Before cutover, imported/shadow state is non-authoritative |

## Invariants

- No single store substitutes for another current authority: Asana document facts and SQLite workflow evidence must agree before mutation.
- The live task is always reread around a governed effect; cached SDK responses do not become authority.
- Creation facts and completed evidence are immutable or monotonic once recorded.
- Unknown or contradictory historical evidence is quarantined, reconciled, or blocked; missing facts are not wildcards.
- Only facts that must survive replacement of SQLite may live in replacement-surviving sidecars.
- PostgreSQL authority is one-way after activation; external Asana observations cannot silently promote themselves back into canonical state.
- Production dark-launch manifest/readiness paths may observe current authority but may not mutate SQLite, Asana, PostgreSQL authority, spool/checkpoints, credentials, or systemd units.

## Process and transaction boundaries

SQLite commands use explicit writer transactions and savepoints owned by the workflow or service coordinator. An intended external effect is committed before the network call and finalized after an exact reread. PostgreSQL services accept caller-owned SQLAlchemy sessions and do not commit independently; runnable workers and protocol services own the session/transaction boundaries around those services.

## Normal flow

1. Resolve the supported Honest release and validate the exact live task.
2. Read durable workflow state and build one `WorkflowSnapshot`.
3. Derive legal actions from that snapshot.
4. For a mutation, persist command/external-effect intent in the current authority.
5. Perform and reread the external effect where required.
6. Commit terminal evidence and return a response derived from the same authoritative state.

## Failure, replay, recovery, and concurrency

Drift between live Asana and recorded identities makes actions unavailable. Uncertain effects remain tied to their original intended identity. Replay returns the first authoritative result or a fail-closed pending state. SQLite uses one writer and explicit task/operation constraints; PostgreSQL uses row locks, revisions, claims, and generation/epoch fences.

## Change routing

- Change the task-document contract in Honest assets and the canonical document modules, not in HTTP or persistence callers.
- Change legal-action rules in `dish_tool/workflow_policy.py` and snapshot construction in `dish_tool/application_service.py`.
- Change durable workflow facts in the owning persistence/service module and add schema constraints where feasible.
- Do not add a second current-state cache that callers can mistake for authority.
- Treat location manifests, legacy export NDJSON, dark-launch status, and readiness reports as derived evidence with explicit source bindings; never promote them to live task/workflow authority.

## Proving tests

- `tests/test_workflow_policy_fail_closed.py` proves snapshot-derived action authority.
- `tests/test_database_schema_and_recovery.py` proves SQLite schema invariants and recovery evidence.
- `tests/test_asana_placement_lifecycle.py` proves exact placement authority.
- `tests/test_backend_effect_recovery_resilience.py` proves external-effect outcomes.
- `tests/test_legacy_mutation_surface.py` proves legacy submission storage is not a second workflow engine.
- `tests/postgresql/test_stage2_core_authority.py` and `tests/postgresql/test_stage3_workflow_authority.py` prove target authority ownership.
- `tests/postgresql/test_location_manifest.py`, `tests/postgresql/test_dark_launch_legacy_source.py`, and `tests/postgresql/test_dark_launch_readiness_authority.py` prove production observation/export/readiness do not create a second authority.

## Current debt and temporary compatibility

The `submissions` table and associated read/migration diagnostics remain compatibility state; production exposes no API that creates, transitions, or recovers a legacy submission. Current production authority remains split across Asana and SQLite until PostgreSQL activation. Several planning documents describe intended post-cutover ownership and must not be read as current runtime state.

## Related documents

- [System context](system-context.md)
- [Workflow and human review](workflow-and-human-review.md)
- [External effects and Asana](external-effects-and-asana.md)
- [PostgreSQL runtime](postgresql-runtime.md)

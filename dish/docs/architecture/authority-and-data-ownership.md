# Authority and data ownership

## Read this when

Read this when changing a durable fact, adding storage, changing a writer, or deciding whether a value is authoritative, derived, historical, or temporary.

## Scope

This document records durable authority boundaries. It intentionally avoids turning current implementation placement into permanent law.

## Authoritative implementation

Current anchors include `dish_tool/releases.py`, `dish_tool/task_document.py`, `dish_tool/database.py`, `dish_tool/task_store.py`, `dish_tool/workflow_policy.py`, `dish_service/request_replay.py`, and PostgreSQL models/services under `dish_pg/`.

## Actors, processes, and stores

Current production combines Honest assets, service-owned SQLite, and Asana. PostgreSQL is the target authority. Sidecars and dark-launch artifacts are supporting evidence/storage, not automatically new authority.

## Authority and data ownership

| Fact | Current authority | Notes |
|---|---|---|
| Supported document contract | Honest assets | Parsed/validated forms are derived |
| Live title/notes/placement/completion | Asana as coordinated and exactly reread by Dish | Transitional until frontend/backend replacement |
| Workflow operations, replay, leases, audit | SQLite | Intended to migrate to PostgreSQL |
| Writable repository Implementation ownership and task/branch/PR lineage | Repository-owned global Implementation claim service | One current `(repository, task_gid)` CAS generation; Asana is a mirrored orchestration/readback surface and GitHub is lineage evidence |
| Legal workflow transitions | Workflow/application policy over authoritative state | Rendered actions are projections |
| Request identity/outcome | Replay authority | Permanent identity semantics |
| Kill request-to-revocation binding | SQLite replay/kill authority | Exact immutable request, operation, owner, run, source authority, and revocation identity; written atomically with revocation |
| PostgreSQL canonical state | PostgreSQL only after explicit cutover | Before cutover it is target/shadow evidence |
| Dark-launch artifacts | No live authority | Evidence only |
| Frontend password/session/limiter/audit state | Frontend security store | Access support only; never task or workflow authority |
| Frontend board/detail DTOs | Derived from the active canonical read source | Presentation snapshots; never writer or legality authority |

## Invariants

- A consequential durable fact has one authoritative writer/decision boundary at a time.
- Repository Implementation ownership has one current durable generation. A stale or superseded `claim_id` cannot authorize semantic authoring, branch/PR adoption, or publication, even when GitHub expected-head checks would otherwise pass.
- The claim service commits ownership before Asana synchronization. Until the exact generation is mirrored and read back successfully, that generation is non-writable and ordinary dispatch remains blocked.
- Derived views, caches, renderers, and transports may contain logic, but must not independently contradict the authoritative fact they represent.
- Missing or contradictory evidence is handled explicitly rather than treated as permission.
- Dark-launch evidence cannot silently become current authority.
- After PostgreSQL authority activation, Asana observations do not promote themselves back into canonical backend state.
- Frontend read projections, caches, presentation registries, and browser state cannot become
  task, placement, completion, workflow, or projection authority.

During pre-cutover observation, the writable frontend security database is physically distinct
from the PostgreSQL task-observation database. Observation credentials are read-only. Restoring
security storage cannot by itself revive an expired, revoked, or superseded browser session; a
deployment-owned fence outside the restored database participates in current session validity.

## Process and transaction boundaries

The current SQLite and PostgreSQL implementations use different transaction mechanisms. Their exact transaction-owner modules are implementation details; the architectural requirement is atomicity where identity/admission/outcome must agree and durable intent before uncertain external effects when recovery depends on it.

## Normal flow

Read authoritative state, derive the decision, persist required intent/outcome evidence, perform any external effect, and expose derived views to clients.

## Failure, replay, recovery, and concurrency

Replay, fencing, and effect settlement preserve identity across failures. Store-specific locking is an implementation mechanism for those guarantees, not itself a product rule.

## Change routing

When changing authority, state clearly which fact changes writer/reader and what migration or compatibility boundary exists. Do not create a second writer merely because a new client or transport needs a different presentation.

## Proving tests

Evidence is distributed across workflow, replay, Asana lifecycle, recovery, and PostgreSQL authority tests, including `tests/test_workflow_policy_fail_closed.py`, `tests/test_request_identity.py`, and PostgreSQL authority tests under `tests/postgresql/`.

## Current debt and temporary compatibility

The current Asana/SQLite split and several migration-era artifacts are temporary. Sidecars and compatibility state should be justified by concrete recovery/operational needs, not by a blanket architectural prohibition or permission. The `submissions` table and associated read/migration diagnostics remain compatibility state; production exposes no API that creates, transitions, or recovers a legacy submission.

## Related documents

- [System context](system-context.md)
- [Workflow and human review](workflow-and-human-review.md)
- [External effects and Asana](external-effects-and-asana.md)
- [PostgreSQL runtime](postgresql-runtime.md)

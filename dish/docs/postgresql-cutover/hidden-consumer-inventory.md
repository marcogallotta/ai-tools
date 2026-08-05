# Phase 1 hidden-consumer inventory

## Status and source identity

This inventory was produced from the repository tree contained in
`ai-tools-venv(20260805-195845).tgz`.

- Archive SHA-256: `09a32bd6f42496de9a6a77b556a8d806a310a9de98e781b26b516e2a7a73377d`.
- The archive did not contain `.git` metadata, so no received checkout `HEAD` can be proved.
- Repository-authored provenance is recorded, but is not treated as the received checkout identity:
  `dish/docs/ops-issues.md` says the snapshot was refreshed on 2026-08-05 and verified through
  synthetic history `09fa713`; the archived Stage A acceptance report records source commit
  `7dff85921f005c62929c46d347a7ab29ae463480`; `PROPOSED_COMMIT_MESSAGE.md` describes a rebased
  `v131` package.
- A synthetic local baseline commit was created only to make the requested documentation commits and
  patches reproducible: `35d6871acb7fb9140f486af8f31254121b8452dc`. It is not a source-repository identity.

## Decision vocabulary

| Verdict | Meaning |
| --- | --- |
| Safe candidate | No executable production reader, writer, or deploy/ops consumer was found. Removal is still conditional on replacing any documented guarantee and rewriting the target schema/migration. |
| Unsafe direct deletion | A production reader, writer, command, release gate, deployment tool, or generated external contract exists. The consumer must be migrated before the object is removed. |
| Unresolved | Evidence is incomplete, outside the supplied archive, or a runtime reader exists without a production producer. Perform the named verification before disposition. |

“Production” below means executable non-test code or operational scripts. Model declarations and
frozen migrations are listed separately and are not counted as proof that a production path exists.
Generated OpenAPI/frontend contracts are also separated from production implementation.

## Search method

The audit combined exact-name searches, ORM metadata enumeration, call-site searches, CLI command
searches, generated-contract inspection, and document/runbook inspection. The important commands were:

```sh
# Inventory the actual tree and migration head.
find dish -type f -not -path 'dish/.venv/*' -print
rg -n '__tablename__|class .*\(Base\)' dish/dish_pg
find dish/dish_pg/migrations/versions -maxdepth 1 -name '*.py' -print | sort

# Search every target noun through executable, test, script, migration, generated, and doc surfaces.
rg -n --hidden --glob '!dish/.venv/**' --glob '!.claude/worktrees/**' \
  'causality_edges|request_uncertainty_resolutions|applied_migration_events|source_import_native_links'
rg -n --hidden --glob '!dish/.venv/**' --glob '!.claude/worktrees/**' \
  'WorkerProbeInventory|WorkerProbeRequirement|WorkerProbeEvidence|WorkerReadinessCompletion'
rg -n --hidden --glob '!dish/.venv/**' --glob '!.claude/worktrees/**' \
  'release_candidates|rehearsal_runs|cutover_runs|candidate_manifest|first_request_reservations'
rg -n --hidden --glob '!dish/.venv/**' --glob '!.claude/worktrees/**' \
  'semantic proposal|Human Review|service_requests|service_leases|projection_reconciliation'
rg -n --hidden --glob '!dish/.venv/**' --glob '!.claude/worktrees/**' \
  'task_gid|dish_id|asana_task_gid|create response|create-response'

# Distinguish a repository API from an actual producer.
rg -n 'add_migration_event|AppliedMigrationEvent\('
rg -n 'add_bootstrap_authority|GenerationBootstrapAuthority\('
rg -n 'SourceImportNativeLink\('
rg -n 'WorkerProbeInventory\(|WorkerProbeEvidence\(|WorkerReadinessCompletion\('

# Inspect command and deployment consumers.
rg -n 'ACTION_COMMANDS|REPLAY_SAFE_COMMANDS|request_replay|apply-proposal|inspect' \
  dish/dish_service dish/dish_pg dish/dish_tool dish/openapi dish/deploy dish/tests
rg -n 'candidate-create|evidence-record|rehearsal-record|cutover|first-admission|projection-worker-ready' \
  dish/scripts/dish-pg-release dish/dish_pg
find dish/deploy -maxdepth 2 -type f -print | sort
```

The ORM metadata contains **102 application tables** and the active Alembic tree contains **29
revisions, 0001 through 0029**. A repository-wide table/reference map was generated from those 102
names and then manually reviewed for the deletion and merger candidates below.

## 1. Confirmed inert-schema candidates

| Candidate | Production use | Test/generated/doc use | Producer status | Verdict and required treatment |
| --- | --- | --- | --- | --- |
| `causality_edges` / `CausalityEdge` | No executable reader or writer found outside the ORM declaration in `dish_pg/stage3_models.py`. | Frozen historical DDL in `dish_pg/migrations/frozen_tables.py`; named by the two PostgreSQL cutover documents. No test constructs it. | None found. | **Safe candidate.** Remove from the approved target schema and squashed migration only after the Stage A behavioral contract confirms no retained causal-query guarantee. Add a schema-absence/retained-audit regression. |
| `request_uncertainty_resolutions` / `RequestUncertaintyResolution` | No executable reader or writer found outside the ORM declaration in `dish_pg/stage3_models.py`. | Frozen historical DDL and cutover-document references; no test constructs it. | None found. | **Safe candidate.** Preserve uncertain-request settlement through the actual request/outcome structures; prove no command or admin flow queries this table before removal. |
| `applied_migration_events` / `AppliedMigrationEvent` | `AuthorityRepository.add_migration_event()` can persist a caller-supplied row, but no non-test call site constructs or invokes it. | `tests/postgresql/test_stage2_core_authority.py` and `test_stage2_provenance_generation.py` construct rows; migration `0002` creates the table; cutover docs name it. | Repository API only; no production producer and no production reader found. | **Safe table-merge candidate; unsafe to delete the provenance guarantee.** Put schema/application version identity on the retained generation or cutover record, then remove the unused standalone table and test-only repository API. |
| `generation_bootstrap_authorities` / `GenerationBootstrapAuthority` | `WorkflowAuthorityRepository.register_run()` reads an optional `bootstrap_id`; `AuthorityRepository.add_bootstrap_authority()` can insert a supplied row. | Tests construct the row; migrations and model definitions include it. | No production constructor/caller found. | **Unresolved, not inert.** Runtime has a reader but no producer. Either implement the approved bootstrap path or remove the optional bootstrap branch and move its authorization guarantee to the retained generation/admission record. |

## 2. Import evidence and native-link structures

| Candidate | Production use | Test/generated/doc use | Producer status | Verdict and required treatment |
| --- | --- | --- | --- | --- |
| `source_import_batches` | Created and updated by `dish_pg/transition.py`; consumed by candidate creation, cutover validation, release validation, and final import controls. | Extensive Stage 5/6, PGlite, and migration tests. | Real production/offline service producer exists. | **Unsafe direct deletion.** The exact final source identity, expected/imported counts, completion state, and generation binding must move to the canonical import report/cutover record before this table is collapsed. |
| `source_import_entity_evidence` | Written by `TransitionService.record_entity()` and read by `candidate_manifest.py` and `release_validation.py`. | Migration and import-link tests construct/check it. | Real service producer exists. | **Unsafe direct deletion.** It may be replaced by one sealed per-entity import report, but current integrity checks depend on it. |
| `source_import_native_links` | Read by `release_validation.validate_source_import()` and hashed by `candidate_manifest.py`. | Created by migration `0024`; tests and support fixtures construct it; schema contract documents it. | **No production constructor or writer found.** `record_entity()` writes entity evidence only. | **Unresolved/unsafe direct deletion.** Current release validation requires links that production tooling cannot create. Decide the canonical import evidence representation, migrate both readers, and add a production import test before removal. |
| Sealed per-entity import manifest | No production command emits a single sealed manifest covering every source entity and target identity. | Existing tests assemble row-level evidence and native links. | Missing. | **Unresolved cutover gap.** Implement or explicitly merge this guarantee into the one-shot final import/reconciliation report; do not claim the current native-link table satisfies it. |

## 3. Readiness and evidence structures

| Candidate | Production use | Test/generated/doc use | Producer status | Verdict and required treatment |
| --- | --- | --- | --- | --- |
| `projection_worker_readiness` | Written by `CutoverControlService.record_projection_worker_readiness()`; read by candidate manifests, release evaluation, cutover gates, and release validation. | Stage 8 and typed-readiness tests. | A production/offline service method exists, exposed through `scripts/dish-pg-release projection-worker-ready`. | **Unsafe direct deletion.** May be merged into a runtime rehearsal report only after release/cutover readers and CLI treatment move together. |
| `worker_probe_inventories` | Read by `cutover_control.py`, `candidate_manifest.py`, and `release_validation.py`; required before recording worker readiness. | Tests and support fixtures construct it; migration `0026` creates it. | No production writer or CLI command found. | **Unresolved and currently unreachable in production.** Candidate for collapse into a sealed rehearsal report, but current readiness command cannot succeed without externally fabricated rows. |
| `worker_probe_requirements` | Read by candidate digesting and readiness validation. | Test-only construction. | No production writer found. | **Unresolved; unsafe direct deletion while validators require it.** Move canonical probe requirements into code/report schema or add a producer, then migrate readers. |
| `worker_probe_evidence` | Read by candidate digesting and readiness validation. | Test-only construction. | No production writer found. | **Unresolved; unsafe direct deletion while validators require it.** Replace with report-contained probe results and an artifact digest, or implement a production writer. |
| `worker_readiness_completions` | Read by candidate digesting and readiness validation. | Test-only construction. | No production writer found. | **Unresolved; unsafe direct deletion while validators require it.** Eliminate the evidence-certifying-evidence layer only after the report validator proves completeness directly. |
| `release_evidence_items` | Written/read by `dish_pg/release.py`; required by candidate evaluation. | Stage 6 and Agent B release tests. | Real service producer exists. | **Unsafe direct deletion.** Merge required evidence into referenced external reports plus one snapshot/cutover record, and migrate candidate evaluation first. |
| `release_evidence_bundles` | Built and validated by `release.py`; read by `final_asana_closure.py`; exposed through `scripts/dish-pg-release bundle`. | Frozen migration history and release/cutover tests. | Real CLI/service producer exists. | **Unsafe direct deletion.** Replace bundle consumers with the canonical cutover snapshot/report attachment set before removal. |
| `runtime_release_attestations` | Written and read by `cutover_control.py`/`release.py`; exposed by release tooling. | Stage 8 tests. | Real service producer exists. | **Unsafe direct deletion.** Artifact digest must survive on the canonical cutover record. |
| Invocation-audit fulfillment structures/fields | Release validation queries invocation and audit obligations across request/execution/audit records. | Tests create satisfying fixtures. | Cutover design states no production path currently fulfills every required field. | **Unresolved.** Trace each validator-required field to an actual service writer; remove or redesign fields that are only test-populated. |

## 4. Release, certification, rehearsal, manifest, and cutover structures

These structures are not inert. They have substantial executable consumers in `dish_pg/release.py`,
`dish_pg/cutover_control.py`, `dish_pg/final_asana_closure.py`, `dish_pg/candidate_manifest.py`,
`dish_pg/release_validation.py`, and `scripts/dish-pg-release`.

| Structure | Proven production/ops consumers | Test/generated/doc consumers | Verdict and migration boundary |
| --- | --- | --- | --- |
| `release_candidates` | Candidate creation/evaluation/replacement, manifest binding, cutover service, readiness, and admission. | Native, PGlite, migration, Stage 6 tests. | **Unsafe direct deletion.** Merge to a cutover-record draft/revision only after every candidate FK, evaluator, command, and admission check is migrated. |
| `rehearsal_runs`, `rehearsal_checkpoints` | `release.py`, `release_evidence.py`, and release CLI rehearsal commands. | Release chronology/evidence tests. | **Unsafe direct deletion.** Move to external immutable rehearsal reports and preserve digest/status checks. |
| `release_candidate_manifests`, `cutover_approval_manifest_bindings`, `candidate_manifest_revalidations` | Candidate snapshot building, approval binding, and revalidation in `candidate_manifest.py`. | PGlite and Stage 6 manifest tests. | **Unsafe direct deletion.** Collapse to one canonical cutover snapshot digest stored directly on the approval/revision. |
| `cutover_approvals` | Approval creation/validation, cutover start, final closure, candidate manifest binding. | Manifest and cutover tests. | **Unsafe direct deletion.** Approval identity, exact words/payload, snapshot digest, and timestamp must remain durable. |
| `cutover_runs`, `cutover_checkpoints` | Full cutover state machine, release status, workflow admission, first request, and CLI transitions. | Stage 6–8 and native first-request tests. | **Unsafe direct deletion.** Replace with the immutable cutover record and explicit admission transitions before changing runtime admission. |
| `final_asana_closures`, `final_asana_closure_invalidations`, `cutover_recertifications` | Final closure, invalidation, recertification, and release evaluation. | Stage 7 tests. | **Unsafe direct deletion.** Merge into revisioned final import/reconciliation reports and new signed approval revisions. |
| `legacy_writer_fences`, `writer_fence_artifact_observations` | Writer-fence preparation, artifact identity validation, engagement, verification, activation, and rollback-burn gates. | PGlite/schema and cutover tests. | **Unsafe direct deletion.** Mechanical fence guarantee and exact writer inventory must survive in the writer-fence report/cutover record. |
| `first_admission_plans`, `first_request_reservations` | Exact first request planning, reservation, consumption, workflow admission, and cutover verification. | Native/PGlite/Stage 6–8 tests. | **Unsafe direct deletion.** Collapse only after the replacement `exact_request` admission mode enforces exact principal/run/command/payload/request identity and replay. |
| `authority_activations` | Generation activation and cutover authority transitions in repositories/release/cutover services. | Stage 2/6 tests. | **Unsafe direct deletion.** May become an activation event on the cutover record, but the one-active-generation and activation provenance guarantees remain. |
| `mutation_admission_controls` | Runtime workflow admission and cutover opening/closing; guarded by migration `0029`. | Extensive admission tests. | **Unsafe direct deletion.** Replace with the approved three-state `closed`/`exact_request`/`open` database-enforced gate; current two-state table is a migration target, not disposable without replacement. |
| `backup_evidence` and related release fields | Candidate/cutover validation and operational evidence tooling. | Stage 8 and operations-evidence tests. | **Unsafe direct deletion.** Merge to a verified backup/restore report referenced by digest. |

### Release CLI commands that must migrate with a control-plane collapse

`dish/scripts/dish-pg-release` exposes executable consumers for candidate creation, evidence recording,
rehearsal recording, bundle construction, final closure/invalidation/recertification, approval,
writer-fence transitions, cutover prepare/activate/burn, runtime attestation, projection-worker
readiness, first admission, completion, and abort. A schema-only deletion would leave these commands
broken or, worse, silently weaker. The CLI/runbook/API migration is part of the same change as any
approved table collapse.

## 5. Semantic proposal and Human Review structures

| Structure/surface | Production use | Test/generated/doc use | Verdict |
| --- | --- | --- | --- |
| Legacy semantic proposals (`proposals`, `apply-proposal`, `review-queue`, `review-inspect`, `review-approve`, `review-reject`) | Fully implemented on the Asana/SQLite path in `dish_tool/semantic_proposals.py`, review/admin commands, service routing, and client/OpenAPI surfaces. | Lifecycle, review-queue, Action/OpenAPI, and baseline tests. | **Retain behavior.** These source-only commands are the explicit Stage A acceptance blocker recorded in the archived acceptance report. |
| PostgreSQL command contract | `dish_pg/command_contract.py` does not provide the legacy semantic-proposal/review lifecycle; Stage A baseline classifies those commands as source-only. | Stage A release decomposition and command-contract tests. | **Unresolved cutover blocker.** Port the approved asynchronous Human Review semantics or define the approved retained authority route before cutover. |
| `human_review_requirements`, `human_review_decisions`, `evidence_holds`, authorization grants/states/events | Real readers/writers in `dish_pg/command_port.py`, `workflow.py`, `read_model.py`, and release validation. | Stage 3/4 audit and workflow tests; frontend generated read contract references some current-state tables. | **Unsafe direct deletion.** Names/table count may be simplified, but exact candidate/evidence/question/answer/decision/application lineage and Marco authorization binding must survive. |
| Planning challenge / override | Legacy service and PostgreSQL paths issue/claim/consume/settle planning challenges. `dish_service/application.py` strips intent fields before ordinary command execution; mutation authorization uses separate Marco authorization structures. | Planning intent, concurrency, baseline, and command-effect tests. | **Unresolved Phase 1 verification gate.** Execute the specified cross-surface authorization-leakage test; do not infer safety from separate type names alone. |

## 6. Request, lease, operation, projection, and reconciliation structures

| Cluster | Proven production use | Test/generated/doc use | Verdict |
| --- | --- | --- | --- |
| `service_requests`, request outcomes, command executions | Exact request replay, request binding, command execution, shadow translation, release validation, and admission. | Broad legacy/PG request, replay, cutover, and audit suites. | **Unsafe direct deletion.** Preserve first-authoritative-outcome replay, pending/uncertain fail-closed behavior, principal/run/command/payload binding, and externally visible request ID. |
| `service_runs` | Shadow/cutover workflow and request ownership. | Migration and cutover fixtures. | **Unsafe direct deletion** unless run identity is moved to requests/executions without weakening ownership. |
| `service_leases`, workflow operations, operation actor facts, execution claims | Live concurrency, safe reclaim, read model, abandonment/recovery, command port, and release validation. | Extensive legacy and PG concurrency/recovery tests; frontend generated read contract consumes selected fields. | **Unsafe direct deletion.** May be normalized/merged only around the approved safe-reclaim transition model. |
| Projection epochs, outbox events, attempts, correlations, drift items, mappings | Real command-port writes, worker claims/dispatch/settlement, external-effect fencing, cutover gates, release validation, and reconciliation. | Native concurrency, PGlite migration, projection/recovery, and frontend generated contracts. | **Unsafe direct deletion.** Preserve atomic ordered intent, generation/epoch fence, no shadow effects, durable attempt identity, observation, and uncertain settlement. |
| `projection_reconciliation_runs`, `projection_reconciliation_items` | Reconciliation worker, candidate manifest, release validation, and cutover verification. | Native/PGlite/release tests. | **Unsafe direct deletion.** Current implementation also has a runtime gap: no concrete production fetcher/comparator and not all release fields can be populated. Replace with an independently sourced complete report before collapsing. |
| Task/project/section/completion authority and current-head tables | Command creation/mutation, import, read model, release evaluation, reconciliation, and projection mapping. | Core authority, import, API, frontend generated contracts, and migration tests. | **Unsafe direct deletion.** Individual event/current tables may be simplified only after a target schema specification proves all retained reads, concurrency checks, and provenance. |

## 7. `task_gid` and canonical `create` response consumers

### Current production behavior

- Legacy `dish_tool.commands._step5_create()` creates an Asana task first and returns `task_gid` both
  at envelope top level and in `data.task_gid`.
- `dish_tool.results.result_envelope()` has a top-level `task_gid` field used by all legacy results.
- `dish_service/openapi.py` and checked-in `openapi/dish-action.openapi.json` expose that envelope.
- `DishServiceClient`/`DishActionClient`, CLI rendering, and downstream lifecycle calls pass the
  returned `task_gid` to `start`, `read`, `prepare`, `inspect`, approval, rejection, and submission.
- The current GPT Action template `deploy/gpt-action.md` instructs agents to discover and use
  `task_gid`.
- PostgreSQL `_create()` currently returns `task_id`, `content_version_id`, and
  `projection_event_id`; it does not implement the approved external `dish_id`/`url`/optional
  `asana_task_gid` response.

### Proven test-only/generated consumers

- `tests/test_action_full_lifecycle.py` assigns `task_gid = created["task_gid"]` and uses it through
  the complete lifecycle.
- Numerous request/Action/OpenAPI/model tests encode `task_gid` argument and result semantics.
- The checked Action OpenAPI contains the top-level result field and command argument schemas.
- Shadow translation/evidence code recognizes legacy `task_gid` and target `task_id` aliases.

### External consumer gap

`CLAUDE.md` says the live custom GPT instructions are outside the repository at
`~/honest-pantry/dish-custom-gpt-instructions.md`. That file was not present in the supplied archive.
Its exact `create`/`task_gid` assumptions are therefore **unresolved** and must be inspected on the
host before the response migration is approved.

### Verdict

The old `create` contract is **unsafe to remove without a coordinated migration**. Phase 1 must
approve and test this exact treatment:

1. external result requires canonical `dish_id`;
2. `url` is optional and resolves to the same Dish UUID;
3. `asana_task_gid` is optional and only carries a real Asana GID after projection identity exists;
4. `task_gid` is never repurposed to contain a Dish UUID;
5. service envelope, clients, CLIs, checked/generated OpenAPI, shadow translators, GPT template,
   live custom GPT instructions, tests, and any rehearsal scripts change together;
6. exact replay returns the same canonical Dish UUID even when Asana projection is delayed or fails.

## 8. Duplicated command and contract consumers

Command identity, consequence/replay classification, exposure, effects, migration disposition, and
dark-launch treatment are distributed across:

- `dish_service/command_spec.py`;
- `dish_service/openapi.py` and checked `openapi/dish-action.openapi.json`;
- `dish_service/client.py` request-ID generation lists;
- `dish_tool/commands.py` handler/command sets;
- `dish_tool/admin_command_spec.py` and admin routing;
- `dish_pg/command_contract.py`;
- `dish_pg/command_effects.py`;
- `dish_pg/command_port.py` handler membership;
- `dish_shadow/policy.py`;
- `docs/database-backend-stage-a-baseline.json` treatment inventory;
- GPT Action instructions and command/API oracle tests.

This is a **merge candidate, not a deletion candidate**. The canonical typed command definition must
first derive service validation, clients’ request-ID behavior, CLI metadata, OpenAPI, PG target
membership, dark-launch treatment, and test inventories. The current defects demonstrate the hidden
consumer risk: `inspect` is consequential in PG effects/port code but classified as read-only in the
legacy service and GPT template, while `apply-proposal` is replay-bound but omitted from both bundled
client request-ID generation lists.

## 9. Documented future frontend consumers

The frontend review/handoff documents are **documentation/planned consumers**, not current production
consumers. Gate A is pending, Gate B is pending, and the pre-database handoff explicitly blocks real
authentication and canonical board reads until the PostgreSQL rollout has an exact final head. They
therefore must not be used to claim that current fixture-only code proves production need. They do,
however, create an explicit post-cutover compatibility obligation for any table or field simplified
now.

| Current authority or proposed structure | Documented frontend dependency | Consumer class | Disposition consequence |
| --- | --- | --- | --- |
| `service_leases` | Gate B proposes exact expired/invalid/contested attention and bounded disclosures, but invalid/contested have no accepted current predicate. | Documentation/planned only. | Do not retain the current table solely for the future UI; preserve the approved lease fact in the target authority and require Gate B to reconcile to that final schema. |
| Verification cycles and Human Review requirements | Gate B can map awaiting-human-review, but failed/disputed and current-cycle precedence are unresolved. | Documentation/planned only; current Human Review runtime is separately a real production consumer. | Semantic/Human Review simplification must expose an exact read contract after the source workflow is ported; no frontend inference from free text. |
| Evidence holds, abandonment attempts, and operation-succession edges | Gate B names them as candidate sources for holds, abandonment, and succession attention/disclosures. | Documentation/planned only; the workflow structures have independent production consumers. | A merger may change table shape, but must leave one approved factual read surface or require a reviewed frontend contract amendment. |
| Projection epoch/mapping/event/attempt/observation/adjudication/drift/readiness structures | Gate B requires one reducer for delayed/failed/drifted/unknown/unavailable/current/not-configured. | Documentation/planned only; projection/release runtime consumers are real and listed above. | Collapse only after the target projection report/state can support an approved reducer; the current unaccepted reducer proposal is not a retention decision. |
| Current task/content/project/section/completion/workflow facts | Gate B requires a coherent set-oriented board/detail query with incomplete/current membership and no raw identities or legal actions. | Documentation/planned only; these facts also have current production consumers. | Target schema must preserve factual queryability, but `PostgresReadModel.section_tasks()` and `task_view()` are not approved browser contracts. |
| Frontend security/session/limiter/audit/password and route-identity/recovery-support tables | Gate A/B propose these as future frontend-owned support state. They do not exist in the current 102-table model. | Proposed future schema, not a current consumer. | Do not invent them during this Phase 1 disposition. Reconcile separate post-cutover migrations against the final PG head and prove they cannot become workflow authority. |
| Final Alembic head, schema fingerprint, release/ledger identity, and indexes/query plans | The pre-database handoff and Gate B B-01 require a final rollout reconciliation record. Existing frontend reconciliation names stale head `0012`. | Documentation/operational handoff. | Schema squash/cutover must publish exact identity; frontend review must be rerun afterward rather than preserving stale filenames or head numbers. |

This separation prevents two opposite errors: deleting a fact that an approved future contract may
need without recording the handoff, and treating a pending frontend design as evidence that the
current physical table must survive unchanged.

## 10. Runtime code without deployable composition

| Runtime area | Code that exists | Missing production composition | Classification |
| --- | --- | --- | --- |
| PostgreSQL authority service | Command port, workflow/repository services, OpenAPI generation, and database helpers exist. | No production HTTP/process composition, auth/principal mapping, health/readiness, configuration, or service unit using the PostgreSQL authority. | **Unresolved cutover blocker.** Existing classes are not a deployable authority. |
| Projection worker | Generic worker and `module:attribute` adapter loading exist. | No concrete production Asana adapter or supervised production worker unit. | **Unresolved cutover blocker.** Generic injection is not proof of a deployable adapter. |
| Reconciliation worker | Generic fetcher/comparator injection and reconciliation storage exist. | No concrete production Asana corpus fetcher/comparator; current path cannot populate every release-validation field. | **Unresolved cutover blocker.** |
| Importer/baseline | Generic importer, legacy source reader, bootstrap, location manifest, and rehearsal scripts exist. | Production baseline capture points at a TEST SQLite manifest path; production-specific session/preparation/already-imported composition and sealed final report are absent. | **Unresolved cutover blocker.** |
| Deployment units | Legacy service/router and dark-launch shadow-worker units/templates exist. | No production PG authority, projection-worker, reconciliation-worker, or importer composition. | **Unresolved cutover blocker.** |

## 11. Deletion classification summary

### Safe candidates after target-contract approval

- `causality_edges`;
- `request_uncertainty_resolutions`;
- standalone `applied_migration_events` table after migration provenance moves to a retained record.

### Unsafe direct deletions

- release candidates/evidence/bundles;
- rehearsal runs/checkpoints;
- candidate manifests/bindings/revalidations;
- cutover approvals/runs/checkpoints/closures/recertifications;
- writer fences and observations;
- first-admission plans/reservations;
- admission and activation authority;
- request/lease/operation/projection/reconciliation structures;
- semantic proposal/Human Review/authorization structures;
- current `task_gid` response and argument contract.

### Unresolved before disposition

- generation bootstrap authority reader without a producer;
- source-import native links required by validators without a producer;
- typed readiness inventory/requirements/evidence/completion required by validators without a producer;
- invocation-audit fields that tests can populate but production cannot yet prove;
- live custom GPT instructions outside the archive;
- planning challenge/override authorization leakage;
- target treatment for every source-only semantic-review command.

## Remaining evidence gaps

1. Inspect the live custom GPT instruction file and any host-local wrappers not in the archive.
2. Run the native PostgreSQL certification lane and the production-shaped rehearsal; the supplied
   PGlite report is explicitly non-certifying.
3. Trace every release validator field to a real production writer after the target control-plane
   specification is approved.
4. Execute a production-shaped final import/reconciliation using an independently enumerated Asana
   corpus, not projection mappings as the expected membership source.
5. Reconfirm immediately before migration squash that no PostgreSQL data outside disposable fixtures
   requires preservation.

# PostgreSQL schema contract: revisions 0019–0027

This is the narrow durable interface for release, cutover, import, reconciliation, readiness, and runtime agents. It lists stable schema names, database-enforced behavior, application-computed values, and required transitions. It is not general architecture documentation.

## Linear revision sequence

1. `0018_projection_attempt_lifecycle` → `0019_request_run_owner_consistency`
2. `0019_request_run_owner_consistency` → `0020_first_request_reservation`
3. `0020_first_request_reservation` → `0021_writer_fence_artifact_identity`
4. `0021_writer_fence_artifact_identity` → `0022_candidate_state_manifest`
5. `0022_candidate_state_manifest` → `0023_legacy_request_tombstones`
6. `0023_legacy_request_tombstones` → `0024_typed_import_linkage`
7. `0024_typed_import_linkage` → `0025_reconciliation_observation_boundary`
8. `0025_reconciliation_observation_boundary` → `0026_typed_worker_readiness_evidence`
9. `0026_typed_worker_readiness_evidence` → `0027_server_default_alignment` (head)

## 0019 — exact service-run ownership

ORM: `dish_pg.stage3_models.ServiceRequest`.

Database contract: `service_requests(generation_id, owner_id, run_id)` has composite FK `fk_service_requests_exact_run_owner` to `service_runs(generation_id, owner_id, run_id)`. `service_runs` exposes matching composite uniqueness. PostgreSQL rejects a request whose referenced run belongs to another owner, including raw SQL writes.

Application responsibility: supply the owner attached to the referenced service run. No new state transition is introduced.

Consumers: request admission and replay services.

## 0020 — exact first-request reservation

ORM/table: `dish_pg.reservation_models.FirstRequestReservation` / `first_request_reservations`.

Service-facing fields: `reservation_id`, `plan_id`, `cutover_run_id`, `candidate_id`, `generation_id`, `request_id`, `command_name`, `owner_id`, `principal_class`, `run_id`, `canonical_payload_sha256`, `state`, `reservation_revision`, `reserved_at`, `consumed_at`.

Application-computed: canonical payload SHA-256, identity UUIDs, command/owner/principal, and timestamps.

Database-enforced: exact candidate/generation, cutover/candidate, first-admission-plan identity, run/owner lineage, one reservation per plan/cutover/candidate/generation/request, and state/timestamp consistency. While mutation admission is open, the request-admission trigger locks the reservation row. Only the request matching every reserved identity may consume it. A different first contender fails closed. A consumed exact request proceeds through native request uniqueness and replay semantics; concurrent contenders cannot both consume the row.

Required transition: `reserved` → `consumed` or `cancelled`; terminal rows cannot be rewritten.

Consumers: cutover logic creates the row before opening admission; request admission supplies the exact persisted request identity.

## 0021 — writer-fence artifact identity

ORM/table: `dish_pg.artifact_identity_models.WriterFenceArtifactObservation` / `writer_fence_artifact_observations`.

Service-facing fields: `observation_id`, `fence_id`, `candidate_id`, `artifact_generation_identity`, `canonical_path`, `content_sha256`, `filesystem_device`, `filesystem_inode`, `file_type`, `regular_file`, `verification_result`, `observation_contract_version`, `observed_at`, `recorded_at`, `evidence_sha256`.

Changed table: `legacy_writer_fences` adds `artifact_observation_id` and `artifact_verification_result` with an exact composite FK to the observation.

Application-computed: secure filesystem observation, canonical path, content SHA-256, device/inode, artifact generation, observation contract, evidence digest, and timestamps. Filesystem traversal, symlink policy, and observation sequencing remain runtime-owned.

Database-enforced: immutable observations; one observation per fence; canonical absolute path; regular-file assertion; positive device/inode; valid verification result; chronology; exact fence/candidate identity. A fence may become `engaged`, `verified`, or `released` only with a bound `matched` regular-file observation. The binding cannot change after preparation.

Consumers: runtime/filesystem observer and cutover fence logic.

## 0022 — candidate authority manifest version 2

ORM/tables:

- `dish_pg.candidate_manifest_models.ReleaseCandidateManifest` / `release_candidate_manifests`
- `dish_pg.candidate_manifest_models.CutoverApprovalManifestBinding` / `cutover_approval_manifest_bindings`
- `dish_pg.candidate_manifest_models.CandidateManifestRevalidation` / `candidate_manifest_revalidations`

Top-level durable identity fields:

- `manifest_version = 2`
- `candidate_id`
- `generation_id`
- `source_import_batch_id`
- `source_import_run_id`
- `shadow_baseline_id`
- `projection_epoch_id`
- active `registry_version_id`
- registry `honest_binding_id`
- `builder_contract_version = 'candidate-authority-v2'`

Release-critical component fields stored on the approved manifest:

- `mapping_membership_sha256`
- `import_completion_sha256`
- `typed_import_linkage_sha256`
- `reconciliation_evidence_sha256`
- `readiness_inventory_sha256`
- `readiness_completion_sha256`

The `canonical_fingerprint` is SHA-256 of the top-level identity plus all six component digests. Each revalidation stores the same six values with an `observed_` prefix as well as `approved_fingerprint`, `observed_fingerprint`, `result`, and `revalidated_at`.

### Canonical component definitions

`mapping_membership_sha256` hashes the exact active project, section, and task mapping rows for the candidate generation and projection epoch. Canonical rows include mapping identity, native entity, alias, state, revision, binding time, and retirement time.

`import_completion_sha256` hashes the exact `source_import_batches` row and its `stage_a_import_runs` row, including source provenance, expected/imported counts, status, and completion chronology.

`typed_import_linkage_sha256` hashes all source import evidence for the candidate batch and every typed `source_import_native_links` row for that batch. The schema/presence marker is part of the digest, so adding the typed-link schema or changing its exact corpus invalidates an earlier fingerprint.

`reconciliation_evidence_sha256` hashes the deterministic latest reconciliation run for the candidate generation and epoch and all exact item rows for that run. Selection is `started_at DESC, reconciliation_run_id DESC`; release-gate selection uses the same tie-break. Run schema presence, candidate/registry binding, observation boundary, counts/status, corpus digest, adapter contract, and item evidence are included when present.

`readiness_inventory_sha256` hashes every candidate inventory plus its exact required probe rows.

`readiness_completion_sha256` hashes the candidate's readiness row, typed probe evidence, and exact completion row. Schema/presence markers are included, so rows introduced after approval cannot be treated as part of the approved state.

### Computation and enforcement

Application-computed: canonical row serialization, deterministic table selection and ordering, six component digests, canonical fingerprint, UUIDs, and timestamps. The builder performs a narrow explicit read of the listed tables; it does not perform a generic table-by-table authority traversal.

Database-enforced:

- manifest version and all digest lengths;
- exact candidate/import/generation/registry/epoch/baseline lineage;
- immutable manifest, approval binding, and revalidation rows;
- one exact approval binding per candidate;
- `matched` revalidation may not claim component values different from the approved manifest;
- activation uses the latest revalidation for the approved manifest, requires it to be `matched`, requires it at or after the approval binding, and rejects a later stale observation even when an earlier match exists;
- unchanged top-level lineage is rechecked at activation.

Required flow: build version-2 manifest during approval → bind the exact approval → rebuild all six components immediately before activation → insert immutable revalidation → activate only when the latest result is `matched`. Any component change beneath unchanged top-level IDs makes the candidate stale and requires a replacement candidate or restored exact certified state.

Consumers: approval service, release manifest builder, activation service, import/reconciliation/readiness agents whose durable rows contribute to the fingerprint.

## 0023 — legacy request-ID tombstones

ORM/table: `dish_pg.legacy_request_models.LegacyRequestTombstone` / `legacy_request_tombstones`.

Fields: `tombstone_id`, globally unique `request_id`, `source_authority`, `import_run_id`, optional `import_batch_id`, `source_identity_sha256`, optional `source_metadata`, `imported_at`.

Application-computed: source identity digest and optional audit metadata.

Database-enforced: source/import FKs, immutable rows, one tombstone per request ID, no tombstone for an existing native PostgreSQL request, and no future native admission with a tombstoned ID. Native PostgreSQL replay remains governed by `service_requests` and is unaffected.

Consumers: legacy import creates all approved tombstones before PostgreSQL admission opens; request admission rejects reuse without an application-side prequery.

## 0024 — typed source-evidence/native-object linkage

ORM/table: `dish_pg.import_link_models.SourceImportNativeLink` / `source_import_native_links`.

Fields: `link_id`, `evidence_id`, `import_batch_id`, `import_run_id`, `entity_kind`, exactly one of `project_id`, `section_id`, `task_id`, `content_version_id`, `request_tombstone_id`, and `linked_at`.

Allowed kinds: `project`, `section`, `task`, `content`, `request_tombstone`.

Application-computed: approved source-evidence-to-native-object mapping and link timestamp.

Database-enforced: real FK for each target column; exactly one target matching `entity_kind`; evidence linked once; target linked at most once per batch; evidence, batch, run, kind, canonical source target, and native import lineage agree; links immutable. Index: `ix_import_native_links_run_kind(import_run_id, entity_kind)`.

Consumers: import logic writes links after native object creation; release/import gates compare the required native corpus to exact typed links.

## 0025 — reconciliation observation boundary

ORM/table: `dish_pg.stage5_models.ProjectionReconciliationRun` / `projection_reconciliation_runs`.

Added fields: `candidate_id`, `registry_version_id`, `observation_started_at`, `observation_completed_at`, `external_snapshot_identity`, `external_high_water`, `corpus_manifest_sha256`, `scope_complete`, `adapter_contract_version`, `evidence_recorded_at`.

Application-computed: corpus-manifest digest, adapter contract, observation start/completion, external snapshot or high-water only when the adapter supplies it, scope completion, and local recording time.

Database-enforced: coherent candidate/generation/projection-epoch/active-registry identity; required digest, adapter version, observation start, and local recording time for candidate-bound rows; complete rows require `scope_complete=true` and completed chronology; identity fields immutable and optional external/completion fields once-set.

Application-validated: adapter-specific external-boundary meaning, pagination/scope semantics, freshness window, and equality of the corpus digest to the required release corpus.

Consumers: reconciliation adapter and release gate. Revision 0022 includes the selected run and item corpus in the candidate fingerprint.

## 0026 — typed worker readiness evidence

ORM/tables:

- `dish_pg.readiness_evidence_models.WorkerProbeInventory` / `worker_probe_inventories`
- `dish_pg.readiness_evidence_models.WorkerProbeRequirement` / `worker_probe_requirements`
- `dish_pg.readiness_evidence_models.WorkerProbeEvidence` / `worker_probe_evidence`
- `dish_pg.readiness_evidence_models.WorkerReadinessCompletion` / `worker_readiness_completions`

Changed table: `projection_worker_readiness.probe_inventory_id` binds the readiness row to its sealed inventory.

Application-computed: inventory digest and contract, required probe inventory, execution/rehearsal identity, worker/deployed-artifact identity, evidence artifact/digest, completion digest, and timestamps.

Database-enforced: exact candidate/epoch/readiness/inventory/requirement lineage; unique kinds and ordinals; evidence kind matches the requirement; immutable evidence; completion accepted only when the actual requirement count equals the sealed required count and every requirement has one exact `pass` evidence row.

Legacy readiness strings remain compatible but cannot create `worker_readiness_completions` without typed evidence.

Consumers: probe runner and release gate. Revision 0022 includes both inventory and completion corpora in the candidate fingerprint.

## 0027 — durable server-default alignment

ORM fields and durable server defaults:

- `ShadowEnvelope.capture_qualification`: `'legacy'`
- `ShadowEnvelope.envelope_schema_version`: `1`
- `ProjectionEpoch.external_effects_enabled`: `false`
- `ProjectionOutboxEvent.origin`: `'live'`

All four remain active raw-SQL contracts, not backfill-only defaults. ORM metadata carries equivalent `server_default` values while retaining Python defaults. Alembic online and offline configuration enables `compare_server_default=True`.

Consumers: raw SQL writers, ORM writers, metadata drift checks, and migration tests. No state transition changes.

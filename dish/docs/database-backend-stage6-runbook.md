# PostgreSQL Stage 6 operator runbook

Status: **Draft / pending production rehearsal and Marco approval**

This runbook operates the Stage 6 controls implemented in the repository. It does not state that the
PostgreSQL backend is ready for production and it does not authorize cutover. Every production
observation, hash, measurement, fence proof, and approval must come from the environment in which it
was actually obtained.

Governing order remains `database-backend.md`, `database-backend-imp.md`, and
`database-backend-migration.md`. Stop on any disagreement rather than adapting this procedure from
memory.

## 1. What the repository can prove offline

The repository can migrate an empty target through `0008_fail_closed_admission_outbox`, execute the Stage 1–6
acceptance suites, hash the exact source tree, store immutable evidence revisions and rehearsal
reports, recompute structural closure from PostgreSQL, build deterministic evidence bundles, fence
the legacy HTTP writer mechanically, and resume an interrupted cutover from durable checkpoints.

It cannot generate truthful evidence for the production Asana corpus, production SQLite/sidecar
bundle, backup/PITR behavior, process and credential fencing, or Marco's decision. Record those only
after running the corresponding operation in the production-shaped or production environment.

## 2. Required environment and identities

Use an isolated operator shell with:

```sh
export DISH_PG_URL='postgresql+psycopg://...'
export DISH_LEGACY_WRITER_FENCE='/absolute/path/legacy-writer-fence.json'
```

The legacy service process must resolve the same `DISH_LEGACY_WRITER_FENCE` path. The file and its
parent directory must be on storage whose write, rename, permission, and directory-fsync behavior is
under operator control.

Freeze and retain these exact identities before candidate creation:

- final source Dish release and commit;
- production-change-ledger high-water commit;
- transactionally complete SQLite database SHA-256;
- every sidecar identity and SHA-256;
- final Asana task/project/section/registry observation identity;
- Stage 2 import run and Stage 5 source-import batch;
- closed shadow baseline;
- active projection epoch and completed reconciliation run;
- Dish, Honest, protocol, OpenAPI, and routing releases;
- PostgreSQL schema head `0008_fail_closed_admission_outbox`.

A changed source commit, ledger high-water mark, production object, release, schema head, or proof gap
requires a new or revised candidate. Do not relabel an old evidence bundle.

## 3. Migrate and produce the offline acceptance report

```sh
scripts/dish-pg-release migrate
scripts/dish-pg-acceptance \
  --python .venv/bin/python \
  --output /secure/evidence/stage6-acceptance.json
```

The acceptance report contains the complete source-file manifest, source-manifest SHA-256, each gate
command and exit status, captured output and output hash, and a report SHA-256. A nonzero gate makes
the report failing evidence; do not edit it into a pass. Rerun after fixing the cause and record a new
evidence revision.

The final repository gate is the complete suite. `--skip-full` is for development rehearsal only and
cannot satisfy production acceptance.

## 4. Create the release candidate

Prepare a mode-0600 JSON file containing exact UUIDs and release identities:

```json
{
  "candidate_id": "UUID",
  "generation_id": "UUID",
  "source_import_batch_id": "UUID",
  "shadow_baseline_id": "UUID",
  "projection_epoch_id": "UUID",
  "source_release": "EXACT_RELEASE",
  "source_commit": "EXACT_COMMIT",
  "ledger_through_commit": "EXACT_COMMIT",
  "schema_head": "0008_fail_closed_admission_outbox",
  "dish_release": "EXACT_RELEASE",
  "honest_release": "EXACT_RELEASE",
  "protocol_release": "EXACT_RELEASE",
  "openapi_release": "EXACT_RELEASE",
  "routing_release": "EXACT_RELEASE",
  "created_at": "RFC3339_WITH_OFFSET"
}
```

```sh
scripts/dish-pg-release candidate-create --file /secure/input/candidate.json
```

Candidate creation also creates a closed mutation-admission control for the generation. It does not
open PostgreSQL mutation authority. Running this against the live production generation halts new
request admission from that instant — treat it as the first operational step of cutover, not a
preparatory or read-only one.

## 5. Record acceptance evidence

Record one JSON object per evidence item:

```json
{
  "category": "authority_coverage",
  "evidence_key": "current_to_target",
  "outcome": "pass",
  "payload": {
    "artifact_kind": "authority-coverage-report",
    "artifact_identity": "authority-coverage@SOURCE_COMMIT",
    "artifact_path": "/secure/evidence/authority-coverage.json",
    "artifact_sha256": "64_LOWERCASE_HEX",
    "source_manifest_sha256": "64_LOWERCASE_HEX",
    "gate_name": "authority_coverage:current_to_target",
    "gate_result": "pass"
  },
  "recorded_at": "RFC3339_WITH_OFFSET"
}
```

```sh
scripts/dish-pg-release evidence-record CANDIDATE_UUID --file ITEM.json
```

Required category/key pairs are:

| Category | Key |
| --- | --- |
| `authority_coverage` | `current_to_target` |
| `command_semantic_delta` | `retained_commands` |
| `characterization` | `frozen_current_behavior` |
| `production_change_ledger` | `source_commit_closure` |
| `fault_injection` | `crash_boundaries` |
| `contention` | `same_task_and_independent_tasks` |
| `backup_restore` | `restore_rehearsal` |
| `create_correlation` | `lost_response_safety` |
| `protocol_coherence` | `service_openapi_routing` |

Only `pass` and `fail` are accepted outcomes. Each category/key pair has one exact artifact kind,
gate name, artifact identity/path, artifact SHA-256, source-manifest SHA-256, and matching gate result.
A bare operator assertion such as `{"result":"pass"}` is not evidence. Evidence is append-only while
the candidate is assembling and frozen after validation. All externally supplied SHA-256 values are
exact 64-character lowercase hexadecimal strings.

## 6. Record production-shaped rehearsals

The required rehearsal classes are `full`, `activation`, `restore`, and `fault_injection`. Each input
binds an exact source manifest and may include monotonic checkpoints:

```json
{
  "rehearsal_kind": "restore",
  "environment_identity": "EXACT_ENVIRONMENT_ID",
  "source_manifest_sha256": "64_HEX",
  "started_at": "RFC3339_WITH_OFFSET",
  "checkpoints": [
    {
      "kind": "backup_verified",
      "payload": {
        "rehearsal_kind": "restore",
        "checkpoint_kind": "backup_verified",
        "evidence_kind": "restore-backup_verified-evidence",
        "artifact_identity": "backup@EXACT_ID",
        "artifact_sha256": "64_LOWERCASE_HEX",
        "source_manifest_sha256": "64_LOWERCASE_HEX",
        "gate_result": "pass"
      },
      "recorded_at": "RFC3339_WITH_OFFSET"
    }
  ],
  "passed": true,
  "report": {
    "rehearsal_kind": "restore",
    "source_manifest_sha256": "64_LOWERCASE_HEX",
    "result": "passed",
    "checkpoint_manifest_sha256": "SHA256_OF_ORDERED_CHECKPOINT_KIND_AND_PAYLOAD_DIGESTS"
  },
  "measured_rpo_seconds": 0,
  "measured_rto_seconds": 0,
  "completed_at": "RFC3339_WITH_OFFSET"
}
```

```sh
scripts/dish-pg-release rehearsal-record CANDIDATE_UUID --file REHEARSAL.json
```

Use measured values, not targets. A passed run must contain every class-specific checkpoint, every
checkpoint must carry a passing typed evidence payload bound to the run source manifest, and the final
report must bind the exact ordered checkpoint set. A failed rehearsal remains evidence and requires a
new passed run; it is not overwritten.

The release CLI rejects duplicate JSON object keys recursively before any payload is hashed or stored.
Do not rely on parser "last key wins" behavior in operator files.

## 7. Evaluate, bundle, validate, and obtain approval

```sh
scripts/dish-pg-release evaluate CANDIDATE_UUID
scripts/dish-pg-release bundle CANDIDATE_UUID \
  --kind release_candidate \
  --built-at RFC3339_WITH_OFFSET \
  --output /secure/evidence/release-candidate.json
scripts/dish-pg-release validate CANDIDATE_UUID BUNDLE_UUID \
  --validated-at RFC3339_WITH_OFFSET
```

Evaluation fails closed unless all of the following are true in authoritative PostgreSQL state:

- the generation, import batch, shadow baseline, projection epoch, and registry are exact and ready;
- every target task has current content, membership, placement, completion, and active Asana alias;
- every registry project and section has an active Asana alias;
- no request lacks an outcome and no execution, operation, lease, Planning challenge, authorization
  reservation, Verification cycle, Evidence hold, Human Review, abandonment, or audit obligation is
  unresolved;
- no projection outbox item, attempt, create correlation, or drift item is unresolved;
- the latest completed reconciliation accounts for every active projection mapping;
- the database is at `0008_fail_closed_admission_outbox`;
- every required evidence item and rehearsal class passes.

Bundle identity is deterministic from authoritative contents; build time does not alter its SHA-256.
Validation regenerates the current manifest and rejects a stale supplied bundle.

After validation, capture the final Asana-authoritative interval before requesting approval:

```json
{
  "capture_manifest_sha256": "64_HEX",
  "observation_high_water": "EXACT_ASANA_HIGH_WATER",
  "watcher_identity": "EXACT_WATCHER_AND_RELEASE",
  "interval_started_at": "RFC3339_WITH_OFFSET",
  "closed_through_at": "RFC3339_WITH_OFFSET",
  "payload": {"task_count": 0, "registry_count": 0},
  "recorded_at": "RFC3339_WITH_OFFSET"
}
```

```sh
scripts/dish-pg-release final-asana-closure-record CANDIDATE_UUID \
  --file /secure/evidence/final-asana-closure.json
```

Marco's approval input must quote the exact candidate and bundle decision:

```json
{
  "approver": "Marco",
  "approval_statement": "EXACT_APPROVAL_TEXT",
  "approval_payload": {
    "candidate_manifest_sha256": "64_HEX",
    "accepted_discrepancies": [],
    "measured_rpo_seconds": 0,
    "measured_rto_seconds": 0,
    "final_asana_closure_id": "CLOSURE_UUID",
    "final_asana_closure_sha256": "64_HEX"
  },
  "approved_at": "RFC3339_WITH_OFFSET"
}
```

```sh
scripts/dish-pg-release approve CANDIDATE_UUID BUNDLE_UUID --file APPROVAL.json
```

Do not run `approve` without Marco's explicit decision for that exact bundle.

If any relevant Asana task, project, section, registry metadata, or alias changes after approval,
stop cutover and record the invalidation:

```sh
scripts/dish-pg-release final-asana-closure-invalidate CLOSURE_UUID \
  --file /secure/evidence/final-asana-invalidation.json
```

Capture a new complete closure, then obtain Marco's explicit recertification of the same candidate:

```sh
scripts/dish-pg-release candidate-recertify CANDIDATE_UUID NEW_CLOSURE_UUID \
  --file /secure/evidence/candidate-recertification.json
```

Do not activate against an invalidated, superseded, or time-incomplete closure.

## 8. Prepare and prove the legacy-writer fence

First record the planned fence in PostgreSQL:

```json
{
  "target_identity": "legacy-service@EXACT_HOST_AND_RELEASE",
  "mechanism": "fail-closed-file",
  "manifest": {
    "path": "/absolute/path/legacy-writer-fence.json",
    "service_release": "EXACT_RELEASE",
    "probe_plan": "authenticated POST rejected before body parsing"
  },
  "prepared_at": "RFC3339_WITH_OFFSET"
}
```

```sh
scripts/dish-pg-release writer-fence-prepare CANDIDATE_UUID --file FENCE.json
scripts/dish-pg-release cutover-prepare CANDIDATE_UUID --started-at RFC3339_WITH_OFFSET
scripts/dish-pg-release writer-fence-engage FENCE_UUID \
  --path "$DISH_LEGACY_WRITER_FENCE" \
  --operator Marco \
  --engaged-at RFC3339_WITH_OFFSET
```

The file is written by atomic rename, mode `0600`, followed by file and directory fsync. Presence of
any file at that path fences every legacy POST after credential-scope authentication and before body
loading. Malformed or unreadable JSON remains fenced.

Probe at minimum:

- health and read-only diagnostics remain reachable;
- an invalid bearer token is rejected as authentication failure;
- a valid scoped token with malformed or oversized body is rejected as `legacy_writer_fenced`;
- no current legacy command, admin route, alternate listener, process, or credential can write;
- the exact fence file survives service restart.

Record the writer-fence proof as a mode-0600 JSON object. It must bind the candidate, target and
recorded fence manifest and prove that one authenticated mutation was rejected before body parsing:

```json
{
  "probe_kind": "authenticated_mutation_rejected_before_body_parse",
  "candidate_id": "CANDIDATE_UUID",
  "target_identity": "EXACT_TARGET_IDENTITY",
  "fence_manifest_sha256": "64_HEX",
  "request_token_sha256": "64_HEX",
  "http_status": 503,
  "body_loaded": false,
  "result": "pass"
}
```

Record exact probe evidence:

```sh
scripts/dish-pg-release writer-fence-verify FENCE_UUID \
  --proof-file /secure/evidence/fence-proof.json \
  --verified-at RFC3339_WITH_OFFSET
scripts/dish-pg-release cutover-mark-fenced CUTOVER_UUID \
  --recorded-at RFC3339_WITH_OFFSET
```

## 9. Activation, rollback burn, and admission

These commands are intentionally separate crash boundaries:

```sh
scripts/dish-pg-release cutover-activate CUTOVER_UUID \
  --final-closure-id CLOSURE_UUID \
  --activated-at RFC3339_WITH_OFFSET
```

At this checkpoint the target generation is selected but mutation admission remains closed. A
pre-burn abort is still possible only under the migration document's conditions.

```sh
scripts/dish-pg-release cutover-burn-rollback CUTOVER_UUID \
  --legacy-bundle-id EXACT_IMMUTABLE_BUNDLE_ID \
  --burned-at RFC3339_WITH_OFFSET
```

**Stop point:** after this commits, ordinary return to Asana/SQLite authority is prohibited. Recover
PostgreSQL; do not remove the legacy fence or reverse-import Asana.

After confirming the burn row and closed admission control are durable, record the exact deployed
runtime and route while admission is still closed:

```sh
scripts/dish-pg-release runtime-attestation-record CANDIDATE_UUID \
  --file /secure/evidence/runtime-attestation.json
```

Run the projection worker's claim, exact-write and restart probes and complete a reconciliation of
every active mapping after rollback burn. Record the exact worker release and reconciliation:

```sh
scripts/dish-pg-release projection-worker-ready CANDIDATE_UUID \
  --file /secure/evidence/projection-worker-readiness.json
```

Choose one bounded first production request before opening admission. Its plan must bind the request
UUID, command, optional task UUID, and exact expected projection-event count:

```sh
scripts/dish-pg-release first-admission-plan CUTOVER_UUID \
  --file /secure/evidence/first-admission-plan.json
```

Only after all three immutable records exist:

```sh
scripts/dish-pg-release cutover-open-admission CUTOVER_UUID \
  --opened-at RFC3339_WITH_OFFSET
```

Issue exactly the planned request through the target service. Fulfil or repair its invocation-audit
obligation and complete a post-request reconciliation covering every active projection mapping.
Confirm the immutable successful outcome, committed execution, governed audit, exact applied
projection-event count and complete reconciliation, then record:

```sh
scripts/dish-pg-release cutover-verify-first-admission CUTOVER_UUID REQUEST_UUID \
  --verified-at RFC3339_WITH_OFFSET
scripts/dish-pg-release cutover-complete CUTOVER_UUID \
  --completed-at RFC3339_WITH_OFFSET
scripts/dish-pg-release bundle CANDIDATE_UUID \
  --kind cutover_final \
  --built-at RFC3339_WITH_OFFSET \
  --output /secure/evidence/cutover-final.json
```

The projection worker may run readiness probes before admission, but it must process production
outbox work only under the approved active epoch and release. A mismatched worker release, incomplete
reconciliation, failed claim/write/restart probe, or readiness record created before rollback burn
keeps admission closed.

## 10. Abort and fence release before rollback burn

Before rollback burn, and only when every §14.1 condition in `database-backend-migration.md` is true:

```sh
scripts/dish-pg-release cutover-abort CUTOVER_UUID \
  --reason 'EXACT_REASON' \
  --aborted-at RFC3339_WITH_OFFSET
scripts/dish-pg-release writer-fence-status --path "$DISH_LEGACY_WRITER_FENCE"
scripts/dish-pg-release writer-fence-release FENCE_UUID \
  --path "$DISH_LEGACY_WRITER_FENCE" \
  --expected-sha256 EXACT_FILE_SHA256 \
  --released-at RFC3339_WITH_OFFSET
```

Fence release first commits the authorized database state and only then removes the file. If file
removal or fsync fails, the database may say released while the old writer remains fenced. Treat that
as a safe blocking discrepancy and reconcile it manually. The command never removes the file first.

Fence release is rejected after rollback burn.

## 11. Restart and crash recovery

All cutover transitions and checkpoints are durable and idempotent for the exact identity. After a
process death:

1. query the candidate, fence, admission, cutover run, checkpoints, and authority activation rows;
2. query the fence file independently;
3. resume only the next legal transition represented by those exact records;
4. never infer authority from routing, service reachability, or Asana appearance;
5. never create a replacement candidate, approval, or cutover UUID to bypass a blocked exact run.

State meanings:

| Cutover state | Mutation admission | Ordinary abort |
| --- | --- | --- |
| `prepared` | closed | conditionally allowed |
| `fenced` | closed | conditionally allowed |
| `activated` | closed | conditionally allowed before burn |
| `rollback_burned` | closed | prohibited |
| `admission_open` | open | prohibited |
| `first_admission_verified` | open | prohibited |
| `completed` | open | prohibited |
| `aborted` | closed | terminal pre-burn path |

## 12. Production-environment work still required

The repository package does not complete these actions:

- capture the final production SQLite and sidecar bundle and hashes;
- close the production-change ledger through the exact final commit;
- observe and classify the complete live Asana task/project/section/registry corpus;
- run production-shaped full, activation, fault, backup, restore, and PITR rehearsals;
- establish PostgreSQL backup/WAL archival and independently verify restoration;
- measure and obtain acceptance of actual RPO/RTO;
- deploy coherent target service, protocol, OpenAPI, routing, credentials, and worker releases;
- enumerate and mechanically fence every old writer process, endpoint, credential, and scheduler;
- obtain Marco's exact bundle-bound approval;
- execute rollback burn, open mutation admission, enable projection, and validate the first live request;
- monitor early-cutover health and retain the final immutable evidence package.

Any unresolved item keeps the migration in **Draft / not authorized for production cutover**.

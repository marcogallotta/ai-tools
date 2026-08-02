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

The repository can migrate an empty target through `0005_release_cutover`, execute the Stage 1–6
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
- PostgreSQL schema head `0005_release_cutover`.

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
  "schema_head": "0005_release_cutover",
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
    "artifact": "/secure/evidence/authority-coverage.json",
    "sha256": "64_HEX",
    "observations": {}
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

`pass` means the referenced evidence actually passed. Use `fail`, `blocked`, or `info` honestly.
Evidence is append-only while the candidate is assembling and frozen after validation.

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
      "payload": {"backup_id": "...", "sha256": "64_HEX"},
      "recorded_at": "RFC3339_WITH_OFFSET"
    }
  ],
  "passed": true,
  "report": {"artifact": "/secure/evidence/restore-report.json", "sha256": "64_HEX"},
  "measured_rpo_seconds": 0,
  "measured_rto_seconds": 0,
  "completed_at": "RFC3339_WITH_OFFSET"
}
```

```sh
scripts/dish-pg-release rehearsal-record CANDIDATE_UUID --file REHEARSAL.json
```

Use measured values, not targets. A failed rehearsal remains evidence and requires a new passed run;
it is not overwritten.

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
- the database is at `0005_release_cutover`;
- every required evidence item and rehearsal class passes.

Bundle identity is deterministic from authoritative contents; build time does not alter its SHA-256.
Validation regenerates the current manifest and rejects a stale supplied bundle.

Marco's approval input must quote the exact candidate and bundle decision:

```json
{
  "approver": "Marco",
  "approval_statement": "EXACT_APPROVAL_TEXT",
  "approval_payload": {
    "candidate_manifest_sha256": "64_HEX",
    "accepted_discrepancies": [],
    "measured_rpo_seconds": 0,
    "measured_rto_seconds": 0
  },
  "approved_at": "RFC3339_WITH_OFFSET"
}
```

```sh
scripts/dish-pg-release approve CANDIDATE_UUID BUNDLE_UUID --file APPROVAL.json
```

Do not run `approve` without Marco's explicit decision for that exact bundle.

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

Only after confirming the burn row and closed admission control are durable:

```sh
scripts/dish-pg-release cutover-open-admission CUTOVER_UUID \
  --opened-at RFC3339_WITH_OFFSET
```

Issue one bounded, explicitly selected first request through the target service. Confirm its immutable
request outcome and all expected authoritative and projection evidence, then record:

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

Enable downstream projection workers only at the point approved by the migration plan, with the
active epoch and reconciliation controls already in place.

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

# PostgreSQL cutover operator runbook

Status: **Draft / pending production rehearsal and Marco approval**

This runbook operates the Stage 6 controls implemented in the repository. It does not state that the
PostgreSQL backend is ready for production and it does not authorize cutover. Every production
observation, hash, measurement, fence proof, and approval must come from the environment in which it
was actually obtained.

Cutover policy and authority ordering are governed by `postgresql-cutover.md`; current runtime
invariants are governed by `architecture/postgresql-runtime.md`. Stop on any disagreement rather
than adapting this procedure from memory.

## 1. What the repository can prove offline

The repository can migrate an empty target through the current `dish_pg.release.ALEMBIC_HEAD`, execute the Stage 1–6
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
export DISH_PG_CERT_URL='postgresql+psycopg://.../dish_native_cert'
export DISH_PG_REHEARSAL_URL='postgresql+psycopg://.../dish_rehearsal'
export DISH_PG_RESTORE_URL='postgresql+psycopg://.../dish_restore_verify'
export DISH_PG_SCHEMA_HEAD="$(.venv/bin/python -c 'from dish_pg.release import ALEMBIC_HEAD; print(ALEMBIC_HEAD)')"
```

The legacy service process must resolve the same `DISH_LEGACY_WRITER_FENCE` path. The file and its
parent directory must be on storage whose write, rename, permission, and directory-fsync behavior is
under operator control.

Freeze and retain these exact identities before candidate creation:

- final source Dish release/commit and reviewed production change set;
- transactionally complete SQLite database SHA-256;
- every sidecar identity and SHA-256;
- final Asana task/project/section/registry observation identity;
- Stage 2 import run and Stage 5 source-import batch;
- closed shadow baseline;
- pre-burn active projection epoch and completed final reconciliation run;
- Dish, Honest, protocol, OpenAPI, and routing releases;
- PostgreSQL schema head `$DISH_PG_SCHEMA_HEAD`.

A changed source commit, reviewed change set, production object, release, schema head, or proof gap
requires a new or revised candidate. Do not relabel an old evidence bundle.

## 3. Migrate and produce the offline acceptance report

```sh
scripts/dish-pg-release migrate
scripts/dish-pg-acceptance \
  --python .venv/bin/python \
  --output /secure/evidence/stage-a-acceptance.json
```

The acceptance report contains the pinned focused-test selectors (Stages 1–8 plus later release-safety
owners), complete source-file manifest, source-manifest SHA-256, each gate command and exit status,
captured output and output hash, and a report SHA-256. Source acceptance separates the governed
production-source digest check as `baseline_identity_gate`. The baseline no longer freezes hashes of
the test-file corpus. Canonical Stage 1 regeneration remains in the focused gate, so a stale baseline
is a source-acceptance failure as well as a failed identity gate. Any nonzero required source gate
remains a failing report. Rerun after fixing the cause and record a new evidence revision.

The final source repository gate is the complete suite apart from the separately executed baseline
identity check. `--skip-full` is for development rehearsal only and cannot satisfy production
acceptance. Governed re-baselining and a passing `baseline_identity_gate` remain separately mandatory
before production acceptance is complete.

### 3.1 Native PostgreSQL certification

When the separate disposable `DISH_PG_CERT_URL` database is available, run the governed inventory exactly once and
retain its mode-0600 report:

```sh
DISH_TEST_POSTGRESQL_DSN="$DISH_PG_CERT_URL" \
  .venv/bin/python scripts/dish-pg-native-certification \
    --expected-head <reviewed-head-sha> \
  --python .venv/bin/python \
  --output /secure/evidence/native-postgresql-certification.json
sha256sum /secure/evidence/native-postgresql-certification.json \
  > /secure/evidence/native-postgresql-certification.json.sha256
```

A missing DSN is **unavailable**, not passed. PGlite and source acceptance are not substitutes for
this certification report.

### 3.2 Clean migration rehearsal

Provision `DISH_PG_REHEARSAL_LIBPQ_URL` as a disposable database. Before migration, prove that its
`public` schema contains no tables; stop if the count is not zero:

```sh
test "$(psql "$DISH_PG_REHEARSAL_LIBPQ_URL" -XAtv ON_ERROR_STOP=1 \
  -c "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public'")" = 0
DISH_PG_URL="$DISH_PG_REHEARSAL_URL" \
  .venv/bin/python scripts/dish-pg-release migrate \
  > /secure/evidence/clean-migration-rehearsal.log 2>&1
sha256sum /secure/evidence/clean-migration-rehearsal.log \
  > /secure/evidence/clean-migration-rehearsal.log.sha256
DISH_PG_URL="$DISH_PG_REHEARSAL_URL" \
  .venv/bin/python scripts/dish-pg-operations-evidence database-fingerprint \
  --database-url-env DISH_PG_URL \
  --expected-database-name dish_rehearsal \
  --expected-schema-head "$DISH_PG_SCHEMA_HEAD" \
  --output /secure/evidence/clean-migration-fingerprint.json
```

The fingerprint command is read-only and fails when the selected database, single Alembic head, or
primary-key requirements do not match. It does not create or migrate the database.

### 3.3 Backup and restore verification

Run this only while the production-shaped rehearsal source is quiesced. The source must already
contain material authority rows; an empty migrated schema cannot satisfy this rehearsal. Provision
the restore target as a separate empty database, and mount the retention destination on an
independent filesystem device before starting:

```sh
mkdir -p /secure/evidence/backup-restore
.venv/bin/python scripts/dish-pg-backup-restore-rehearsal \
  --python .venv/bin/python \
  --source-commit "$(git rev-parse HEAD)" \
  --expected-schema-head "$DISH_PG_SCHEMA_HEAD" \
  --expected-source-database dish_rehearsal \
  --expected-restore-database dish_restore_verify \
  --output-dir /secure/evidence/backup-restore \
  --retention-destination /mnt/OFF_DEVICE/dish/postgresql-authority.dump
```

The governed rehearsal refuses `DISH_PG_URL`, `DISH_PG_TEST_URL`, and
`DISH_TEST_POSTGRESQL_DSN` as database inputs and also rejects exact URL aliases of those protected
environments. `DISH_PG_REHEARSAL_URL` and `DISH_PG_RESTORE_URL` are the canonical
`postgresql+psycopg` targets. The command derives the libpq URI used by `psql`, `pg_dump`, and
`pg_restore` from each canonical target; a separately supplied `--source-libpq-url-env` or
`--restore-libpq-url-env` is only a fail-closed compatibility assertion and is never used as the
backup/restore target. A mismatched assertion stops before checkout or database commands.

It verifies the checkout commit, source and restore database identities, and an empty restore
`public` schema before `pg_dump`. It fingerprints every public authority table before and after the
dump and requires the material state to remain identical, then copies the mode-0600 dump to the
independent retention device and verifies its SHA-256 before restore.

Restore uses `--exit-on-error --single-transaction --no-owner --no-privileges`. The restored database
must have the expected single Alembic head and the same complete table inventory, row counts, ordered
row digests, and database fingerprint as the source. The backup and retention artifacts are rehashed
before the rehearsal can pass.

`rehearsal-report.json` binds the exact source commit, canonical source and restore environment names,
the derived-libpq binding (plus any verified assertion environment), source and restore database
identities, PostgreSQL tool versions, dump path/hash/size, independent-retention path/hash/device
identity, all fingerprint/comparison artifacts and hashes, material source row count, and the final
outcome. Failed runs remain hashed failure evidence and never satisfy the cutover gate. This
implements the current off-device-copy requirement only; it does not add a PITR/RPO product policy.

### 3.4 Scheduled production backup operation

The production backup job is deliberately separate from the PostgreSQL service lifecycle. Starting a
backup never starts or restarts PostgreSQL; an unavailable database makes the backup service fail and
remain visible in systemd/journal evidence. The default timer cadence is hourly, on the hour, with
`Persistent=true`. Retention is tiered (GFS-style) via `DISH_PG_BACKUP_RETENTION_TIERS`: full hourly
density for the last 24 hours, thinned to one backup every 4 hours out to 7 days, then one per day out
to 90 days; backups older than the last tier are deleted. Pruning still runs only after a new backup's
local and off-device copies both pass checksum/archive verification. A deployment that sets only the
legacy flat `DISH_PG_BACKUP_RETENTION_SECONDS` (default seven days, `604800` seconds) instead gets the
original untiered behaviour unchanged. Health becomes stale after two hours (`7200` seconds), leaving
one hour of run-time grace beyond the default hourly cadence. All of these values are
operator-configurable.

Before activation, create the mode-0600 environment file from
`deploy/systemd/postgres-backup.env.example`. `DISH_PG_BACKUP_OFF_DEVICE_DIR` must already exist on a
filesystem device different from `DISH_PG_BACKUP_LOCAL_DIR`; the backup command refuses to create a
missing off-device path so an absent mount cannot degrade silently into another local copy. The
default systemd sandbox whitelists the default local backup directory and assumes the off-device
destination is outside `/home` (for example `/mnt`). If an authorized deployment changes
`DISH_PG_BACKUP_LOCAL_DIR`, or chooses an off-device destination under a protected home path, add an
explicit `ReadWritePaths=` drop-in for the configured path rather than weakening the service sandbox
generally.

The repository ships these activation artifacts, but **do not enable or start them merely because
they are installed**. Installation/activation is a separate production-authorized operation:

```sh
install -m 0600 deploy/systemd/postgres-backup.env.example \
  /home/marco/.config/dish-service/postgres-backup.env
# Edit the populated file with the exact production DB identity, deployed Alembic head,
# independent mounted destination, retention, and freshness threshold.

sudo install -m 0644 deploy/systemd/dish-postgres-backup.service \
  /etc/systemd/system/dish-postgres-backup.service
sudo install -m 0644 deploy/systemd/dish-postgres-backup.timer \
  /etc/systemd/system/dish-postgres-backup.timer

# Optional cadence override. Edit OnCalendar before installation. Keep
# DISH_PG_BACKUP_MAX_AGE_SECONDS coherent with the resulting interval.
sudo install -d -m 0755 /etc/systemd/system/dish-postgres-backup.timer.d
sudo install -m 0644 deploy/systemd/postgres-backup-cadence.conf.example \
  /etc/systemd/system/dish-postgres-backup.timer.d/cadence.conf

sudo systemctl daemon-reload
# Production authorization required before either command below:
# sudo systemctl enable --now dish-postgres-backup.timer
# sudo systemctl start dish-postgres-backup.service
```

Each successful service run:

1. fail-closes on the configured database name, single Alembic head, empty-table inventory, missing
   off-device mount, same-device destination, or overlapping backup run;
2. writes a mode-0600 custom-format `pg_dump --no-owner --no-privileges` archive into a hidden local
   candidate directory, validates it with `pg_restore --list`, writes a SHA-256 sidecar, and rehashes
   it;
3. copies that exact archive to a hidden temporary file on the independent device, fsyncs it, checks
   the SHA-256, atomically publishes the copy plus checksum sidecar, rehashes it, and validates the
   copied archive with `pg_restore --list`;
4. atomically publishes the local backup report/directory only after both artifact copies are valid;
5. applies retention only after that new local + off-device pair has succeeded. A failed dump/copy
   therefore cannot prune the previous usable backup. A retention failure leaves the newly verified
   pair in place but makes the run fail visibly.

The local root also carries a self-hashed `last-attempt.json`. The health command rehashes both
latest artifact copies and checksum sidecars, rechecks current device independence, reports the
latest successful backup time/age/database/schema/hash/local path/off-device destination, and fails
when the artifact is stale or the latest attempt failed. Run it with the same environment values as
the service, for example from an operator shell that has loaded the mode-0600 environment file:

```sh
set -a
. /home/marco/.config/dish-service/postgres-backup.env
set +a
.venv/bin/python scripts/dish-pg-scheduled-backup health
systemctl status dish-postgres-backup.timer dish-postgres-backup.service
journalctl -u dish-postgres-backup.service --since '24 hours ago'
```

The scheduled `.dump` is intentionally the same PostgreSQL custom archive shape used by section 3.3
and declares the same clean-restore flags: `--exit-on-error --single-transaction --no-owner
--no-privileges`. This implementation does not itself certify a real production restore. The final
recovery gate must use an artifact produced by an actual authorized timer-triggered run, restore that
artifact into a separate clean database, and execute the existing `database-fingerprint` /
`compare-database-fingerprints` procedure while the production source is quiesced so the source
fingerprint can be bound meaningfully to that backup. Record the scheduled backup report/hash,
off-device hash, clean-restore evidence, source/restored fingerprints, comparison, and exact deployed
source commit together.

## 4. Create the release candidate

Prepare a mode-0600 JSON file containing exact UUIDs plus the canonical source and governed rehearsal-environment identities:

```json
{
  "candidate_id": "UUID",
  "generation_id": "UUID",
  "source_import_batch_id": "UUID",
  "shadow_baseline_id": "UUID",
  "projection_epoch_id": "UUID",
  "source_release": "EXACT_SOURCE_RELEASE",
  "source_commit": "EXACT_SOURCE_COMMIT",
  "ledger_through_commit": "EXACT_SOURCE_COMMIT",
  "source_manifest_sha256": "64_LOWERCASE_HEX",
  "rehearsal_environment_identity": "production-shaped@64_LOWERCASE_HEX",
  "openapi_release": "EXACT_RELEASE",
  "routing_release": "EXACT_RELEASE",
  "created_at": "RFC3339_WITH_OFFSET"
}
```

`schema_head` and `dish_release` are derived from the active authority generation. `honest_release`
and `protocol_release` are derived from the exact Honest release binding referenced by that
generation's active section-registry version. If those four fields are supplied in the JSON for an
operator-side assertion, `candidate-create` requires exact equality and never treats them as the
source of truth. The rehearsal environment identity must use a governed typed form, currently
`production-shaped@<sha256>` or `native-postgresql@<sha256>`.

```sh
scripts/dish-pg-release candidate-create --file /secure/input/candidate.json
```

Candidate creation also creates a closed mutation-admission control for the generation. After an
exact pre-burn abort, a replacement candidate transactionally rebinds that same closed control to
the replacement identity; it never creates a second control for the generation. Candidate creation
does not open PostgreSQL mutation authority. Running this against the live production generation
halts new request admission from that instant — treat it as the first operational step of cutover,
not a preparatory or read-only one.

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
- the database is at `$DISH_PG_SCHEMA_HEAD`;
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


Rollback-burn replay is exact: the legacy bundle identity must be nonblank, and a repeated burn
request must match both the stored bundle identity and burn timestamp. A conflicting replay is an
operator error, not an idempotent success.

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
  "fence_manifest_sha256": "64_LOWERCASE_HEX",
  "request_token_sha256": "64_LOWERCASE_HEX",
  "http_status": 409,
  "response_code": "CONFLICT",
  "response_rule": "legacy_writer_fenced",
  "response_retryable": false,
  "body_loaded": false,
  "result": "pass"
}
```

A `401` response proves only that authentication failed; it is never accepted as writer-fence
evidence. The proof must match the exact authenticated legacy mutation response above and bind the
exact request-token digest.

Before recording the fence checkpoint, validate a complete inventory of every legacy writer class.
The input is mode `0600` and contains exactly one category for `process`, `endpoint`, `credential`,
and `scheduler`. Each category includes a hashed discovery artifact; applicable categories list every
writer with a closed state and a hashed owner-only evidence file. Non-applicable categories contain
no writers and give a nonblank reason.

```sh
.venv/bin/python scripts/dish-pg-operations-evidence validate-legacy-writer-inventory \
  --file /secure/evidence/legacy-writer-inventory.json \
  --expected-candidate-id CANDIDATE_UUID \
  --expected-cutover-run-id CUTOVER_UUID \
  --expected-source-commit EXACT_FULL_SOURCE_COMMIT \
  --output /secure/evidence/legacy-writer-inventory-report.json
```

A self-consistent inventory for the wrong candidate, cutover run, or source commit is rejected.
`writer-fence-verify` revalidates that raw inventory and binds its inventory/report SHA-256
identity into the persisted writer-fence proof.

Record exact probe evidence:

```sh
scripts/dish-pg-release writer-fence-verify FENCE_UUID \
  --proof-file /secure/evidence/fence-proof.json \
  --writer-inventory-file /secure/evidence/legacy-writer-inventory.json \
  --verified-at RFC3339_WITH_OFFSET
scripts/dish-pg-release cutover-mark-fenced CUTOVER_UUID \
  --recorded-at RFC3339_WITH_OFFSET
```

## 9. Activation, rollback burn, and admission

Release chronology is fail-closed and is checked against the database/service clock. The minimum
ordering is:

- final closure `recorded_at >= closed_through_at >= interval_started_at`;
- invalidation observation is no earlier than the closure interval start, and invalidation recording
  is no earlier than both the observation and the closure record;
- approval and cutover preparation follow the exact closure record;
- fence engagement follows preparation, verification follows engagement, and the cutover fence
  checkpoint follows every verification;
- activation follows verified fencing and the closure record while remaining covered by
  `closed_through_at`; rollback burn follows activation;
- rollback burn disables external projection for the exact candidate generation while preserving
  existing projection/reconciliation rows as forensic history;
- post-burn runtime attestation and the first-admission plan follow rollback burn; the isolated
  first-request gate follows both while general mutation admission remains closed;
- first-admission verification follows the exact PostgreSQL request, immutable successful outcome,
  committed execution, governed audit event, and terminal invocation-audit obligation.

No post-burn projection-worker readiness record, applied Asana projection event, or fresh Asana
reconciliation is an admission prerequisite. Backdated, future-dated, or impossible operator
timestamps are rejected rather than normalized.

These commands are intentionally separate crash boundaries:

```sh
scripts/dish-pg-release cutover-activate CUTOVER_UUID \
  --final-closure-id CLOSURE_UUID \
  --activated-at RFC3339_WITH_OFFSET
```

At this checkpoint the target generation is selected but mutation admission remains closed. A
pre-burn abort remains possible only under the conditions below.

```sh
scripts/dish-pg-release cutover-burn-rollback CUTOVER_UUID \
  --legacy-bundle-id EXACT_IMMUTABLE_BUNDLE_ID \
  --burned-at RFC3339_WITH_OFFSET
```

**Stop point:** after this commits, ordinary return to Asana/SQLite authority is prohibited. Recover
PostgreSQL; do not remove the legacy fence or reverse-import Asana. The burn also flips the
candidate projection epoch to `external_effects_enabled=false`. Do not re-enable it: the frozen
final Asana source boundary is the end of Asana involvement. Existing projection outbox, mapping,
drift, readiness, and reconciliation rows stay in place for forensic use.

Rollback burn first reruns all candidate, quiescence, manifest, writer-fence, and closure checks
against fresh state. After confirming the burn row, disabled external-projection mode, and closed
admission control are durable, record the exact deployed PostgreSQL service and route while
admission is still closed:

```sh
scripts/dish-pg-release runtime-attestation-record CANDIDATE_UUID \
  --file /secure/evidence/runtime-attestation.json
```

The runtime attestation binds the exact candidate release identities, service artifact, route probe,
`route_target=postgresql`, `mutation_admission=closed`, and
`external_projection=disabled_post_burn`. A projection-worker artifact is not required. If an older
operator bundle still supplies one, it is observed and retained only as historical evidence; it does
not become a post-burn gate.

Choose one bounded first production request before opening admission. The request must target an
existing task in the candidate generation and use canonical `task_id`. Commands that require a
pre-existing open operation are not eligible: candidate validation closes the operation corpus, so
such a plan could not be executed after admission opens. `create` is also intentionally excluded
because its newly allocated task identity cannot be prebound. The plan binds the request UUID,
command, exact `command_arguments`, canonical task identity, canonical request-payload SHA-256, and
operator evidence. Post-burn expected external projection count is always zero; do not supply an
`expected_projection_events` field.

```sh
scripts/dish-pg-release first-admission-plan CUTOVER_UUID \
  --file /secure/evidence/first-admission-plan.json
```

Only after the runtime attestation and first-admission plan exist, open the isolated first-request
gate. This command re-observes the exact service/route artifacts and disabled external-projection
mode. It does not open ordinary mutation admission; the control remains `closed`:

```sh
scripts/dish-pg-release cutover-open-admission CUTOVER_UUID \
  --opened-at RFC3339_WITH_OFFSET
```

Issue exactly the planned request through the target service with the repository one-shot helper:

```sh
export DISH_SERVICE_URL_PROD='https://EXACT_TARGET_SERVICE_ORIGIN'
export DISH_SERVICE_TOKEN_PROD='EXACT_SCOPED_TOKEN'
.venv/bin/python scripts/dish-pg-first-admission-request \
  --plan /secure/evidence/first-admission-plan.json \
  --service-url-env DISH_SERVICE_URL_PROD \
  --token-env DISH_SERVICE_TOKEN_PROD \
  --output /secure/evidence/first-admission-request.json
```

The helper sends no retry. If the report says `delivery_state=unknown`, stop and resolve the exact
request UUID against PostgreSQL request/outcome state and service logs; never resubmit by guessing.
Any unrelated new request remains rejected after reservation consumption until verification
succeeds.

Fulfil or repair the exact request's invocation-audit obligation. Then verify the first request from
PostgreSQL-native evidence: the persisted canonical request payload must hash to its reserved request
SHA and match the plan, the immutable result payload must hash to its recorded outcome SHA, the
outcome must be immutable success, the exact command execution must be committed for the planned
task/command, the governed audit event must bind that execution/task, and the invocation obligation
must be fulfilled or repaired. Successful post-burn live commands must create no new external
projection intent.

Record verification and completion:

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

The verification transition alone opens ordinary mutation admission. After rollback burn, do not
run projection-worker readiness or Asana reconciliation as a release/admission step. Historical
rows remain available for incident reconstruction, but their pending/drift/failure state is not a
post-cutover service-health or mutation-admission signal.

## 10. Abort and fence release before rollback burn

Before rollback burn, abort and fence release are allowed only when PostgreSQL has accepted no
authoritative mutation, no production PostgreSQL projection effect was issued, the frozen legacy
bundle remains valid, the writer fence can be reversed deterministically, and the cutover run will
be recorded as aborted rather than erased:

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
5. never create a replacement candidate, approval, or cutover UUID to bypass a blocked exact run;
   replacement is permitted only after the exact run is durably aborted before rollback burn, and
   the generation admission control must remain closed while it is rebound.

Before a production maintenance window, certify this restart table with the maintained TEST-only
runner and retain its off-repository report:

```sh
scripts/dish-pg-cutover-activation-rehearsal \
  --output /secure/evidence/stage6-activation-report.json \
  --evidence-dir /secure/evidence/stage6-activation
```

Only `status=passed` with `evidence_validation.ok=true` certifies the non-production checkpoint/process
rehearsal. `status=blocked` keeps this rehearsal incomplete and never authorizes production work.

State meanings:

| Cutover state | Mutation admission | Ordinary abort |
| --- | --- | --- |
| `prepared` | closed | conditionally allowed |
| `fenced` | closed | conditionally allowed |
| `activated` | closed | conditionally allowed before burn |
| `rollback_burned` | closed | prohibited |
| `admission_open` | closed; exact reserved first request only | prohibited |
| `first_admission_verified` | open | prohibited |
| `completed` | open | prohibited |
| `aborted` | closed | terminal pre-burn path |

## 12. Production-environment work still required

The repository package does not complete these actions:

- capture the final production SQLite and sidecar bundle and hashes;
- review production changes through the exact final source commit;
- observe and classify the complete live Asana task/project/section/registry corpus;
- run production-shaped full, activation, fault, backup, and restore rehearsals; run PITR only if
  the selected RPO requires it;
- establish PostgreSQL backup/WAL archival and independently verify restoration;
- measure and obtain acceptance of actual RPO/RTO;
- deploy coherent target service, protocol, OpenAPI, routing, credentials, and worker releases;
- enumerate and mechanically fence every old writer process, endpoint, credential, and scheduler;
- obtain Marco's exact bundle-bound approval;
- execute rollback burn, confirm external projection is disabled, open isolated mutation admission, and validate the first live request from PostgreSQL-native evidence;
- monitor early-cutover health and retain the final immutable evidence package.

Any unresolved item keeps the migration in **Draft / not authorized for production cutover**.

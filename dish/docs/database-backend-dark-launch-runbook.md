# PostgreSQL dark-launch runbook

**Status: live in production. Preflight passed; capture and shadow execution are both enabled.**

The dark launch leaves SQLite and Asana authoritative. The production service captures completed
legacy commands into an owner-only local spool; a separate worker delivers and evaluates eligible
envelopes in PostgreSQL. The worker receives no Asana, service, admin, Action, or projection-adapter
credential. Shadow-origin projection rows are unconditionally excluded from projection claiming,
and the active projection epoch must also keep `external_effects_enabled = false`.

Production service restarts, dark-launch mode changes, worker installation or lifecycle changes, and
kill-switch changes are Marco-only. The commands below describe those authorized operations; an
agent must not execute them without Marco's explicit authorization.

## Required production identities and paths

Use explicit owner-only environment variables rather than substituting TEST identities:

```sh
DISH_PRODUCTION_SERVICE_ENV=/home/marco/.config/dish-service/prod.env
DISH_DARK_LAUNCH_WORKER_ENV=/home/marco/.config/dish-service/dark-launch.env
DISH_PG_LOCATION_MANIFEST=/home/marco/.local/state/dish/prod/dark-launch-evidence/location-manifest.json
DISH_PG_LEGACY_NDJSON=/home/marco/.local/state/dish/prod/dark-launch-evidence/legacy.ndjson
DISH_PG_BOOTSTRAP_RECEIPT=/home/marco/.local/state/dish/prod/dark-launch-evidence/bootstrap-receipt.json
DISH_PG_DARK_LAUNCH_READINESS_REPORT=/home/marco/.local/state/dish/prod/dark-launch-evidence/readiness.json
```

The readiness command accepts only the actual production service environment at
`/home/marco/.config/dish-service/prod.env`; another owner-only file under the same configuration
root is not equivalent and fails closed. That file must explicitly define the dark-launch spool,
emergency directory, and kill-switch paths. The command loads its effective dark-launch busy
timeout and spool limits through `ServiceConfig`, including the service's existing defaults, then
requires the effective spool, kill switch, and shared limits to match the explicit preflight inputs
and worker environment exactly.

Start the worker environment from `deploy/systemd/dark-launch.env.example`, keep it mode `0600`, and
replace every placeholder. It must define every variable referenced by the committed unit, including
`DISH_PG_EXPECTED_DATABASE_NAME` and `DISH_DARK_LAUNCH_KILL_SWITCH`. Numeric limits must be positive,
reservation TTL must be at least 90 seconds, and delivered retention must be at least the reservation
TTL. The readiness command rejects credential-bearing variables rather than redacting and accepting
them.

The production SQLite database, spool, emergency directory, kill switch, cursor secret, manifest,
NDJSON, receipt, environment files, and report destination must remain under the approved production
roots. They must be non-TEST, owner-safe, non-aliased paths without symlink traversal or hard links.

## Prepare immutable source and PostgreSQL authority

0. Ensure the production PostgreSQL container is running before anything below. It is
   code-defined, not manually provisioned: `deploy/postgresql/compose.yaml` (shared with TEST,
   selected by `--env-file` and `-p` project name) plus the systemd unit
   `deploy/systemd/dish-postgres-prod.service`, whose environment file
   (`/home/marco/.config/dish-service/postgres-prod.env`, from
   `deploy/systemd/postgres-prod.env.example`) supplies `DISH_POSTGRES_DB`,
   `DISH_POSTGRES_USER`, `DISH_POSTGRES_PASSWORD`, and `DISH_POSTGRES_HOST_PORT`. Installing the
   unit under `/etc/systemd/system` and starting it is Marco-only (outside the passwordless
   `dish-service-{prod,test}` sudo grant); once installed, `DISH_PG_DATABASE_URL` and
   `DISH_PG_EXPECTED_DATABASE_NAME` used below must match the same database name, user, and port.
1. Migrate the explicit production PostgreSQL database to the repository Alembic head.
2. Capture the complete source-location manifest through the explicit production read-only path:

   ```sh
   scripts/dish-pg-build-location-manifest \
     --environment production \
     --env-file "$DISH_PRODUCTION_SERVICE_ENV" \
     --output "$DISH_PG_LOCATION_MANIFEST"
   ```

   The command accepts only the fixed production service environment, Cooking project, and SQLite
   state root. It opens SQLite read-only, performs exact Asana task reads, requires a non-zero corpus,
   and fails closed on TEST, mixed, aliased, or ambiguous identities.
3. Export the exact legacy corpus using that manifest:

   ```sh
   scripts/dish-pg-export-legacy \
     --database "$DISH_DB_PATH" \
     --location-manifest "$DISH_PG_LOCATION_MANIFEST" \
     --output "$DISH_PG_LEGACY_NDJSON"
   ```

4. Bootstrap the empty PostgreSQL target with the explicit database identity and preserve the
   owner-only receipt:

   ```sh
   scripts/dish-pg-bootstrap-initial \
     --database-url "$DISH_PG_DATABASE_URL" \
     --expected-database-name "$DISH_PG_EXPECTED_DATABASE_NAME" \
     --source "$DISH_PG_LEGACY_NDJSON" \
     --source-generation "$DISH_DARK_LAUNCH_SOURCE_GENERATION" \
     --dish-repo /home/marco/ai-tools/dish \
     --dish-commit "$DISH_SOURCE_COMMIT" \
     --honest-repo /home/marco/honest-pantry \
     --honest-commit "$HONEST_SOURCE_COMMIT" \
     --receipt "$DISH_PG_BOOTSTRAP_RECEIPT"
   ```

   Record the receipt's `generation_id`, `import_run_id`, `binding_id`,
   `source_bundle_sha256`, and `source_record_count` in the operator shell without changing the
   receipt.
5. Create one open baseline bound to the same generation and source identity:

   ```sh
   scripts/dish-pg-dark-launch baseline-create \
     --database-url "$DISH_PG_DATABASE_URL" \
     --spool-path "$DISH_DARK_LAUNCH_SPOOL_PATH" \
     --generation-id "$DISH_PG_GENERATION_ID" \
     --source-generation "$DISH_DARK_LAUNCH_SOURCE_GENERATION" \
     --source-commit "$DISH_SOURCE_COMMIT"
   ```

6. Import the receipt-bound NDJSON:

   ```sh
   scripts/dish-pg-import-legacy \
     --database-url "$DISH_PG_DATABASE_URL" \
     --source "$DISH_PG_LEGACY_NDJSON" \
     --expected-source-sha256 "$DISH_PG_SOURCE_BUNDLE_SHA256" \
     --expected-record-count "$DISH_PG_SOURCE_RECORD_COUNT" \
     --generation-id "$DISH_PG_GENERATION_ID" \
     --import-run-id "$DISH_PG_IMPORT_RUN_ID" \
     --contract-binding-id "$DISH_PG_BINDING_ID"
   ```

7. Activate one effects-disabled projection epoch:

   ```sh
   scripts/dish-pg-dark-launch activate-epoch \
     --database-url "$DISH_PG_DATABASE_URL" \
     --generation-id "$DISH_PG_GENERATION_ID" \
     --reason "dark-launch shadow execution"
   ```

8. Put the returned baseline UUID in `DISH_DARK_LAUNCH_BASELINE_ID`. Ensure the existing spool is
   closed and checkpointed: strict readiness refuses a missing spool or any `-wal`/`-journal`
   sidecar rather than creating, checkpointing, or repairing it.

## Agreed approach: rehearse now, resync before go-live

Bootstrapping PostgreSQL from a legacy export is a one-time snapshot. Nothing in this runbook
backfills SQLite/Asana activity that happens between that snapshot and capture mode being enabled,
so a long gap between bootstrap and go-live leaves PostgreSQL stale for that whole window. The
agreed way to handle this:

1. Run the full "Prepare immutable source and PostgreSQL authority" sequence now, against real
   production paths, credentials, and data, as a rehearsal. Capture stays off and PostgreSQL stays
   non-authoritative throughout, so this has no live effect; it validates the procedure end-to-end
   ahead of time.
2. Before actually enabling capture, during a maintenance window, use the reviewed production
   reset command to wipe and rebuild PostgreSQL immediately before enabling capture. This keeps the
   stale-snapshot gap as small as practical without any manual/inline database operations.

`scripts/dish-pg-production-prepare` scripts steps 1-7 of the "Prepare immutable source and
PostgreSQL authority" sequence below as one repeatable non-destructive command for rehearsals. It
takes the same environment variables documented in that section, never restarts the service, and
never touches capture mode, the kill switch, or the worker.

The maintenance-window wipe is **only** `scripts/dish-pg-production-reset`; do not run manual
`DROP DATABASE`, `CREATE DATABASE`, `pg_terminate_backend`, inline Python, or heredoc SQL against
production. The reset requires Marco's explicit authorization for that specific wipe, an exact
human confirmation of `DISH_PG_EXPECTED_DATABASE_NAME`, and a **new, non-existing** recovery-record
path for this one reset lineage. Keep the completed record as durable evidence; never reuse or
overwrite it for another reset.

```sh
RESET_RECOVERY=/home/marco/.local/state/dish/prod/dark-launch-evidence/production-reset-<maintenance-id>.json
.venv/bin/python scripts/dish-pg-production-reset \
  --confirm-database-name "$DISH_PG_EXPECTED_DATABASE_NAME" \
  --recovery-record "$RESET_RECOVERY"
```

Before destructive work, the command checks for any PostgreSQL-resident incomplete-reset guard and
for an existing recovery record **before it snapshots live ACL state**. A normal invocation refuses
an active guard, any unresolved record, and a completed record at the same path. The initial run
then validates prepare preflight and durably records a versioned/checksummed original snapshot plus
reset UUID, database/owner identity, and PostgreSQL cluster identity. The record is written with
restrictive permissions and is the only trusted access baseline for that reset lineage; it contains
no DSN or password.

Recreation is born with `ALLOW_CONNECTIONS=false`. The command installs the UUID-bound reserved
PostgreSQL database guard and revokes the fresh database's non-owner/default PUBLIC access before it
allows the owner connection needed by `dish-pg-production-prepare`. This closes the CREATE-to-guard
crash window: if the process dies after CREATE but before guard installation, the recreated database
remains connection-fenced rather than becoming an unguarded accessible target.

If prepare or access restoration fails, do **not** invoke the reset as a new operation and do not
repair/adopt the current ACL state. Resume only the retained original lineage with the same record:

```sh
.venv/bin/python scripts/dish-pg-production-reset \
  --confirm-database-name "$DISH_PG_EXPECTED_DATABASE_NAME" \
  --recovery-record "$RESET_RECOVERY" \
  --resume
```

Resume verifies the record checksum/version, database/owner/cluster identity, and any active guard
UUID before mutation. For an incomplete rebuild it re-recreates the database and runs prepare from
the beginning using the retained **original** snapshot; it never snapshots the live post-failure ACL
as a replacement baseline. A guard/reset-ID mismatch fails before target mutation, and an active
guard with the original recovery artifact missing fails closed.

After prepare succeeds, the command restores and verifies the original database/schema/table/
sequence/column grants, default privileges, and database-scoped settings while the reset guard is
still active. It then durably records `access_restored`, clears the matching PostgreSQL guard, and
finally records `completed`. If the process dies in either finalization window, `--resume` verifies
the restored original authority and completes only the missing guard-clear/record-finalization step;
it does not establish a new baseline. This includes `dish_frontend_observer` and every other
non-owner role represented by the retained snapshot. There is no force-adopt-current-baseline path.

### Backfill one task after `allow_open_operations`

When a production prepare explicitly used `DISH_PG_ALLOW_OPEN_OPERATIONS=1`, the task itself was
imported but any then-open operation, verification cycle, or service lease was absent from that
immutable source bundle. After the relevant legacy history has become fully terminal, backfill only
that task with the reviewed operator command instead of changing the original import run:

```sh
.venv/bin/python scripts/dish-pg-backfill-task "$TASK_GID" \
  --database-url "$DISH_PG_DATABASE_URL" \
  --expected-database-name "$DISH_PG_EXPECTED_DATABASE_NAME" \
  --legacy-database "$DISH_DB_PATH" \
  --snapshot-output "$DISH_PG_TERMINAL_HISTORY_SNAPSHOT"
```

`DISH_PG_TERMINAL_HISTORY_SNAPSHOT` is an operator-chosen retained evidence path for that one task.
The command first requires the existing PostgreSQL `DishTask`, then reads SQLite in one read
transaction and fails before any PostgreSQL import mutation if any operation is non-terminal, any
verification cycle is open, or any lease is active. On success it records the exact one-task source
bytes under a new immutable supplemental `ImportRun` and inserts only missing terminal-history rows;
matching stable identities are verified and conflicting identities fail closed. Repeating the exact
snapshot is idempotent. Task/content/placement/completion/registry rows and the bootstrap `ImportRun`
are not rewritten.

Supplemental terminal-history application and ReleaseCandidate validation share one active-generation
serialization gate. The final backfill transaction acquires that gate before it re-reads generation and
candidate state, then holds it through supplemental `ImportRun` creation/reuse and terminal-history
commit. A candidate that reaches `validated` first therefore blocks a waiting backfill; a backfill that
commits first is visible before waiting validation can finish. Forward candidate manifests preserve the
primary-only v3 fingerprint when no supplement exists; when supplemental history is present, the v3
builder extension folds deterministic supplemental ImportRun/source/primary-linkage provenance and the
exact imported operation/cycle/lease rows into `import_completion_sha256`. Revalidation treats later
supplemental-history drift as stale.

## Install the worker while stopped

Marco may install the committed unit, reload systemd, and confirm that it remains disabled and
inactive:

```sh
sudo install -o root -g root -m 0644 \
  deploy/systemd/dish-shadow-worker.service \
  /etc/systemd/system/dish-shadow-worker.service
sudo systemctl daemon-reload
systemctl is-enabled dish-shadow-worker.service
systemctl is-active dish-shadow-worker.service
```

Expected state before readiness is `disabled` and `inactive`. Do not enable or start the worker yet.
The readiness preflight uses only `systemctl show`; it fails if the installed digest differs from the
repository unit, the unit is enabled, active, or failed, the installed file is unsafe, a drop-in or
inline/pass-through environment is present, or the effective environment file is not exactly the
explicit worker environment supplied to preflight.

## Run the read-only production readiness preflight

Run from the exact checkout intended for deployment:

```sh
scripts/dish-pg-dark-launch-readiness \
  --service-environment "$DISH_PRODUCTION_SERVICE_ENV" \
  --worker-environment "$DISH_DARK_LAUNCH_WORKER_ENV" \
  --database-url "$DISH_PG_DATABASE_URL" \
  --expected-database-name "$DISH_PG_EXPECTED_DATABASE_NAME" \
  --manifest "$DISH_PG_LOCATION_MANIFEST" \
  --legacy-ndjson "$DISH_PG_LEGACY_NDJSON" \
  --bootstrap-receipt "$DISH_PG_BOOTSTRAP_RECEIPT" \
  --spool-path "$DISH_DARK_LAUNCH_SPOOL_PATH" \
  --kill-switch "$DISH_DARK_LAUNCH_KILL_SWITCH" \
  --unit-name dish-shadow-worker.service \
  --repository-unit deploy/systemd/dish-shadow-worker.service \
  --report-path "$DISH_PG_DARK_LAUNCH_READINESS_REPORT"
```

The command emits one bounded JSON object. Every required check has `passed`, `status`, and a
redacted actionable `reason`. The `service_environment` check includes the effective production
spool, emergency directory, kill switch, and shared numeric limits; it fails if the service would
start with values different from those certified for the worker. It opens PostgreSQL in an explicit
read-only transaction and always rolls it back. It verifies exact database and Alembic identities;
the active generation; receipt,
import, registry, baseline, and effects-disabled epoch bindings; and the complete imported corpus.
It reads an existing checkpointed spool in immutable mode and inspects systemd without changing it.
It creates no generation, baseline, epoch, spool, marker, import, row, unit change, or external
effect. `--report-path` writes only the requested owner-only evidence file.

A fixture report proves output shape and decision logic only. Production readiness exists only after
this command returns `status = "ready"` against the production inputs. `blocked` means a dependency
was unavailable; `not_ready` means at least one authoritative check failed.

## Enable capture first

After a ready report, Marco may set `DISH_DARK_LAUNCH_MODE=capture` in the production service
environment and restart only the production legacy service. A PostgreSQL outage or spool failure
must not change the live command result; emergency evidence is written under
`DISH_DARK_LAUNCH_EMERGENCY_DIR` when possible.

Observe with explicit limits and operator-selected thresholds:

```sh
scripts/dish-pg-dark-launch status \
  --database-url "$DISH_PG_DATABASE_URL" \
  --spool-path "$DISH_DARK_LAUNCH_SPOOL_PATH" \
  --baseline-id "$DISH_DARK_LAUNCH_BASELINE_ID" \
  --kill-switch "$DISH_DARK_LAUNCH_KILL_SWITCH" \
  --worker-unit dish-shadow-worker.service \
  --max-spool-bytes "$DISH_DARK_LAUNCH_MAX_SPOOL_BYTES" \
  --max-spool-records "$DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS" \
  --min-free-bytes "$DISH_DARK_LAUNCH_MIN_FREE_BYTES" \
  --busy-timeout-ms "$DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS" \
  --warning-backlog "$DISH_DARK_LAUNCH_WARNING_BACKLOG" \
  --critical-backlog "$DISH_DARK_LAUNCH_CRITICAL_BACKLOG" \
  --warning-lag-seconds "$DISH_DARK_LAUNCH_WARNING_LAG_SECONDS" \
  --critical-lag-seconds "$DISH_DARK_LAUNCH_CRITICAL_LAG_SECONDS" \
  --warning-capacity-percent "$DISH_DARK_LAUNCH_WARNING_CAPACITY_PERCENT" \
  --critical-capacity-percent "$DISH_DARK_LAUNCH_CRITICAL_CAPACITY_PERCENT" \
  --warning-mismatches "$DISH_DARK_LAUNCH_WARNING_MISMATCHES" \
  --critical-mismatches "$DISH_DARK_LAUNCH_CRITICAL_MISMATCHES" \
  --warning-gaps "$DISH_DARK_LAUNCH_WARNING_GAPS" \
  --critical-gaps "$DISH_DARK_LAUNCH_CRITICAL_GAPS"
```

Status is read-only and bounded. It reports observation time, oldest-pending age, spool backlog and
capacity, PostgreSQL delivery/parity/gap counts, kill-switch state, and optional worker state. Each
threshold dimension reports `healthy`, `warning`, `critical`, or `unavailable` without changing
capture or worker state. A missing warning/critical pair makes that dimension unavailable rather
than guessing an operator policy.

## Enable shadow execution

After capture is visibly accumulating and the status decision is acceptable, Marco may set
`DISH_DARK_LAUNCH_MODE=execute`, restart the production legacy service, and start the worker.
Envelopes captured in `capture` remain capture-only evidence. The worker claims executable work in
rollout order while earlier deliveries are still `pending` or `claimed`. A terminal `failed` delivery
remains explicit gap evidence but does not halt later comparison collection for the whole baseline.
The worker records capture-only work as explicit gaps, validates the exact database identity and
baseline, and exits before reading more work whenever the kill switch is engaged.

Inspect status repeatedly. Mismatches and gaps are evidence, not permission to change live authority.
Do not enable projection effects, writer fencing, PostgreSQL admission, or production routing.

## Investigating dark launch

When Marco says "check dark launch status/logs," start with the read-only status command in
"Enable capture first" above — it is safe to run at any time and does not change capture, worker,
or kill-switch state.

Delivery and comparison state is authoritative in PostgreSQL, not in the spool. The local SQLite
spool (`DISH_DARK_LAUNCH_SPOOL_PATH`) only tracks capture registrations; it has no delivery or
comparison outcome. For root cause on a specific failure or mismatch, query PostgreSQL directly:
`shadow_envelopes` (captured input/outcome, `pinned_inputs`), `shadow_deliveries` (`state`,
`attempts`, `last_error`), `shadow_comparisons` (`parity_class`, `differences`), and `shadow_gaps`
(`gap_kind`, `state`, `details`).

Read worker/service logs without an interactive sudo password:

```sh
systemctl status dish-shadow-worker.service --no-pager -l   # no sudo needed
sudo /usr/bin/systemctl status dish-service-prod.service    # passwordless, exact form only
```

Plain `journalctl` requires an interactive password and does not work non-interactively.

Recovering a delivery in terminal `failed` state (open `delivery_failure` gap) requires proof the
shadow attempt had no external effect — always true for dark launch, since shadow execution never
has external effects enabled — and is safe to requeue only when no later rollout evaluation is in
flight and no later rollout command has completed a real evaluation:

```sh
scripts/dish-pg-dark-launch gap-resolve \
  --database-url "$DISH_PG_DATABASE_URL" \
  --gap-id "$GAP_ID" \
  --reason "operator reason"
```

This resolves the gap and requeues the delivery as `pending` for the worker to retry. It fails closed
while a later rollout delivery is currently `claimed`, because that evaluation may already be mutating
target state in another transaction, and after a later command has produced a real comparison, because
replaying the earlier command then would make the recorded sequence out of order. A later delivery that
ended in `failed` rolled its evaluation transaction back; an explicit capture-only skip or operator void
did not evaluate the command. Those terminal no-evaluation outcomes, and later unattempted `pending`
captures, do not by themselves prevent recovery.

If the failed delivery is genuinely terminal and must never be retried or evaluated, Marco may
explicitly void that one delivery instead:

```sh
scripts/dish-pg-dark-launch void-failed-delivery \
  --database-url "$DISH_PG_DATABASE_URL" \
  --delivery-id "$DELIVERY_ID" \
  --reason "operator reason" \
  --comparator-release "$DISH_DARK_LAUNCH_COMPARATOR_RELEASE"
```

This command accepts only a terminal `failed` delivery whose baseline is still `open` and whose
generation is still `active` (the same liveness check `skip_delivery`/`fail_delivery` apply). It
permanently gives up evaluating that envelope, settles the delivery as `delivered` so its terminal
abandonment is explicit and the baseline can eventually close, records a `parity_class=gap`
comparison whose target is explicitly `not_evaluated`, and opens a new `delivery_failure` gap with
`audit_kind=operator_voided`. Later rollout evidence may already have advanced past the failed row. It also
resolves the original `delivery_failure` gap opened when the delivery first failed, linking its
resolution to the new gap's identity — so an operator reviewing open gap counts sees one gap close
and one open, not two open gaps for the same delivery. The existing schema's allowed gap kinds do
not include a separate `operator_voided` value; the audit subtype and gap identity distinguish this
operator action from both the original delivery failure and ordinary capture-time `uncomparable`
skips. It does not enable external effects or transfer authority.

Use `gap-resolve` (above), not `void-failed-delivery`, when the failure was transient or
infrastructure-related (e.g. a schema mismatch since fixed), the envelope should still be evaluated,
and no later rollout evaluation is in flight or has completed a real comparison. Later failed, skipped,
or operator-voided deliveries do not alone prevent retry. Once a later real evaluation is in flight or
recorded, do not force an out-of-order retry in this baseline: `gap-resolve` refuses it.
`void-failed-delivery` is only for an explicit decision to abandon that envelope's evaluation; otherwise
use a clean superseding replay path rather than rewriting the current sequence.

Known non-bug: `parity_class=gap` alone is not evidence of a problem. `rollout_mode` is pinned into
each envelope's `pinned_inputs` at the moment of capture; an envelope captured while the service was
in `capture` mode is permanently and correctly evaluated as an uncomparable gap, even after the
service later switches to `execute` mode. Check `envelope.pinned_inputs.rollout_mode` before
treating a `gap` as a defect.

Two identity/config values must stay synchronized with the active baseline, and drifting silently
breaks delivery or preflight rather than erroring loudly:

- `DISH_DARK_LAUNCH_SOURCE_GENERATION`, in the production service environment (`prod.env`), must
  exactly match the active baseline's source-generation label. If unset it silently defaults to
  `legacy-sqlite`, and every captured envelope permanently fails delivery with "shadow envelope
  source generation does not match baseline."
- `DISH_DARK_LAUNCH_BASELINE_ID`, in the worker environment (`dark-launch.env`, a different file
  from `prod.env`), must be updated after any resync/rewipe that creates a new baseline, or the
  read-only readiness preflight fails with "shadow baseline is absent, closed, stale, or
  source-mismatched" even though everything else is correct.

`systemctl start` on an already-active unit is a no-op — it does not restart the process or reload
code. After any code change intended to reach the running worker or service, use `restart`, not
`start`; confirm with `systemctl status` that the reported "Active: active (running) since" time
moved forward before trusting that a fix is live.

## Immediate disable and rollback

Marco can immediately disable both new capture and further worker delivery by engaging the shared
kill switch:

```sh
scripts/dish-pg-dark-launch disable \
  --kill-switch "$DISH_DARK_LAUNCH_KILL_SWITCH" \
  --reason "operator reason"
```

The marker does not edit service configuration. The worker exits cleanly, so it must be restarted
explicitly after any later resume. Existing spool and PostgreSQL evidence remain intact.

Configuration rollback is separate:

1. Restore the previously reviewed production service environment. Any mode or service-environment
   change requires an explicit production service restart.
2. Restore the previously reviewed worker environment or unit. Any worker environment or unit change
   requires an explicit worker restart; unit replacement also requires `systemctl daemon-reload`.
3. Keep the kill switch engaged while validating the restored configuration and status.
4. Resume capture only through Marco's explicit action:

   ```sh
   scripts/dish-pg-dark-launch enable-capture \
     --kill-switch "$DISH_DARK_LAUNCH_KILL_SWITCH"
   ```

   Removing the marker does not change `DISH_DARK_LAUNCH_MODE` and does not restart the worker. Set
   the intended mode, restart the production service if its configuration changed, and restart the
   worker explicitly only when execute-mode delivery is authorized.

## TEST dark-launch acceptance sequence

**TEST dark-launch shadow comparison stays running (settled 2026-08-13):**
TEST remains legacy-authoritative and PostgreSQL shadow comparison is meant
to keep running there, accumulating evidence toward an eventual fenced
cutover and PostgreSQL-only TEST exercise. A 2026-08-11 entry previously
here claimed TEST's dark-launch shadow worker was permanently retired
because its original projection epoch had `external_effects_enabled=true`;
that was an agent inference written up as settled policy, not an actual
Marco decision, and it is superseded. An effects-enabled epoch blocking the
shadow worker calls for retiring that one epoch and activating a fresh
effects-disabled epoch/baseline for shadow comparison — not for concluding
dark launch itself should stop. See `ops.md`'s "TEST dark-launch state"
entry for the current epoch/baseline identities and runtime observation.
This procedure is kept below for reference; it does not necessarily
describe TEST's exact current epoch/baseline, which `ops.md` tracks.

### Maintained TEST deployment

TEST has repository-owned wiring parallel to production:

- `deploy/systemd/dish-postgres-test.service` owns the PostgreSQL container;
- `deploy/systemd/dish-shadow-worker-test.service` owns the persistent shadow worker;
- `service-test.env.example` contains the capture-side settings; and
- `dark-launch-test.env.example` contains only the worker's PostgreSQL and local-evidence settings.

Keep the TEST worker disabled and stopped until the target is bootstrapped. The two environment
files must use the same spool path, kill switch, and shared numeric limits. The worker file must not
receive Asana credentials, service tokens, Action tokens, or projection-adapter credentials.

The maintained TEST unit deliberately retains the older `postgresql` Compose project identity so
it adopts the existing `postgresql_pgdata` volume and preserves `dish_frontend_auth_test`.

For a clean TEST bootstrap, clear the TEST Asana project and legacy SQLite/state evidence as one
coordinated reset, then create one seed task through TEST Dish. No separate Asana synchronization is
required, but the importer rejects an empty source bundle. Capture the resulting non-empty TEST
manifest and prepare the empty target with:

```sh
DISH_TEST_SERVICE_ENV=/home/marco/.config/dish-service/test.env \
DISH_DB_PATH=/home/marco/.local/state/dish/test/shared.sqlite3 \
DISH_PG_DATABASE_URL='postgresql+psycopg://dish:...@127.0.0.1:55432/dish_stage_a_test' \
DISH_PG_EXPECTED_DATABASE_NAME=dish_stage_a_test \
DISH_PG_LOCATION_MANIFEST=/home/marco/.local/state/dish/test/dark-launch-evidence/location-manifest.json \
DISH_PG_LEGACY_NDJSON=/home/marco/.local/state/dish/test/dark-launch-evidence/legacy.ndjson \
DISH_PG_BOOTSTRAP_RECEIPT=/home/marco/.local/state/dish/test/dark-launch-evidence/bootstrap-receipt.json \
DISH_DARK_LAUNCH_SOURCE_GENERATION=<exact-test-legacy-release> \
HONEST_SOURCE_COMMIT=<sha> \
DISH_DARK_LAUNCH_SPOOL_PATH=/home/marco/.local/state/dish/test/dark-launch-spool.sqlite3 \
  .venv/bin/python scripts/dish-pg-test-prepare
```

The TEST entrypoint forces TEST manifest identity and rejects a non-`_test` database or evidence
paths outside the TEST state root before migration. It performs the same migrate, manifest, export,
bootstrap, baseline, import, and effects-disabled epoch sequence as production prepare. It does not
change capture mode, restart a service, operate the kill switch, or start the worker.

After prepare succeeds, put its baseline ID in the TEST worker environment. Staged activation is a
separate operation: first enable capture and verify SQLite/Asana behavior is unchanged, then enable
execute mode and start the TEST worker. PostgreSQL remains non-authoritative throughout.

This acceptance package is separate from production readiness and from the §§1–4 PostgreSQL
validation program. Run it only against the real TEST service and `dish_stage_a_dark_test` database:

```sh
DISH_PG_DATABASE_URL="$DISH_PG_DATABASE_URL" \
  .venv/bin/python scripts/dish-pg-dark-launch-test-acceptance \
  --agent codex \
  --expected-source "$(git rev-parse HEAD)" \
  --capture-timeout-seconds 900 \
  --worker-timeout-seconds 2400 \
  --termination-grace-seconds 10 \
  --output .test-artifacts/dark-launch-test-acceptance/report.json
```

The runner refuses non-TEST service identity, database names, state paths, aliases, and permanent
product-state output. It executes each child once, preserves bounded reports, strips service and
Asana credentials from the worker environment, and reports `pass`, `fail`, `partial`, or `blocked`.
A TEST acceptance result is not a production preflight result.

## Not part of dark launch

Do not engage the legacy writer fence, open PostgreSQL mutation admission, enable projection external
effects, burn rollback, or route callers to PostgreSQL. Backup/restore certification and production
cutover acceptance remain later work under `docs/postgresql-cutover.md` and
`docs/database-backend-stage6-runbook.md`.

# PostgreSQL dark-launch runbook

**Status: implementation complete; production preflight has not been executed.**

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
2. Before actually enabling capture, during a maintenance window, wipe the production PostgreSQL
   database and redo the same sequence fresh, immediately followed by enabling capture. This keeps
   the stale-snapshot gap as small as practical.

`scripts/dish-pg-production-prepare` scripts steps 1-7 of the "Prepare immutable source and
PostgreSQL authority" sequence below as one repeatable command, so both the rehearsal and the
pre-go-live resync run identically. It takes the same environment variables documented in that
section, never restarts the service, and never touches capture mode, the kill switch, or the
worker. Run it once now, and again after the maintenance-window wipe.

There is no dedicated reset script; the wipe is a manual drop/recreate (or full truncate) of the
production PostgreSQL database. It is a distinct destructive action from the rehearsal steps above
and requires Marco's explicit authorization at the time it is performed, separate from any earlier
authorization to run the rehearsal.

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
Envelopes captured in `capture` remain capture-only evidence. The worker drains in rollout order,
records capture-only work as explicit gaps, validates the exact database identity and baseline, and
exits before reading more work whenever the kill switch is engaged.

Inspect status repeatedly. Mismatches and gaps are evidence, not permission to change live authority.
Do not enable projection effects, writer fencing, PostgreSQL admission, or production routing.

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
`docs/postgresql-cutover-imp.md`.

# PostgreSQL dark launch runbook

**Status: Draft — requires host review before enablement.**

The dark launch leaves SQLite/Asana authoritative. The legacy service captures command completion
to an owner-only local spool; a separate worker delivers and evaluates those envelopes in
PostgreSQL. The worker has no Asana adapter or credential. Shadow replay writes immutable
`origin = shadow` outbox evidence that projection workers refuse unconditionally. Projection epochs
should still remain `external_effects_enabled = false` as an independent operational guard.

## Prepare

1. Migrate the target PostgreSQL database to Alembic head.
2. Capture the complete location manifest through the explicit production read-only path:
   `scripts/dish-pg-build-location-manifest --environment production --output <owner-only-path>`.
   The command accepts only the fixed production service environment and fails closed on TEST or mixed
   identity. Then create the importer NDJSON with `scripts/dish-pg-export-legacy`; confirm the manifest
   and export contain the same non-zero task corpus before continuing.
3. Create the first active PostgreSQL generation and its imported section registry. This is a
   one-time empty-target operation; the command refuses any existing authority generation or
   registry state and verifies both Git heads, the Honest version/schema/protocol assets, the exact
   NDJSON SHA256, the target database name, and the Alembic head:

   ```sh
   scripts/dish-pg-bootstrap-initial \
     --database-url "$DISH_PG_DATABASE_URL" \
     --expected-database-name dish_stage_a_dark_test \
     --source "$DISH_PG_LEGACY_NDJSON" \
     --source-generation "$DISH_DARK_LAUNCH_SOURCE_GENERATION" \
     --dish-repo /home/marco/ai-tools/dish \
     --dish-commit "$DISH_SOURCE_COMMIT" \
     --honest-repo /home/marco/honest-pantry \
     --honest-commit "$HONEST_SOURCE_COMMIT" \
     --receipt "$DISH_PG_BOOTSTRAP_RECEIPT"
   ```

   Preserve the owner-only receipt. Its `generation_id`, `import_run_id`, `binding_id`,
   `source_bundle_sha256`, and `source_record_count` are the exact inputs to the remaining rehearsal.
4. Create one open shadow baseline against the receipt's active generation:

   ```sh
   scripts/dish-pg-dark-launch baseline-create \
     --database-url "$DISH_PG_DATABASE_URL" \
     --spool-path "$DISH_DARK_LAUNCH_SPOOL_PATH" \
     --generation-id "$DISH_PG_GENERATION_ID" \
     --source-generation "$DISH_DARK_LAUNCH_SOURCE_GENERATION" \
     --source-commit "$DISH_SOURCE_COMMIT"
   ```

5. Import the exact NDJSON bound by the bootstrap receipt. The wrapper performs the real
   `DishTask` idempotency check, verifies the bootstrapped preconditions, requires imported plus
   skipped counts to equal the source record count, and compares every imported task's content,
   alias, project membership, section placement, and completion head with its source record:

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

6. Activate one projection epoch for the generation before starting the shadow worker. This is an
   explicit, idempotent operator decision and must be performed once per generation. Dark-launch
   activation always keeps external effects disabled:

   ```sh
   scripts/dish-pg-dark-launch activate-epoch \
     --database-url "$DISH_PG_DATABASE_URL" \
     --generation-id "$DISH_PG_GENERATION_ID" \
     --reason "dark-launch shadow execution"
   ```

7. Verify that the resolved live SQLite database, spool, emergency directory, and kill-switch paths
   are pairwise distinct. Do not place the spool or kill switch behind a symlink or hard link to live
   authority storage. Status and worker startup refuse a missing or incomplete spool rather than
   creating one. The disable command creates a versioned marker without replacing an existing file;
   enable-capture removes only that validated marker.
8. Put the returned baseline UUID in the owner-only dark-launch worker environment file.
9. Install `deploy/systemd/dish-shadow-worker.service`, but do not start it yet.

## Enable capture first

1. Set `DISH_DARK_LAUNCH_MODE=capture` in the production legacy-service environment. Keep
   `DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS=50` unless host contention testing justifies another small,
   positive value; capture must fail open well before the live request timeout. Set explicit host
   limits for `DISH_DARK_LAUNCH_MAX_SPOOL_BYTES`, `DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS`, and
   `DISH_DARK_LAUNCH_MIN_FREE_BYTES`; reservation and completion writes are both checked inside their
   transactions, and reaching any bound automatically creates the shared kill switch.
2. Restart only the legacy service and issue representative normal commands.
3. Check local spool status before starting PostgreSQL execution:

   ```sh
   scripts/dish-pg-dark-launch status \
     --database-url "$DISH_PG_DATABASE_URL" \
     --spool-path "$DISH_DARK_LAUNCH_SPOOL_PATH" \
     --baseline-id "$DISH_DARK_LAUNCH_BASELINE_ID"
   ```

A PostgreSQL outage or spool failure must not change the live command result. Spool failures are
recorded under `DISH_DARK_LAUNCH_EMERGENCY_DIR` when possible.

## Enable shadow execution

Set `DISH_DARK_LAUNCH_MODE=execute`, restart the legacy service, then start `dish-shadow-worker` only
after capture is visibly accumulating. Envelopes captured while mode was `capture` remain capture-only
evidence. The worker drains in rollout sequence, evaluates only commands marked `execute`, and records `capture_only` commands as explicit
uncomparable gaps. It must not receive `ASANA_ENV`, `ASANA_PAT`, or any projection adapter.

Inspect status repeatedly, including `spool.capacity.accepting_new_records`. The worker compacts
old delivered payloads after `DISH_DARK_LAUNCH_DELIVERED_RETENTION_SECONDS` while preserving replay
fingerprints. Keep `DISH_DARK_LAUNCH_RESERVATION_TTL_SECONDS` at or above the legacy recovery
quarantine (currently 90 seconds). An earlier unresolved reservation blocks all later spool delivery
until it completes or ages into an explicit proof gap. PostgreSQL claims enforce the same sequence
barrier after delivery. The worker also refuses a source-generation mismatch or a baseline whose target
generation is no longer active, and execute/capture-only delivery requires an explicit positive rollout
sequence. Workflow-ID translation uses only successful versioned comparisons and requires a one-to-one
binding. Parity is based on a versioned shared response contract plus canonical pre-state, post-state,
and transition effects. Result-created identities expand post-state capture, and a snapshot query error
is recorded as a gap rather than empty evidence. Inspect axis-specific differences rather than treating
raw transport shape as parity. Mismatch and gap counts are evidence, not authority failures; disabling
the dark launch must not affect the live service.

## TEST dark-launch acceptance sequence

This acceptance package is separate from the §§1–4 PostgreSQL validation program. It composes the
existing host-capture and worker-restart rehearsals; it does not replace or duplicate their
assertions. Run it only against the real TEST service and `dish_stage_a_dark_test` database:

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

The runner refuses non-TEST service identity, database names, state paths, path aliases, and output
inside permanent Dish product state. It executes each child once without automatic reruns. Each child
runs in its own process group with a finite deadline; timeout sends `SIGTERM` to the group, escalates
to `SIGKILL` after the configured grace period, and records the timeout, cleanup, stdout, stderr, and
report evidence. The runner preserves the capture report and worker scratch evidence, strips
Asana/service credentials from the worker environment, and emits one bounded aggregate JSON report.

The aggregate records the exact source identity and child commands, child report hashes,
first-attempt statuses, preserved failure paths, and a final `pass`, `fail`, `partial`, or `blocked`
status. A capture failure or either child timeout is `fail`. `partial` is reserved for a completed
earlier stage followed by a genuinely unavailable later prerequisite; `blocked` is reserved for an
unavailable preflight prerequisite.

The cross-child checks confirm that private CLI and GPT Action observations stayed unchanged and
that the active projection epoch remains effects-disabled. Shadow-origin exclusion is proved
separately from that switch: inside one rollback-only database transaction, the runner temporarily
enables the active epoch, creates equivalent eligible live- and shadow-origin probe rows on the same
rehearsal task, and makes the shadow row earlier in claim order. A separate observer transaction
still sees effects disabled and cannot see either synthetic row. The real
`ProjectionService.claim_next` nevertheless selects the later live row while leaving the shadow row
pending, creates no projection attempt or external adapter path, then rolls back. A final transaction
verifies the synthetic rows are absent and external effects are still disabled.

## Immediate disable

Create the kill switch without editing service configuration:

```sh
scripts/dish-pg-dark-launch disable \
  --kill-switch "$DISH_DARK_LAUNCH_KILL_SWITCH" \
  --reason "operator reason"
```

This stops new legacy capture on the next request and causes `dish-shadow-worker` to exit before
delivering or evaluating further envelopes. Existing spool and PostgreSQL evidence remain intact.
Re-enable capture only by explicit operator action:

```sh
scripts/dish-pg-dark-launch enable-capture \
  --kill-switch "$DISH_DARK_LAUNCH_KILL_SWITCH"
```

After removing the switch, restart `dish-shadow-worker` explicitly; the systemd unit exits cleanly
while disabled and therefore does not restart itself.

## Not part of dark launch

Do not engage the legacy writer fence, open PostgreSQL mutation admission, enable projection external
effects, burn rollback, or route callers to PostgreSQL. Backup/restore certification and production
cutover acceptance remain later work.

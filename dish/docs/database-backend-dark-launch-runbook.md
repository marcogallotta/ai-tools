# PostgreSQL dark launch runbook

**Status: Draft — requires host review before enablement.**

The dark launch leaves SQLite/Asana authoritative. The legacy service captures command completion
to an owner-only local spool; a separate worker delivers and evaluates those envelopes in
PostgreSQL. The worker has no Asana adapter or credential. Shadow replay writes immutable
`origin = shadow` outbox evidence that projection workers refuse unconditionally. Projection epochs
should still remain `external_effects_enabled = false` as an independent operational guard.

## Prepare

1. Migrate the target PostgreSQL database to Alembic head.
2. Import a coherent baseline. `scripts/dish-pg-export-legacy` can produce importer NDJSON from the
   SQLite task-content heads plus a complete location/completion manifest captured by the existing
   authority path.
3. Create one open shadow baseline:

   ```sh
   scripts/dish-pg-dark-launch baseline-create \
     --database-url "$DISH_PG_DATABASE_URL" \
     --spool-path "$DISH_DARK_LAUNCH_SPOOL_PATH" \
     --generation-id "$DISH_PG_GENERATION_ID" \
     --source-generation "$DISH_DARK_LAUNCH_SOURCE_GENERATION" \
     --source-commit "$DISH_SOURCE_COMMIT"
   ```

4. Put the returned baseline UUID in the owner-only dark-launch worker environment file.
5. Install `deploy/systemd/dish-shadow-worker.service`, but do not start it yet.

## Enable capture first

1. Set `DISH_DARK_LAUNCH_MODE=capture` in the production legacy-service environment. Keep
   `DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS=50` unless host contention testing justifies another small,
   positive value; capture must fail open well before the live request timeout.
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

Inspect status repeatedly. Mismatch and gap counts are evidence, not authority failures; disabling
the dark launch must not affect the live service.

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

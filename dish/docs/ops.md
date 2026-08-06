# Ops state

Live local resources that exist right now — as opposed to `ops-issues.md`
(the hardening/gap backlog) or `database-backend-postgresql-test-plan.md`
(the runbook for exercising them). This file exists because agent sessions
lose track of what was already set up across context compaction; check here
before assuming a resource doesn't exist or needs to be created again.

Update this file whenever a live local resource is created, torn down, or
its state materially changes. Stale entries are worse than no entry — if
you're not sure a row is still accurate, verify it (`docker ps`, `psql`,
etc.) before trusting or removing it.

## Disposable local PostgreSQL (dark-launch runtime validation)

- **What**: a disposable native PostgreSQL instance for exercising
  `database-backend-postgresql-test-plan.md` §1-4 (process-failure,
  backup/PITR, runtime wiring, production-shaped rehearsal) without
  touching either `dish-service` profile or production PostgreSQL.
- **Where**: `docker-compose`, project `postgresql`, config at
  `deploy/postgresql/compose.yaml`. Container `postgresql-postgres-1`,
  image `postgres:17.10`, recreated 2026-08-06 to match the compose file's
  pin (was `postgres:17.5` since 2026-08-02). No named volume is declared;
  `docker compose up -d` reused the container's anonymous data volume
  rather than discarding it, so the existing schema and fixture rows
  survived the recreate (same-major-version binary upgrade).
- **Connection**: `postgresql://dish:dish@127.0.0.1:55432/dish_stage_a_test`
  (bound to `127.0.0.1` only).
- **Schema state as of 2026-08-06**: migrated to Alembic head, 103 tables
  in `public`, full `dish_pg` table set present. Holds §3 rehearsal fixture
  data (3 outbox events as of this snapshot) plus whatever else prior local
  runs left behind — not guaranteed empty; check row counts before treating
  any rehearsal result as a clean-slate run.
- **Status**: running continuously since 2026-08-02 (container recreated,
  not torn down, on 2026-08-06). Confirmed via `docker ps` on 2026-08-06 —
  `postgres:17.10`, healthy.
- **Done against it**: §3 runtime wiring rehearsal (this container), 2026-08-04
  — service, `projection_worker.py`, and `reconciliation_worker.py` run as
  separate OS processes (no systemd). §1 and §2 native runs on 2026-08-06 used
  their own disposable Compose/native-binary instances (PostgreSQL 17.10, not
  this container), not this long-lived one. Full reports were written to
  session scratchpads (not committed to the repo); result summary is in
  `ops-issues.md`'s "Local runtime validation plan" table. §3 rerun on
  2026-08-06 reproduced a real `record_replay_validation_failure` gap
  (see `ops-issues.md`); §4 still not run, blocked on the same gap.

## Production and test `dish-service`

Live service topology, ports, env file locations, and credential loading
are documented once in `/home/marco/ai-tools/CLAUDE.md` under "Live Dish
rehearsal credentials" — not duplicated here. Confirmed on 2026-08-04:
`dish-shadow-worker.service` is not installed on this host.

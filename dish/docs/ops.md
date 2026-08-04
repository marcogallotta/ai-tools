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
  image `postgres:17.5`.
- **Connection**: `postgresql://dish:dish@127.0.0.1:55432/dish_stage_a_test`
  (bound to `127.0.0.1` only).
- **Schema state as of 2026-08-04**: migrated to Alembic head, full
  `dish_pg` table set present. Contains whatever data prior local runs
  left behind — not guaranteed empty; check row counts before treating any
  rehearsal result as a clean-slate run.
- **Status**: running continuously since 2026-08-02 (not torn down between
  sessions). Confirmed via `docker ps` on 2026-08-04.
- **Not yet done against it**: §3 runtime wiring rehearsal (service +
  `projection_worker.py` + `reconciliation_worker.py` as separate
  processes against this instance) — see `ops-issues.md`'s "Local runtime
  validation plan" section for status of all four sections.

## Production and test `dish-service`

Live service topology, ports, env file locations, and credential loading
are documented once in `/home/marco/ai-tools/CLAUDE.md` under "Live Dish
rehearsal credentials" — not duplicated here. Confirmed on 2026-08-04:
`dish-shadow-worker.service` is not installed on this host.

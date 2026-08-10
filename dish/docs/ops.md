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
- **Maintained deployment**:
  `deploy/postgresql/compose.yaml` was made env-driven (`DISH_POSTGRES_DB`,
  `DISH_POSTGRES_USER`, `DISH_POSTGRES_PASSWORD`, `DISH_POSTGRES_HOST_PORT`,
  plus a declared named volume) so the same file serves both TEST and
  production, and a systemd unit (`deploy/systemd/dish-postgres-test.service`,
  env at `~/.config/dish-service/postgres-test.env` from
  `deploy/systemd/postgres-test.env.example`) now owns its start/stop instead
  of an ad hoc `docker compose up -d`. The unit retains the legacy
  `postgresql` Compose project identity so it adopts the existing container
  and `postgresql_pgdata` volume without replacing the frontend auth data.
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

## Private authenticated frontend

- **What**: the local private frontend authenticates against the separate writable
  `dish_frontend_auth_test` database on the test PostgreSQL instance and observes
  production dark-launch task data in `dish_stage_a_prod` through the
  `dish_frontend_observer` role. The observer has `SELECT` but no task-table
  mutation privileges and has `default_transaction_read_only=on`. PostgreSQL task
  data remains non-authoritative during dark launch.
- **Configuration**: owner-only environment file
  `/home/marco/.config/dish-service/frontend-local.env`; HTTPS configuration
  `deploy/caddy/dish-frontend-local.Caddyfile`. The Caddy listener and Dish private
  listener bind loopback only. Password provisioning and rotation remain explicit
  `scripts/dish-frontend-security` operations; service startup does not migrate a
  database or change security generation.
- **Services**: `dish-frontend-private.service` owns the frontend `dish-service`
  process, and `dish-frontend-caddy.service` owns HTTPS. The optional
  `dish-frontend.target` starts both. PostgreSQL remains an explicit prerequisite:
  the TEST PostgreSQL unit owns the legacy `postgresql` Compose project,
  while production observation PostgreSQL is independently service-owned.
  The target must not start a competing Compose project on port 55432. Stopping
  the frontend target stops its frontend/Caddy members and intentionally leaves both
  PostgreSQL instances running.
- **Install/update**:

  ```sh
  sudo install -m 0644 deploy/systemd/dish-frontend-private.service /etc/systemd/system/
  sudo install -m 0644 deploy/systemd/dish-frontend-caddy.service /etc/systemd/system/
  sudo install -m 0644 deploy/systemd/dish-frontend.target /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now dish-frontend.target
  ```

- **Operate**:

  ```sh
  sudo systemctl status dish-frontend.target dish-frontend-private dish-frontend-caddy
  sudo systemctl restart dish-frontend-private
  sudo systemctl restart dish-frontend-caddy
  sudo systemctl stop dish-frontend.target
  journalctl -u dish-frontend-private -u dish-frontend-caddy
  ```

- **URL**: `https://127.0.0.1:4443/`. The Caddy internal root currently trusted by
  the local browser remains under `/home/marco/.local/share/caddy`; the managed
  Caddy unit reuses that state rather than issuing from another local CA.

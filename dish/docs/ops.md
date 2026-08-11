# Ops state

Live local resources that exist right now — as opposed to `ops-issues.md`
(the hardening/gap backlog) or `testing.md` and the PostgreSQL runbooks
(the procedures for exercising them). This file exists because agent sessions
lose track of what was already set up across context compaction; check here
before assuming a resource doesn't exist or needs to be created again.

Update this file whenever a live local resource is created, torn down, or
its state materially changes. Stale entries are worse than no entry — if
you're not sure a row is still accurate, verify it (`docker ps`, `psql`,
etc.) before trusting or removing it.

## Disposable local PostgreSQL (dark-launch runtime validation)

- **What**: a disposable native PostgreSQL instance for process-failure,
  backup/PITR, runtime-wiring, and production-shaped rehearsal without
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

### TEST dark-launch state (2026-08-11) — shadow worker retired

TEST's active generation's only projection epoch (`00000000-...-1d`, created
2026-08-01 for a "stage6 rehearsal") has `external_effects_enabled=true` and
was never retired; `bbc8501` ("Adopt existing TEST PostgreSQL volume") kept
that epoch alive by reusing the persistent `postgresql_pgdata` volume instead
of a fresh one. The 2026-08-10 entry previously here claimed this epoch had
"external effects disabled" — that was already wrong at the time it was
written and should not be trusted for anything else it asserted.

This epoch's `external_effects_enabled=true` predates `#67` (it was created
2026-08-01, before `#67` landed 2026-08-10) — it is not itself evidence that
`#67`'s PostgreSQL-authority wiring is what's live in the running service.
Checked separately: `/home/marco/.config/dish-service/test.env` has no
`DISH_AUTHORITY_BACKEND` set (defaults to `legacy`) and the systemd unit
passes no `--postgresql-test-runtime` flag, so `dish-service-test.service`
was still running the legacy/dark-launch capture code path, unrelated to
whatever generation-level authority state `#67`'s wiring targets. Regardless
of which code path is live, the epoch's effects-enabled flag is what the
shadow worker checks, and it correctly refuses to run against it — so the
TEST dark-launch shadow worker is retired for this generation:
`dish-shadow-worker-test.service` should be `disable --now`d rather than kept
running/crash-looping (not yet done — needs sudo with a TTY). Do not flip the
epoch back to effects-disabled or spin up a replacement shadow generation to
resurrect shadow comparison here unless there's an explicit new requirement.

Resolved 2026-08-11: `dish-service-test.service`'s `DISH_DARK_LAUNCH_MODE` was
flipped from `execute` to `off` (`test.env`) and the service restarted, so it
no longer spools captured commands with nothing to consume them. Before this
change, the spool held only 30 records (`shadow_capture_registrations`),
far below the configured 100k-record/512MB caps — even left unaddressed,
capture would have self-disabled via the shared kill switch
(`_engage_capacity_kill_switch`) well before causing any live-request impact;
it was corrected anyway to keep runtime state coherent with the worker
retirement rather than leave a live no-op producer running.

## Production and test `dish-service`

Live service topology, ports, env file locations, and credential loading
are documented once in `/home/marco/ai-tools/CLAUDE.md` under "Live Dish
rehearsal credentials" — not duplicated here. Confirmed on 2026-08-04:
`dish-shadow-worker.service` is not installed on this host.

As of 2026-08-10, the public Action router has a fixed path split on the one
Funnel origin: root routes to production and `/test` routes only the TEST
schema and Action commands. The two services use distinct Action tokens.

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
## Opt-in PostgreSQL-authoritative TEST rehearsal

Normal `dish-service-test.service` remains legacy SQLite/Asana-authoritative. To rehearse the
cutover runtime without Asana, keep the same service unit and set `DISH_AUTHORITY_BACKEND=postgresql`
in the TEST environment together with the `DISH_PG_*` identity values documented in
`deploy/systemd/service-test.env.example`. Also set `DISH_PROFILE=test` and remove/comment every
populated Asana variable, including `ASANA_ENV`; PostgreSQL-authority startup fails closed if any
environment key containing `ASANA` is populated. There is no fallback to the legacy service.

Initialize the disposable TEST database with the existing PostgreSQL path: migrate it to the checked-in
head, run `scripts/dish-pg-bootstrap-initial` against an importer-compatible synthetic or prebuilt
NDJSON corpus, then run `scripts/dish-pg-import-legacy` with the bootstrap receipt identities. Those
commands consume local corpus/checkouts and require no Asana read. Use the existing cutover/rehearsal
admission tooling when mutation admission is required; do not create a TEST-only authority path.

The Action listener remains the shared `dish-service` listener. In PostgreSQL authority mode its
`/openapi/action.json` is supplied by the existing PostgreSQL Action contract and advertises only
implemented PostgreSQL commands. `proposals`, `apply-proposal`, and `safe-reclaim` are intentionally
not advertised: they require independent PostgreSQL workflow feature work rather than #67 transport
or configuration wiring. Existing `--postgresql-test-runtime` rehearsal invocations remain supported.

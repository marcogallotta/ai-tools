# dish

`dish` is the guarded interface for protocol-governed Cooking tasks. The live Asana task remains the content authority; the tool validates exact content, records durable operation evidence, enforces independent Verification, and confirms every write and movement by reread.

## Supported runtime modes

### Shared-service live mode

This is the only supported multi-agent path. Each environment has one `dish-service` process that
owns:

- the shared SQLite operation database;
- task operation locks and client/run leases;
- the Asana write credential and backend;
- exact-content baselines, audit repair, backup, and recovery;
- requests from `dish`, `dish-admin`, and the GPT Action.

Agent laptops and GPT Actions must not receive `ASANA_PAT`, `ASANA_ENV`, or a writable copy of a
shared database. Test and production run as separate, permanently available instances with distinct
projects, databases, backup directories, and loopback ports. A loopback-only Caddy router selects
which Action listener Funnel exposes; it does not own workflow state or credentials.

### Local test mode

Local mode remains available for controlled, single-agent tests and development. Set `DISH_MODE=local` explicitly; an unset mode fails closed. It is not a multi-agent lock and must not be used with `DISH_LIVE_MODE=1`. Once `dish-service` has marked a database as service-owned, direct local CLI/admin access to that database remains forbidden even while the service is stopped.

## Installation

Create the repository virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`dish`, `dish-admin`, and `dish-service` re-exec under `.venv/bin/python` and fail closed if it is unavailable.

### Isolated Stage A PostgreSQL development

The PostgreSQL target is isolated under `dish_pg/` and is not imported by the live
SQLite/Asana service. Start its disposable local database with:

```sh
docker compose -f deploy/postgresql/compose.yaml up -d
.venv/bin/alembic -c alembic.ini upgrade head
.venv/bin/python -m pytest -q -k 'stage1 or stage2'
```

Override `sqlalchemy.url` or supply a test configuration when the database is not on
`127.0.0.1:55432`. The Compose database is development/test state only; it is not a
production authority or migration source.

## Service-host configuration

Start from `deploy/systemd/service-test.env.example` and `service-prod.env.example`. Each instance
needs:

```sh
DISH_HONEST_PATH=/home/marco/honest-pantry
DISH_COOKING_PROJECT_GID=<Cooking project gid>
DISH_DB_PATH=<environment-specific database>
DISH_SERVICE_BACKUP_DIR=<environment-specific backup directory>
DISH_SERVICE_BIND=127.0.0.1
DISH_SERVICE_PORT=8765
DISH_ACTION_BIND=127.0.0.1
DISH_ACTION_PORT=8766
DISH_SERVICE_AGENT_TOKEN=<private CLI token>
DISH_SERVICE_ADMIN_TOKEN=<separate Marco-admin token>
DISH_SERVICE_ACTION_TOKEN=<dedicated GPT Action token>
ASANA_ENV=/home/marco/.config/asana-cli/.env
```

Only the service-host environment contains Asana credentials. Protect each environment file and
state directory with owner-only permissions. All three service tokens are required for each live
dual-listener process, must be distinct within that process, and must not use placeholder or short
values. The Action token must match between test and production so an authorized router flip does
not require an editor-secret change; CLI/admin tokens may remain environment-specific. Listener
hosts must remain loopback and all four service ports must be distinct. Invalid configuration fails
before either listener binds.

The fixed environment identities are:

```sh
DISH_HONEST_PATH=/home/marco/honest-pantry
DISH_COOKING_PROJECT_GID=1216693403164366
DISH_DB_PATH=/home/marco/.local/state/dish/test/shared.sqlite3
DISH_SERVICE_BACKUP_DIR=/home/marco/.local/state/dish/test/backups
# private/action ports: 8765/8766

DISH_COOKING_PROJECT_GID=1217084805070730
DISH_DB_PATH=/home/marco/.local/state/dish/prod/shared.sqlite3
DISH_SERVICE_BACKUP_DIR=/home/marco/.local/state/dish/prod/backups
# private/action ports: 8775/8776
```

The production corpus and durable baselines are live in the production instance. The public Action
route normally selects production; test remains separately available for explicit test work.

Install the two service units and Caddy router with:

```sh
sudo apt-get install caddy
sudo systemctl disable --now caddy.service
sudo install -m 0755 deploy/caddy/dish-action-route /usr/local/bin/
sudo install -m 0644 deploy/systemd/dish-service-test.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/dish-service-prod.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/dish-action-router.service /etc/systemd/system/
sudo systemctl disable --now dish-service.service
sudo systemctl daemon-reload
sudo systemctl enable --now dish-service-test dish-service-prod dish-action-router
```

Populate `/home/marco/.config/dish-service/test.env` and `prod.env` from their examples before
starting the units; both files must be mode `0600`. The distribution's generic `caddy.service` is
disabled because Dish's router has a dedicated config, state directory, and unit.

The legacy `dish-service.service` conflicts with the test unit because both bind `8765/8766`; stop
and disable it when installing `dish-service-test`. View logs with `journalctl -u
dish-service-test -u dish-service-prod -u dish-action-router`.

Dish reads the current protocol and task-schema assets from `DISH_HONEST_PATH` when it handles
workflow commands, so edits to those assets do not require a service restart. Restart the affected
service after changing its environment or Python code. Verification cycles already in
progress remain bound to their recorded Verification protocol release.

The unit is `Type=notify`: the process sends systemd a `READY=1` notification only once both
listeners are bound and their serve loops are running, so a systemd restart
blocks until the new process is actually ready to take requests — no race where a command issued
right after `restart` returns hits the old process mid-shutdown.

The instances bind four loopback listeners:

- test private/Action on `127.0.0.1:8765` and `127.0.0.1:8766`;
- production private/Action on `127.0.0.1:8775` and `127.0.0.1:8776`.

Caddy listens on `127.0.0.1:8786` and defaults to test on first start. Inspect the active route with
`deploy/caddy/dish-action-route status`. An authorized change uses `set test` or `set prod`; every
change requires `--authorize-route-change`, and production additionally requires
`--authorize-production-cutover`. Caddy's autosaved native configuration preserves API changes
across restart, and the command uses the route Etag plus an exact read-back.

Keep `DISH_DB_PATH` in one stable host-state location independent of any checkout or worktree. The
service derives its process lock and persistent ownership marker from the canonical database target,
so pathname aliases do not create another authority. A service-owned database cannot later be
opened through direct local mode.

Each instance's two listeners are one supervised service. Failure to bind either listener stops
startup and closes the other. On shutdown, one process-wide admission gate closes both surfaces before either
listener is drained. Requests that have not crossed that gate are disconnected without dispatch;
requests already executing are allowed to finish because they may own a transaction or an in-flight
Asana effect. Loopback HTTP responses close their backend connection, so Serve or Funnel must open a
new connection—and cross admission again—for every later request.

See `deploy/tailscale/README.md` before configuring Serve or Funnel. Tailnet clients use `8444` for
test and `8445` for production; public port `443` points only to Caddy's router.

## CLI client configuration

The normal live CLI is an HTTP client and does not open SQLite or construct an Asana backend:

```sh
export DISH_LIVE_MODE=1
export DISH_MODE=service
export DISH_PROFILE=prod
export DISH_SERVICE_URL_TEST=https://<laptop-tailnet-name>:8444
export DISH_SERVICE_URL_PROD=https://<laptop-tailnet-name>:8445
export DISH_SERVICE_TOKEN_TEST=<test private CLI token>
export DISH_SERVICE_TOKEN_PROD=<production private CLI token>
export DISH_CLIENT_RUN_ID=<non-nil canonical lowercase UUID for this run>
```

Bare `dish` uses production. Claude, Codex, or a human can select test for one command without
changing the process environment:

```sh
dish sections --agent claude
dish section-tasks SECTION_GID --agent claude
dish --profile test sections --agent claude
```

Profiles select the matching URL and credential together; `--profile` overrides `DISH_PROFILE`,
which overrides the production default. Interactive shells load both admin credentials, but agents
may use only `dish-admin --profile test`; production administration remains Marco-only. Never place
a CLI/admin token in the GPT Action configuration. GPT Action environment selection remains
exclusively Caddy's public route and is unaffected by CLI profiles.

## HTTP request boundary

Every authenticated POST requires exactly one `application/json` media type; parameters such as
`charset=utf-8` are accepted. Duplicate JSON object keys are rejected recursively before request
identity or workflow validation, so no parser-specific last-value rule can authorize a mutation.

## Workflow

Every CLI command response is one canonical JSON result envelope. Follow only `allowed_actions`;
they are derived from the exact live content, placement, durable operation evidence, pending
recovery work, and signoff state. The HTTP health and OpenAPI documents have their own response
shapes.

Planning uses a durable two-call confirmation gate on both the live CLI and GPT Action. The first
call creates no operation or lease and returns `data.intent_challenge_id`:

```sh
dish start TASK_GID --agent gpt --kind planning
# => CONFIRMATION_REQUIRED; copy data.intent_challenge_id

dish start TASK_GID --agent gpt --kind planning \
  --intent-challenge-id CHALLENGE_UUID \
  --intent-basis user_requested
```

Use `--intent-basis agent_override --override-reason "..."` only for an intentional agent override.
The second invocation is a fresh request; the bundled live CLI generates its new request UUID
automatically. Replaying a lost first response requires the original request UUID at the service/API
level, so the CLI's existing lost-response guidance still applies.

Typical Research and Verification lifecycle:

```text
start initial/change
→ prepare (writes and rereads exact pending-verification content, then hands off)
→ start verification
→ approve or reject
→ submit after approval
```

The bounded agent surface contains discovery/read commands (`sections`, `section-tasks`, `read`,
`inspect`) and
governed mutations (`create`, `start`, `prepare`, `approve`, `reject`, `submit`). `create` is a
mutation even though it starts from a bare task. In service mode, every agent, admin, lease, and
backup mutation carries a client-generated request UUID that durably binds its first authoritative
outcome. The GPT Action supplies and can reuse this UUID for an exact replay after response loss.
The bundled CLIs generate it internally and do not expose it after a transport failure, so inspect
the live state instead of blindly rerunning a lost-response mutation. Reads do not require an ID.

Run `dish --help`, `dish <command> --help`, and the stage walkthroughs for exact arguments.

For post-signoff change operations, `prepare --material-classification` classifies the exact canonical
body diff from the signed baseline. Supply it only when the body changed; Dish reports the effective
route and may force material handling for protocol-governed material paths.

An initial Research run that is blocked before constructing canonical content may use the existing
`reject` action with route `evidence` or `human-review` and resume status `pending-research`. The same
operation resumes at `prepare` after the corresponding admin resolution; no candidate or Verification
evidence is fabricated.

## Administrative operations

`dish-admin` is available to agents only with the test profile; production use is Marco-only. In
service mode it exposes:

- `recover-lease` to release an expired client/run lease when the same durable run will continue, without transferring workflow ownership to Marco;
- `abandon-operation` to permanently retire the latest expired or administratively released actor attempt and automatically prepare the safe stage-specific continuation;
- `reconcile-abandonment` to reclassify a blocked or interrupted abandonment after the live task has been inspected or repaired;
- `expire-lease` to release the active lease selected by exact lease ID, task GID, or a supported Asana task URL;
- `recover` for ambiguous operation-backed write or movement evidence;
- `reopen-planning` to reopen a completed bare task and, after interruption, replay the exact original request UUID without blindly repeating the Asana update;
- `repair-destination` to replace only an approved Planning destination after an unrecoverable final movement failure, while preserving the original Verification evidence;
- `discard` for a provably unapplied stale operation;
- `reopen`, `supply-evidence`, and `record-human-decision` for the existing protocol-specific hold routes;
- `authorize-governed-change` for one exact governed-field change;
- `migrate` for explicit task-schema migration;
- `backup-create` and `backup-restore` for managed shared-database snapshots.

For destination repair, use the exact admin continuation returned by `dish inspect`:

```sh
dish-admin repair-destination OPERATION_ID \
  --destination-section-gid SECTION_GID \
  --reason "approved destination was deleted or became inaccessible"
```

The replacement must be a current legal Cooking destination. The command changes no recipe content,
does not replace or rewrite the approved Verification cycle, and returns `submit` for the pending
movement.

There is intentionally no generic workflow-state `unblock` mutation. `expire-lease` is narrower: it releases one lease row without changing workflow state, actor lineage, execution claims, or unresolved external-effect evidence. It is a point-in-time release, not durable run revocation; the previous run may acquire a new lease if it remains lineage-eligible and no replacement lease exists.

Use `recover-lease` when the original chat/run will return. Use `abandon-operation` only when that run is permanently unavailable. Dish selects the stage-specific outcome in code: it restarts only from a clean unchanged frontier, finalizes already-committed work, preserves a governed hold, or blocks for `reconcile-abandonment`. Marco does not manually choose Planning, Research, or Verification rollback behavior. When a private continuation is returned, relay the exact command, wait for success, then refresh the authoritative Dish action before continuing.

```sh
dish-admin abandon-operation OPERATION_ID \
  --lease-id LEASE_ID \
  --reason "original chat session is permanently unavailable"

dish-admin reconcile-abandonment ABANDONMENT_ID
```

Use it only in shared-service mode:

```sh
dish-admin expire-lease LEASE_ID_OR_TASK_GID_OR_TASK_URL \
  --reason "agent process died"
```

The task-URL target accepts only `https://app.asana.com/0/PROJECT_GID/TASK_GID` and `https://app.asana.com/1/WORKSPACE_GID/project/PROJECT_GID/task/TASK_GID`. This operator-only parser is intentionally narrower than the deferred agent-surface URL design.

Before dispatch, `dish-admin` prints the request UUID and `DISH_CLIENT_RUN_ID` to stderr. If stdout returns `BACKEND_UNCERTAIN / service_response_ambiguous`, retain the same admin principal and `DISH_CLIENT_RUN_ID`, then retry the identical normalized target and trimmed reason with `--request-id` set to that UUID. A fresh task-target request is new work and may release a replacement lease.

An interrupted Planning reopen blocks only that task from another reopen or Planning start. Check
`GET /health` at `startup.planning_reopen_recovery`: `resume_safe` means exact replay may perform the
original update because the completion timestamp is unchanged; `applied_pending_replay` means the
live task is already incomplete and exact replay will confirm it without another update. Use the
returned command verbatim, including the original reason and request UUID. Contradictory live
evidence remains uncertain and requires explicit Marco-authorized reconciliation rather than a new
request UUID.

## Backup and restore

Create a validated online snapshot:

```sh
dish-admin backup-create --label before-maintenance
```

Restore only a managed snapshot identifier returned by the service:

```sh
dish-admin backup-restore dish-<timestamp>-<label>-<id>.sqlite3
```

Restore copies the selected managed backup into a temporary candidate, migrates and validates the candidate against the current dish schema/evidence contract, and then replaces the live database atomically. The source backup is never modified. Managed backup names may not be symlinks. Before replacement starts, the service durably arms the restore-fault sidecar; if that lockout cannot be written, restore does not begin. A validated pre-restore snapshot is attempted when the live database is readable; corruption of the live database does not block recovery from a valid managed backup. If replacement validation fails, the service rolls back when a validated pre-restore snapshot is available. If rollback cannot be proven, the pre-armed sidecar keeps mutations disabled across service restart until a validated restore succeeds. Restore metadata identifies the bytes actually installed, not merely the source backup bytes.

A restore interrupted by `SIGKILL` is reconciled automatically during the next service startup from the sibling restore-request journal and fault marker. The journal records exact, monotonic checkpoints for candidate preparation, the uniquely named pre-restore snapshot, replacement, validation, and rollback. Startup resumes only the checkpointed operation and validates matching file fingerprints; it does not issue a second restore from inference. The same reconciliation also runs when `dish-admin backup-restore <managed-id>` is called before startup recovery has completed. A retry with a newly generated client request UUID receives the recovered original result when it names the same backup. Check `GET /health`: `startup.restore_recovery` reports whether startup attempted reconciliation, while `maintenance.restore_recovery_required` remains true if exact recovery could not be proven. A legacy pending journal row without checkpoints remains non-retryable and requires manual diagnosis rather than replay.

## Health and compatibility

The private listener exposes readiness-oriented `GET /health`. It reports:

- local database validation, schema version, and rollback-only write readiness;
- Honest protocol/task-schema compatibility;
- Asana access and required section registry;
- pending audit repairs;
- active operations and leases.

The write-readiness probe is rolled back and creates no durable workflow or request-journal rows. A
read-only database therefore makes health fail even when reads still succeed; transient lock
contention is reported as a lock rather than database corruption.

Mutation requests recheck compatibility and Asana access before entering workflow code. A failed health dependency blocks mutation before any task write, movement, or new operation is created.

Valid service configuration is sufficient to start the listeners even when a recoverable database,
compatibility, Asana, or restore-fault dependency is unhealthy. This keeps the private health and
administrative restore surface available for diagnosis while normal mutations fail closed.

## GPT Action

The Action uses only the dedicated Funnel URL and Action token. Its checked-in schema is:

```text
openapi/dish-action.openapi.json
```

The checked-in schema intentionally uses the placeholder server `https://dish.example.invalid`. Before importing it, replace that server with the exact Funnel URL, or import the runtime schema from the public listener at `GET /openapi/action.json` so the server URL is generated from the request host. Validate the final URL and HTTPS port in the GPT Action editor before activation.

The Action listener serves the bounded `/v1/action/*` workflow and lease-renewal routes plus the read-only generated schema at `GET /openapi/action.json`. Admin, recovery, migration, backup, private CLI, and generic Asana routes are not present on that listener or in the Action OpenAPI document. The OpenAPI generator and HTTP request validator share one command specification; missing, extra, wrongly typed, or invalid-enum Action arguments are rejected before backend or workflow code. Every Action mutation requires `client.request_id`; reads neither advertise nor accept one. `client.run_id` is the single run identity. A non-blank `independence_attestation` is required only on Verification start. Approval inherits the exact attestation persisted at Verification start and accepts only the same verifier agent/run; every rejection route — Large, Evidence, and Human Review — also inherits the persisted start attestation and does not accept the field. The GPT must reuse the same non-nil canonical lowercase request UUID only when replaying the exact call after a lost response. Both UUID fields use non-nil canonical lowercase form.

Follow `deploy/gpt-action.md` for the exact editor configuration, run-identity rules, Preview gate,
lease handling, and token rotation.

## Tests

Each checkout or agent session creates its own repository-local test environment from
`dish/requirements-test.txt`. Runtime installation remains defined separately by
`dish/requirements.txt`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
```

Use the curated smoke suite for rapid confidence during normal iteration, then run the complete
suite before handing work back. Smoke membership is an explicit per-test contract, so moving or
splitting a test module cannot silently remove a critical test from the gate. Collection also checks
that the gate retains representative coverage of each required launch-critical invariant. The
separate database-boundary lane disables the fast schema-clone shortcut and filesystem-sync
override for tests that must exercise real bootstrap, migration, locking, and durability behavior:

```sh
.venv/bin/python -m pytest --smoke
.venv/bin/python -m pytest --database-boundary
.venv/bin/python -m pytest
```

Do not copy or package `.venv`; it is interpreter-local. Flake detection uses a separate
`.venv-flake` environment and explicit commands that preserve seeds, JUnit XML, environment
metadata, and pass-on-rerun failures without weakening the authoritative gates. See
[`docs/testing.md`](docs/testing.md) for the candidate/quarantine policy and reproducible detection
commands. The committed implementation tests cover service restart, concurrency, leases, credential
scopes, the generated Asana SDK path, Action/CLI equivalence, private/admin HTTP parity,
backup/restore, operational health, and private/public surface separation.

## Documentation map

- `docs/architecture.md` — mandatory agent change map: authorities, invariants, owning layers, and routed reading.
- `docs/testing.md` — authoritative test gates, flake detection, candidate/quarantine policy, and artifact handling.
- `docs/runtime-contract.md` — JSON meanings, exit statuses, retry rules, and operational recovery.
- `docs/future.md` — only work that is not already implemented.

The production migration and cutover are complete. Git history preserves the retired migration
tooling, evidence, and rollout runbook. Use the managed backup/restore commands above for recovery
and require Marco's explicit authorization for any public Action route change.

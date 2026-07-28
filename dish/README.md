# dish

`dish` is the guarded interface for protocol-governed Cooking tasks. The live Asana task remains the content authority; the tool validates exact content, records durable operation evidence, enforces independent Verification, and confirms every write and movement by reread.

## Supported runtime modes

### Shared-service live mode

This is the only supported multi-agent path. One `dish-service` process on Marco's laptop owns:

- the shared SQLite operation database;
- task operation locks and client/run leases;
- the Asana write credential and backend;
- exact-content baselines, audit repair, backup, and recovery;
- requests from `dish`, `dish-admin`, and the GPT Action.

Agent laptops and GPT Actions must not receive `ASANA_PAT`, `ASANA_ENV`, or a writable copy of the shared database.

### Local test mode

Local mode remains available for controlled, single-agent tests and development. Set `DISH_MODE=local` explicitly; an unset mode fails closed. It is not a multi-agent lock and must not be used with `DISH_LIVE_MODE=1`. Once `dish-service` has marked a database as service-owned, direct local CLI/admin access to that database remains forbidden even while the service is stopped.

## Installation

Create the repository virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`dish`, `dish-admin`, and `dish-service` re-exec under `.venv/bin/python` and fail closed if it is unavailable.

## Service-host configuration

Start from `deploy/systemd/service.env.example`. The service host needs:

```sh
DISH_HONEST_PATH=/home/marco/honest-pantry
DISH_COOKING_PROJECT_GID=<Cooking project gid>
DISH_DB_PATH=/home/marco/.local/state/dish/shared.sqlite3
DISH_SERVICE_BACKUP_DIR=/home/marco/.local/state/dish/backups
DISH_SERVICE_BIND=127.0.0.1
DISH_SERVICE_PORT=8765
DISH_ACTION_BIND=127.0.0.1
DISH_ACTION_PORT=8766
DISH_SERVICE_AGENT_TOKEN=<private CLI token>
DISH_SERVICE_ADMIN_TOKEN=<separate Marco-admin token>
DISH_SERVICE_ACTION_TOKEN=<dedicated GPT Action token>
ASANA_ENV=/home/marco/.config/asana-cli/.env
```

Only the service-host environment contains Asana credentials. Protect the environment file and state directory with owner-only permissions. All three service tokens are required for the live dual-listener process, must be distinct, and must not use placeholder or short values. Listener hosts must remain loopback and the private and Action ports must be distinct. Invalid configuration fails before either listener binds.

For the controlled rollout test deployment, keep test state separate from production:

```sh
DISH_HONEST_PATH=/home/marco/honest-pantry-dish-rollout
DISH_COOKING_PROJECT_GID=1216693403164366
DISH_DB_PATH=/home/marco/.local/state/dish/test/shared.sqlite3
DISH_SERVICE_BACKUP_DIR=/home/marco/.local/state/dish/test/backups
```

Do not switch those values to the production checkout, project, or database until the separately
authorized production cutover.

Install and start the systemd unit only during the controlled rollout:

```sh
sudo install -m 0644 deploy/systemd/dish-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dish-service
```

Dish reads the current protocol and task-schema assets from `DISH_HONEST_PATH` when it handles
workflow commands, so edits to those assets do not require a service restart. Restart
`dish-service` after changing its environment or Python code. Verification cycles already in
progress remain bound to their recorded Verification protocol release.

The service binds two loopback listeners:

- private CLI/admin listener on `127.0.0.1:8765`;
- Action listener on `127.0.0.1:8766`, including the read-only generated Action schema.

Keep `DISH_DB_PATH` in one stable host-state location independent of any checkout or worktree. The
service derives its process lock and persistent ownership marker from the canonical database target,
so pathname aliases do not create another authority. A service-owned database cannot later be
opened through direct local mode.

The two listeners are one supervised service. Failure to bind either listener stops startup and
closes the other. On shutdown, one process-wide admission gate closes both surfaces before either
listener is drained. Requests that have not crossed that gate are disconnected without dispatch;
requests already executing are allowed to finish because they may own a transaction or an in-flight
Asana effect. Loopback HTTP responses close their backend connection, so Serve or Funnel must open a
new connection—and cross admission again—for every later request.

See `deploy/tailscale/README.md` before configuring Serve or Funnel.

## CLI client configuration

The normal live CLI is an HTTP client and does not open SQLite or construct an Asana backend:

```sh
export DISH_LIVE_MODE=1
export DISH_MODE=service
export DISH_SERVICE_URL=https://<laptop-tailnet-name>:8444
export DISH_SERVICE_TOKEN=<private CLI token>
export DISH_CLIENT_RUN_ID=<canonical lowercase UUID for this run>
```

Marco's admin shell uses the same private tailnet URL but a separate token:

```sh
export DISH_LIVE_MODE=1
export DISH_MODE=service
export DISH_SERVICE_URL=https://<laptop-tailnet-name>:8444
export DISH_ADMIN_TOKEN=<Marco-admin token>
export DISH_CLIENT_RUN_ID=<canonical lowercase UUID for this admin run>
```

Never place the CLI/admin token in the GPT Action configuration.

## HTTP request boundary

Every authenticated POST requires exactly one `application/json` media type; parameters such as
`charset=utf-8` are accepted. Duplicate JSON object keys are rejected recursively before request
identity or workflow validation, so no parser-specific last-value rule can authorize a mutation.

## Workflow

Every CLI command response is one canonical JSON result envelope. Follow only `allowed_actions`;
they are derived from the exact live content, placement, durable operation evidence, pending
recovery work, and signoff state. The HTTP health and OpenAPI documents have their own response
shapes.

Typical Research and Verification lifecycle:

```text
start initial/change
→ prepare (writes and rereads exact pending-verification content, then hands off)
→ start verification
→ approve or reject
→ submit after approval
```

The bounded agent surface contains discovery/read commands (`sections`, `read`, `inspect`) and
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

`dish-admin` is Marco-only. In service mode it exposes:

- `recover-lease` to release an expired client/run lease without transferring workflow ownership to Marco;
- `recover` for ambiguous write or movement evidence;
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

There is intentionally no generic `unblock` mutation. Existing protocol-specific recovery routes remain authoritative.

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

The Action listener serves the bounded `/v1/action/*` workflow and lease-renewal routes plus the read-only generated schema at `GET /openapi/action.json`. Admin, recovery, migration, backup, private CLI, and generic Asana routes are not present on that listener or in the Action OpenAPI document. The OpenAPI generator and HTTP request validator share one command specification; missing, extra, wrongly typed, or invalid-enum Action arguments are rejected before backend or workflow code. Every Action mutation requires `client.request_id`; reads neither advertise nor accept one. `client.run_id` is the single run identity. A non-blank `independence_attestation` is required on Verification start and Large rejection. Approval inherits the exact attestation persisted at Verification start and accepts only the same verifier agent/run; Evidence and Human Review rejection routes also inherit the persisted start attestation and do not accept the field. The GPT must reuse the same canonical lowercase request UUID only when replaying the exact call after a lost response. Both UUID fields use canonical lowercase form.

Follow `deploy/gpt-action.md` for the exact editor configuration, run-identity rules, Preview gate,
lease handling, and token rotation.

## Tests

Each checkout or agent session creates its own repository-local environment from `dish/requirements.txt`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Use the fast suite during normal iteration, then run the complete suite before handing work back:

```sh
.venv/bin/python -m pytest --fast
.venv/bin/python -m pytest
```

Do not copy or package `.venv`; it is interpreter-local. The committed implementation tests cover service restart, concurrency, leases, credential scopes, the generated Asana SDK path, Action/CLI equivalence, private/admin HTTP parity, backup/restore, operational health, and private/public surface separation.

## Documentation map

- `docs/architecture.md` — mandatory agent change map: authorities, invariants, owning layers, and routed reading.
- `docs/runtime-contract.md` — JSON meanings, exit statuses, retry rules, and operational recovery.
- `docs/rollout.md` — separately authorized test-project rehearsal, migration, production cutover, and rollback.
- `docs/future.md` — only work that is not already implemented.

The implementation does not itself authorize production activation. Follow
[`docs/rollout.md`](docs/rollout.md) for the separately authorized migration rehearsal, live
test-project smoke, cutover, and rollback.

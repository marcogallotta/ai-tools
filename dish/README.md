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

Local mode remains available for controlled, single-agent tests and development. It is not a multi-agent lock and must not be used with `DISH_LIVE_MODE=1`.

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

For the controlled Step 12 test deployment, keep test state separate from production:

```sh
DISH_HONEST_PATH=/home/marco/honest-pantry-dish-rollout
DISH_COOKING_PROJECT_GID=1216693403164366
DISH_DB_PATH=/home/marco/.local/state/dish/test/shared.sqlite3
DISH_SERVICE_BACKUP_DIR=/home/marco/.local/state/dish/test/backups
```

Do not switch those values to the production checkout, project, or database until the separately
authorized production cutover.

Install and start the systemd unit only during the controlled Step 12 activation:

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
- Action-only listener on `127.0.0.1:8766`.

See `deploy/tailscale/README.md` before configuring Serve or Funnel.

## CLI client configuration

The normal live CLI is an HTTP client and does not open SQLite or construct an Asana backend:

```sh
export DISH_LIVE_MODE=1
export DISH_MODE=service
export DISH_SERVICE_URL=https://<laptop-tailnet-name>:8444
export DISH_SERVICE_TOKEN=<private CLI token>
export DISH_CLIENT_RUN_ID=<unique run identity>
```

Marco's admin shell uses the same private tailnet URL but a separate token:

```sh
export DISH_LIVE_MODE=1
export DISH_MODE=service
export DISH_SERVICE_URL=https://<laptop-tailnet-name>:8444
export DISH_ADMIN_TOKEN=<Marco-admin token>
export DISH_CLIENT_RUN_ID=<unique admin run identity>
```

Never place the CLI/admin token in the GPT Action configuration.

## Workflow

Every response is one canonical JSON result envelope. Follow only `allowed_actions`; they are
derived from the exact live content, placement, durable operation evidence, pending recovery work,
and signoff state.

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
mutation even though it starts from a bare task.

Run `dish --help`, `dish <command> --help`, and the stage walkthroughs for exact arguments.

## Administrative operations

`dish-admin` is Marco-only. In service mode it exposes:

- `recover-lease` for an expired client/run lease;
- `recover` for ambiguous write or movement evidence;
- `discard` for a provably unapplied stale operation;
- `reopen`, `supply-evidence`, and `record-human-decision` for the existing protocol-specific hold routes;
- `authorize-governed-change` for one exact governed-field change;
- `migrate` for explicit task-schema migration;
- `backup-create` and `backup-restore` for managed shared-database snapshots.

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

Restore creates an automatic pre-restore snapshot, validates SQLite integrity and the complete current dish schema/evidence contract, replaces the database atomically, and rolls back if validation fails.

## Health and compatibility

The private listener exposes `GET /health`. It reports:

- local database validation and schema version;
- Honest protocol/task-schema compatibility;
- Asana access and required section registry;
- pending audit repairs;
- active operations and leases.

Mutation requests recheck compatibility and Asana access before entering workflow code. A failed health dependency blocks mutation before any task write, movement, or new operation is created.

## GPT Action

The Action uses only the dedicated Funnel URL and Action token. Its checked-in schema is:

```text
openapi/dish-action.openapi.json
```

The checked-in schema intentionally uses the placeholder server `https://dish.example.invalid`. Before importing it, replace that server with the exact Funnel URL, or import the runtime schema from the public listener at `GET /openapi/action.json` so the server URL is generated from the request host. Validate the final URL and HTTPS port in the GPT Action editor before activation.

The Action listener serves only the bounded `/v1/action/*` workflow and lease-renewal routes. Admin, recovery, migration, backup, private CLI, and generic Asana routes are not present on that listener or in the Action OpenAPI document. The OpenAPI generator and HTTP request validator share one command specification; missing, extra, wrongly typed, or invalid-enum Action arguments are rejected before backend or workflow code.

Follow `deploy/gpt-action.md` for the exact editor configuration, run-identity rules, Preview gate,
lease handling, and token rotation.

## Tests

From `dish/`:

```sh
.venv/bin/python -m pytest
```

The committed Step 11 tests cover service restart, concurrency, leases, credential scopes, the generated Asana SDK path, Action/CLI equivalence, backup/restore, operational health, and private/public surface separation.

## Documentation map

- `docs/architecture.md` — current internals, authority boundaries, persistence, recovery, and extension rules.
- `docs/runtime-contract.md` — JSON meanings, exit statuses, retry rules, and operational recovery.
- `docs/dish-tool-future.md` — only work that is not already implemented.
- `docs/dish-tool-update.md` and `docs/dish-tool-update-imp.md` — historical change analysis and implementation provenance, not current architecture authority.

Step 11 implements the shared-service and GPT Action gate. It does not itself authorize production activation; migration rehearsal, live test-project smoke, cutover, and rollback remain Step 12.

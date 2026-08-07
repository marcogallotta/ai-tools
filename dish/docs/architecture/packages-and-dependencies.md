# Packages and dependencies

## Read this when

Read this when adding a module, moving a rule, introducing a cross-package import, changing a package entry point, or deciding where a new worker/service belongs.

## Scope

This document owns package responsibilities and dependency direction. It does not list every file or restate workflow behavior.

## Authoritative implementation

- `dish_tool/`: current command applications, workflow use cases, canonical document logic, SQLite persistence, Asana gateway, CLI clients.
- `dish_service/`: shared-process composition, HTTP/auth, replay, leases, process/database ownership, backup/restore, dark-launch capture.
- `dish_shadow/`: transport-independent dark-launch command-treatment policy.
- `dish_pg/`: isolated PostgreSQL target models, command port, workers, migrations, rehearsal, release, and cutover controls.
- `test_selection/`: path ownership and governed test planning.
- Executable entry points: `dish`, `dish-admin`, `dish-service`, and `scripts/`.

## Actors, processes, and stores

`dish_tool` and `dish_service` run in the default service process. CLI modules in `dish_tool` may call the service client but must not gain live workflow authority. `dish_pg` runs in explicit test/runtime worker processes and owns PostgreSQL sessions. `dish_shadow` is pure policy shared by legacy capture.

## Authority and data ownership

| Package | Owns | Must not own |
|---|---|---|
| `dish_tool` | Current workflow/domain rules, SQLite evidence, exact task effects, canonical envelopes | HTTP credentials, service process lifecycle, PostgreSQL commits |
| `dish_service` | Process composition, authentication, transport, replay orchestration, leases, backup/restore, shadow capture | Duplicate workflow transition policy or canonical task schema |
| `dish_shadow` | Execute/capture-only/excluded treatment for already-authorized commands | Workflow legality, database state, network I/O |
| `dish_pg` | Target PostgreSQL authority, transactions, projections, dark-launch comparison, release/cutover evidence | Current legacy production authority before activation |
| `test_selection` | Classification of changed paths into proof lanes | Runtime behavior |

## Invariants

- Put a rule in the highest shared layer that owns the fact; do not duplicate it in each caller.
- HTTP and CLI layers parse and map; they do not decide legal workflow transitions.
- Repositories and PostgreSQL domain services participate in caller-owned transactions and do not commit independently.
- `dish_service.application` must remain the default current-service composition root.
- PostgreSQL imports of legacy policy/helpers are compatibility bridges; they must not let legacy runtime state become target authority.
- A new cross-package import must not create a second ownership path for the same durable fact.

## Process and transaction boundaries

`dish_service` opens current SQLite connections and passes them to `dish_tool` applications. The PostgreSQL runtime and workers create SQLAlchemy sessions through `dish_pg/database.py`; service classes operate inside those sessions. External adapters are called between separately committed claim/attempt/evidence transactions.

## Normal flow

1. Entry wrappers select the interpreter and invoke a CLI or service main.
2. CLI code parses local arguments and either calls the service client or controlled local application mode.
3. Service transport code validates the request and delegates to request coordinators.
4. Coordinators construct `DishApplication` or `DishAdminApplication` and call workflow/domain modules.
5. Persistence and external gateway modules own durable facts and exact effects.
6. Optional shadow capture hands a completed-command envelope to the spool without changing the result authority.

## Failure, replay, recovery, and concurrency

Package boundaries preserve recovery ownership: transport failures become canonical envelopes only at the transport boundary; request replay is service-owned; workflow recovery is domain-owned; database replacement recovery is sidecar-owned; PostgreSQL worker recovery is attempt/claim-owned. Avoid broad exception translation inside pure domain modules.

## Change routing

- New agent command: shared specification, CLI parser, service dispatch, application command, workflow owner, OpenAPI, tests.
- New admin command: `dish_tool/admin_command_spec.py`, CLI/private HTTP, `DishAdminApplication`, service lease classification, tests.
- New PostgreSQL target behavior: `dish_pg` service/repository plus Alembic migration when persistence changes.
- New operator script: `scripts/` calling existing authorities; scripts must not reimplement domain decisions.
- Do not add a generic “helpers” module that obscures ownership.

## Proving tests

- `tests/test_action_authority_structure.py` proves shared action authority is not duplicated.
- `tests/test_admin_command_spec.py` proves the typed admin registry drives surfaces consistently.
- `tests/test_dish_transaction_ownership.py` proves transaction ownership boundaries.
- `tests/postgresql/test_stage_a_release_decomposition.py` proves PostgreSQL release components remain separated.
- `tests/postgresql/test_stage4_command_port.py` proves command-port delegation.

## Current debt and temporary compatibility

There is an intentional import cycle at the package level: CLI modules in `dish_tool` use `dish_service.client`, while `dish_service` composes `dish_tool` applications. This is a client/runtime seam, not permission for arbitrary mutual dependencies. `dish_pg` imports selected legacy policy and identifier helpers to preserve semantics during migration; those bridges should shrink as target-native ownership becomes complete.

## Related documents

- [System context](system-context.md)
- [Commands and surfaces](commands-and-surfaces.md)
- [Extension rules](extension-rules.md)
- [PostgreSQL runtime](postgresql-runtime.md)

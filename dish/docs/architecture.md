# Dish architecture

This document is the current internal map of the Dish system. It is written for contributors and
agents changing the codebase. Operator setup belongs in [`../README.md`](../README.md); command
syntax belongs in `dish --help` and `dish-admin --help`; result and retry semantics belong in
[`runtime-contract.md`](runtime-contract.md).

Update this document in the same commit whenever a change moves an authority boundary, adds a
runtime surface, changes the workflow state model, or changes which component owns a durable fact.

## Purpose and authority

Dish is the guarded mutation path for protocol-governed Cooking tasks.

Three sources have distinct authority:

1. **Honest assets** define the supported protocol release and canonical task schema.
2. **The live Asana task** is authoritative for the current title, notes, and Cooking-project
   placement.
3. **The Dish SQLite database** is authoritative for workflow intent, exact content bindings,
   verification/signoff evidence, leases, external-effect attempts, recovery facts, and audit
   history.

The database never replaces the live task as the document. The live task never replaces durable
operation evidence. A mutation is legal only when the exact live task and the durable evidence agree.

## System at a glance

```text
private dish CLI ───────┐
private dish-admin ─────┼─ HTTP clients ──> private listener ──┐
GPT Action ─────────────┘                  action listener ────┤
                                                              v
                                                       DishService
                                             auth / health / leases / backup
                                                              |
                                      ┌───────────────────────┴───────────────────────┐
                                      v                                               v
                                DishApplication                              DishAdminApplication
                                      |                                               |
                                      └──────── OperationApplicationService ──────────┘
                                                              |
                                              authoritative snapshot + policy
                                                              |
                                          step5–step9 workflow use cases
                                                              |
                            ┌─────────────────────────────────┴──────────────────────────────┐
                            v                                                                v
                   SQLite evidence/recovery                                      ExactTaskGateway
                                                                                          |
                                                                                real Asana SDK
                                                                                          |
                                                                                 live task reread
```

Local test mode bypasses HTTP, authentication, and service leases, but it uses the same
`DishApplication`, `DishAdminApplication`, workflow use cases, database, and Asana gateway contracts.
It is not a supported multi-agent lock.

## Runtime processes and trust boundaries

### Shared live service

`dish-service` is the only supported live multi-agent authority. One process on Marco's laptop owns:

- the writable SQLite database;
- the Asana credential and backend;
- task operation locks and actor/run leases;
- recovery, request replay, audit repair, health, backup, and restore;
- both HTTP listeners.

`dish_service.process_lock.ServiceProcessLock` prevents two service processes from owning the same
database. `DishService` also uses an in-process reader/writer maintenance gate: ordinary requests may
run concurrently, while restore waits for active requests, blocks new requests, and owns database
replacement exclusively.

The service exposes two separate loopback listeners:

- **private listener:** CLI, admin, health, recovery, and backup;
- **Action listener:** only the bounded GPT Action commands and Action lease renewal.

Tailscale Serve exposes the private listener to trusted tailnet clients. Tailscale Funnel exposes the
Action listener. The Action token is not accepted for private or admin routes, and the Action schema
contains no raw Asana, migration, recovery, health, or backup endpoint.

The two listeners run in supervised non-daemon threads. `SIGTERM`, `SIGINT`, or failure of either
listener stops both listeners. Shutdown first stops acceptance, then closes the servers and waits for
every active request handler to finish, because a handler may own a database transaction or an
in-flight Asana mutation. Failure to bind the second listener closes the first before startup exits.

### Local test mode

`DISH_MODE=local` constructs a local SQLite connection and `AsanaBackend` in the CLI process. The
mode must be selected explicitly; an unset mode fails closed. Live mode rejects this path. When the
shared service first owns a database it writes a persistent sidecar ownership marker, and direct local
CLI/admin access to that database remains forbidden even while the service is stopped. Use local mode
only with a separate development database for controlled development, hermetic integration, and
manual test-project smoke checks.

## Entry points and transport

### Agent CLI

`dish` re-execs under `dish/.venv/bin/python`, then calls `dish_tool.cli.main`.

Agent and admin command dispatch use the explicit runtime registries
`CURRENT_COMMAND_HANDLERS` and `CURRENT_ADMIN_COMMAND_HANDLERS`. There is one public application
class per surface; no import-time subclass rebinding or compatibility dispatcher is used.

`dish_tool.cli.build_application` chooses one of two adapters:

- `DishServiceClient` in service mode;
- `DishApplication` in local test mode.

The parser and renderer stay client-side. Candidate files are read by the client and transported as
complete text; the service never opens a client filesystem path.

### Admin CLI

`dish-admin` follows the same pattern through `dish_tool.admin_cli` and either
`DishAdminServiceClient` or `DishAdminApplication`. Admin credentials and routes are separate from
agent credentials and routes.

### GPT Action

The generated/checked-in Action contract is defined by `dish_service.command_spec`,
`dish_service.openapi`, and `openapi/dish-action.openapi.json`. The HTTP validator and OpenAPI
generator consume the same command/argument definitions, so malformed or extra arguments are
rejected before workflow code. `DishActionClient` and the public listener use the same canonical
command envelope as the CLI. The Action is a bounded Dish client, not a generic Asana integration.

### HTTP layer

`dish_service.http` owns only HTTP mechanics:

- route separation;
- bearer-token scope checks;
- body size, timeout, UTF-8, and JSON-object validation;
- client/run identity extraction;
- mapping transport failures to the canonical result envelope.

It does not decide workflow legality.

## Application and policy layers

### `DishService`

`dish_service.application.DishService` is the shared-runtime façade. It:

- opens a fresh validated SQLite connection per request;
- constructs the backend and current Honest release;
- acquires/asserts/releases service leases;
- takes a durable per-operation execution claim around each operation mutation so two requests cannot reach external effects concurrently;
- delegates workflow work to `DishApplication` or `DishAdminApplication`;
- preserves committed success if post-success lease or owned-resource cleanup fails;
- records and replays every mutation request after a valid UUID establishes request identity;
- keeps ordinary replay evidence in SQLite and restore replay evidence in an atomic sidecar outside the replaceable database;
- owns health, backup, restore, and startup checks.

The service must not duplicate stage-specific workflow rules.

### Command applications

`dish_tool.commands.DishApplication` and `dish_tool.admin.DishAdminApplication` own command dispatch,
canonical result envelopes, and invocation auditing. They are thin orchestration layers over the
current workflow use cases.

There is no executable legacy mutation workflow. Historical database records may be inspected or
quarantined through the read-only compatibility boundary, but unsupported historical states do not
fall back into old create/prepare/approve/reject/submit implementations.

### Authoritative application service

`dish_tool.application_service.CurrentWorkflowService` builds one authoritative snapshot from:

- operation status and phase;
- live task content identity and Cooking-project placement;
- current Verification cycle and exact signoff binding;
- pending workflow steps;
- unresolved write or movement attempts;
- migration reconciliation state;
- held content and placement baselines.

`dish_tool.workflow_policy.legal_actions` derives the executable action list from that snapshot.
Mutation entry points call `assert_action` before executing a use case and return a fresh snapshot
afterward. Transports and clients must follow `allowed_actions`; they must not reconstruct legal
transitions independently. Agent-facing results expose only commands available on the bounded
agent surface. When internal policy requires a Marco-admin continuation, `allowed_actions` is
empty and `data.required_admin_action` names the private command for audit and handoff.

## Workflow use cases

The numbered module names reflect the implementation sequence, not separate runtime authorities:

| Module | Current responsibility |
|---|---|
| `step5.py` | exact reads, operation claims, inspection primitives, and explicit task-schema migration |
| `step6.py` | guarded prepare/check-in, canonical candidate validation, content write, and queue handoff |
| `step7.py` | Verification read/binding, independent verifier evidence, approval, and exact signoff |
| `step8.py` | rejection routes, Small/Large correction handling, Evidence/Human holds, and two-pass reopen |
| `step9.py` | signed destination submission, Marco-only destination repair, and recovery of interrupted current operations |

The normal lifecycle is:

```text
create
→ start planning / initial / change
→ prepare
→ start verification
→ approve or reject
→ submit after approval
```

Reject may route to Small correction, Large correction, Evidence, Human Review, or two-pass hold.
Marco-only admin commands resolve the protocol-specific hold and recovery paths.

New workflow logic should live in a use-case/domain module and be entered through
`CurrentWorkflowService`. Do not add a second mutation path in a CLI, HTTP handler, recovery helper,
or compatibility adapter.


## Generic Asana tooling boundary

`tools/asana` is not a supported mutation path for governed Cooking tasks. Its generic read
commands remain useful, but every write passes through `dish_tool.generic_asana_guard`, which
uses live read-only Asana membership lookups and fails closed for managed Cooking tasks, managed
Cooking sections, raw task/project/section mutation paths, and unresolved classifications. The
guard deliberately does not open or write the Dish database. Reference and Sourcing remain the
only explicitly excluded Cooking sections. Do not weaken this to advisory logging or add a bypass
flag; governed writes belong exclusively to the shared Dish service.

## Canonical task and change authority

`dish_tool.task_document` owns deterministic parsing, rendering, and structural validation of the
canonical task. `dish_tool.schema_validation` validates the external Honest schema envelope.
`dish_tool.releases` resolves exactly one supported Honest protocol/schema pair.

`dish_tool.governed_diff` compares canonical fields and sections. It is shared by Small-correction
and post-signoff change handling so explicit material categories cannot be reclassified by a caller.
Authorization permits an exact protected-field change; it does not make a material change
non-material. A change operation's validated level and reason are captured as immutable completed
`operation_steps` intent at `start`, so later canonical Material changes output does not reconstruct
or guess that provenance.

`material_classification` classifies only the canonical body diff of a post-signoff change against
the signed baseline. It is required when that body changed and invalid when no such diff exists. The
caller proposes `material` or `non-material`; Dish remains authoritative for protocol-defined
material paths and reports the requested classification, effective classification, forced reasons,
and resulting route. An accepted non-material diff preserves the prior exact signoff; an effective
material diff creates a new Verification route.

Material-change records use a tool-normalized authority model. An initial canonical candidate may
propose entries using the documented seven-field grammar. Once a canonical baseline exists, prior
entries are immutable and tool-owned: a later candidate may preserve them or omit them, in which case
Dish restores them; any supplied rewrite is rejected. Dish appends the current workflow entry from
durable change intent and the independent approval transition finalizes the latest pending entry. A
ready or submitted task whose latest relevant entry still says `pending-verification` is invalid.

Final destination failure has a split recovery contract. If live reread proves a movement was not
applied, `submit` remains the only legal retry and does not rewrite content. If the approved section
no longer resolves or is no longer legal, the operation remains open in `ready_move_failed`; the
agent surface exposes no mutation and names `repair-destination` as the required Marco-admin action.
`dish-admin repair-destination` is legal only in that state. It validates the replacement against the
live Cooking section registry, proves that the complete canonical diff is exactly
`planning.Destination section`, writes and rereads the complete task once, and records the admin run,
reason, old/new destination, approved identity, and repaired identity in the operation step, write
attempt, and audit trail. The original Verification cycle and signed identity are immutable. The
confirmed repair identity becomes the exact content baseline for the later `submit`, which performs
only the still-pending movement and never repeats approval or an already confirmed content write.

A fresh initial Research operation may enter an Evidence or Human Review hold before candidate
construction through the existing `reject` action with `resume_status=pending-research`. This is an
operation hold, not a content rejection: it records the route, reason, originating agent/run and
request UUID, resolver, timestamp, and the explicit fact that no candidate content or Verification
cycle existed. Resolution returns the same operation to `prepare_required` without installing partial
canonical content or inventing review evidence.

Verification binds an exact confirmed `content_versions` record. Signoff is valid only for that
exact identity. A live `Verified by` string is not sufficient local evidence. Independent
Verification is proven by the verifier's durable `client.run_id` differing from the run that
constructed or last materially edited that candidate; operation IDs, Verification cycle IDs,
actor/model labels, and attestations are not substitutes for run lineage.

## Persistence model

The local SQLite schema is versioned independently from the Honest task schema. Startup runs schema
migrations, integrity checks, foreign-key checks, and semantic validation before serving mutations.

The database location (`DISH_DB_PATH`, falling back to `constants.DEFAULT_DB_PATH`) is deliberately
independent of any Dish checkout or worktree path. There is exactly one shared writable database
(see `ServiceProcessLock` above); deriving its path from the running checkout would fragment that
database across worktrees instead of keeping it singular.

Conceptually important tables are:

| Evidence | Tables |
|---|---|
| operation lifecycle | `operations`, `operation_steps`, `operation_actor_facts` |
| exact task state | `task_content_state`, `content_versions` |
| verification/signoff | `verification_cycles`, `two_pass_resets` |
| external effects | `write_attempts`, `movement_attempts` |
| governed authority | `marco_authorizations` |
| shared ownership | `service_leases` |
| mutation request replay | `service_requests`; restore-safe sibling request journal for `backup-restore` |
| audit and repair | `audit_events`, `command_audit_repairs` |
| historical quarantine | `legacy_submission_quarantine` and retained read-only legacy records |

Triggers enforce append-only or monotonic evidence where recovery depends on historical truth.
Creation facts and intended external effects become immutable when recorded, not only after success.
Confirmed content, signoff, movement, actor, authorization, step, and audit evidence cannot be
silently weakened later.

`dish_tool.database_schema` owns the schema, migrations, triggers, and semantic startup validation.
Repository modules expose narrower persistence operations:

- `workflow_repository.py` — operation, step, actor, and transition facts;
- `database.py` — current persistence primitives and audit-repair processing.

Do not bypass repository/database invariants with ad hoc SQL in workflow or transport code.

## Exact external effects and recovery

All task writes and movements use the same protocol:

1. reread the complete live task and reassert that it still belongs to the Cooking project;
2. compare exact content identity and expected Cooking-project section;
3. persist immutable intended effect;
4. call the real Asana SDK with automatic retries disabled;
5. reread the complete live task;
6. classify the effect as `confirmed`, `not_applied`, or `uncertain`;
7. atomically finalize the corresponding local evidence.

`dish_tool.task_store` implements this contract. `ExactTaskGateway` is the narrow workflow-facing
adapter. Placement is selected by the Cooking project GID; code must never use the first membership
on a multi-project task.

An empty or incomplete Asana response never proves success. Reread state is the proof. A confirmed unchanged reread proves only that an effect was not applied; it does not make a non-retryable backend rejection such as access denial retryable. Section-registry reads follow Asana pagination until no next-page offset remains.

An uncertain result is not mechanically retryable. `dish-admin recover` rereads live state and may
only reconcile it against the immutable expected/intended evidence already stored. Recovery uses the
same declared workflow steps and idempotent executors as normal execution.

## Concurrency and leases

There are three distinct concurrency/ownership mechanisms:

- the database guarantees at most one active operation per task;
- `operation_execution_claims` permits only one request at a time to execute an operation mutation and unresolved external attempts are unique per operation;
- `service_leases` bind that operation to a client owner and run identity for a renewable period.

A workflow handoff may release the actor lease while keeping the task operation active. `allowed_actions`
is filtered for the authenticated owner/run, so a caller is never told to perform an action that its
lease cannot execute. Read-only calls inspect lease state but never acquire, renew, release, or transfer
it. Expired leases fail closed and require `dish-admin recover-lease`, which releases stale ownership
without assigning the operation to Marco. A run may reclaim a missing lease only when durable actor
lineage proves it owns that workflow role. Protocol-specific admin continuations use request-scoped
admin leases and release them before returning. A cleanup failure after the continuation committed is
reported as cleanup/recovery metadata and never reverses the command success. Terminal lease release
waits until workflow steps and ambiguous attempts have durable outcomes. Acquire, renew, release, and
terminal checks are transactional; the configured TTL must exceed the maximum admitted request lifetime
plus the recovery margin.

Workflow state changes and their governed audit facts commit in the same SQLite transaction. Audit-repair workers claim work transactionally, so concurrent workers cannot append duplicate repair events. Startup semantic validation rejects impossible combinations such as multiple unresolved attempts for one operation or an active lease on a terminal operation.

The service process lock and ownership marker are derived from the canonical database target, not the supplied pathname, so symlink aliases cannot create independent ownership of the same SQLite database.

`service_requests` is the ordinary mutation idempotency boundary. Every externally callable agent,
admin, lease, and backup mutation requires a client UUID; reads do not. Once the UUID itself is valid,
the immutable record binds request UUID, owner, run, command, and canonical argument hash before
command validation or external effects, so expected failures and successes replay identically. Exact
repeats return the stored result; mismatched reuse fails; pending or uncertain work is not blindly
reissued. `backup-restore` uses an atomic sibling journal because replacing the database would replace
an in-database replay record. These request records complement, rather than replace, operation
constraints and exact external-effect attempt records.
A fresh request for an already completed logical submission is resolved from the exact signed-content and confirmed destination-movement evidence rather than reopening the operation or repeating movement.

The host process lock is not a substitute for database operation constraints. The client run ID is
the durable unit of actor/verifier lineage, but it does not replace exact candidate bindings and
role facts: independence is checked against the constructor and latest material editor recorded for
the candidate.

## Compatibility and migration

The current engine supports exactly the protocol/schema pair declared in Honest `DISH_VERSION` and
checked against `dish_tool.constants`. Protocol assets are loaded from `DISH_HONEST_PATH`; they are
not copied into the Dish database.

Task-schema migration is explicit through `dish-admin migrate`. Migration code performs only
approved deterministic transformations. Records whose historical content, placement, actor, or
attempt evidence cannot be proven are marked for reconciliation or quarantined rather than treated
as wildcards.

Database migrations must cover:

- fresh databases;
- every supported historical database version;
- open, uncertain, held, and terminal operations;
- restart and recovery immediately after upgrade.

Do not add executable workflow compatibility merely because a test can construct an old state. A
supported compatibility path needs a real producer or a real preserved database requirement.

## Results, auditing, and success boundaries

Every surface returns the canonical JSON envelope described in `runtime-contract.md`. HTTP status is
transport information; the envelope code and `retryable` field carry workflow meaning.

Invocation auditing is supplementary to the governed mutation. If a mutation commits and a later
view refresh, lease finalization, or invocation-audit write fails, the result remains successful and
suppresses unsafe follow-on actions. Durable audit-repair metadata is recorded whenever a persistence
path remains available. A client must never be told to retry a mutation that already succeeded.

## Health, backup, and restore

Private `GET /health` combines database validation, Honest compatibility, Asana access and section
registry checks, pending audit repairs, active operations, and leases. Failed mutation dependencies
block before entering workflow code.

`BackupManager` uses SQLite's online backup API, validates the complete current database contract,
and accepts only managed backup identifiers. Restore obtains exclusive maintenance access, copies a managed source into a candidate, and
migrates and validates that candidate without altering the source backup. A validated pre-restore
snapshot is attempted when the live
database is readable; an invalid live database does not block recovery from a valid managed backup.
Replacement is atomic and a validated pre-restore snapshot is used for rollback when available. If
rollback cannot be proven, the service writes an atomic sidecar fault marker outside the replaceable
database and remains diagnosis-only across process restart. A later validated restore clears that
marker.

Startup distinguishes listener readiness from dependency health. Valid service configuration is the
listener-start boundary; database, compatibility, Asana, and restore-fault failures keep health
unhealthy and mutations fail closed, but the private administrative surface remains available for
diagnosis and validated restore.

## Testing architecture

The test suite should prove invariants and external contracts, not preserve obsolete branches.
High-value test layers are:

- parser, schema, and canonical-renderer tests;
- workflow route × lifecycle phase matrices;
- material category × caller classification matrices;
- exact content drift × placement drift matrices;
- actor role × run-lineage matrices;
- crash injection at every declared workflow step;
- `confirmed` / `not_applied` / `uncertain` external-effect matrices;
- database version × open-operation upgrade matrices;
- service restart, leases, concurrent clients, health, backup, and restore;
- private/action surface separation and credential scope;
- real generated Asana SDK methods over a controlled fake `ApiClient.call_api` transport;
- opt-in live test-project smoke before activation or an SDK upgrade.

Do not replace generated SDK methods with handwritten mocks when testing the SDK contract. Mock the
low-level transport. Do not keep impossible fixtures solely to obtain branch coverage. Mutation tests
or deliberate production breakage are useful for proving that critical tests fail for the intended
reason.

Run the full suite from `dish/`:

```sh
.venv/bin/python -m pytest
```

## Change rules for contributors

Before adding code, identify the owning layer:

- transport/authentication → `dish_service/http.py`, `auth.py`, clients;
- shared runtime, leases, health, backup → `dish_service/application.py` and service modules;
- action legality → `application_service.py` and `workflow_policy.py`;
- workflow behavior → current use-case/domain modules;
- canonical document semantics → `task_document.py`, `governed_diff.py`, schema validation;
- exact Asana effects → `task_store.py`, `task_gateway.py`, `backend.py`;
- durable facts and migrations → repositories, `database.py`, `database_schema.py`;
- command parsing/rendering → CLI/admin modules only.

For every change:

1. state the invariant, not just the example that failed;
2. update every affected route and recovery path through the shared mechanism;
3. add an adversarial matrix case, not only a happy-path assertion;
4. test fresh state, restart, retry/recovery, and historical upgrade when persistence changes;
5. update this document if an authority boundary changes.

## Intentionally separate or deferred

The current architecture does not make Dish a general multi-user platform or raw Asana proxy. It does
not provide generic task editing, arbitrary admin unblocking, automatic semantic recipe judgment, or
an alternate writable legacy workflow.

Potential post-activation work lives in [`dish-tool-future.md`](dish-tool-future.md). Historical
change analysis and implementation plans remain useful as provenance, but they are not the current
architecture contract.

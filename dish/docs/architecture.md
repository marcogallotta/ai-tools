# Dish architecture: agent change map

This is the mandatory orientation for agents changing Dish. It is a decision map, not a narration of
the implementation: it identifies the authorities, invariants, owning layers, and dangerous
boundaries that a locally reasonable change must not violate.

Read this document end to end before changing code. Then use the routing table below instead of
loading every Dish document.

## Route additional reading

| If the change concerns | Also read |
|---|---|
| installation, deployment, service operation, or an operator command | [`../README.md`](../README.md) and the linked deployment guide |
| response fields, exit status, retry, leases, recovery, or client-visible behavior | [`runtime-contract.md`](runtime-contract.md) |
| GPT Action exposure or editor configuration | [`../deploy/gpt-action.md`](../deploy/gpt-action.md) |
| Tailscale Serve or Funnel | [`../deploy/tailscale/README.md`](../deploy/tailscale/README.md) |
| test-project rehearsal, corpus migration, production cutover, or rollback | [`rollout.md`](rollout.md) |
| the protocol's own structure, canonical fields, process records, or change classes | `~/honest-pantry/dish-docs-design.md` and the relevant current Honest assets |
| work not yet implemented | [`future.md`](future.md) |

Removed plans remain available in Git history.

Update this document in the same commit when a change moves an authority boundary, adds a runtime
surface, changes the workflow state model, or changes which component owns a durable fact. Do not
add implementation walkthroughs here when a code pointer or routed reference is enough.

## Mental model

Dish is the guarded mutation path for protocol-governed Cooking tasks. Three sources have distinct
authority:

1. **Current Honest assets** define the supported protocol release and canonical task schema.
2. **The live Asana task** owns current title, notes, and Cooking-project placement.
3. **Dish durable state** owns workflow intent, exact-content bindings, verification evidence,
   actor/run lineage, leases, external-effect attempts, recovery facts, and audit history.

Dish state never replaces the live task as the document. The live task never replaces durable
operation evidence. A mutation is legal only when the authoritative live snapshot and durable
evidence agree.

```text
private dish CLI ───────┐
private dish-admin ─────┼─ HTTP ──> private listener ──┐
GPT Action ─────────────┘          action listener ────┤
                                                       v
                                                DishService
                                                       |
                                 ┌─────────────────────┴─────────────────────┐
                                 v                                           v
                         DishApplication                           DishAdminApplication
                                 └──────────> CurrentWorkflowService <───────┘
                                                       |
                                      authoritative snapshot + policy
                                                       |
                                            workflow use cases
                                                       |
                                  durable evidence <───┴───> ExactTaskGateway
                                                                  |
                                                               Asana SDK
                                                                  |
                                                            live task reread
```

Local test mode bypasses HTTP, authentication, and service leases. It still uses the same command
applications, workflow use cases, database contract, and Asana gateway contract.

## Invariants every change must preserve

### One live mutation authority

`dish-service` is the only supported multi-agent authority. It owns the writable SQLite database,
Asana credential, locks, leases, request replay, recovery, health, backup, and both HTTP listeners.
Clients never receive the Asana credential or writable shared database.

Do not add a governed mutation path to a CLI, transport, generic Asana helper, compatibility
adapter, or recovery shortcut. All live mutations converge on the shared service and current
workflow use cases.

### One action authority

`CurrentWorkflowService` builds one authoritative snapshot. `workflow_policy.legal_actions` derives
legal transitions from it, and ordinary mutations assert the selected action against it.

Clients, transports, and individual use cases must not independently reconstruct which action is
legal. Agent-facing callers follow only `allowed_actions`. Private continuations are reported as
`data.required_admin_action`, not exposed as agent actions.

### Exact live state, not assumed state

Every governed write or movement is bound to exact content identity and Cooking-project placement.
The task is reread before and after an external effect; an SDK response alone never proves success.
Placement is selected by Cooking project GID, never by the first membership.

An effect has exactly one evidence-backed outcome: `confirmed`, `not_applied`, or `uncertain`.
Uncertain effects are reconciled against recorded intent; they are never blindly retried.

### Durable intent before external effects

Dish persists the intended effect before calling Asana and durably finalizes the corresponding
attempt after reread. Creation facts and intended effects become immutable when recorded, not only
after success.

Every multi-step workflow mutation routed through the operation service has a request-scoped
`operation_executions` baseline. Failure reconstruction may attribute only evidence created or
changed by that execution; older operation history cannot be presented as the failed call's work.

### Committed success stays success

A later view refresh, lease cleanup, or invocation-audit failure must not turn an already committed
mutation into retry advice. Record repair or recovery metadata and suppress unsafe follow-on
actions. Never tell a client to repeat an external effect that durable evidence says committed.

### Verification binds content and run lineage

Verification signs one exact confirmed `content_versions` identity. A rendered `Verified by` field
is not sufficient evidence. Independence comes from durable client run lineage: the verifier run
must differ from the constructor or latest material editor run. Operation IDs, cycle IDs, model
labels, and caller attestations do not substitute for run identity. Approval and rejection additionally
require an append-only `dish_inspect_facts` row produced by the exact verifier run after rereading
the exact reviewed content identity in Verification Queue. The fact is bound to the cycle, confirmed
content version, verifier actor fact, attestation, and queue placement; a new cycle or changed live
head therefore requires a new inspection.

A completed non-material check-in does not create a new signoff. Its confirmed candidate identity
inherits the exact approved cycle recorded on the operation. Later non-material check-ins resolve
that durable operation/write lineage transitively, so the original signoff remains explicit without
claiming that an intermediate identity was independently verified.

### Recovery is specific

There is no generic `unblock`. Lease recovery, ambiguous-effect recovery, destination repair,
discard, Evidence resolution, Human Review, completed-task Planning reopen, and two-pass hold reopen
each have narrow preconditions and preserve different evidence. A completed task cannot directly
claim a Planning operation: Marco must use `reopen-planning`, which records an exact completion-state
attempt and audit before the task becomes eligible. Add a new recovery route only when its durable facts and legal continuation
cannot be represented by an existing route.

### Compatibility does not become a second engine

The executable workflow supports the current Honest protocol/schema pair. Historical records may be
read, migrated, reconciled, or quarantined, but they do not fall back into legacy mutation code. A
compatibility path needs a real producer or database-preservation requirement; an artificial test
fixture is not enough.

## Layers and ownership

Put a rule in the highest shared layer that owns the fact. Do not patch every caller separately.

| Layer | Primary code | Owns | Must not own |
|---|---|---|---|
| CLI clients | `dish_tool.cli`, `admin_cli`, service clients | parsing, local candidate-file reads, rendering | workflow legality or live credentials |
| HTTP | `dish_service.http`, `auth` | routes, token scopes, body validation, transport mapping | stage rules |
| shared runtime | `dish_service.application` and service modules | connections, leases, replay, execution claims, health, backup/restore | duplicate workflow transitions |
| command applications | `dish_tool.commands`, `admin` | dispatch, canonical envelopes, invocation audit | alternate domain logic |
| action authority | `application_service`, `workflow_policy` | authoritative snapshot and legal actions | transport behavior |
| workflow | `step5.py`–`step9.py` and domain helpers | stage behavior and governed transitions | ad hoc SQL or transport concerns |
| canonical document | `task_document`, `schema_validation`, `governed_diff`, `releases` | parsing, rendering, schema, protected/material diffs | persistence or network calls |
| external effects | `task_store`, `task_gateway`, `backend` | exact reread/write/move protocol and Asana SDK boundary | workflow policy |
| persistence | repositories, `database.py`, `database_schema.py` | durable facts, migrations, triggers, semantic validation | caller-specific shortcuts |

Agent and admin dispatch use the explicit `CURRENT_COMMAND_HANDLERS` and
`CURRENT_ADMIN_COMMAND_HANDLERS` registries. Do not reintroduce import-time subclass rebinding or a
compatibility dispatcher.

`DishService` owns and closes backend instances only when it selects its internal default factory.
An explicitly injected `backend_factory` and every resource returned by it remain caller-owned; the
service must never close them or infer a different ownership mode from the returned object.

The numbered workflow modules reflect implementation order, not separate authorities:

| Module | Responsibility |
|---|---|
| `step5.py` | exact reads, operation claims, inspection, task-schema migration |
| `step6.py` | prepare, canonical validation, content write, queue handoff |
| `step7.py` | Verification binding, verifier evidence, approval, signoff |
| `step8.py` | rejection, correction, Evidence/Human holds, two-pass reopen |
| `step9.py` | destination submission and interrupted-operation recovery |

Most operation-scoped admin mutations enter `CurrentWorkflowService` and use its durable execution
claim. The deliberate exceptions are `authorize-governed-change` and `discard`, whose handlers own
specialized state/evidence checks. Both retain service request replay; `discard` also takes a
request-scoped admin lease. Do not copy this exception into new commands.

## Runtime and trust boundaries

The shared service exposes two loopback listeners:

- **private:** CLI, admin, health, migration, recovery, backup, and generated Action schema;
- **Action:** bounded `/v1/action/*` workflow, Action lease renewal, and generated schema.

Tailscale Serve exposes the private listener to trusted tailnet clients. Funnel exposes the Action
listener. The Action token is invalid on private/admin routes, and the Action surface contains no
raw Asana, migration, recovery, health, or backup route. `dish_service.command_spec` is shared by the
Action validator and OpenAPI generator so their accepted arguments cannot drift.

Every protected POST authenticates before reading its body, then requires exactly one
`application/json` media type. The shared decoder rejects duplicate object keys recursively before
client identity, request replay, or workflow validation can observe a parser-selected value. Private
routes use HTTP 415 for media-type failures; the Action listener retains its canonical HTTP-200
workflow-envelope rule for authenticated expected failures.

Private `GET /health` is mutation readiness, not process liveness. Database readiness includes a
bounded rollback-only write probe against the schema ledger, reports `database.write_ready`, and
must not create durable workflow, request-journal, or probe rows. A transient writer lock remains a
retryable lock condition; a read-only database is not healthy.

`DISH_MODE=local` is controlled, single-agent development only. It must use a separate database.
Once a database has the service-ownership sidecar, direct local CLI/admin access remains forbidden
even while the service is stopped.

The Action lease renewal is exposed through the same request envelope as every other connected
mutation: `POST /v1/action/renew-lease` carries `arguments.operation_id` plus `client.run_id` and
`client.request_id`. The operation identifier is part of canonical replay arguments, not a separate
path parameter. The private CLI lease endpoint retains its transport-specific path because it is not
part of the connected Action schema.

The generic `tools/asana` interface is not a mutation path for governed Cooking tasks.
`generic_asana_guard` fails closed for covered managed-task writes and moves. Its read commands
remain available, including Planning's deliberate read-only lookup of completed cooking history.
The guard is not a general project-metadata policy engine, so never use raw project or section
metadata paths against the Cooking project. Do not add a bypass flag or weaken covered paths to
advisory logging.

## Workflow and content authority

The ordinary lifecycle is:

```text
create
→ for a completed bare task, Marco explicitly reopens it for Planning
→ start planning / initial / change
→ prepare
→ start verification
→ inspect the exact current reviewed candidate
→ approve or reject
→ submit after approval
```

The live phase alone does not determine legality. Policy also considers exact content, placement,
operation evidence, unresolved effects, reconciliation state, signoff, and caller lease/run.

`task_document` owns deterministic canonical parsing and rendering. `governed_diff` is shared by
Small-correction and post-signoff change handling so callers cannot reclassify protected material
paths. Change-operation level and reason are captured as immutable intent at `start`.

Dish owns canonical Material-change history after the first baseline. A later candidate may preserve
or omit prior entries but cannot rewrite them; Dish appends from durable intent and independent
approval finalizes the pending entry. Changes to the governed Planning facts—including `Dish
candidate`, Purpose, Role, Locks, Exemptions, Research emphasis, Destination section, and
Decisions—require an exact persisted Marco authorization before any candidate write. Caller-supplied
`model` is display metadata, never authenticated provenance.

Specialized client-visible rules for material classification, audit normalization, pre-construction
Research holds, destination repair, and reruns belong in the corresponding sections of
[`runtime-contract.md`](runtime-contract.md).

## Persistence, concurrency, and recovery

SQLite is versioned independently from the Honest task schema. Startup migrates and validates the
database before mutation. The database path is independent of the checkout so multiple worktrees
cannot silently create separate live authorities.

| Durable concern | Tables or storage |
|---|---|
| operation lifecycle | `operations`, `operation_steps`, `operation_actor_facts` |
| exact task state | `task_content_state`, `content_versions` |
| Verification/signoff | `verification_cycles`, `two_pass_resets` |
| external effects | `write_attempts`, `movement_attempts` |
| governed authority | `marco_authorizations` |
| execution and ownership | `operation_executions`, `operation_execution_claims`, `service_leases` |
| request replay | `service_requests`; sibling identity, checkpoint, and result journal for `backup-restore` |
| audit and repair | `audit_events`, `command_audit_repairs` |
| historical quarantine | `legacy_submission_quarantine` and read-only legacy records |

Triggers enforce append-only or monotonic evidence where recovery depends on history. Workflow state
and its governed audit facts commit in one transaction. Workflow and transport code must use
repository primitives rather than bypassing those invariants with ad hoc SQL.

External-effect intent and confirmation are intentionally visible between transactions because they
are the recovery authority around Asana calls. Local terminalization is different: submit's terminal
step, operation transition, transition audit, and submission audit commit as one SQLite unit, so a
reader sees either the pre-terminal operation or the complete terminal evidence.

Concurrency uses separate mechanisms for separate facts:

1. a database constraint permits at most one active operation per task;
2. `operation_execution_claims` serializes workflow mutation execution, while unresolved external
   attempts are unique per operation;
3. `service_leases` bind an operation to an authenticated owner and durable run identity;
4. `ServiceProcessLock` permits one service process for the canonical database target;
5. the in-process maintenance gate makes restore exclusive while ordinary requests may run
   concurrently.

None substitutes for another. In particular, a process lock is not an operation lock, and a run ID
does not replace exact content/signoff bindings.

Workflow terminal status removes mutation authority before service response bookkeeping finishes.
The owning service lease may therefore be briefly visible on a terminal operation, or remain after a
process crash between workflow commit and lease cleanup. That row is a non-authoritative cleanup
tail only when every workflow step and external-effect attempt is resolved; otherwise semantic
validation still fails closed. Terminal cleanup is idempotent, and a later lease acquisition for the
same task reaps only that safe stale row.

Every externally callable service mutation has a client request UUID whose first authoritative
outcome is replay-bound. Pending or uncertain work is inspected or reconstructed, not reissued.
`backup-restore` uses a sibling journal because replacing SQLite would replace an ordinary
in-database request record. Its append-only checkpoints bind the accepted request to the source
backup, prepared candidate, pre-restore snapshot attempt, atomic replacement, validation, and any
rollback. Restart recovery advances only from an exact durable checkpoint and matching file
fingerprints; a legacy pending row without that evidence remains fail-closed. Exact response and
replay behavior belongs in the runtime contract.

The only durable state deliberately outside SQLite is tied to database ownership or replacement:
the service-ownership marker, restore request journal, and restore-fault marker. Do not create a new
sidecar unless the fact must survive replacement of the database itself.

## Compatibility, startup, and availability

`dish_tool.releases` resolves exactly one supported Honest protocol/schema pair from
`DISH_HONEST_PATH`. Protocol assets are not copied into SQLite. Task-schema migration is explicit
through `dish-admin migrate`; database migrations remain automatic at startup and must cover every
preserved historical database version. Verification cycles already in progress remain bound to
their recorded Verification protocol release.

Database migrations must handle fresh state plus open, held, uncertain, and terminal operations.
When historical content, placement, actor, or attempt evidence cannot be proven, reconcile or
quarantine it rather than treating missing facts as wildcards.

Valid service configuration is the listener-start boundary. Recoverable dependency failures may
leave the private diagnosis/restore surface available while health is unhealthy and mutations fail
closed. The operational behavior and recovery instructions belong in the README and runtime
contract.

## Agent change procedure

Before editing:

1. name the invariant and owning layer, not only the failing example;
2. read the routed document and the complete owning module;
3. find every route, recovery path, persistence constraint, and generated contract that shares the
   rule;
4. check whether the change affects Honest protocol structure or current schema assets.

While implementing:

- change the shared authority rather than duplicating a decision at its callers;
- persist intent before effects and preserve exact reread confirmation;
- keep agent and admin surfaces distinct;
- preserve completed evidence through retry, recovery, restart, and migration;
- regenerate checked-in contracts from their shared specification;
- do not preserve an impossible state solely because a test constructs it.

Test the invariant adversarially. Select the applicable dimensions rather than adding only a happy
path:

- lifecycle phase and route;
- exact-content drift and placement drift;
- actor role, owner, and run lineage;
- `confirmed`, `not_applied`, and `uncertain` external effects;
- crash/restart at each newly affected durable step;
- fresh and preserved database versions;
- concurrent clients, leases, and request replay;
- private versus Action surface and credential scope.

When testing the generated Asana SDK contract, call its real generated methods and fake the
low-level `ApiClient.call_api` transport. Handwritten method mocks do not prove the integration.

Use `.venv/bin/python -m pytest --fast` while iterating and run the complete
`.venv/bin/python -m pytest` suite before handoff. When persistence changes, test upgrade and
recovery immediately after upgrade.

After editing, reread this entire document and every changed documentation file for conceptual
overlap and stale claims. Architecture is converged only when each durable fact and decision has one
clear home.

## Deliberately absent

Dish is not a general multi-user platform, raw Asana proxy, generic task editor, automatic semantic
recipe judge, or writable legacy workflow. It has no arbitrary admin unblock.

Potential post-activation work belongs in [`future.md`](future.md), not in a
description of current architecture.

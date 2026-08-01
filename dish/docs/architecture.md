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
| test execution, flaky-test diagnosis, quarantine, or test artifacts | [`testing.md`](testing.md) |
| post-rollout candidates, testing boundaries, and accepted launch limitations | [`known-issues.md`](known-issues.md) |
| GPT Action exposure or editor configuration | [`../deploy/gpt-action.md`](../deploy/gpt-action.md) |
| Tailscale Serve or Funnel | [`../deploy/tailscale/README.md`](../deploy/tailscale/README.md) |
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
Result-envelope formatting never derives actions from a workflow state string. Callers that expose
current actions must pass the exact list produced from the authoritative snapshot. Persistence may
return phase candidates for snapshot construction, but those candidates are not themselves legal
actions.
Principal and lease filtering must update every exposed current-action projection together. Actions
that may become legal only after a private recovery are reported separately and never mixed into a
current `allowed_actions` or nested authoritative view.

### Exact live state, not assumed state

Every governed write or movement is bound to exact content identity and Cooking-project placement.
The task is reread before and after an external effect; an SDK response alone never proves success.
Placement is selected by Cooking project GID, never by the first membership.

An effect has exactly one evidence-backed outcome: `confirmed`, `not_applied`, or `uncertain`.
Uncertain effects are reconciled against recorded intent; they are never blindly retried.

### Exception and cleanup observability

Broad exception handling is permitted only at an explicit process, transport, external-effect, or
success-preserving cleanup boundary. The boundary must preserve the primary outcome, record or log
the secondary failure type, and expose recovery guidance when durable authority may remain. Domain
parsing and validation catch only their documented exception types; programming errors are never
reclassified as ordinary invalid task content.

Pending invocation-audit repair failures do not reverse the current command, but they are logged and
returned as `data.audit_repair_processing_warning`. A failed rejected-command lease release preserves
the original rule error while clearing exposed actions and returning exact cleanup/recovery evidence.

### Durable intent before external effects

Dish persists the intended effect before calling Asana and durably finalizes the corresponding
attempt after reread. Creation facts and intended effects become immutable when recorded, not only
after success.

A Change operation and its completed `change_intent` step are one local transaction; an open Change without that exact intent is invalid and cannot be reconstructed as a successful start.

Planning legality does not establish user intent. A connected `start` with `kind=planning` therefore
uses a durable two-request gate before workflow execution. The first request atomically completes its
`service_requests` result with a single-use `planning_intent_challenges` row and returns
`CONFIRMATION_REQUIRED`; it does not construct the workflow application, read or change the task,
open an operation, or acquire a lease. A fresh request from the same authenticated owner/run may
claim that exact challenge only for the same task, agent, and start target, with either
`intent_basis=user_requested` or `intent_basis=agent_override` plus a non-blank
`override_reason`. The follow-up request is durable before the serialized challenge transition;
only one fresh request can claim the issued row, and successful Planning start atomically binds the
consumed challenge to the resulting operation and authoritative request result. Exact replay returns
the existing challenge or result; a different request cannot
reuse the challenge. Supplying an intent basis on the first request cannot bypass challenge issuance.

Every multi-step workflow mutation routed through the operation service has a request-scoped
`operation_executions` baseline. Failure reconstruction may attribute only evidence created or
changed by that execution; older operation history cannot be presented as the failed call's work.
Workflow audit rows written inside the claimed executor carry the exact durable
`audit_events.operation_execution_id`. Recovery selects those positively bound rows rather than
inferring ownership from operation-wide row order or event names. Concurrent invocation audits,
verifier inspection facts, and Marco authorization grants remain unbound to the unrelated execution
that happened to be active. A pre-schema-35 execution whose immutable baseline predates this field
retains the conservative row-order fallback only until that already-in-flight execution resolves.

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

A Small correction does not rewrite that reviewed binding. The cycle keeps the exact content version
and identity that produced the inspection fact. A confirmed correction write links that reviewed
identity to the verifier's corrected pending-verification identity, and the confirmed signoff write
links the corrected identity to the cycle's signed ready identity. Every current Small transition
with a distinct corrected candidate must prove this three-part lineage. Historical collapsed rows
that have no distinct reviewed-to-corrected transition remain readable without fabricated evidence.
Approval, including restart recovery that completes a pending Small signoff, validates semantic
evidence before its execution journal can complete successfully, so the request that creates an
invalid approval cannot return `OK` and leave a later request to discover it.

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

Marco identifies admin targets by Asana task, not by internal ID. Every `submission_id`-targeted
admin command (`recover`, `repair-destination`, `discard`, `abandon-operation`, `reopen`,
`supply-evidence`, `record-human-decision`, `authorize-governed-change`) and `reconcile-abandonment`'s
`abandonment_id` accept a task GID or supported Asana task URL in place of the exact ID; a decimal or
URL-shaped value is resolved to that task's open operation (or, for `reconcile-abandonment`, its one
non-completed abandonment) before dispatch, in the shared layer both the local and service-mode
`DishAdminApplication.execute()` call, so resolution is identical regardless of transport. An
already-exact ID is never reinterpreted. `expire-lease`'s task GID/URL target and `recover-lease`
(a path-parameter, service-only route) are unchanged and out of scope for this resolution.

### Compatibility does not become a second engine

The executable workflow supports the current Honest protocol/schema pair. Historical records may be
read, migrated, reconciled, or quarantined, but they do not fall back into legacy mutation code. A
compatibility path needs a real producer or database-preservation requirement; an artificial test
fixture is not enough. Legacy submission rows and their write-attempt columns remain readable for
migration and diagnostics, but production exposes no API that creates, transitions, or recovers a
legacy submission.

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

The isolated Stage A target implementation lives under `dish_pg/`. Its application
services own SQLAlchemy sessions and transaction boundaries; repositories accept the
owned session and never commit independently. Until an explicit authority activation,
current transports and workflow modules must not import `dish_pg` or treat its state as
production authority.

Stage 2 gives that isolated target the foundational authority model. Alembic revision
`0002_core_authority_model` owns generation and activation provenance, immutable Honest
contract bindings, governed project/section registries and aliases, stable Dish task identity,
immutable complete task documents and activations, and append-only membership, placement, and
completion occurrences with validated current pointers. `CoreAuthorityService` may assemble an
imported task only as one caller-owned transaction with exact import provenance; it fabricates no
request or command execution.

Stage 3 adds the isolated workflow and concurrency authority through
`0003_workflow_authority`, `stage3_models.py`, and `workflow.py`. It owns generation-bound runs,
immutable requests and outcomes, replay identity, command executions and claims, exact task and
operation fences, workflow operations/steps/actors, classified leases, Planning challenges, Marco
authorization state and immutable event history, Verification occurrences and signoff, named
Evidence and Human Review authority, abandonment/succession evidence, governed audit/causality,
and restart-discoverable invocation-audit obligations and repairs. Mutable current rows are
revisioned; evidence rows are immutable. The service methods participate in the caller's one
transaction and never commit independently. Same-task exclusivity is database-constrained while
independent tasks have no global serialization point.

Stage 4 adds the isolated command and service port through `command_contract.py`, `planner.py`,
`read_model.py`, `command_port.py`, `protocol.py`, and the checked-in PostgreSQL Action OpenAPI.
The port owns the complete retained command registry, exact request replay, caller-owned command
transactions, deterministic planning, exact external-effect adjudication, registry-bound opaque
pagination, and one-task current-view computation. It delegates workflow legality to
`workflow_policy` rather than copying the policy matrix. The protocol adapter reuses the established
route-class bearer model and authenticates before body loading; it introduces no cookie/session
authority. `section-tasks` is one bounded relational query and does not run workflow policy per row.

This remains an isolated non-production target. Current production transports and workflow modules
still do not import `dish_pg`; downstream Asana projection is absent until Stage 5, and production
authority remains closed until Stage 6 activation.

Agent and admin dispatch use the explicit `CURRENT_COMMAND_HANDLERS` and
`CURRENT_ADMIN_COMMAND_HANDLERS` registries. Do not reintroduce import-time subclass rebinding or a
compatibility dispatcher.

`DishService` owns and closes backend instances only when it selects its internal default factory.
An explicitly injected `backend_factory` and every resource returned by it remain caller-owned; the
service must never close them or infer a different ownership mode from the returned object.

Cross-stage workflow concepts live in neutral domain modules. In particular, Small-correction
write lineage and abandoned pre-construction hold resolution are not owned by a numbered stage.
Numbered workflow modules must not import one another through local imports to evade an architectural
cycle, and private stage helpers are never cross-stage APIs.

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

Shutdown has one process-wide admission boundary across both listeners. A connection does not own
request authority merely because the kernel accepted its socket: each request must cross the gate
before authentication, replay, database, or workflow dispatch. Once shutdown begins, unadmitted
connections are closed and all responses terminate the loopback backend connection, preventing a
reverse proxy from carrying later work over pre-shutdown keep-alive state. A handler that already
crossed admission remains non-daemon and drains to completion; listener shutdown must never abandon
a transaction or external-effect attempt.

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
approval finalizes every pending entry in the reviewed correction chain. Changes to the governed
Planning facts—including `Dish candidate`, Purpose, Role, Locks, Exemptions, Research emphasis,
Destination section, and Decisions—require an exact persisted Marco authorization before any
candidate write. Caller-supplied `model` is display metadata, never authenticated provenance.

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
| abandoned-attempt lineage | `abandonment_attempts`, `operation_successions` |
| exact task state | `task_content_state`, `content_versions` |
| Verification/signoff | `verification_cycles`, `two_pass_resets` |
| external effects | `write_attempts`, `movement_attempts` |
| governed authority | `marco_authorizations` |
| execution and ownership | `operation_executions`, `operation_execution_claims`, `service_leases` |
| request replay | `service_requests`; sibling identity, checkpoint, and result journal for `backup-restore` |
| Planning intent confirmation | `planning_intent_challenges` |
| audit and repair | `audit_events` with optional exact `operation_execution_id`, `command_audit_repairs` |
| historical quarantine | `legacy_submission_quarantine` and read-only legacy records |

Triggers enforce append-only or monotonic evidence where recovery depends on history. Workflow state
and its governed audit facts commit in one transaction. The pre-construction Research hold is one
explicit example: its phase transition, completed hold step, and
`research.preconstruction_blocked` decision audit either all commit or all roll back. If that local
unit fails before any workflow effect commits, the operation execution remains uncertain only to
bind exact request replay; replay of that request UUID reconstructs the same hold outcome and
resolves the request ledger after the hold and audit are durable. Workflow and transport code must
use repository primitives rather than bypassing those invariants with ad hoc SQL.

SQLite transaction control is centralized in `dish_tool.transactions`. Runtime code chooses one of
three explicit ownership contracts: `immediate_transaction` owns a serialized writer unit and uses a
savepoint when nested; `join_or_begin_immediate` joins a caller-owned atomic unit or creates the
boundary when none exists; and `require_transaction` marks helpers that may only participate in an
existing caller transaction. `savepoint_transaction` owns nested rollback isolation. No workflow,
service, lease, request-journal, recovery, migration, or audit-repair module issues raw
`BEGIN`/`COMMIT`/`ROLLBACK`/`SAVEPOINT` statements. This preserves each documented atomic unit while
making commit ownership reviewable in one module and ensuring process-exit exceptions roll back the
unit they own.

A Marco authorization
grant is one `BEGIN IMMEDIATE` unit: the operation-open check, exact semantic deduplication,
authorization row, and `marco.authorization` audit either all commit or all roll back. Reservation
never treats an unaudited historical row as a usable capability.

An unresolved `uncertain` operation execution remains an operation-scoped mutation fence even after
its active process claim is gone. Only exact replay of that request or explicit authoritative
recovery may reacquire the operation. Resolution is monotonic: the original uncertainty evidence is
preserved, while separate resolution evidence and a resolution result complete the execution and
request once the missing governed proof is durable. Fresh request UUIDs cannot bypass that fence.

External-effect intent and confirmation are intentionally visible between transactions because they
are the recovery authority around Asana calls. Planning reopen uses the same rule: a `started` or
`uncertain` attempt is valid durable evidence, not whole-database corruption, but it is a task-level
admission lock until live evidence and the original request converge. When exact replay is allowed
to resume a proven-not-applied reopen, the authoritative reread, external update, confirmation
reread, and terminal evidence run under one `BEGIN IMMEDIATE` writer boundary. Concurrent exact
replays therefore cannot both issue the Asana update; after a crash, rollback leaves the persisted
attempt available for live-state reconciliation. Known trade-off: because the writer boundary spans
the external Asana call, it holds the database's single writer lock for that call's full duration,
blocking every other write in the service, not only this task's. A slow or hung call can stall
concurrent agent writes until it returns or the runtime busy-timeout elapses; blocked callers see a
retryable `database_writer_lock` failure rather than an incorrect result. Acceptable while reopen is
an infrequent, Marco-issued admin action; the risk shrinks once the backend is no longer an external
network call. Local terminalization is different: after exact
movement confirmation, submit persists a `submission_terminal_intent`; the terminal step, operation
transition, transition audit, and `operation.submitted` audit then commit as one SQLite unit. A
failed terminal audit therefore leaves an open, explicitly recoverable operation whose movement is
not repeated, rather than a completed submission without proof.

Concurrency uses separate mechanisms for separate facts:

1. a database constraint permits at most one active operation per task;
2. `operation_execution_claims` serializes workflow mutation execution, while unresolved external
   attempts are unique per operation;
3. `service_leases` bind an operation to an authenticated owner and durable run identity;
4. `ServiceProcessLock` permits one service process for the canonical database target;
5. the in-process maintenance gate makes restore exclusive while ordinary requests may run
   concurrently.

New service leases classify their authority at creation. Actor leases carry a task-monotonic
`actor_attempt_seq`; Verification actor leases also carry the exact `context_cycle_id`. Temporary
operation-scoped admin leases use `lease_kind=admin_request` and do not consume actor-attempt
sequence numbers or carry cycle context. These creation facts are immutable. Legacy rows may remain
unclassified until drained, but new code must never infer attempt order or Verification-cycle identity
from timestamps or from admin lease history.

Missing-lease reacquisition follows the durable role that owns the next command, not the command name
alone. In particular, an Initial Research run retrying a pre-construction Evidence or Human Review
`reject` remains stage-actor work: only the exact recorded Research run may reacquire, and its new
actor lease carries no Verification cycle context. A different run remains ineligible and must use
the permanent-abandonment path instead.

Permanent-run abandonment is exposed only through the private Marco admin surface.
`abandonment_attempts` binds one exact classified actor lease, owner, run, and optional Verification
cycle; a partial unique index permits only one non-completed abandonment per task. A clean restart
may publish one immutable `operation_successions` edge from an `agent_abandoned` terminal source to
an exact prepared successor. The successor owns a confirmed `successor_baseline`, begins with
`successor_claim_mode=stage_actor` or `verifier`, and has no active service lease. The transaction-
scoped persistence primitive refuses incomplete workflow steps or unresolved external-effect
attempts, retires the exact source lease, and commits source terminalization, optional incomplete-
cycle abandonment, successor operation/cycle creation, baseline transfer, lineage, and abandonment
state together. Only `dish-admin abandon-operation` and `dish-admin reconcile-abandonment` may call this foundation, and both route through `CurrentWorkflowService` plus the existing operation-execution claim. Connected agents cannot select a transition, terminal outcome, source lease, or replacement target; the successor is always resolved by the system from `task_gid`, never chosen by the caller.

The abandonment frontier policy lives in `dish_tool.abandonment` and is routed only through
`CurrentWorkflowService`. `abandon-operation` creates the durable exact-attempt record while holding the source operation execution claim; a crashed admin invocation is resumed by reclaiming that same linked execution rather than creating an unresolved execution chain. `reconcile-abandonment` is valid only for the recorded abandonment.
The policy rereads the exact live task and revalidates the persisted latest actor attempt before
selecting one of four bounded results: clean restart preparation, preservation of a pre-construction
Research hold, completion of an already-applied recovery suffix, or manual reconciliation. It never
initiates compensation. A clean restart requires exact baseline and placement plus no pending steps
or unresolved effects. Committed finalization is limited to existing
`step9.recover_operation(..., applied)` suffixes whose external write or movement is already proved
by the live task; the classifier verifies that recovery will not issue a new external effect. If that
existing recovery commits before abandonment result bookkeeping, a subsequent reconciliation
recognizes the already-terminal or independently continuable route and completes the abandonment
without repeating recovery.

Clean Planning and Research frontiers now publish the immutable successor, successor-owned baseline,
and exact `prepared_operation_id` start action. The successor remains unowned with
`successor_claim_mode=stage_actor` until that exact target is claimed. A connected `start` for the
same task that omits `prepared_operation_id` is resolved automatically: because at most one
non-completed abandonment can exist per task, the service reads the exact recorded
`abandonment_attempts` row and, only while it is `awaiting_successor_claim` with a recorded
`successor_operation_id`, substitutes that exact target into the same claim transaction the caller
would otherwise have had to name. An explicitly supplied `prepared_operation_id` is still validated
against that exact recorded value and rejected if it differs. Claiming binds the fresh planner,
constructor, or material editor run, records its actor fact, clears the claim mode, and
completes the abandonment; the abandoned run is ineligible. Because no stage work has started on
the prepared successor, an exact claim that passes deployment-current live validation may also
adopt the current schema version in that same claim transaction. This is the only permitted update
to an operation creation-time schema binding; ordinary and already-claimed operations remain
immutable. Change successors retain the exact
completed `change_intent`. A preserved pre-construction Research hold keeps the dead-run source fenced
until its existing hold-resolution command succeeds; that resolution atomically terminalizes the
source and publishes the fresh Research successor instead of returning the source to
`prepare_required`. Ordinary Planning/Research starts still omit `prepared_operation_id`.

Clean Verification abandonment now terminalizes the source operation, closes only the exact
incomplete abandoned cycle as `abandoned`, and publishes a fresh `await_verification` successor with
an unbound cycle. Producer and material-editor lineage are retained; verifier projection, review
binding, attestation, and decision authority are not. The connected continuation names the exact
`target_operation_id` and `target_cycle_id`. The exact abandoned owner/run is ineligible, and the
claim revalidates the target before the external reread and again before persisting review authority.
Successful claim binds the new verifier, clears `successor_claim_mode=verifier`, and completes the
abandonment with the review-start facts in the same local transaction.

Every Verification start selected by an abandonment uses the same exact-target contract, including
an already-created cycle preserved after a committed Research handoff or rejection route. Ordinary
Verification starts may still omit target IDs. A target pair is canonical request identity; one ID
without the other is invalid, and a delayed target for an older cycle fails rather than selecting a
later current cycle. A connected start that omits both target IDs is resolved the same way as the
Planning/Research case: while the exact recorded abandonment is `awaiting_successor_claim` with a
recorded `target_operation_id`/`target_cycle_id` pair, the service substitutes that exact pair into
the same claim; an explicitly supplied pair is still validated against it exactly. The private
operator surface returns the exact connected start target; it never transfers the abandoned actor identity.


While an abandonment is `started`, `blocked_manual_reconciliation`, `awaiting_hold_resolution`, or `awaiting_successor_claim`, it is also a task-level connected-mutation fence. Reads and inspection remain available, but no actor lease is acquired for an unrelated mutation. The service checks this fence before an ordinary `start` can construct a backend or select an operation, and `create_operation` rechecks it inside the operation-creation writer transaction. The sole exception is the exact prepared successor claim returned by the abandonment, whether the caller supplies that exact target or omits it and lets the service resolve it from `task_gid`; the service-layer check permits pass-through only while `awaiting_successor_claim` and only for the exact recorded successor, so `started`, `blocked_manual_reconciliation`, and `awaiting_hold_resolution` remain hard-fenced exactly as before. Blocked results include a generated `reconcile-abandonment` command, a wait-for-confirmation relay, and an instruction to refresh the authoritative action afterward, so a connected agent hitting a still-blocked or wedged fence learns the exact command to relay for Marco to run rather than a raw internal identifier. Hold continuations that require human-authored detail include the generated command template directly in the relay text. A completed route-preserved Verification continuation remains exact-targeted in authoritative reads until its cycle is claimed.

A prepared Planning/Research successor that drifts before claim does not remain in an unusable `awaiting_successor_claim` loop. The exact claim transaction atomically moves the abandonment to `blocked_manual_reconciliation` and returns the generated reconciliation command. `reconcile-abandonment` restores the successor-owned immutable baseline and expected placement through journaled successor write/movement attempts, then republishes the same exact prepared start. Contradictory or unrelated successor effects remain blocked. Immutable succession and baseline bindings are never rebased in place.

Abandonment workflow settlement and operation-execution/request settlement are separate crash
boundaries. If succession, hold preservation, or committed finalization commits before the linked
Marco execution and service request complete, the durable operation claim remains the exact replay
authority even though `abandonment_attempts.current_execution_id` has already been cleared. A later
`reconcile-abandonment` reclaims that same dead execution, returns the already-stored abandonment
result without repeating workflow effects, and completes both the original request and the
reconciliation request.

Request-scoped lease renewal, expired-lease recovery, and explicit administrative lease expiry
commit the lease effect and replayable service-request result in the same SQLite transaction; neither
fact may become durable alone. `expire-lease` resolves an exact lease ID or the one active lease for a
task inside that writer transaction and releases it only when the existing process-identity helper
reports no live operation execution claim. The claim row and all workflow/recovery evidence remain
untouched. Exact request replay returns the stored outcome without resolving the target again, so a
replacement lease is never affected by replay. This is a point-in-time lease release, not durable
owner/run revocation: the previous run may automatically reacquire if durable actor lineage still
permits it and no other active lease exists.

Protocol recovery of an unresolved uncertain execution is narrower: when that exact durable
execution advertises `required_admin_action: recover`, Marco may run only that recovery while the
original actor lease is still live. The lease remains bound to its owner/run and is never
transferred. If that exact recovery durably resolves the execution into a role-handoff phase, the
service may release only the exact lease row that predated and fenced that execution. Release is
revalidated under one SQLite writer transaction against the resolved execution, handoff phase,
absence of mutation claims, pending steps, and unresolved attempts; a replacement or unrelated
lease is never touched. Every other admin or agent mutation still requires ordinary lease ownership
or the existing expired-lease recovery path.

None substitutes for another. In particular, a process lock is not an operation lock, and a run ID
does not replace exact content/signoff bindings.

Workflow terminal status removes mutation authority before service response bookkeeping finishes.
The owning service lease may therefore be briefly visible on a terminal operation, or remain after a
process crash between workflow commit and lease cleanup. That row is a non-authoritative cleanup
tail only when every workflow step and external-effect attempt is resolved; otherwise semantic
validation still fails closed. Terminal cleanup is idempotent, and a later lease acquisition for the
same task reaps only that safe stale row.

Every externally callable service mutation has a non-nil client request UUID whose first authoritative
outcome is replay-bound. Pending or uncertain work is inspected or reconstructed, not reissued. An
interrupted `reopen-planning` request remains pending while its attempt is unresolved: startup may
complete a terminal attempt/request pair, while only exact owner/run/argument replay may reissue a
reopen that unchanged live `modified_at` proves did not apply. Historical attempts without a usable
original request identity remain task-blocking and require explicit Marco authority rather than an
invented repair.
Request-scoped backup creation reserves its exact output identifier before the filesystem effect,
then commits validated backup metadata with the service-request result; replay reconciles only that
reserved path. `backup-restore` uses a sibling journal because replacing SQLite would replace an ordinary
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

Use `.venv/bin/python -m pytest --smoke` for rapid confidence while iterating and run the complete
`.venv/bin/python -m pytest` suite before handoff. When persistence changes, test upgrade and
recovery immediately after upgrade.

Keep the smoke selection broad, representative, and normally bounded to ten seconds; it is a
curated confidence gate, not a bucket for every quick test. `tests/conftest.py` lists whole
high-signal test files, while the `smoke` and `full_suite_only` markers add or remove deliberate
exceptions. When adding or moving a test, decide whether it materially improves smoke confidence
and whether the resulting bundle still meets its budget. Do not automatically include every new
test or exclude all integration cost: smoke must retain representative workflow, persistence,
restore, concurrency, subprocess, HTTP, and production-topology coverage. The default suite remains
the complete handoff authority.

After editing, reread this entire document and every changed documentation file for conceptual
overlap and stale claims. Architecture is converged only when each durable fact and decision has one
clear home.

## Deliberately absent

Dish is not a general multi-user platform, raw Asana proxy, generic task editor, automatic semantic
recipe judge, or writable legacy workflow. It has no arbitrary workflow-state admin unblock; the private `expire-lease` authority is limited to releasing a service lease and cannot alter workflow facts.

Tracked gaps and accepted limitations belong in [`known-issues.md`](known-issues.md). Broader
post-activation proposals belong in [`future.md`](future.md), not in current architecture.

### Typed recovery and succession bundles

Database restore checkpoints retain their JSON-compatible journal shape, but runtime restore code owns that shape through `RestorePlan`. The type rejects unknown fields and is the only mutable restore-plan representation passed between preparation, replacement, validation, rollback, and recovery phases.

Atomic abandonment succession accepts one immutable `AbandonmentSuccessionSpec`. Callers construct the complete source, successor, cycle, actor, and transfer evidence before entering persistence; the database function no longer exposes a long list of independently swappable scalar arguments.

### Request coordination and HTTP routing

`DishService` remains the composition root and sole shared-service authority, but top-level agent and admin request lifecycles are owned by `AgentRequestCoordinator` and `AdminRequestCoordinator`. They sequence initialization, replay, application construction, lease handling, dispatch, result finalization, and cleanup while calling the existing authoritative service helpers.

HTTP POST path recognition is declarative in `http_routing.py`. The request handler owns transport validation and response mapping; it does not encode route shape through an expanding conditional chain.

### Database initialization layers

`database_initialization.py` owns connection setup, WAL negotiation, migration serialization, and validation mode. `initialize_database` performs canonical schema validation plus the complete historical semantic audit and is used for startup, health, administration, backup, restore, and explicit diagnostics. `open_runtime_database` uses a bounded version-and-ledger check for an already initialized database and leaves historical semantic auditing to those full-audit boundaries; ordinary connected-agent requests continue to validate the exact task, operation, lease, cycle, request, and external-effect evidence they consume.

A first runtime request may bootstrap a missing database for embedded and test callers that have no separate listener-startup phase. Concurrent first callers join the serialized full initialization path when they observe a file that has not yet converged. After convergence, request connections do not rescan the complete append-only history.

The migration ledger and canonical DDL remain in `database_schema.py`. Request code must not reproduce connection or migration setup locally. Stage 8 removes the temporary `database_schema.initialize_database` and private validation aliases; `database_initialization.initialize_database` and the public validation functions are now the only owners.


### Compatibility-surface retirement

Current production code calls `savepoint_transaction`, `immediate_transaction`,
`pending_operation_steps`, and `phase_candidate_actions` directly. Historical
transaction aliases and the forwarding-only `WorkflowRepository` facade are not
part of the supported architecture. New code must use the authoritative primitive
owned by the transaction or workflow module rather than add a second name for it.

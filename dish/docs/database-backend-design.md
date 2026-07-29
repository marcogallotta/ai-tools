# Database-backed task store: draft design

Status: draft future design. This document is not implementation or cutover authorization. Its
purpose is to define the smallest safe replacement for Asana as Dish's live task store and to name
the decisions and proof required before implementation.

Current behavior remains defined by [`architecture.md`](architecture.md),
[`runtime-contract.md`](runtime-contract.md), and [`rollout.md`](rollout.md). Until an explicitly
authorized cutover completes, the live Asana task remains authoritative.

## Decision summary

Replace Asana's remaining live authority with a domain-native task store in Dish's existing SQLite
database. Keep `dish-service` as the only live mutation authority and keep the current command,
workflow, Verification, lease, replay, audit, backup, and recovery boundaries. Add a separate
private human frontend that reads through bounded query APIs and mutates only through Dish commands.

The replacement is not an Asana clone. It owns only:

- the canonical task title and body;
- one current Dish location and completion state;
- immutable content versions and location history;
- the existing workflow and verification evidence;
- task creation, browsing, search, history, and Marco's narrow interventions;
- lifecycle-authorized Markdown editing without a generic content-save bypass;
- a bounded command for every current human Asana action that remains necessary;
- future cook-log records when separately designed and approved.

The cutover is one authority change. Production must never select Asana or the database per task,
write both stores, or accept mutations through both paths. Asana may remain available read-only as a
cutover snapshot, but it stops being live evidence once the database authority is activated.

## Why consider this after activation

The current external-effect protocol is intentionally conservative. Dish records intent, calls
Asana, rereads the task, and classifies the effect as `confirmed`, `not_applied`, or `uncertain`.
That protects production work but creates recovery states that exist only because the document and
workflow evidence commit in different systems.

A database-native task mutation can commit the new task revision, workflow transition, audit,
execution evidence, and replay result together. A process failure before commit rolls the whole
unit back; a response loss after commit is answered by exact request replay. This removes ordinary
content writes, moves, completion changes, and task creation from the ambiguous external-effect
model.

This safety gain does not by itself justify immediate implementation. Under the launch triage
policy, activation evidence should determine priority. Reconsider this design when Asana ambiguity,
rate limits, connectivity, manual recovery, or UI friction becomes recurring operator cost, or when
cook-log and reading needs make a purpose-built store materially simpler than continued Asana use.

## Goals

1. Make one SQLite transaction authoritative for a task mutation and its durable workflow result.
2. Preserve the canonical task document, exact identities, independent Verification, run lineage,
   action authority, request replay, leases, audits, backup, and restore.
3. Remove out-of-band live task edits and Asana network uncertainty from normal workflow.
4. Preserve stable Dish commands and response meaning wherever the backend change does not require
   a deliberate identifier or recovery-contract revision.
5. Give Marco a practical private interface for reading, finding, and intervening in tasks.
6. Import the live corpus deterministically, quarantine exceptions, and retain exact source
   snapshots for acceptance.
7. Delete the live Asana credential and executable Asana mutation path after acceptance.
8. Replace Planning's read-only lookup of completed cooking history with a Dish query.

## Non-goals

- generic projects, memberships, teams, assignees, comments, notifications, or permissions;
- task-body editing that bypasses a lifecycle-authorized, revision-checked Dish command;
- browser or CLI access to raw SQL or generic row CRUD;
- multi-user or hostile-tenant authorization;
- PostgreSQL, replication, multi-host failover, or continuous point-in-time recovery as a cutover
  prerequisite;
- automatic semantic recipe judgment;
- recursive dependency discovery;
- simultaneous Asana and database authorities;
- a writable compatibility engine for historical Asana workflow states.

## Target authority model

After cutover, the three authorities become:

1. **Current Honest assets** define the supported protocol release and canonical task schema.
2. **Dish task storage** owns the current title, body, location, completion state, and immutable task
   revisions.
3. **Dish workflow storage** owns operation intent, Verification evidence, actor/run lineage,
   leases, request replay, recovery facts, and audit history.

The second and third authorities share one database and transaction manager, but remain separate
domain concepts. The current task row is not a substitute for append-only workflow evidence, and
workflow phase is not a substitute for the current task revision.

```text
private Dish frontend ──> private query/command routes ──┐
private dish CLI ────────────────────────────────────────┤
private dish-admin ──────────────────────────────────────┼─> DishService
GPT Action ─────────────> bounded Action routes ─────────┘        |
                                                                    v
                                                     CurrentWorkflowService
                                                       |          |
                                                       v          v
                                               task repository  workflow evidence
                                                       \          /
                                                        SQLite transaction
```

The Action listener remains bounded. The human frontend exists only on the private surface. Neither
surface receives the database path, and neither reconstructs legal actions independently.

## Stable interface and identifiers

The agent-facing command lifecycle remains `create`, `read`, `start`, `prepare`, `inspect`,
`approve` or `reject`, and `submit`. Existing administrative commands remain narrow. Asana-specific
recovery commands or fields are removed only after their historical and current callers have been
accounted for.

The public `task_gid` field should remain during the first cutover to avoid a simultaneous command
rename. It becomes an opaque stable task identifier rather than an Asana claim:

- imported tasks retain their exact Asana GID as their Dish identifier;
- newly created tasks use a canonical non-nil Dish UUID;
- validation accepts exactly those two forms;
- a later API version may rename the field to `task_id`, but the backend migration does not require
  that cosmetic break.

Section GIDs must not survive as invented database identifiers. Responses should introduce stable
Dish location identifiers and names. Asana section GIDs remain only in import provenance and
historical attempt evidence. Any temporary compatibility projection must be explicit and must not
become a second location authority.

## Storage model

The exact SQL belongs to an approved implementation plan. The conceptual model is:

### `tasks`

One current row per task:

```text
task_id                      opaque stable identifier
current_content_version_id  exact immutable version currently displayed
current_location_id         controlled Dish location
completed                   current lifecycle flag
revision                    monotonically increasing optimistic revision
legacy_asana_gid            nullable unique import provenance
created_at
modified_at
```

`current_content_version_id`, `current_location_id`, `completed`, and `revision` change only in the
same transaction as their workflow evidence and audit. `modified_at` is generated by Dish and is
not mutation authority by itself.

Tasks are never hard-deleted through an ordinary command. Completion, exclusion, or a future
explicit archival state preserves their identifiers, versions, and audit relationships.

The current content pointer replaces `task_content_state` as the authoritative current projection.
There must not be two independently writable current-content tables. During database migration,
`task_content_state` may be converted into a compatibility view or retired after every caller uses
the task pointer and its version-specific schema and confirmation provenance has been migrated.

### `content_versions`

Keep immutable full-title/full-body versions and their cryptographic identity. Existing confirmed
versions already contain title and notes; the migration should reuse them rather than introduce a
second document-history table.

Each version also carries the provenance required to interpret it without reparsing it against
whatever Honest assets happen to be current later:

```text
document_kind      bare | planning_brief | canonical
schema_version     nullable only where the document kind has no applicable schema
source_kind        import | creation | workflow | migration
recorded_at        durable provenance time
confirmed_at       nullable time at which this version became confirmed authority
protocol_release   nullable; present only when the release is a fact of that version
```

These fields absorb the version-specific `schema_version` and `confirmed_at` facts currently held
only by `task_content_state`; `recorded_at` does not silently substitute for `confirmed_at`. An
operation or Verification cycle may still carry its own protocol release for operation semantics;
that does not substitute for document-version provenance. Imported or human-created origin versions
must not depend on an operation row to explain their kind, source, or schema claim.

Every current task points to one confirmed version. Bare creation and corpus import create explicit
origin versions with no fabricated workflow operation. A governed mutation appends a new version
and advances the task pointer atomically. Confirmed versions remain append-only and retain their
operation, boundary, schema-release, and lineage relationships.

### `task_locations`

Replace the Asana section registry with controlled Dish locations:

```text
location_id       stable Dish identifier
name              unique display name
role              research_queue | verification_queue | destination | excluded
active            whether new routing may target it
display_order
```

Exactly one active Research Queue and Verification Queue are required. Sourcing and Reference import
as excluded locations. Other approved Cooking sections import as destinations. Location names may
change without changing identity. Removing or repurposing a referenced location is prohibited;
retire it instead.

The Honest task's destination name and identifier must resolve to the same active destination
record. The current deterministic destination checks remain; only their registry source changes.

### Location history

Add a dedicated append-only `task_location_transitions` table. Each committed transition records
task, operation when applicable, old and new locations, purpose, request/execution provenance, and
timestamp.

Historical Asana `movement_attempts` remain immutable evidence and are never reused for local
transitions. For database-native transitions, there is no `started`, `not_applied`, or `uncertain`
network outcome: a committed transaction contains one transition and a rolled-back transaction
contains none. Local transitions must not manufacture terminal “confirmed attempts” for work that
never crossed an external-effect boundary.

### Content transition evidence

Historical Asana `write_attempts` also remain immutable. Database-native content changes append the
new content version and all required lineage in one transaction. They do not insert
database-backed `write_attempts`.

Do not discard the intent, purpose, and reviewed-to-corrected-to-signed relationships currently
carried by historical write records. Give those facts domain-native columns or explicit lineage
relationships on content versions and workflow evidence before migrating every semantic validator
and historical query that consumes them. Keep the external-effect attempt tables readable for
pre-cutover history, but do not carry their recovery ontology into the local authority.

### Audit and read projections

The existing `audit_events` table remains the append-only audit authority. Do not add a generic
`task_events` stream unless a concrete query cannot be served from content versions, location
transitions, operations, Verification records, and audit events.

Search indexes, denormalized list views, or full-text indexes are disposable read projections. They
may be rebuilt from authoritative rows and must never decide workflow legality.

### Request execution ownership

Extend request persistence with a request-scoped execution claim, either on `service_requests` or in
a one-to-one claim table:

```text
request_id          replay-bound request identity
owner_token         unique executor ownership token
claim_generation    monotonic recovery generation
claimed_at
expires_at
reserved_task_id    nullable deterministic output identity
```

Only an atomic compare-and-swap may acquire an unowned or expired claim. A live foreign claim
returns in-progress guidance rather than executing. Recovery increments the generation and issues a
new owner token; the displaced executor can no longer pass the ownership check inside its effect
transaction. Completion of the effect and storage of the canonical result retire the claim
atomically. These semantics are required for request-scoped mutations and do not replace the
existing operation claim for work that already has an operation.

## Transaction contract

Request reservation and execution ownership remain durable admission steps because they must
survive a dead executor. They may commit before the task mutation, but they grant no document
effect. Request identity and execution ownership are separate facts: a pending `service_requests`
row does not by itself authorize an executor to run the request.

Mutations use one of two ownership scopes:

**Operation-scoped mutations** already have an operation. They use the existing operation execution
claim and required service lease.

**Task/request-scoped mutations** have no claimable operation at admission. These include `create`,
`start`, Marco's completion command, permitted bare-task title changes, and comparable lifecycle
interventions. They use a durable request-level execution claim, or an equivalent unique
ownership record, with an owner token and expiry/recovery rules. Task-scoped mutations additionally
serialize on and reread the task inside the SQLite writer transaction.

Where useful, reservation stores deterministic output identity before execution. In particular,
`create` reserves its new task UUID on the replay-bound request. After acquiring request ownership,
its effect transaction proves that the request is still pending, the executor still owns the
request claim, and no task already has that reserved identifier. Exact concurrent replays can
observe or recover the same request, but cannot both execute it.

After admission, every database-native task mutation has one effect transaction:

1. authenticate and validate the request envelope;
2. reserve or match the replay-bound service request and any deterministic output identifiers;
3. acquire the applicable operation-scoped or request-scoped execution ownership;
4. begin the SQLite writer transaction;
5. reread the pending request, ownership token, current task when one exists, and exact
   content/location/version expectations;
6. assert the action through `CurrentWorkflowService`, including lifecycle-specific content
   authority;
7. append the new content version or location transition;
8. update the current task pointer/state and append workflow, Verification, lineage, and governed
   transition-audit evidence;
9. finalize operation execution claims, request claims, and service leases;
10. build a fresh authoritative post-finalization snapshot;
11. derive principal-filtered `allowed_actions` and ownership guidance from that snapshot;
12. construct and persist the canonical request result;
13. commit once.

A crash before step 13 leaves none of steps 7–12 committed. A crash or response loss afterward
returns the stored post-finalization result on exact replay. A fresh conflicting request sees the
committed task revision and fails closed.

An interruption after admission but before the effect transaction may leave a pending request,
expired request claim, or dead operation claim, but no task change. Recovery reacquires the exact
claim type under its durable token and expiry rules. It must reread the request and task after
ownership is reacquired and must not infer a task effect from the pending admission record.

Expected content identity and location remain the semantic concurrency check. The monotonic
`revision` is an additional compare-and-swap guard and query aid, not a replacement for exact
content, placement, signoff, or actor evidence.

### Audit boundaries

Governed audit facts and transition evidence required to prove the mutation are written inside the
effect transaction. The canonical request result is also atomic with that mutation.

Invocation and transport audit remains a success-preserving, repairable boundary after the
canonical result. Its failure must not roll back or turn a committed workflow success into a retry
signal. Moving that audit into the effect transaction would be a separate contract change and is
not part of this design.

Read commands use one consistent SQLite snapshot to build the authoritative task and workflow view.
They never update leases or read projections as a side effect.

Filesystem backup and database restore remain external effects with their existing specialized
journals. Future notifications or exports would also require their own classified effect protocol;
moving task storage into SQLite does not justify weakening non-database effect handling.

## Workflow and recovery changes

The guarded state machine and independent Verification do not change. In particular:

- one active operation per task remains enforced;
- actor and verifier run lineage remains durable;
- inspection and signoff remain bound to exact content versions;
- Small-correction lineage remains reviewed → corrected → signed;
- allowed actions remain derived once from the authoritative snapshot;
- Marco-only holds and interventions remain private and narrow.

Normal DB-native content, placement, completion, and creation mutations no longer return
`BACKEND_UNCERTAIN`. A database availability or writer-lock failure before commit is safe to retry
under the existing request identity rules. Semantic constraint failures remain fail-closed.

If storage failure makes commit acknowledgement itself indeterminate, the service must stop
mutation readiness, reopen and validate the database, and inspect the replay record and task
revision before advising retry. It must not report rollback merely because the backend is local.

Recovery must distinguish:

- historical unresolved Asana effects preserved from before cutover;
- database transactions that either committed or rolled back;
- filesystem backup/restore effects;
- workflow holds and expired leases, which remain real regardless of backend.

Do not keep generic write/movement recovery executable for new DB-native transitions merely because
historical rows use it. Historical unresolved effects must be resolved or quarantined before
cutover; historical terminal evidence stays readable.

Planning reopen becomes an ordinary transactional completion-state change. It remains Marco-only
because that is a lifecycle authority decision, not because the update is technically uncertain.

## Private frontend

The frontend should be delivered incrementally. Its first useful version is deliberately narrow:

- list and search tasks by title, location, completion, and active-operation status;
- open the exact current canonical document;
- show content, location, operation, Verification, audit, and recovery history;
- create a bare task with a title-only form;
- offer text editing only as the input control for a lifecycle-authorized command;
- expose Marco's existing private interventions with their exact preconditions;
- show backup health and cutover/import quarantine status;
- later append cook logs through a separately designed command.

The initial editor may be an ordinary textarea with preview. A later release may replace that
control with an established open-source Markdown or text editor for syntax highlighting,
keyboard behavior, preview, or version comparison. The editor component is a presentation choice:
adopting or replacing it must not change the canonical Markdown representation, database schema,
or lifecycle command contract. Rich-text editor state and editor-specific document formats do not
become authoritative task content.

There is no generic canonical-content save command. Revision and exact-version checks protect
concurrency, but they do not confer authority to create a new current version. Content legality is
state-based:

- a bare task is created title-only with empty body; a narrow command may change its title while it
  remains bare;
- a Planning brief is authored or changed only through the Planning workflow;
- a governed canonical task is authored or changed only through the applicable Research, Change,
  correction, or explicitly designed Marco lifecycle operation;
- a signed or destination task has no ordinary save action; changing it starts Change or another
  named lifecycle operation that invalidates or supersedes evidence explicitly;
- a completed task must be reopened or cloned through a named command before content changes.

The service derives these actions from the authoritative task and workflow snapshot. Merely having
no active operation is not sufficient: an inactive task may still be signed, submitted,
destination-placed, or completed. An edit control may therefore be read-only or absent even though
the task has no current owner.

When an authorized lifecycle command accepts edited text, the browser loads the task identifier,
exact content version, monotonic revision, action identity, and any operation/run authority that
command requires. It submits the complete proposed document with those expectations and a fresh
request UUID. `dish-service` reasserts lifecycle legality, rejects stale state without overwriting
either version, appends the new immutable content version, advances the task pointer, and records
the required lineage, governed audit, and replay result in one transaction. The UI then renders
the committed canonical result or presents the newer current version for explicit reconciliation.
Silent last-write-wins and editor-level force-save behavior are prohibited.

The frontend must not impersonate an agent or invent run lineage. Agent workflow actions remain on
the authenticated agent surfaces. If a future UI hosts an authenticated agent session, it may
render only the actions returned for that exact principal and run.

Before cutover, inventory the human actions currently performed in Asana. At minimum, define how a
bare task is created, how completed cooking history is searched, and how a cooked task is marked
complete. Any required replacement is a narrow command with explicit preconditions and audit—not a
generic row or content editor. Markdown editing may use a reusable editor component, but content is
accepted only by the lifecycle command authorized for the current state. Completion needs its own
lifecycle design because removing out-of-band Asana edits otherwise removes the only way to produce
the completed state that Planning reopen consumes.

The browser never sends SQL, chooses arbitrary state transitions, patches task rows, or derives legal
actions. State-changing UI controls call the same command applications as CLI/admin routes with
fresh request UUIDs and render the canonical result envelope.

The frontend is served only on the private listener or through a same-origin private companion.
Marco's admin bearer credential must not be stored in frontend source, URLs, logs, or browser
persistent storage. The chosen UI architecture must preserve the existing private-versus-Action
credential boundary and must not make admin routes reachable from the Funnel listener.

## Import and cutover

### Rehearsal

1. Freeze the exact Dish and Honest revisions used for the rehearsal.
2. Snapshot the complete Asana corpus and the complete Dish database.
3. Require no executing claims, unresolved effects, or uncompleted service requests.
4. For the first production cutover, finish, discard, or explicitly quarantine every open
   operation rather than migrating live mutation authority mid-operation.
5. Import every in-scope task, section, completion state, title, body, and source timestamp into a
   copied database.
6. Classify each imported record under the rules below and apply only that class's reconciliation
   and validation requirements.
7. Quarantine mismatches that affect live authority; do not infer content, readiness, provenance,
   destination, or signoff.
8. Validate database semantics, applicable task-schema conformance, queries, backup/restore, and the
   full workflow suite against the imported copy.
9. Exercise the private frontend against that copy without production mutation authority.

Import classes are:

1. **Active or incomplete governed tasks:** reconcile exact current identity and location against
   `task_content_state`, content versions, operation history, and applicable signoff; require
   current structural validity before admitting live work.
2. **Tasks connected to unresolved or open evidence:** resolve the evidence or quarantine the task
   before cutover. No unresolved external effect becomes a local committed fact by inference.
3. **Completed historical tasks:** import the exact source snapshot as read-only history with
   explicit historical provenance. Do not assert current-schema conformance, complete workflow
   evidence, or signoff that the source does not prove. Apply migration and current validation only
   if a later named command reopens or clones the task.
4. **Excluded Sourcing and Reference records:** import only when an approved reading, search, or
   provenance requirement includes them; otherwise retain them in the source snapshot without
   making them governed Dish tasks.

The importer is one-purpose migration tooling, not a permanent alternate backend. It reads an exact
snapshot and writes only the staged database.

### Production cutover

After separate explicit authorization:

1. stop mutation admission and drain admitted requests;
2. prove the same quiescence conditions used in rehearsal;
3. take final Asana, database, configuration, and code snapshots;
4. import into the production database and validate every task or approved quarantine;
5. activate the matching Dish code, database schema, Honest revision, query surface, and frontend as
   one compatible set;
6. remove Asana from health readiness and from all live task reads and mutations;
7. revoke or remove the service's Asana mutation credential;
8. keep the source corpus snapshot and Asana project read-only during acceptance;
9. admit DB-backed mutations only after read, search, identity, location, backup, restore, and
   workflow gates pass.

There is no dual-write acceptance period. Shadow reads before cutover may compare stores, but only
one store is writable and authoritative at a time.

### Rollback boundary

Before the first DB-native production mutation, rollback may restore the complete prior Asana-based
code, database, configuration, and corpus authority.

After the first DB-native mutation, Asana is stale. Ordinary rollback must restore a compatible
DB-backed code, database, and frontend set from managed backup. Returning authority to Asana would
require a separately designed, rehearsed reverse migration that preserves every intervening
revision and audit fact; it is not part of this design.

This boundary must be explicit in the cutover approval. Acceptance gates should complete before
opening mutations so rollback to Asana remains simple while it is still valid.

## Implementation sequence

1. Inventory every Asana-owned fact, gateway call, identifier, health dependency, recovery branch,
   semantic validator, generated schema field, test fixture, and required human action.
2. Approve the exact schema, identifier compatibility, location registry, transaction owner, and
   frontend trust model.
3. Add version provenance, task/location storage, dedicated local transition evidence, and semantic
   validation behind a test-only database repository.
4. Add request-scoped execution ownership, then convert operation- and task/request-scoped
   mutations to the single effect-transaction contract with crash/concurrency tests.
5. Add bounded list, search, history, title-only creation, lifecycle-authorized Markdown editing,
   and private frontend surfaces; start with a textarea and keep command authority independent of
   the editor component.
6. Build and rehearse the one-purpose importer and both rollback modes.
7. Perform the separately authorized production cutover.
8. After acceptance, remove the Asana credential, SDK runtime dependency, generic governed-task
   guard, external-effect branches that have no historical role, and any temporary compatibility
   projections.

During development, production remains entirely Asana-backed. A test configuration may select the
database repository, but no production configuration may route different tasks to different live
backends. After acceptance, do not retain an executable legacy mutation engine.

## Required proof

At minimum, implementation must test:

- fresh task creation and imported legacy identifiers;
- audited human task completion and completed-history lookup;
- exact reads and consistent list/search snapshots;
- title-only bare creation and rejection of a bare body;
- lifecycle-authorized Markdown editing, stale-revision rejection, and preservation of both
  versions after an edit conflict;
- rejection of ordinary edits to Planning briefs, governed canonical tasks, signed/destination
  tasks, completed tasks, and tasks owned by an operation;
- request-scoped ownership for concurrent exact replays of `create`, `start`, completion, bare-title
  change, and other non-operation admission paths;
- deterministic `create` identity across crash, recovery, and replay;
- concurrent mutations against the same and different tasks;
- request replay before, during, and after transaction commit;
- replayed results whose leases, ownership guidance, and principal-filtered `allowed_actions`
  reflect the committed post-finalization snapshot;
- content, location, completion, signoff, and actor drift;
- every Planning, Research, Verification, correction, hold, reopen, and submit route;
- version-specific document kind, schema, source, timestamp, and applicable release provenance;
- historical terminal write/movement evidence, dedicated local transitions, and absence of
  fabricated database-backed attempt records;
- governed audit rollback on failure and success-preserving invocation-audit repair;
- class-specific import validation, including read-only historical imports and rejected unresolved
  live evidence;
- database migration from every preserved schema version;
- semantic validation of current pointers and append-only lineage;
- service restart, writer contention, backup creation, restore, and restore rollback;
- private frontend isolation from the Action listener and command-only mutation;
- absence of Asana calls and credentials in DB-backed production mode;
- exact corpus import counts, identities, locations, completion states, and quarantine reports.

The complete automated suite, an imported-corpus rehearsal, live test-project workflow, backup and
restore rehearsal, and cutover/rollback rehearsal are handoff gates. Testing must exercise real
repository transactions rather than mocking the task repository at the workflow boundary.

## Risks and controls

| Risk | Control |
| --- | --- |
| Two current-content authorities inside SQLite | One task pointer; retire or project `task_content_state` |
| Accidental Asana-shaped schema | Model only canonical documents, locations, workflow evidence, and required reads |
| Backend abstraction becomes a permanent second engine | Test-only selection before cutover; delete live Asana mutation after acceptance |
| Frontend bypasses workflow legality | Query APIs for reads; existing command applications for every mutation |
| Editor overwrites newer or governed content | State-specific lifecycle command plus exact version/revision; no generic save |
| Editor library shapes stored content | Canonical Markdown only; editor-specific state remains disposable presentation data |
| Concurrent replay executes a non-operation mutation twice | Durable request execution ownership; deterministic reserved IDs; transactional ownership recheck |
| Stored replay result describes pre-finalization state | Finalize claims and leases, reread, filter actions, then persist the result |
| Retiring `task_content_state` loses provenance | Move version-specific kind, schema, source, time, and release facts onto immutable versions |
| Local facts inherit Asana uncertainty semantics | Dedicated local transition evidence; historical attempt tables remain immutable and external-only |
| Incidental audit failure reverses success | Governed evidence is transactional; invocation/transport audit remains success-preserving and repairable |
| Identifier migration breaks agents | Preserve `task_gid` field initially; accept imported GIDs and new UUIDs explicitly |
| Historical evidence becomes unreadable | Preserve terminal attempts and provenance; migrate consumers before cleanup |
| Single database loss | Managed validated backups, rehearsed restore, source snapshot, and sensible off-device copies |
| Cutover rollback loses DB-native work | Complete acceptance before mutation; use DB backup rollback after first DB write |
| SQLite writer contention increases | Keep transactions local and bounded; measure real activation load before changing backend technology |
| Import silently blesses drift | Exact snapshot reconciliation and quarantine; never infer missing facts |

## Decisions requiring approval before implementation

The recommended defaults in this draft are:

1. keep SQLite and the existing service deployment;
2. preserve `task_gid` as the first-version field while allowing UUIDs for new tasks;
3. use controlled Dish locations rather than project/membership emulation;
4. require no open operations at the first production cutover;
5. make the private frontend command-driven, with title-only bare creation and Markdown editing only
   inside a lifecycle-authorized action; begin with a textarea and optionally adopt an open-source
   editor later;
6. add request-scoped execution ownership for mutations that have no existing operation;
7. store document kind, schema, source, timestamp, and applicable release provenance per immutable
   content version;
8. use dedicated local transition evidence and never manufacture database-backed Asana attempts;
9. approve narrow replacements for every required human Asana action, including completion;
10. import completed history without current-schema or signoff claims until a named reopen or clone;
11. keep invocation/transport auditing success-preserving and repairable;
12. treat Asana rollback as valid only before the first DB-native production mutation;
13. retain off-device backup as a sensible operational measure, not a replicated-database project.

Implementation needs Marco's explicit approval of those decisions, the final schema, the frontend
trust model, the corpus scope, the acceptance period, and the separately authorized rehearsal and
production cutover.

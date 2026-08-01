# Database backend

Status: reconstructed Stage A architecture draft

Role: this document defines the Stage A database-backend scope, approved decisions, preserved authority semantics, target authority model, and non-negotiable safety invariants. It does not define physical tables, worker algorithms, migration commands, cutover runbooks, or exhaustive tests.

Companion documents:

- `database-backend-imp.md` — implementation design and implementation acceptance;
- `database-backend-migration.md` — baseline, shadow, rehearsal, cutover, rollback, backup, and restore procedures;
- `database-backend-design.archive.md` — frozen non-governing design history and evidence.

The current governing behavior remains defined by the repository architecture and runtime contract until production authority is explicitly activated on PostgreSQL.

## 1. Problem

Dish currently divides durable authority across Asana and a service-owned SQLite database.

Asana owns the live task document and project placement. SQLite owns workflow operations, request replay, leases, execution evidence, Verification, recovery, authorizations, audit facts, and related control state. Commands that affect both systems must preserve intent and adjudicate external effects across a boundary that cannot commit atomically.

This creates three recurring classes of difficulty:

1. content and workflow authority can be observed at different times;
2. a process or network failure can leave an external Asana effect uncertain;
3. migration, backup, and recovery must preserve several distinct evidence sources rather than one transactionally consistent authority.

Stage A moves the live task document and all governed workflow authority into PostgreSQL. A normal authoritative command can then commit task content, workflow state, request outcome, execution evidence, audit facts, and projection intent in one PostgreSQL transaction.

Asana remains authoritative during rollout and becomes a downstream human-facing projection only after cutover.

## 2. Stage A scope

Stage A includes:

- PostgreSQL authority for current Dish tasks and current workflow behavior;
- title/body document storage without structured-content conversion;
- stable Dish UUID identity with Asana aliases;
- preservation of current replay, Planning, Verification, completion, lease, execution, authorization, audit, abandonment, succession, and recovery semantics;
- complete legacy baseline capture;
- Asana-authoritative shadowing with exact evidence or explicit proof gaps;
- resolved-only production cutover;
- asynchronous projection to the existing in-scope Asana projects after cutover;
- operator-managed PostgreSQL backup and restore;
- implementation and migration evidence sufficient for Marco to decide whether production cutover is justified.

Stage A does not include:

- structured recipe or structured dish authority;
- Cooked, Archive, Cooking History, or `log-cook` product behavior;
- a general historical-task promotion or demotion lifecycle;
- a new full user interface;
- high availability, managed PostgreSQL, or multi-region deployment;
- bidirectional Asana synchronization;
- automatic import of direct Asana edits after cutover;
- migration of unresolved operations into live PostgreSQL authority at production cutover;
- routine hard deletion as a lifecycle feature.

Completion has no independent product meaning. It becomes true only as the consequence of a governed Cooked or Archive transition. Stage A does not design those transitions and does not add a generic completion-setting command. Imported completion remains preserved, and the existing narrow `reopen-planning` route may clear it.

## 3. Approved human decisions

Each decision in this section is stated once. Detailed enforcement mechanisms belong in the companion documents.

### 3.1 PostgreSQL authority

PostgreSQL becomes the target authoritative database for task documents, workflow state, request replay, execution authority, and audit evidence.

### 3.2 Document-compatible Stage A

Stage A preserves the current canonical title/body document representation. Structured representation is deferred until after Stage A battle-hardening and separate authorization.

### 3.3 Canonical command boundaries

Canonical content advances only when a complete governed command commits. Private drafts, incomplete agent work, and uncommitted intermediate checkpoints are not canonical Dish authority.

### 3.4 One-way authority transfer

Authority moves in one direction:

- before cutover: Asana and the current SQLite authority are authoritative;
- after cutover: PostgreSQL is authoritative and Asana is downstream.

The systems are never peer authorities and there is no ordinary bidirectional merge.

### 3.5 Existing Asana projects remain the human interface

After cutover, the existing in-scope Asana project set remains Marco's downstream human-facing interface. Direct Asana changes are non-authoritative drift and are never imported as Dish authority.

### 3.6 Universal Dish identity

Every authoritative task has a Dish UUID. Asana GIDs and any later external identifiers are aliases, not canonical task identity.

### 3.7 Initial deployment

Stage A initially runs self-managed PostgreSQL through Docker Compose on Marco's laptop and remains portable to a self-managed AWS host. High availability and managed PostgreSQL are not Stage A requirements.

### 3.8 Resolved-only, one-way cutover

Production cutover admits only resolved authority. Ordinary rollback to Asana ends immediately before the first PostgreSQL mutation request is admitted.

### 3.9 Database-first scope

Implementation re-baselines the live governed domain that exists at that time. Cooked, Archive, and Cooking History are not prerequisites unless separately authorized before re-baselining.

### 3.10 Compatibility policy

Backward compatibility with the current command names, request envelopes, response shapes, and GPT Action schema is not required. The service contract, OpenAPI/Action schema, agent instructions, examples, and protocol identity must change coherently at the authority switch.

### 3.11 Evidence-based rollout duration

Battle-hardening has no automatic duration. Marco decides production cutover from evidence rather than elapsed time.

### 3.12 Destructive PostgreSQL restore

PostgreSQL restore or point-in-time recovery is an offline, exclusive operator action. It establishes a new authority generation. Requests and agent runs from an earlier generation are rejected, and any needed work is deliberately reissued.

### 3.13 Operator-managed backup and restore

At cutover, `backup-create` and connected `backup-restore` retire from the Dish command API. PostgreSQL backup and restore become operator-managed procedures. Historical request, backup, and restore evidence remains preserved.

### 3.14 Bounded mutation coverage

Stage A preserves current governed actions and adds only specifically identified actions accepted before cutover. Repeated direct Asana habits do not automatically become new Dish requirements.

### 3.15 Historical exceptions

Problematic historical source items are not silently discarded. Marco approves their reconciliation or isolation from exact presented evidence. Stage A does not introduce a general governed `historical_read_only` lifecycle.

### 3.16 Planning-intent settlement

Marco may permanently settle an issued or claimed-but-unconsumed Planning-intent challenge through a reason-bearing, audited action. Settlement is non-reusable, proves that no Planning operation was created from the challenge, and requires a new confirmation exchange for any later Planning start.

## 4. Current authority that Stage A must preserve

The migration changes storage and authority location, not the meaning of current governed facts unless this document explicitly says otherwise.

| Authority concern | Required Stage A preservation |
|---|---|
| Live mutation ownership | One Dish service remains the only supported governed mutation authority. Clients never receive writable database access or the Asana authority credential. |
| Workflow legality | One authoritative current snapshot determines legal actions. Transports, individual commands, and compatibility paths do not independently invent legality. |
| Exact live state | Governed mutations bind to exact task content, logical project membership, logical section placement, and relevant state evidence, not assumed or stale state. |
| External effects | Intent is durable before an external call. Every attempt settles as `confirmed`, `not_applied`, or `uncertain`; uncertain effects are reconciled rather than blindly retried. |
| Request replay | One immutable request identity binds owner, run, command, canonical arguments, and authoritative outcome. Reuse with conflicting identity fails closed. |
| Committed success | A later view, cleanup, transport-audit, or projection failure cannot turn committed success into retry advice. |
| Planning intent | The two-request Planning gate remains durable, exact, owner/run/task/agent/target-bound, single-use, and admission-only on the first request. |
| Marco authorization | Governed-change authorizations remain exact, reservable, releasable, single-use capabilities rather than generic audit annotations. |
| Verification | Verification binds exact content/version occurrence, cycle, actor, run lineage, inspection evidence, correction lineage, and signoff evidence. |
| Completion | Completion remains separate from workflow phase and gates Planning. Completion may become true only through a governed Cooked or Archive transition; Stage A adds no generic completion-setting command. Imported completion remains authoritative, and `reopen-planning` clears it through a narrow audited route. |
| Leases | Actor leases remain distinct from workflow ownership, executor claims, task mutation fences, and run revocation. |
| Execution | Request execution claims and unresolved execution evidence prevent duplicate work and support exact takeover or recovery. |
| Recovery | Recovery remains route-specific. Lease recovery, ambiguous-effect reconciliation, destination repair, discard, evidence handling, Human Review, Planning reopen, and abandonment are not collapsed into a generic unblock. |
| Abandonment and succession | An abandoned attempt remains historical evidence. A continuation uses a fresh successor operation where current semantics require one. |
| Audit and repair | Governed audit and success-preserving invocation-audit repair remain durable, append-only, and crash-safe. |
| Restore control | Legacy restore journals, ownership evidence, fault markers, and recovery checkpoints remain part of the authority bundle until retired at cutover. |
| Historical compatibility | Historical records may be read, migrated, reconciled, or quarantined, but they do not activate a second mutation engine. |
| Schema history | Applied database migration provenance remains immutable and attributable to a release and authority generation. |

The implementation design must map every current durable relation and external sidecar to exactly one target disposition. Generic audit rows do not replace named domain authority when current behavior depends on that domain relation.

## 5. Target authority model

### 5.1 PostgreSQL is the authoritative transaction boundary

After cutover, authoritative task, workflow, request, execution, audit, and projection-intent changes commit through PostgreSQL transactions.

A normal governed mutation must not rely on an Asana write to become authoritative. It may emit a downstream projection event in the same transaction.

### 5.2 Task identity and aliases

A task is identified by a stable Dish UUID. External identifiers are recorded as aliases with origin and provenance.

Imported Asana GIDs remain aliases to the imported Dish task. A post-cutover Asana projection mapping is downstream evidence and cannot create or transfer task authority.

### 5.3 Logical placement and section registry

After cutover, PostgreSQL owns logical membership in the governed project set, logical section placement, and the authoritative section registry used by workflow legality. Asana project and section GIDs are aliases or projection mappings. Downstream Asana observations never change legal actions or authoritative placement. Governed transitions that currently couple workflow and placement advance those authorities together.

### 5.4 Honest protocol and canonical schema authority

Stage A does not transfer authority for the Honest protocol or canonical task schema into PostgreSQL. Canonical interpretation continues to come from the governing Honest release source. PostgreSQL records immutable release identity, hashes, provenance, and operation or Verification-cycle bindings. Any later transfer of that authority requires a separate explicit decision.

### 5.5 Canonical document and immutable versions

Stage A stores a canonical title/body document. Every authoritative content occurrence is immutable.

A task's current content pointer advances through append-only activation evidence. Initial import has migration provenance rather than a fabricated user request. After initial creation or import, canonical pointer changes require governed command authority.

Content identity must remain reproducible across migration. Existing identity schemes remain named and versioned rather than being silently reinterpreted.

### 5.6 Workflow and control state

Workflow operations, steps, actor facts, Verification cycles, inspection facts, authorizations, completion, logical placement, holds, abandonment, succession, and recovery evidence remain separate domain authorities. Completion is imported or derived only from a governed Cooked or Archive transition; Stage A defines no standalone positive-completion mutation.

A generic event stream or audit log may provide causality and observability, but it does not replace these domain facts.

### 5.7 Requests, runs, executions, and fences

Request identity is immutable within an authority generation. A request stores its canonical initial outcome separately from any later uncertainty resolution or fresh current view.

A command execution records admission and execution ownership. Executor claims are distinct from task or operation mutation fences. Fences prevent concurrent or stale execution from committing authoritative domain changes.

Runs and requests are generation-bound. After destructive restore, stale processes cannot regain mutation authority merely by selecting a new run UUID or echoing the current generation. Reissue must be deliberately authorized by the post-restore control boundary.

### 5.8 Planning intent

Planning challenge issuance is a replay-bound admission mutation that does not read or mutate task state, create an operation, or acquire an actor lease.

A fresh request must claim the exact challenge before ordinary Planning admission. Successful Planning start consumes the challenge atomically with the resulting operation and request outcome.

Marco-only terminal settlement is append-only and non-reusable.

### 5.9 Verification

Verification reviews and signs exact immutable content occurrences. Reviewed, corrected, approved-candidate, and signed occurrences remain explicit where current behavior requires distinct lineage.

Inspection creates durable decision evidence even though it does not advance canonical content. The target service contract must give that evidence an exact replay or equivalent idempotent admission boundary.

### 5.10 External effects

Before cutover, Asana effects remain governed external effects under current authority. During shadowing, exact effect intent and observations feed both the live adjudication and PostgreSQL shadow evidence.

After cutover, Asana projection effects use the same intent-before-call and evidence-after-call discipline. A projection effect never becomes task authority.

### 5.11 Projection to Asana

PostgreSQL emits ordered downstream projection intent transactionally with authoritative changes.

The projector:

- applies only committed PostgreSQL state;
- never imports direct Asana changes as authority;
- uses per-task ordering and exact mapping evidence;
- records attempt and adjudication history;
- treats ambiguous effects as unresolved until evidence supports settlement;
- preserves continuous corpus closure across the reused projects.

Every visible in-scope Asana task must be classified as one of:

- mapped imported or projected task;
- in-flight projection creation with exact correlation evidence;
- explicit non-authoritative isolation;
- blocking unknown object.

A blocking unknown object makes projection readiness unhealthy until isolated or resolved. It is not automatically deleted or promoted.

PostgreSQL-native task creation remains disabled during shadowing and rehearsal until the deployed Asana contract proves lost-response-safe creation correlation. Production cutover cannot leave the current governed `create` semantic unavailable unless Marco explicitly approves its retirement. If the proof fails, cutover remains blocked until Marco approves a bounded topology that preserves `create` or explicitly retires it.

### 5.12 Audit, migration provenance, and repair

Governed domain facts and their authoritative audit commit together where current semantics require atomicity.

Invocation or transport audit may remain outside that transaction only if committed success remains success and missing audit intent is durably repairable even while PostgreSQL is unavailable.

Alembic may execute schema migrations, but applied migration history remains immutable, release-bound, and authority-generation-bound.

### 5.13 Backup and restore

PostgreSQL backups, WAL retention, restore, and PITR are operator responsibilities. They are not ordinary Dish task commands.

Restore control must survive the database timeline being replaced. Mutation admission remains closed until the restored database, new generation, schema, application release, and post-restore run-registration authority are validated.

## 6. Migration direction

### 6.1 Phase 1: complete baseline

Before command shadowing, build a complete, gap-free PostgreSQL baseline from:

- the exact current Asana corpus in the re-baselined in-scope project set;
- a transactionally complete SQLite snapshot;
- all durable legacy sidecars and restore-control evidence;
- exact release, schema, protocol, source-document, and identity provenance;
- a closed delta from the baseline high-water mark to shadow start.

The baseline is non-authoritative while Asana/SQLite remains authoritative.

### 6.2 Phase 2: Asana-authoritative shadow

Each eligible governed command continues to execute under current authority.

Before the live external effect, the current authority durably registers either:

- a complete immutable shadow envelope containing the exact pre-command snapshot, canonical intent, pinned nondeterministic inputs, request/execution identity, and external-effect intent; or
- an explicit permanent command-level proof gap.

PostgreSQL availability does not change the live Asana result. Exact envelopes may be delivered and adjudicated asynchronously. A post-state reread may repair current-state mirroring but cannot manufacture missing command-parity evidence.

Shared decision logic is not accepted as its own behavioral oracle. Preservation evidence must be independent of the shared implementation logic; adapter agreement alone is insufficient.

A destructive legacy SQLite restore establishes a new legacy authority generation, rejects or invalidates prior-generation requests and runs, disqualifies prior-generation parity evidence, and requires a fresh complete baseline before shadowing resumes.

### 6.3 Phase 3: production cutover

Production cutover requires:

- Marco's explicit evidence-based authorization;
- no unresolved operations, leases, requests, execution claims, external effects, authorization reservations, Planning challenges, backup reservations, restore activity, abandonment transitions, or other open authority accepted by the cutover policy;
- an exact final Asana authority snapshot covering content, completion, governed project membership, section placement, and the complete in-scope object set, with gap-free closure through durable activation;
- a complete transactionally consistent legacy authority bundle;
- a validated PostgreSQL import with exact provenance;
- a proven downstream projection path;
- a hard mechanical fence preventing any old Asana-authoritative writer after activation;
- a durable activation decision binding the approved import, authority generation, schema, application/protocol release set, and mutation-admission state;
- rollback burned before the first PostgreSQL mutation request is admitted.

Open authority may be represented non-authoritatively during shadowing. It is not imported as unresolved live authority at production cutover.

## 7. Non-negotiable safety invariants

### 7.1 Complete baseline

Command parity begins only after a complete baseline and gap-free delta closure.

### 7.2 Honest shadow evidence

Every shadowed command has an exact pre-effect envelope or an explicit permanent proof gap. Current-state reconciliation never upgrades a gap into command parity.

### 7.3 Legacy restore generation

Destructive restore of the pre-cutover SQLite authority changes the legacy authority generation and invalidates evidence from the replaced timeline.

### 7.4 Hard authority activation

Authority activation is a durable decision, not only a routing change. Old writers are mechanically fenced before PostgreSQL mutation admission opens.

### 7.5 Complete cutover bundle

The cutover evidence binds an exact final Asana authority snapshot and gap-free closure through activation together with WAL-complete SQLite state, sidecars, restore evidence, ownership evidence, audit-repair evidence, and exact migration provenance. Any relevant Asana change after the accepted snapshot invalidates approval and requires recapture and revalidation.

### 7.6 Import activation provenance

Initial imported current versions are activated by exact import and cutover provenance, not fabricated command executions.

### 7.7 Continuous Asana closure

The reused Asana projects remain continuously classified as mapped, in-flight-correlated, isolated, or blocking unknown.

### 7.8 Projector-create feasibility

Lost-response-safe Asana creation is proven before PostgreSQL-native creation depends on it. Production cutover preserves the current governed `create` semantic unless Marco explicitly approves its retirement.

### 7.9 Post-restore deliberate reissue

A stale process cannot self-authorize continuation after PITR. New work after restore must pass a post-restore authority boundary that proves deliberate reissue.

### 7.10 Crash-safe audit repair

Invocation-audit repair remains durable even when PostgreSQL is unavailable.

### 7.11 Immutable migration provenance

Current schema head is not the only migration evidence. Applied history remains immutable and attributable.

### 7.12 Independent behavioral oracle

Behavior-preservation evidence is independent of shared target implementation logic. Adapter parity does not substitute for preservation evidence.

### 7.13 Resolved production import

Shadow may model open state; production cutover imports only resolved authority.

## 8. Interface and protocol boundary

The target command surface may break compatibility, but its semantics remain bounded by this architecture and the current authority coverage.

Before cutover, every current agent and admin command must have an explicit approved treatment consistent with preserved authority and the compatibility policy. The implementation companion owns the concrete semantic-delta artifact and its fields. This coverage is not permission to add speculative commands. New product behavior requires explicit acceptance before becoming a cutover dependency.

The public task identifier changes to Dish UUID only when the live authority path can route that identifier correctly. A shadow-only UUID must not become pre-cutover mutation authority.

## 9. Deployment and availability

PostgreSQL starts as a self-managed local service with explicit persistent storage, backup, health, and upgrade ownership.

The service fails closed for governed mutations when authoritative PostgreSQL state is unavailable or invalid. Administrative diagnosis and recovery paths may remain available under an unhealthy readiness state.

Asana availability after cutover affects downstream freshness, not PostgreSQL task authority. Projection lag and unresolved projection effects are visible and auditable.

The architecture remains portable to a self-managed AWS host without changing authority semantics.

## 10. Deferred decisions and later gates

The following are intentionally deferred:

- production cutover authorization;
- measured RPO and RTO acceptance;
- exact Asana projector-create fallback if deployed correlation proof fails;
- Stage B representation, equivalence, and structured command design;
- Cooked, Archive, and Cooking History product semantics;
- any general historical-item promotion lifecycle;
- high availability or managed PostgreSQL;
- broader private browsing or search interfaces.

Deferred items do not become Stage A implementation or proof gates unless explicitly re-authorized.

## 11. Handoff boundary

`database-backend-imp.md` owns:

- conceptual and physical storage mapping;
- SQLAlchemy/Alembic conventions;
- transaction boundaries and repository design;
- run, request, execution, fence, audit-repair, and projection mechanisms;
- command semantic-delta matrix;
- implementation sequence;
- implementation acceptance and fault tests.

`database-backend-migration.md` owns:

- legacy authority capture;
- baseline and delta closure;
- shadow rollout and parity accounting;
- rehearsal and production cutover procedure;
- authority activation and writer fencing;
- rollback boundary;
- backup, restore, and post-restore operational procedure;
- migration-specific evidence and go/no-go gates.

Neither companion document may silently change an approved decision, weaken a current authority obligation, add a Stage A product feature, or redefine an invariant in this document.

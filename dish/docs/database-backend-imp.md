# Database backend implementation

Status: Stage A implementation-design draft

Role: this document translates `database-backend.md` into an implementable PostgreSQL design and defines implementation acceptance. It may choose mechanisms, table shapes, libraries, and worker structure, but it may not change architecture decisions, authority semantics, or Stage A scope.

Migration and operational cutover procedures belong in `database-backend-migration.md`.

## 1. Governing inputs

Implementation must reconcile against:

1. `database-backend.md`;
2. current `dish/docs/architecture.md`;
3. current `dish/docs/runtime-contract.md`;
4. the current database schema and service code at implementation re-baseline;
5. the frozen current-behavior characterization corpus;
6. the migration evidence and cutover rules in `database-backend-migration.md`.

When these sources differ:

- the approved Stage A decisions in `database-backend.md` control target behavior;
- otherwise current governing behavior must be preserved;
- implementation convenience does not resolve a semantic conflict;
- unresolved product semantics return to Marco rather than being invented by the implementer.

## 2. Implementation defaults

The default Stage A stack is:

- PostgreSQL;
- SQLAlchemy 2.x declarative mappings and explicit sessions;
- Alembic for ordered schema changes;
- psycopg 3 as the PostgreSQL driver;
- Pydantic models at service boundaries;
- Docker Compose for the initial local deployment;
- repository and application-service boundaries rather than command-local SQL;
- explicit transaction ownership rather than implicit autocommit.

Exact package versions are pinned in the implementation change set and tested together.

These defaults may change only when the replacement preserves the same authority and failure semantics.

## 3. Implementation principles

### 3.1 One service authority

Only `dish-service` receives mutation authority. Agent and admin clients call the service. They do not open PostgreSQL sessions or receive the Asana projection credential.

### 3.2 Domain authority is explicit

Named domain facts remain named. Do not replace Planning challenges, Marco authorizations, Verification facts, leases, execution claims, abandonment, or repair evidence with only a generic audit or event table.

### 3.3 Immutable evidence, mutable projections

Immutable occurrences and append-only events establish history. Mutable current pointers and read projections may be rebuilt from authoritative evidence where specified.

### 3.4 Transactions own authoritative state

Repositories do not commit independently. Application services define authoritative transactions and pass one session through the participating repositories.

### 3.5 External effects are never inferred from SDK success

Every effect has persisted intent, attempt ownership, evidence-based adjudication, and exact retry rules.

### 3.6 No hidden compatibility engine

Legacy evidence may be imported or quarantined. Old mutation implementations do not remain active behind a compatibility flag.

## 4. Command semantic-delta matrix

Before implementation changes public behavior, maintain one version-controlled matrix containing every current route and its Stage A disposition.

The initial matrix is:

| Current command | Current class | Stage A default disposition |
|---|---|---|
| `create` | replay-bound agent mutation | Retain semantics; public identity becomes Dish UUID at authority cutover. Pre-cutover routing remains Asana-GID-compatible until live authority can route Dish UUID safely. |
| `sections` | read-only Action query | Retain or replace coherently with the target project/section read contract. |
| `read` | read-only Action query | Retain semantics against the authoritative backend; after cutover read PostgreSQL plus projection freshness, not Asana task authority. |
| `inspect` | currently requestless read route that creates durable evidence | Reclassify as replay-bound evidence mutation, or implement an explicitly equivalent durable idempotency contract. It remains non-content-changing. |
| `start` | replay-bound agent mutation | Retain all current operation kinds and Planning confirmation semantics. |
| `prepare` | replay-bound agent mutation | Retain current workflow and content semantics. |
| `approve` | replay-bound agent mutation | Retain exact Verification occurrence and lineage semantics. |
| `reject` | replay-bound agent mutation | Retain current Large, Evidence, and Human Review semantics. |
| `submit` | replay-bound agent mutation | Retain current destination, movement, completion, and recovery semantics. |
| `renew-lease` | replay-bound lease mutation | Retain lease semantics; bind to target run authority. |
| `recover` | Marco-only admin mutation | Retain narrow ambiguous-effect reconciliation. |
| `repair-destination` | Marco-only admin mutation | Retain narrow destination repair. |
| `discard` | Marco-only admin mutation | Retain only for provably unapplied operations. |
| `abandon-operation` | Marco-only admin mutation | Retain current permanent-attempt abandonment semantics. |
| `reconcile-abandonment` | Marco-only admin mutation | Retain exact reconciliation semantics. |
| `reopen-planning` | Marco-only admin mutation | Retain narrow completion clearing and audit. |
| `reopen` | Marco-only admin mutation | Retain two-pass Human Review semantics. |
| `supply-evidence` | Marco-only admin mutation | Retain protocol semantics. |
| `record-human-decision` | Marco-only admin mutation | Retain protocol semantics. |
| `authorize-governed-change` | Marco-only admin mutation | Retain exact authorization grant semantics. |
| `recover-lease` | Marco-only admin mutation | Retain narrow expired-lease recovery. |
| `expire-lease` | Marco-only admin mutation | Retain exact lease release semantics; do not treat as run revocation. |
| `migrate` | Marco-only legacy compatibility mutation | Conditional on the re-baselined corpus. Retain only for an identified live compatibility need; otherwise replace with migration-time reconciliation or isolation. |
| `backup-create` | replay-bound admin mutation | Retire at authority cutover. Preserve historical requests, records, and artifacts. |
| `backup-restore` | replay-bound admin mutation | Retire at authority cutover. Preserve historical journal and outcome evidence. |
| Planning-intent settlement | new Marco-only admin mutation | Add one reason-bearing, terminal, non-reusable settlement route. Exact route name is an implementation choice. |

For every changed command, the matrix must also specify:

- request schema and canonicalization;
- authenticated principal and scope;
- authority generation binding;
- legal preconditions;
- transaction and effect boundary;
- replay outcome;
- current-view behavior;
- protocol/OpenAPI release introduction;
- migration handling for historical requests.

## 5. Current-to-target authority coverage

The implementation must maintain a row-by-row coverage matrix at re-baseline. The following is the minimum current inventory.

| Current authority | Target responsibility |
|---|---|
| `submissions` | Imported historical submission/request compatibility evidence or explicit retirement witness. Do not use as a second live task engine. |
| `audit_events` | Append-only governed and operational audit events. |
| `task_content_state` | Imported current content/placement head provenance; target current task/version/location pointers. |
| `operations` | Workflow operation authority. |
| `content_versions` | Immutable document/version occurrences with preserved identity scheme. |
| `verification_cycles` | Verification cycle, reviewed occurrence, decision, correction, and signoff bindings. |
| `write_attempts` | Historical Asana write-effect evidence; target authoritative or projection effect attempts as appropriate. |
| `movement_attempts` | Historical Asana placement-effect evidence; target projection effect attempts as appropriate. |
| `legacy_submission_quarantine` | Preserved isolated historical evidence. No automatic governed lifecycle. |
| `operation_steps` | Immutable or monotonic operation-step facts. |
| `operation_actor_facts` | Operation-scoped actor/run lineage. |
| `marco_authorizations` | Authorization grants, reservations/releases, and single-use consumption. |
| `command_audit_repairs` | Durable pending/repaired/quarantined invocation-audit repair authority. |
| `two_pass_resets` | Two-pass Human Review reopen evidence. |
| `service_leases` | Actor lease authority. |
| `service_requests` | Generation-bound immutable request identity and canonical outcome. |
| `operation_execution_claims` | Executor-claim authority, distinct from mutation fences. |
| `operation_executions` | Command/execution baseline, attempt, and terminal evidence. |
| `dish_inspect_facts` | Durable Verification inspection evidence. |
| `planning_reopen_attempts` | Completion-clear attempt and result evidence. |
| `backup_creations` | Imported immutable historical backup evidence; no post-cutover command authority. |
| `abandonment_attempts` | Permanent-attempt abandonment authority. |
| `operation_successions` | Fresh-successor lineage. |
| `schema_migrations` | Imported immutable migration provenance. |
| Planning-intent challenge storage | Exact issued, claimed, consumed, or settled challenge authority. |
| Restore request journal | Imported historical restore identity, checkpoints, and outcomes; post-cutover operator restore uses external control evidence. |
| Restore-fault marker | Cutover closure and historical recovery evidence. |
| Service database ownership marker | Legacy canonical database identity and cutover closure evidence. |
| Audit-repair JSONL and `.importing` file | Lock-coordinated import, repair, or quarantine with exact provenance. |
| SQLite database plus WAL state | Transactionally complete legacy database snapshot. |
| Managed backup artifacts | Historical immutable backup evidence and cutover closure. |

The final matrix must include any authority added to the current repository before implementation re-baseline.

## 6. Target domain model

The names below are conceptual. Physical table names may differ, but the authority separation may not.

### 6.1 Database authority generations

Maintain one current PostgreSQL authority generation.

A generation records or references:

- stable generation identity;
- creation reason: initial cutover or destructive restore;
- predecessor where applicable;
- external restore-control identity where applicable;
- schema/release compatibility;
- active or retired status.

Initial cutover activation and destructive restore are different provenance routes even if they use one generation relation.

### 6.2 Authority activation

Maintain append-only activation evidence binding:

- cutover approval;
- exact import run and legacy bundle;
- initial database generation;
- schema head;
- Dish, Honest, protocol, OpenAPI, and routing release set;
- projection epoch;
- activation or aborted outcome;
- rollback-burn point.

Only the active activation may admit PostgreSQL mutations.

The physical protocol may combine external fencing evidence and PostgreSQL evidence, but process death at any step must leave a deterministic outcome.

### 6.3 Tasks

A task contains:

- Dish UUID;
- lifecycle existence state;
- current content-version pointer;
- task revision used for authoritative content/location projection;
- current governed location and completion references where appropriate;
- creation/import provenance;
- current projection/read metadata only when clearly non-authoritative.

Do not place operation, lease, execution, Verification, or future Cooked/Archive state into one task status column.

### 6.4 External aliases

Aliases bind an external system and external identifier to exactly one Dish task.

Record:

- alias origin: imported authority evidence or downstream projection;
- exact creation/import provenance;
- active/retired mapping state where needed;
- non-transferability across tasks.

An Asana GID cannot become authority for task identity.

### 6.5 Content versions and activations

A content version is immutable and contains:

- task identity;
- representation kind (`document` for Stage A);
- complete title/body content;
- exact content identity under a named identity scheme;
- creator provenance;
- predecessor or lineage references where semantically meaningful;
- creation time and release/protocol context.

Activation is append-only and uses exactly one provenance route:

- command execution; or
- initial creation/import authority.

A command may create multiple complete lineage occurrences where Verification requires reviewed, corrected, approved-candidate, and signed identities, but only the governed current activation advances the task pointer.

### 6.6 Task locations and completion

Location history remains separate from content history. Current location references exact governed project/section identity and provenance.

Completion remains an independent Planning-eligibility axis. `reopen-planning` records the attempted before/after state and audit before clearing completion.

No Cooked or Archive state is implemented in Stage A unless separately authorized before re-baseline.

### 6.7 Operations, steps, and actors

Operations preserve:

- kind and lifecycle;
- task binding;
- creation request/execution;
- exact protocol/release context;
- predecessor/successor and abandonment lineage;
- terminal outcome.

Operation steps and actor facts remain append-only or monotonic. Actor facts bind exact operation participation, agent, owner, and run authority.

### 6.8 Verification

Verification storage must represent:

- cycle identity and operation/task binding;
- exact reviewed version occurrence;
- verifier actor/run and independence evidence;
- inspection fact and attestation;
- rejection category/reason where applicable;
- corrected candidate lineage;
- signed occurrence and signoff evidence;
- two-pass reset or Human Review evidence;
- inherited signoff for permitted non-material check-ins.

Do not infer signoff from rendered text or current content hash alone.

### 6.9 Planning-intent challenges

Challenge authority includes:

- challenge identity;
- issuing request and exact principal/run/task/agent/target binding;
- intent basis and any authorized override reason;
- issued, claimed, consumed, or settled lifecycle;
- claiming request;
- resulting operation for consumed challenges;
- Marco actor and reason for settled challenges.

Transitions are monotonic and single-use. Issuance has no task snapshot, operation, lease, or command execution.

### 6.10 Marco authorizations

Model authorizations as three related authority classes:

1. immutable grant;
2. append-only reservation and release history;
3. single-use consumption bound to the committed version occurrence or governed result.

A grant binds exact task, optional operation, field, before value, after value, reason, actor, and run provenance.

Reservation prevents ambiguous or concurrent use. Release does not erase the grant. Consumption is final and cannot be duplicated.

### 6.11 Requests and outcomes

A service request binds:

- authority generation;
- request UUID;
- authenticated owner;
- registered run authority;
- command identity;
- canonical payload digest and retained canonical payload evidence;
- protocol/release identity;
- immutable initial outcome;
- optional append-only uncertainty-resolution evidence.

Fresh current actions and views are computed separately and do not rewrite the stored original outcome.

Imported historical terminal requests may lack a current run capability only as immutable historical evidence. They may never execute again.

### 6.12 Runs and post-restore authority

A run is service-registered and generation-bound.

The implementation must prevent a pre-restore process from becoming current merely by:

- observing the new generation;
- selecting a new UUID;
- retaining an old bearer credential;
- replaying a local request buffer.

Use a generation-specific bootstrap authority established outside the restored timeline. Exact token, key, capability, or registration design is implementation-specific, but ordinary reconnect must remain automatic while destructive-restore re-entry requires deliberate operator-controlled reauthorization.

### 6.13 Command executions and claims

A command execution represents admitted work and its deterministic execution context.

Store or reference:

- generation, request, task, and optional operation;
- command identity;
- canonical intent;
- pinned nondeterministic inputs;
- executor claim state;
- terminal command result or uncertainty state;
- causality links to committed facts.

Executor claims allow safe takeover and are not the task/operation mutation fence.

### 6.14 Mutation fences

Use explicit task and operation revision/fence predicates so stale or concurrent executors cannot commit over newer authoritative state.

A fence binds the execution to the exact snapshot or revision it planned against. The transaction verifies the fence immediately before authoritative mutation.

The implementation may use row versions, predicate checks, locks, or a combination, but must prove stale-executor rejection.

### 6.15 Leases

Leases remain principal/run/task/operation-scoped actor authority.

They must preserve:

- exact owner and actor;
- issuance and expiry;
- renewal;
- narrow recovery/release;
- distinction from execution claims and run revocation;
- inability of admin recovery to silently assume workflow ownership.

### 6.16 Abandonment and succession

An abandonment attempt records exact target operation/lease/run evidence, reason, checkpoints, and terminal result.

Where current semantics require continuation, create a fresh successor operation and immutable succession relation. Do not reopen or rewrite the abandoned operation as the new attempt.

### 6.17 Audit and repair

Governed audit facts commit with their governed domain transaction.

Invocation/transport audit outside that transaction uses:

- immutable repair identity;
- exact request/result/audit payload;
- durable append before success response when PostgreSQL cannot store repair intent;
- claim/import semantics safe under concurrent writer and importer;
- deduplication;
- repaired or quarantined terminal outcome;
- backup and restore-generation reconciliation.

A local durable journal is the default continuation of the current contract.

### 6.18 Schema migration provenance

Alembic controls executable ordering and current-head projection.

Maintain immutable applied-migration events containing at least:

- revision and predecessor;
- migration code identity or checksum;
- Dish release;
- authority generation;
- initiator/deployment authority;
- start and terminal outcome;
- applied time;
- explicit event for repair, reversal, or stamp.

Imported SQLite `schema_migrations` rows remain historical provenance rather than being rewritten as Alembic events that never happened.

## 7. Deterministic decision boundary

### 7.1 Characterization before refactoring

Before shared-kernel extraction, freeze a current-behavior corpus for every retained route and material recovery state.

Each case records:

- canonical principal/request/arguments;
- exact current Asana task and SQLite authority snapshot;
- allowed actions and current view;
- planned external intent;
- observations and adjudication;
- canonical result;
- normalized durable facts and terminal classification.

The corpus is read-only evidence after capture.

### 7.2 Shared planner

Implement a deterministic planner that accepts:

- one authoritative snapshot;
- canonical command intent;
- pinned contract inputs such as time, generated UUIDs, release identities, and resolved destinations.

It returns a command plan containing:

- legality and expected result class;
- authoritative domain mutations;
- external-effect intents;
- expected fences;
- projection intents;
- audit/causality requirements.

### 7.3 Shared adjudicator

The adjudicator accepts the plan and exact effect observations and returns:

- confirmed/not-applied/uncertain effect settlement;
- authoritative transaction input;
- exact result and recovery guidance;
- projection or audit follow-up.

Do not maintain separate live and shadow reducers with independently drifting semantics.

### 7.4 Two independent proofs

Implementation acceptance requires:

1. the shared planner/adjudicator matches the frozen current-behavior corpus; and
2. the PostgreSQL adapter matches the live path for the same exact shadow envelope.

Agreement between two consumers of the same defective planner is not behavioral proof.

## 8. Transaction contracts

### 8.1 Pure read

A pure read opens a read transaction or consistent session view and writes no authority.

### 8.2 Evidence mutation

An evidence mutation such as target `inspect`:

- admits or deduplicates its request/evidence identity;
- verifies exact snapshot and actor/run binding;
- appends decision evidence and governed audit atomically;
- does not create a new content version or advance canonical content.

### 8.3 Local authoritative command

A command with no external authoritative effect commits in one PostgreSQL transaction:

- request admission/replay decision;
- execution claim/fence validation;
- operation and workflow facts;
- immutable content/version occurrences where applicable;
- activation/current pointer;
- request outcome;
- governed audit;
- projection outbox events;
- causality links.

### 8.4 Post-cutover projection effect

The authoritative command commits before projector execution.

The projector uses separate transactions for:

1. claim and durable pre-call intent;
2. external call;
3. reread/adjudication and settlement;
4. mapping or freshness updates.

An ambiguous effect remains unresolved and blocks unsafe retry for that effect identity.

### 8.5 Pre-cutover shadow delivery

The current SQLite authority durably registers a rollout sequence before the live effect and records either:

- the complete exact shadow envelope; or
- an explicit proof gap.

PostgreSQL delivery and adjudication are asynchronous. Delivery failure never changes the Asana command result.

### 8.6 Import initialization

One import transaction or bounded batch transaction creates:

- task and alias;
- imported immutable version;
- non-command initial activation;
- initial task revision/location/completion state;
- workflow historical facts;
- exact import provenance.

It does not fabricate service requests or command executions.

## 9. Asana projection implementation

### 9.1 Credentials and isolation

Use a dedicated projector credential after cutover. The authoritative mutation service path must not use the Asana authority credential for task state.

### 9.2 Outbox

Projection events are emitted in the authoritative command transaction.

Each event identifies:

- task;
- event identity and type;
- task projection sequence;
- exact task revision/version/location to project;
- projection epoch;
- source execution or maintenance authority;
- supersession/coalescing eligibility.

Creation events are not supersedable until mapping is settled.

### 9.3 Mapping

A mapping binds one Dish task to one Asana GID and records imported or projector-created origin.

Mapping creation is exactly-once. Conflicting GIDs or duplicate correlations block automatic progress.

### 9.4 Lost-response-safe create

Before enabling PostgreSQL-native `create`, prove that the deployed Asana API supports a non-canonical marker that is:

- supplied atomically with create;
- unique for the Dish task/event/epoch;
- discoverable after response loss;
- queryable as zero, one, or multiple matches;
- durable across worker takeover;
- not part of canonical title/body authority.

Settlement rules:

- one exact match: bind once and continue;
- multiple matches: unresolved, block automatic action;
- zero matches: do not retry until the effect can be proven not applied under the supported API/indexing contract.

If feasibility fails, keep PostgreSQL-native create disabled and return bounded topology alternatives to Marco.

### 9.5 Corpus reconciler

Periodically enumerate every in-scope Asana project and classify each task GID as:

- mapped;
- exact in-flight create correlation;
- isolated non-authoritative object;
- blocking unknown.

A blocking unknown makes projection readiness unhealthy. Isolation must be conspicuous and must not manufacture Dish authority.

### 9.6 Drift

For mapped tasks, compare Asana observation with exact committed PostgreSQL projection state.

Direct edits are logged and overwritten or reprojected according to policy. They are never imported as commands or canonical versions.

## 10. Service and repository structure

A recommended package split is:

- domain snapshot and policy;
- command planner/adjudicator;
- task/version repositories;
- workflow/Verification repositories;
- request/run/execution repositories;
- lease and abandonment repositories;
- audit and repair repositories;
- projection outbox and worker repositories;
- migration/import repositories;
- service application layer;
- HTTP/admin transports.

Repositories expose domain operations, not unrestricted session access to transports.

Use dependency injection for sessions, clocks, UUID generation, release resolution, and external adapters so deterministic tests can pin all inputs.

## 11. Concurrency and database behavior

Implementation must define and test:

- transaction isolation for each command class;
- deterministic row-lock ordering;
- serialization/deadlock retry rules;
- task and operation fence predicates;
- request uniqueness within generation;
- run and capability uniqueness;
- single active lease constraints;
- single unresolved effect/claim constraints where required;
- append-only and monotonic constraints;
- safe worker claim and takeover;
- outbox ordering and idempotency;
- restore-generation invalidation.

Database constraints enforce authority invariants even when application code is defective.

## 12. Read model and current view

The service exposes a fresh current view derived from authoritative PostgreSQL state.

A current-view token should bind the axes that can change action legality, including:

- task revision/content pointer;
- location and completion;
- operation and Verification state;
- active lease;
- execution uncertainty/fence state;
- holds and recovery requirements;
- authority generation;
- relevant protocol/release identity.

The physical token format is implementation-specific. It is not the immutable request outcome.

Projection freshness is reported separately and never changes legal PostgreSQL actions merely because Asana is stale.

## 13. Implementation sequence

1. Freeze current behavior characterization and authority inventory.
2. Establish PostgreSQL project skeleton, SQLAlchemy session ownership, Alembic, and test database isolation.
3. Implement authority generation, activation, migration provenance, and service readiness foundations.
4. Implement task identity, aliases, immutable document versions, activations, locations, and completion.
5. Implement request/run/execution/claim/fence/lease authority.
6. Implement operations, steps, actor facts, Verification, inspect evidence, authorizations, Planning challenges, abandonment, succession, and recovery.
7. Implement audit, external repair journal, and exact causality.
8. Extract the shared planner/adjudicator while preserving the characterization corpus.
9. Implement import provenance and shadow-envelope storage/delivery.
10. Implement projection outbox, mappings, attempts, adjudication, and corpus reconciler.
11. Prove Asana creation correlation or leave PostgreSQL-native create disabled.
12. Implement coherent target service/OpenAPI/Action protocol and semantic-delta matrix.
13. Complete migration tooling and rehearsal support under `database-backend-migration.md`.
14. Run implementation acceptance before any production cutover decision.

Implementation may be delivered incrementally, but no partial slice becomes production authority without the full cutover gates.

## 14. Implementation acceptance

### 14.1 Authority coverage

- Every current table and sidecar at re-baseline has one documented target disposition.
- No named current authority is replaced only by generic audit.
- Deferred product features are absent or feature-gated and non-gating.
- Historical quarantine remains preserved and isolated.

### 14.2 Schema and migration

- Fresh database migration from empty to head succeeds.
- Upgrade through every Alembic revision succeeds.
- Applied migration provenance is immutable and complete.
- Database constraints reject duplicate aliases, illegal pointer activation, duplicate consumptions, conflicting mappings, stale fences, and illegal monotonic transitions.
- Restore generation changes invalidate earlier run/request authority.

### 14.3 Current-behavior preservation

- Every retained command passes its frozen characterization cases.
- Legal actions and recovery guidance match current governing behavior unless the semantic-delta matrix records an approved change.
- Verification exact occurrence and run-lineage cases pass.
- Planning first-call admission performs no task read, operation creation, or actor lease.
- Completion/reopen, authorization, abandonment, and successor cases pass.

### 14.4 Requests and concurrency

- Exact request replay returns the stored outcome.
- Identity conflict fails closed.
- Concurrent duplicate delivery performs one logical execution.
- Stale executor, task fence, and operation fence commits are rejected.
- Lease expiry and executor takeover do not transfer actor authority incorrectly.
- A restored generation cannot admit old capabilities, runs, or requests.
- A surviving stale client cannot self-register without post-restore bootstrap authority.

### 14.5 Audit and repair

- Governed audit commits atomically with domain facts.
- Invocation-audit failure after committed success does not change the result.
- PostgreSQL-unavailable audit repair survives process death.
- Concurrent repair append/import loses no record.
- Duplicate repair delivery is idempotent.
- Malformed repair evidence is quarantined without silent loss.

### 14.6 Shadow

- Gap-free baseline and delta closure are proven before command shadowing.
- Every registered command has an exact envelope or explicit gap.
- PostgreSQL outage does not alter live Asana command success.
- Exact envelope delivery can resume after outage.
- Post-state reconciliation does not count as command parity.
- Legacy destructive restore changes generation, disqualifies old parity, and requires a fresh baseline.
- Shared-kernel parity and independent characterization both pass.

### 14.7 Projection

- Outbox event commits atomically with authoritative state.
- Per-task ordering is preserved across worker restart and takeover.
- Duplicate delivery is idempotent.
- Ambiguous writes and moves remain unresolved until adjudicated.
- Mapping cannot transfer between tasks.
- Lost create response is reconciled by exact marker before retry.
- Multiple marker matches block automation.
- Unknown in-scope Asana tasks are detected and isolated or reported as blocking.
- Direct mapped-task edits are logged and overwritten without import.
- Restore projection epochs prevent stale workers from winning.

### 14.8 Backup, restore, and readiness

- `backup-create` and connected restore are absent from the post-cutover command surface.
- Historical backup/restore evidence remains readable.
- Operator restore establishes an externally controlled new generation.
- Mutation readiness remains closed through restore and validation.
- Old processes cannot regain authority automatically.
- Projection and audit repair reconcile under the new generation.

### 14.9 Deployment

- Docker Compose deployment uses persistent PostgreSQL storage and explicit configuration.
- Service readiness distinguishes authoritative database health, mutation readiness, migration state, projection health, and recoverable administrative availability.
- PostgreSQL outage fails governed mutations closed.
- Asana outage affects projection freshness but not committed PostgreSQL authority.
- The same authority model works on the intended self-managed AWS host.

## 15. Out of implementation scope

Do not implement without separate authorization:

- structured Stage B content;
- Cooked, Archive, Cooking History, or `log-cook`;
- general historical promotion/demotion;
- broad private search/browsing product work;
- managed PostgreSQL or HA;
- direct Asana-to-PostgreSQL ingestion;
- generic workflow unblock;
- routine task hard deletion.

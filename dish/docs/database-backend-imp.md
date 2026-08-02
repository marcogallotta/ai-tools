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

### 1.1 Production change control from August 1, 2026

August 1, 2026 is the Stage A production-change control epoch. Production feature work and bug fixes may continue under the current Asana/SQLite authority while Stage A is implemented, including urgent changes that touch durable state. They may not disappear from the target contract merely because they were added after the original re-baseline.

Maintain one append-only production-change ledger covering every commit merged or deployed on or after August 1, 2026, including commits already merged before this rule was added. Screen each commit when it is merged and again before each implementation, rehearsal, and cutover acceptance milestone.

The version-controlled ledger lives at `database-backend-production-change-ledger.md`. Creating it and backfilling it from the real repository Git history through the selected implementation re-baseline is implementation work item 1. The ledger may not be reconstructed from an exported source archive that omits `.git`; commit identity, merge/deployment timing, and changed-file evidence must come from the authoritative repository history and deployment records.

A commit is in scope when it changes or can change any of the following:

- database schema, migrations, constraints, indexes with semantic effect, or durable sidecars;
- task content, completion, project membership, section placement, or section-registry behavior;
- commands, principals, legal actions, request identity, replay, outcomes, or public protocol semantics;
- operations, Verification, holds, leases, claims, fences, recovery, abandonment, audit, backup, restore, or projection;
- external-effect intent, correlation, adjudication, or retry behavior;
- release, Honest asset, migration, or provenance bindings;
- any production feature or bug fix whose observable behavior becomes part of the current governed system.

For each in-scope commit, record at least:

- commit identity, merge time, deployment time, and source release;
- changed files and a concise behavior summary;
- affected commands, authorities, tables, sidecars, protocols, and migration paths;
- required data migration, backfill, compatibility, characterization, shadow, and acceptance updates;
- disposition as **already covered**, **implementation/migration document update required**, **locked-architecture amendment required**, or **explicit retirement/isolation decision required**;
- reviewer and closure evidence.

Urgent current-production work does not have to wait for the PostgreSQL implementation. However:

- no implementation or migration milestone may be accepted with an unreviewed in-scope commit;
- a deployed feature becomes current governed behavior and must be preserved, explicitly retired, or explicitly isolated before cutover;
- a bug fix that changes persisted facts or governed semantics must update the characterization corpus and target treatment rather than being treated as code-only;
- schema or sidecar changes must update the authority inventory, import contract, and migration evidence;
- command or protocol changes must update the normative semantic-delta contract before the corresponding target behavior is implemented;
- a change that contradicts the locked architecture requires a bounded architecture amendment approved by Marco rather than an implementation guess.

The implementation re-baseline is therefore moving but controlled: the target contract must reconcile the complete ledger through the exact source release selected for production cutover.

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

### 2.1 Stage A operating envelope

Stage A is optimized for a small private deployment with concentrated agent contention rather than high aggregate throughput:

- one human operator;
- approximately 100 current tasks at the August 1, 2026 re-baseline, with modest near-term growth;
- low aggregate command and listing throughput;
- multiple autonomous agents may act concurrently;
- typical same-task contention is two or three agents;
- the required adversarial ceiling is ten simultaneous agents or requests targeting the same task;
- correctness, deterministic replay, and recoverability take priority over throughput optimization.

The implementation does not require sharding, table partitioning, read replicas, distributed locking, or high-availability infrastructure. One service deployment and one active projector are acceptable initially, but database constraints, request identity, leases, claims, fences, transaction profiles, and worker ownership must remain correct if additional service or projector processes are later started.

Performance choices must be proportionate to this envelope. Ordinary indexed PostgreSQL queries, bounded import batches, and bounded pagination are sufficient unless measured evidence shows otherwise. Small scale does not permit weakening same-task concurrency, crash, replay, restore-generation, or external-effect guarantees.

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

## 4. Command semantic-delta contract

Complete and approve this contract immediately after current-system re-baseline and characterization, before target command, planner, repository, or public-protocol implementation. Public implementation may not use an unresolved row.

Shared transaction profiles:

- **Q — query:** consistent authoritative read; no durable mutation.
- **E — evidence mutation:** exact request or deterministic evidence identity; snapshot and actor validation; evidence plus governed audit commit atomically; no content-pointer advance.
- **L — local authoritative command:** generation-bound request, execution/fences, domain facts, versions/activation where applicable, outcome, governed audit, causality, and projection outbox commit atomically.
- **R — recovery command:** profile L plus exact target attempt/execution/hold identity and route-specific settlement predicates; never a generic unblock.
- **P — projection-only recovery:** settles only a downstream projection attempt and cannot change canonical content, logical placement, workflow legality, or the original command outcome.
- **X — retired:** no new admission after cutover; historical request outcomes and evidence remain replayable/readable only as imported history.

Normative command treatment:

| Command | Stage A treatment | Principal / authority | Profile and effect boundary | Replay / migration treatment |
|---|---|---|---|---|
| `create` | Retain bare-task creation and initial Research Queue placement; public identity becomes Dish UUID at authority cutover. | Agent; active generation and registered run. | L; task, initial version/activation, logical location, request outcome, audit and create-projection event commit together. | Exact request replay. Imported tasks use import activation, never fabricated `create` executions. |
| `sections` | Retain as a query over the active PostgreSQL section registry. | Authenticated Action reader. | Q; no live Asana registry read after cutover. | No request replay. Registry and aliases are imported with provenance. |
| `section-tasks` | Retain bounded pagination over tasks whose authoritative logical placement is the requested active-registry section. The cursor is opaque and binds the registry version, section identity, ordering key, and page boundary; stale or mismatched cursors fail closed rather than silently rebasing. | Authenticated Action reader. | Q; one bounded relational query over PostgreSQL task/location/registry authority, with no per-task Asana read or application-service call. | No request replay. Imported task aliases and exact logical placement establish the initial rows; pagination tokens are ephemeral read artifacts, not durable authority. |
| `read` | Retain against PostgreSQL authority plus separately reported projection freshness. | Authenticated reader. | Q. | Historical/current state comes from imported and later PostgreSQL authority. |
| `inspect` | Reclassify as replay-bound evidence mutation. | Verification actor with exact operation/cycle/run authority. | E; inspection occurrence and governed audit commit atomically. | Exact request replay; existing inspection facts import unchanged. |
| `start` | Retain all operation kinds; Planning retains two-request challenge admission. | Agent; registered run and applicable challenge/lease authority. | L; successful Planning start consumes the challenge with operation, outcome and audit. | Exact replay; open legacy authority is drained before production import. |
| `prepare` | Retain current content/workflow semantics. | Current stage actor. | L; PostgreSQL authority commits before asynchronous projection. | Exact replay; resolved historical facts import. |
| `approve` | Retain exact Verification occurrence, correction, candidate and signoff lineage. | Authorized verifier/actor. | L. | Exact replay; exact cycles, occurrences and Honest bindings import. |
| `reject` | Retain Large, Evidence, Human Review and Small-correction routes. | Authorized actor. | L or hold-entry L profile according to route. | Exact replay; holds/decisions import as named authority. |
| `submit` | Retain signed-state validation and exact logical destination transition. | Authorized actor. | L; logical placement commits atomically, Asana movement is projection-only. | Exact replay and convergence from already committed PostgreSQL domain evidence. |
| `renew-lease` | Retain narrow renewal. | Exact lease owner/run. | L limited to lease plus request outcome/audit. | Exact replay against the original lease identity. |
| `recover` | Retain as post-cutover adjudication of an exact unresolved downstream Asana projection attempt. It never settles PostgreSQL command authority or changes canonical task state. | Marco/admin. | P; exact projection-attempt target, persisted intent, observation, and three-way adjudication. | Exact replay. Imported legacy authoritative attempts and their terminal requests remain immutable historical outcomes and are not reopened. |
| `repair-destination` | Retain as projection-only repair of an exact downstream Asana destination movement after canonical PostgreSQL logical placement has committed. | Marco/admin. | P; exact projection movement attempt, mapping, intended logical destination, and observation evidence. | Exact replay; never infer or alter canonical placement from Asana. |
| `discard` | Retain cancellation of an exact provably unapplied open operation. | Marco/admin. | R; targets the exact operation, originating request/execution, immutable pre-operation task baseline, and current task/operation fences. Unresolved or confirmed external effects and completed workflow steps fail closed. Operation terminalization as `cancelled_by_marco`, request/execution outcome, governed audit, causality, and any required projection intent commit atomically. | Exact replay returns the original cancellation outcome. Imported terminal cancellation evidence remains immutable history and is never reopened. |
| `abandon-operation` | Retain permanent exact actor-attempt abandonment. | Marco/admin. | R using the abandonment state machine in §6.17. | Exact request/execution replay; no duplicate successor publication. |
| `reconcile-abandonment` | Retain exact blocked-abandonment continuation. | Marco/admin. | R using the same abandonment execution and immutable successor baseline. | Exact replay; never rebase succession. |
| `reopen-planning` | Retain the only current completion-clearing route. | Marco/admin. | L; completion clear, attempt/result, audit, outcome and projection commit together. | Exact replay; imported completion and attempts preserved. |
| `reopen` | Retain two-pass Human Review reset. | Marco/admin. | R with exact cycle/content/reset authority. | Exact replay. |
| `supply-evidence` | Retain Evidence-hold continuation. | Marco/admin. | R; targets exact hold and continuation predicate. | Exact replay; hold evidence imported. |
| `record-human-decision` | Retain Human Review continuation. | Marco/admin. | R; targets exact hold/decision requirement. | Exact replay; decision evidence imported. |
| `authorize-governed-change` | Retain exact grant creation. | Marco/admin. | L; immutable grant and governed audit commit together. | Exact replay and semantic deduplication. |
| `recover-lease` | Retain expired-lease release without ownership transfer. | Marco/admin. | R; exact lease target. | Exact replay; must not resolve a replacement lease on replay. |
| `expire-lease` | Retain exact point-in-time lease release; not run revocation and not recovery of uncertain work. | Marco/admin. | L limited to the exact resolved lease identity, release fact, request outcome, and governed audit. | Exact replay against the original lease; never retarget a replacement lease. |
| `migrate` | Retain bounded Honest task-schema migration for admitted older-schema tasks. | Marco/admin. | L; exact migration binding, new version activation, operation/result, audit and projection event. | Exact replay; completed migration history imports; no silent retirement. |
| `backup-create` | Retire at cutover. | Historical only. | X. | Preserve request outcomes, rows and artifacts as immutable evidence. |
| `backup-restore` | Retire at cutover; operator PostgreSQL restore replaces it. | Historical only. | X. | Preserve journal/checkpoints/outcomes; no new connected admission. |
| Planning-intent settlement | Add reason-bearing terminal settlement. | Marco/admin. | L limited to challenge/request/audit authority; proves no operation exists. | Exact replay; settled challenge is permanently non-reusable. |

Every route implementation must additionally pin its request canonicalization, protocol/OpenAPI introduction, current-view effects, exact fence set, and approved error/result classes. Shared profiles may be referenced, but no route may leave its target authority or replay behavior unresolved.

This matrix is approved for the frozen `42619b9` source baseline recorded in `database-backend-stage-a-baseline.json`. A later in-scope production change reopens only the affected rows through the production-change ledger; it does not permit implementation-time invention.

## 5. Current-to-target authority coverage

The implementation must maintain a row-by-row coverage matrix at re-baseline. The following is the minimum current inventory.

| Current authority | Target responsibility |
|---|---|
| `submissions` | Imported historical submission/request compatibility evidence or explicit retirement witness. Do not use as a second live task engine. |
| `audit_events` | Append-only governed and operational audit events. |
| `task_content_state` | Imported current content/placement head provenance; target current task/version/location pointers. |
| Current Asana project/section registry | Imported governed project/section identities, active registry version, aliases, and exact placement provenance. |
| Honest protocol/schema/migration assets | Immutable external-contract bindings with hashes and provenance; canonical authority remains external. |
| Holds and route-specific recovery facts | Named hold requirements, attempts, checkpoints, outcomes, and exact target bindings; never generic unblock state. |
| `operations` | Workflow operation authority. |
| `content_versions` | Immutable document/version occurrences with preserved identity scheme. |
| `verification_cycles` | Verification cycle, reviewed occurrence, decision, correction, and signoff bindings. |
| `write_attempts` | Imported legacy authoritative Asana-effect evidence only. Post-cutover Asana writes are represented exclusively as downstream projection attempts. |
| `movement_attempts` | Imported legacy authoritative placement-effect evidence plus separately typed post-cutover projection attempts; never canonical post-cutover authority. |
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

The executable re-baseline inventory is `database-backend-stage-a-baseline.json`. It freezes the exact Action/admin command surfaces, all current SQLite tables, external sidecar families, governing-source hashes, and the complete current characterization-test file set through source commit `42619b9`. The final matrix must include any authority added to the current repository after that baseline through the production-change ledger.

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

### 6.6 Governed projects, sections, registry, location, and completion

PostgreSQL owns stable logical governed-project and section identities after cutover. The target model must provide:

- governed project records and lifecycle;
- governed section records and lifecycle;
- one active section-registry version or equivalent activation;
- immutable registry provenance;
- Asana project-GID and section-GID aliases with origin, non-transferability, and active/retired state;
- task membership and section-placement history;
- exact registry/location revisions used by workflow legality and current-view tokens.

`sections` reads the active PostgreSQL registry. Asana enumeration after cutover is projection reconciliation only and cannot create, rename, retire, or select a logical section.

Commands including `create`, workflow handoffs, Verification handoff, destination movement, and recovery use logical section identities. Where current semantics couple workflow phase, project membership, placement, or completion, those facts commit in the same authoritative transaction.

Completion remains a separate Planning-eligibility axis, but it has no standalone positive-setting command. It may become true only through a governed Cooked or Archive transition. Stage A preserves imported completion and retains `reopen-planning`, which records the attempted before/after state and audit before clearing completion.

No Cooked or Archive transition is implemented in Stage A unless separately authorized before re-baseline.

### 6.7 Operations, steps, actors, and Honest contract bindings

Operations preserve:

- kind and lifecycle;
- task binding;
- creation request/execution;
- exact Honest/protocol/schema binding;
- predecessor/successor and abandonment lineage;
- terminal outcome.

Operation steps and actor facts remain append-only or monotonic. Actor facts bind exact operation participation, agent, owner, and run authority.

Canonical Honest authority remains the governing external release source. PostgreSQL stores immutable evidence bindings, not a competing canonical copy. An Honest binding records, as applicable:

- source/repository or checkout identity;
- protocol release and artifact hash;
- task-schema release and artifact hash;
- migration ID, source/target schema versions, migration metadata hash, and source IDs;
- supporting Dish release;
- resolution/import time and provenance.

Bind the exact record to activation/import evidence, operations, Verification cycles, content occurrences whose interpretation depends on it, command executions requiring pinned release semantics, and retained `migrate` executions. Missing or hash-mismatched bindings fail closed.

### 6.8 Verification and inspection occurrences

Verification storage must represent:

- cycle identity and operation/task binding;
- exact reviewed version occurrence;
- exact Honest protocol binding;
- verifier actor fact, owner/run, and independence evidence;
- immutable inspection occurrence and attestation;
- exact logical Verification Queue section identity and active registry/location provenance at inspection;
- rejection category/reason where applicable;
- corrected candidate lineage;
- signed occurrence and signoff evidence;
- two-pass reset or Human Review evidence;
- inherited signoff for permitted non-material check-ins.

Target `inspect` is a replay-bound evidence mutation. Its idempotency identity includes operation, cycle, reviewed occurrence and identity, verifier actor fact/run, attestation, and exact logical Verification Queue placement provenance. Evidence plus governed audit commits atomically. A changed head, actor fact, cycle, registry/location occurrence, or placement cannot reuse an earlier inspection.

Do not infer signoff or inspection eligibility from rendered text, a current hash, or current placement alone.

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

### 6.15 Leases and actor-attempt context

Leases remain principal/run/task/operation-scoped actor authority and preserve immutable creation context:

- lease kind: actor or temporary admin request;
- exact owner, actor/role, run, task, and operation;
- task-monotonic actor-attempt sequence for actor leases;
- optional exact Verification cycle context;
- issuance, expiry, renewal, recovery, and release evidence.

Constraints must ensure:

- admin-request leases have no actor-attempt sequence or Verification context;
- actor-attempt sequence is unique and monotonic per task;
- Verification context belongs to the same task and operation;
- creation classification never changes;
- recovery/release cannot silently assume workflow ownership, revoke a run, or become executor takeover.

Legacy classified and unclassified leases receive explicit import disposition and cannot be guessed from timestamps.

### 6.16 Holds and route-specific recovery authority

Represent each retained hold/recovery family as named authority rather than a generic status or unblock event. At minimum distinguish:

- Evidence hold and supplied-evidence continuation;
- Human Review requirement and recorded decision;
- expired-lease recovery/release;
- unfinished PostgreSQL command execution continuation through the original request/execution authority, never through an admin projection-recovery route;
- downstream projection-attempt adjudication through `recover`;
- downstream destination projection repair through `repair-destination`;
- exact cancellation of a provably unapplied open operation through `discard`;
- Planning reopen/completion-clear attempts;
- abandonment and successor recovery;
- downstream projection-only recovery.

Each family defines exact target identity, admission principal, request/execution profile, task/operation/lease/effect fences, monotonic checkpoints and outcomes, and interactions with content, location, audit, and projection. Unfinished PostgreSQL command work is resumed, taken over, or settled only through its original generation-bound request and execution authority. The `recover` and `repair-destination` routes are profile P after cutover and settle only exact downstream projection attempts. `discard` is profile R and retains the current authoritative cancellation of an exact provably unapplied open operation; it does not adjudicate projection non-application. A post-cutover Asana observation can settle only projection authority unless it is immutable evidence for an imported legacy attempt; it cannot change canonical PostgreSQL content or logical placement.

### 6.17 Abandonment and succession

Abandonment is a task-fencing state machine, not a generic recovery flag.

**Attempt creation** binds the exact classified actor lease and actor-attempt sequence, owner/run, task, source operation, optional Verification cycle, reason, request, and command execution. Only one active abandonment may fence a task.

**Active fence** blocks unrelated connected mutation and new actor lease acquisition while the attempt is preparing, published, blocked, or reconciling.

**Clean successor publication** atomically commits:

- source operation terminalization;
- exact incomplete-cycle disposition;
- source-lease retirement;
- fresh unowned successor operation and optional cycle;
- immutable successor content/location baseline;
- immutable succession edge;
- exact successor claim mode and prepared target identity;
- abandonment checkpoint/result.

**Successor claim** requires the exact prepared operation or operation/cycle target, prohibits the abandoned owner/run, revalidates the immutable baseline and logical placement, appends the new actor fact and lease, clears claim mode, and terminalizes the abandonment as appropriate.

**Drift and reconciliation** atomically mark the attempt blocked when baseline or placement diverges. Reconciliation uses successor-owned effect evidence to restore the immutable successor baseline and never rebases or rewrites the succession relation.

All checkpoints remain under the same admitted command execution or exact continuation execution so process death cannot repeat committed workflow settlement or publish a second successor.

### 6.18 Audit and repair

Governed audit facts commit with their governed domain transaction.

Every authoritative command transaction also commits a durable invocation-audit obligation keyed to the authority generation, request, immutable initial outcome, execution, causality, and required invocation metadata. The obligation is the restart-discoverable owner of audit completion after authoritative commit. Successful invocation-audit persistence terminalizes the obligation; process death after authoritative commit leaves it pending for deterministic scanning and repair rather than losing it.

Invocation/transport audit outside that transaction uses:

- immutable repair identity;
- exact request/result/audit payload, reconstructible from the committed obligation and referenced authoritative facts;
- durable external append before success response when PostgreSQL cannot record a later repair transition;
- restart scanning of pending obligations before they can be treated as silently complete;
- claim/import semantics safe under concurrent writer and importer;
- deduplication;
- fulfilled, repaired, or quarantined terminal outcome;
- backup and restore-generation reconciliation.

A local durable journal is the default continuation when PostgreSQL is unavailable after the authoritative obligation already exists. No crash interval after authoritative commit may make the missing invocation audit undiscoverable.

### 6.19 Schema migration provenance

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

## 9. External-effect classes and Asana projection implementation

### 9.1 Effect authority classes

Keep three non-interchangeable classes:

1. **Imported legacy authoritative Asana attempts:** immutable historical write/movement evidence from the pre-cutover authority. They are never reopened as post-cutover task authority.
2. **Shadow envelopes and observations:** non-authoritative parity evidence while Asana/SQLite remains live authority.
3. **Post-cutover projection attempts:** downstream effects only, with intent, claim, observation, adjudication, settlement, epoch, and mapping context.

`recover` and `repair-destination` are post-cutover projection-only recovery routes. Each targets an exact class-3 projection attempt and mapping/effect identity; neither may target a PostgreSQL command execution or an imported legacy authoritative attempt. `discard` is not an effect-class recovery route: it retains exact cancellation of a provably unapplied PostgreSQL operation under profile R. No post-cutover Asana observation may mutate canonical content or logical placement.


### 9.2 Credentials and isolation

Use a dedicated projector credential after cutover. The authoritative mutation service path must not use the Asana authority credential for task state.

### 9.3 Outbox

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

### 9.4 Mapping

A mapping binds one Dish task to one Asana GID and records imported or projector-created origin.

Mapping creation is exactly-once. Conflicting GIDs or duplicate correlations block automatic progress.

### 9.5 Lost-response-safe create

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

If feasibility fails, keep PostgreSQL-native create disabled during shadowing and rehearsal and return bounded topology alternatives to Marco. Production cutover remains blocked unless Marco approves a topology that preserves the current `create` semantic or explicitly retires that semantic.

### 9.6 Corpus reconciler

Periodically enumerate every in-scope Asana project and classify each task GID as:

- mapped;
- exact in-flight create correlation;
- isolated non-authoritative object;
- blocking unknown.

A blocking unknown makes projection readiness unhealthy. Isolation must be conspicuous and must not manufacture Dish authority.

### 9.7 Drift

For mapped tasks, compare Asana observation with exact committed PostgreSQL projection state.

Direct edits are logged and automatically corrected by reprojecting the exact committed PostgreSQL state. They are never imported as commands or canonical versions.

## 10. Service and repository structure

A recommended package split is:

- domain snapshot and policy;
- command planner/adjudicator;
- task/version and governed project/section registry repositories;
- workflow/Verification and Honest-binding repositories;
- request/run/execution repositories;
- lease, hold/recovery, and abandonment repositories;
- audit and repair repositories;
- projection outbox and worker repositories;
- migration/import repositories;
- service application layer;
- HTTP/admin transports.

Repositories expose domain operations, not unrestricted session access to transports.

Use dependency injection for sessions, clocks, UUID generation, release resolution, and external adapters so deterministic tests can pin all inputs.

## 11. Concurrency and database behavior

Concurrency design targets low aggregate throughput with concentrated contention. Two or three agents may commonly target one task, and the implementation must remain correct with ten simultaneous agents or requests targeting that same task. This is a correctness ceiling, not a high-throughput or distributed-systems requirement.

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
- restore-generation invalidation;
- deterministic winner and loser behavior under two-, three-, and ten-way same-task contention;
- absence of duplicate operations, activations, lease ownership, capability consumption, signoff, successor publication, request outcomes, or projection effects under that contention.

Independent-task work should not require global serialization merely to satisfy the same-task safety contract. Database constraints enforce authority invariants even when application code is defective.

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

The authoritative read model must support deriving each task's current legal next action entirely from local PostgreSQL state. `CurrentWorkflowService` and `workflow_policy.legal_actions` remain the sole semantic owner of legal-action derivation: the PostgreSQL read model exposes one normalized relational authority projection consumed by that policy and by a bounded set-oriented query compiled from the same declared predicates. It must not introduce a separately maintained action matrix, denormalized authoritative task-status column, transport-owned rule set, or per-task application-service loop. The bounded, paginated query must select tasks by derived legal action without per-task Asana reads and must correctly include tasks for which the next legal action exists even when no operation row has yet been opened for that next phase. Contract tests compare the set-oriented query against the authoritative single-task computation for every frozen policy case.

## 13. Implementation sequence and commit stages

Implementation is organized into six top-level stages. Each stage is a reasonable review and commit milestone with one coherent completion condition. Agents may create smaller working commits within a stage, but those commits do not redefine the milestone and no intermediate stage becomes production authority.

### Stage 1 — Baseline and executable contracts

Purpose: freeze the exact system being implemented before target schema or command work begins.

The production-change ledger (`database-backend-production-change-ledger.md`) is
backfilled and closed through commit `42619b9`. The `section-tasks` pagination
contract now has a normative §4 row, and §12 explicitly preserves
`CurrentWorkflowService`/`workflow_policy.legal_actions` as the sole semantic owner
while requiring set-oriented PostgreSQL selection from the same predicates.

Includes:

- maintain the closed ledger range and reopen affected contracts for every later
  in-scope production change;
- freeze current-behavior characterization and complete the authority inventory
  in `database-backend-stage-a-baseline.json`;
- complete and approve the normative command semantic-delta contract in §4, with no unresolved command treatment;
- establish the isolated `dish_pg` PostgreSQL project skeleton, explicit SQLAlchemy session ownership, Alembic lineage, and Docker Compose test database.

Commit result:

> The source baseline is known, every current command and authority has one target treatment, and the target project can run migrations and tests.

This stage is mandatory before target domain or command implementation. The ledger remains open after this commit and every later in-scope production change must be reconciled continuously.

### Stage 2 — Core PostgreSQL authority model

Purpose: establish the foundational authoritative data model without activating the full command surface.

Includes:

- authority generations, activation records, restore/bootstrap foundations, and migration provenance;
- immutable Honest release, schema, migration, hash, and provenance bindings;
- governed projects, sections, registry versions, and project/section aliases;
- Dish task identity and Asana task aliases;
- immutable title/body versions and activations;
- logical project membership, section placement, and completion;
- foundational constraints, repositories, and import-style activation support.

Commit result:

> PostgreSQL can represent the complete authoritative task document, identity, registry, placement, completion, release context, and authority generation, but it is not yet the production command authority.

Implemented physical boundary: `dish_pg.models` and Alembic revision
`0002_core_authority_model` define only the Stage 2 tables above; `dish_pg.repositories`
participates in caller-owned sessions without committing; and `dish_pg.services.CoreAuthorityService`
provides atomic import-style task activation without fabricating requests, executions, operations,
or projection facts. PostgreSQL triggers enforce immutable evidence, monotonic generation and alias
transitions, exact current-pointer targets, and active-registry placement legality.

Acceptance at this stage covers migrations, constraints, aliases, registry legality, immutable versions, import activation, and generation isolation.

### Stage 3 — Command execution and workflow authority

Purpose: implement the complete authoritative domain and concurrency machinery required by retained commands.

Includes:

- immutable requests and stored outcomes;
- runs, command executions, executor claims, and task/operation fences;
- classified leases and actor-attempt sequencing;
- operations, steps, and actor facts;
- Planning challenges and Marco authorizations;
- Verification cycles, exact inspection occurrences, correction lineage, and signoff;
- named holds and route-specific recovery authorities;
- abandonment and fresh-successor state machine;
- governed audit, causality, invocation-audit obligations, and durable repair support;
- shared transaction profiles and typed effect authority.

Commit result:

> PostgreSQL has the complete authoritative workflow, replay, recovery, and concurrency layer needed to execute retained commands safely.

This stage must include concentrated two-, three-, and ten-way same-task contention tests. Independent-task work must remain legal without global serialization.

Implemented physical boundary: Alembic revision `0003_workflow_authority` and
`dish_pg.stage3_models` add only the workflow/replay/recovery/concurrency authorities named above.
`dish_pg.workflow` owns generation-bound run registration, exact request admission and replay,
execution claims, revision fences, operation and actor authority, classified lease acquisition and
renewal, Planning challenge claim/consume/settle, Marco authorization grant/reservation/consumption,
Verification inspection/signoff, named hold continuations, abandonment baselines, governed audit,
and invocation-audit obligation/repair transitions. All methods use a caller-owned session and no
repository or domain service commits. Immutable occurrences are protected against update/delete;
current execution, lease, challenge, authorization, cycle, hold, requirement, abandonment, and
audit-obligation rows are revisioned or monotonic. Partial unique constraints fence one open
operation, active actor lease, open Verification cycle, open hold, open Human Review requirement,
and active abandonment at their exact task/operation scope.

Stage 3 acceptance covers fresh migration to head, immutable evidence, exact replay identity, stale
generation and stale-fence rejection, atomic outcome/audit/repair-obligation rollback, ten-way
same-task lease and authorization contention, and independent-task concurrency. It does not expose
a command or HTTP surface and it creates no shadow or projection authority.

### Stage 4 — Command and service port

Purpose: connect the authoritative domain to the complete Dish command and read surface.

Includes:

- extract or finalize the shared planner and adjudicator while preserving the independent characterization oracle;
- implement every retained agent and admin command against PostgreSQL using the approved §4 contract;
- implement reads, current-view tokens, `sections`, and new listing/read features against the PostgreSQL registry;
- preserve `discard` as authoritative cancellation of an exact provably unapplied operation;
- preserve `migrate`, `reopen-planning`, recovery, lease, Verification, authorization, and abandonment semantics;
- implement the coherent service, OpenAPI, and Action protocol;
- run frozen characterization cases against the PostgreSQL implementation.

Commit result:

> The full retained command and read surface operates against PostgreSQL and matches approved current behavior in an isolated non-production environment.

Implemented physical boundary: `dish_pg.command_contract` is the executable approved §4 registry;
`dish_pg.planner` is deterministic and delegates legal-action decisions to the shared policy;
`dish_pg.read_model` owns active-registry reads, exact task current views, and authenticated opaque
pagination; `dish_pg.command_port` admits and replays every retained mutation and dispatches every
retained agent/admin route in caller-owned transactions; and `dish_pg.protocol` authenticates the
existing Action/private bearer scopes before parsing a body. The checked-in PostgreSQL Action
OpenAPI is generated from the same command registry. Stage 4 emits projection intent only through
an injected recorder and therefore cannot perform an Asana write before Stage 5 authority exists.

Stage 4 acceptance covers command inventory closure, retired-command rejection, deterministic
planning/adjudication, exact replay, atomic bare-task creation, Planning challenge admission and
consumption, active-registry reads, query-bound pagination, current-view policy delegation,
OpenAPI parity, and authentication-before-body-loading. Production routing remains unchanged.

### Stage 5 — Import, shadow, and projection

Purpose: implement the transition machinery and downstream Asana behavior without changing live authority prematurely.

Includes:

- complete source import with exact provenance;
- shadow-envelope storage, asynchronous delivery, and independent parity comparison;
- transactional projection outbox;
- task, project, and section mappings;
- projection attempts, observation, adjudication, and ordering;
- direct-edit drift detection, automatic reprojection, and corpus reconciliation;
- lost-response-safe Asana creation correlation;
- reconciliation of every in-scope production change recorded after the Stage 1 baseline.

Commit result:

> The target can import the current system, shadow live behavior without affecting it, and project authoritative PostgreSQL state safely to Asana.

The conditional Asana-create fallback becomes a human decision only if the required correlation proof fails. Production cutover remains blocked if current `create` would otherwise be unavailable.

Implemented Stage 5 foundation:

- Alembic revision `0004_transition_projection` and `stage5_models.py` add exact source-import
  batches and immutable entity evidence, shadow baselines/envelopes/deliveries/comparisons/gaps,
  projection epochs and historical mappings, ordered outbox events, exact attempts, append-only
  observations and adjudications, create correlations, drift evidence, and corpus reconciliation.
- `SourceImportService` closes a source import only when the declared entity corpus is complete and
  duplicate source identities reproduce the same target and provenance.
- `ShadowService` stores source success independently from target delivery, uses revisioned claims,
  resumes failed deliveries after explicit gap resolution, and refuses baseline closure while any
  delivery or gap remains unresolved. Missing or uncomparable command evidence is recorded as an
  explicit gap rather than inferred from later state.
- `ProjectionService` binds imported aliases only for the active generation and registry, permits
  historical rebinding only after the prior epoch mapping is retired, and database guards reject
  alias transfer or stale-epoch mapping.
- Command-sourced projection events require the exact task-bound command execution and commit in the
  same caller-owned transaction as authoritative state. Service-sourced reprojection is separately
  typed. Event identity, intent, task sequence, generation, and epoch are immutable.
- Claims preserve per-task order. A worker records an attempt before external dispatch and appends
  exact observations and adjudications after reread. Uncertainty can receive later evidence on the
  same attempt; `recover` and `repair-destination` are task-bound and cannot settle another task's
  command execution or imported legacy attempt.
- Create correlation is attempt-bound. One canonical GID binds exactly once, multiple matches block
  automation, and a not-applied settlement requires an exact zero-match correlation plus complete
  reread evidence.
- Retiring an epoch retires its active mappings and supersedes non-applied events, preventing stale
  workers from winning or blocking the next epoch. Direct mapped-task drift emits an ordered
  authoritative reproject event; unknown corpus objects keep reconciliation blocked.
- Stage 5 services perform no network I/O and never commit. The live Asana/SQLite path remains
  authoritative; production credentials, worker activation, final import, rehearsal, and cutover
  remain Stage 6 decisions.

Stage 5 acceptance covers full migration from an empty database, source-import closure and
immutability, shadow delivery/gap closure, mapping identity and epoch fences, atomic command/outbox
rollback, exact replay and idempotency, per-task ordering, lost-response create correlation, later
uncertainty recovery, drift reprojection, and blocking corpus reconciliation.

### Stage 6 — Rehearsal, acceptance, and cutover package

Purpose: produce the complete evidence-backed release candidate and rollout package.

Includes:

- repeatable migration tooling and full rehearsal support under `database-backend-migration.md`;
- final import and semantic validation;
- all implementation acceptance in §14;
- crash-boundary, fault-injection, destructive-restore, stale-generation, and ten-way same-task contention tests;
- exact source commit and production-change ledger closure;
- final Asana task, registry, alias, membership, placement, and completion closure;
- old-writer fencing;
- durable activation, rollback burn, and first-admission procedure;
- operator runbooks and the final evidence bundle.

Commit result:

> A reproducible release candidate exists with all evidence required for Marco to authorize production cutover.

Implemented Stage 6 offline foundation:

- Alembic revision `0005_release_cutover` adds release candidates, immutable evidence revisions,
  rehearsal runs and checkpoints, legacy-writer fences, deterministic evidence bundles, exact
  approvals, resumable cutover runs/checkpoints, and mutation-admission controls.
- `ReleaseCandidateService` derives acceptance from the authoritative Stage 2–5 database state. It
  verifies exact import closure, closed shadow evidence, active registry and projection epoch,
  complete task/registry alias coverage, no unresolved workflow authority, no unresolved projection
  work, reconciliation coverage for every active mapping, schema head, required acceptance evidence,
  and all required rehearsal classes.
- Candidate evidence is append-only until validation. Validation binds the exact current bundle and
  rejects a stale bundle even when later evidence also passes. Approval is single-use and bound to
  that validated bundle rather than to a candidate name or release label.
- `MutationAdmissionControl` is created closed. Database guards reject target request admission after
  validation until rollback-burn evidence is durable and the exact cutover run opens admission.
- `dish_service.legacy_writer_fence` supplies an atomic mode-0600 file fence. The legacy HTTP path
  authenticates first and then rejects every POST before loading its body. A malformed fence file is
  still an engaged fence.
- `scripts/dish-pg-acceptance` runs the focused Stage 1–6 lane, smoke gate, database-boundary gate,
  and full suite and writes a source-manifest-bound JSON report. `scripts/dish-pg-release` records
  candidate, evidence, rehearsal, approval, fence, activation, rollback-burn, first-admission, and
  completion transitions through caller-owned transactions.
- `database-backend-stage6-runbook.md` fixes the operator order and recovery boundary. Filesystem
  fence release records the authorized database transition first, so an I/O failure can only leave
  the legacy writer fenced, never reopen it early.

Not completed by the offline implementation: production snapshot capture, production database and
sidecar hashes, real Asana corpus/registry closure, PostgreSQL backup/PITR setup, measured production
RPO/RTO, credential/process fencing probes, production routing, projection-worker enablement, Marco
approval, rollback burn, mutation-admission opening, and first live request validation. These are
Stage 6 environment actions, not evidence that the repository may synthesize.

The actual production activation is a controlled release event, not a seventh implementation stage.

### Stage summary

| Stage | Main purpose | Commit milestone |
| --- | --- | --- |
| 1 | Freeze source behavior and executable contracts | Baseline, ledger, semantic matrix, and project skeleton complete |
| 2 | Build foundational PostgreSQL authority | Core schema, constraints, aliases, and repositories complete |
| 3 | Build workflow, replay, recovery, and concurrency authority | Authoritative domain engine complete |
| 4 | Port the full command and service surface | PostgreSQL application behavior functionally complete |
| 5 | Add import, shadow, and downstream projection | Transition path technically complete |
| 6 | Prove and package production cutover | Release candidate ready for Marco authorization |

No partial stage becomes production authority. Throughout all six stages, continuously reconcile the August 1, 2026 production-change ledger. A newly merged or deployed in-scope commit may require renewed characterization, semantic-matrix revision, target schema or service changes, migration updates, or repeated acceptance before the affected stage remains complete.

## 14. Implementation acceptance

### 14.1 Authority coverage

- Every current table and sidecar at re-baseline has one documented target disposition.
- No named current authority is replaced only by generic audit.
- Deferred product features are absent or feature-gated and non-gating.
- Historical quarantine remains preserved and isolated.
- The active project/section registry, aliases, Honest bindings, holds/recovery families, and typed effect classes each have explicit owners and import dispositions.

### 14.2 Schema and migration

- Fresh database migration from empty to head succeeds.
- Upgrade through every Alembic revision succeeds.
- Applied migration provenance is immutable and complete.
- Database constraints reject duplicate task/project/section aliases, illegal registry or pointer activation, duplicate consumptions, conflicting mappings, stale fences, invalid lease classification/context, and illegal monotonic transitions.
- Restore generation changes invalidate earlier run/request authority.

### 14.3 Current-behavior preservation

- Every retained command passes its frozen characterization cases.
- Legal actions and recovery guidance match current governing behavior unless the semantic-delta matrix records an approved change.
- Verification cases prove exact cycle, reviewed/corrected/approved/signed occurrence lineage, verifier actor fact/run, inspection attestation, exact Verification Queue placement provenance, signoff and inherited-signoff rules.
- Planning first-call admission performs no task read, operation creation, or actor lease.
- Completion/reopen, hold/recovery, authorization, abandonment, and successor cases pass.
- Every command row in §4 is implemented with its selected profile, exact target authority, replay behavior, and retirement/import treatment.
- A bounded PostgreSQL read-model query can select tasks by derived current legal next action without per-task external reads or per-task policy evaluation, including tasks with no operation yet opened for the next phase; its result matches the authoritative single-task legal-action computation.

### 14.4 Requests and concurrency

- Exact request replay returns the stored outcome.
- Identity conflict fails closed.
- Concurrent duplicate delivery performs one logical execution.
- Acceptance exercises the expected two- and three-agent same-task contention cases and an adversarial ten-request same-task case for every transaction family with exclusive or single-use authority.
- Under conflicting same-task contention, at most one incompatible transition wins; every loser fails closed or returns its deterministic stored outcome without duplicate domain facts or external effects.
- Concurrent work on unrelated tasks remains legal and is not forced through a global task lock.
- Failure injection before the authoritative commit exposes none of the command bundle; failure after commit exposes the complete request/execution, domain facts, versions/activation, outcome, governed audit, causality, and outbox bundle, and exact replay returns that outcome.
- Stale executor, task fence, and operation fence commits are rejected.
- Lease expiry and executor takeover do not transfer actor authority incorrectly.
- A restored generation cannot admit old capabilities, runs, or requests.
- A surviving stale client cannot self-register without post-restore bootstrap authority.
- Planning challenge claim/consume/settle races have one winner, deterministic loser replay, no partial operation, and no reuse after consumption or settlement.
- Marco authorization reserve/release/consume races preserve all-or-nothing reservation, exact ownership, committed-result binding, and final single use.
- Later uncertainty resolution, current-view computation, projection, cleanup, or invocation-audit failure cannot rewrite the immutable initial outcome or convert committed success into retry advice.

### 14.5 Audit and repair

- Governed audit commits atomically with domain facts.
- Invocation-audit failure after committed success does not change the result.
- Every committed authoritative command has a transactionally durable, restart-discoverable invocation-audit obligation until audit completion is fulfilled, repaired, or quarantined.
- Process death immediately after authoritative commit cannot lose or hide the invocation-audit obligation.
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
- Imported legacy authoritative attempts, shadow observations, and post-cutover projection attempts remain distinctly typed and cannot be settled through the wrong recovery route.
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
- The implementation contains no laptop-specific authority assumptions that would prevent later relocation to the intended self-managed AWS host. Actual AWS deployment is not a Stage A acceptance gate.

### 14.10 Production-change closure

- The production-change ledger is complete from August 1, 2026 through the exact source release under review.
- Every in-scope commit has an approved disposition and reviewer evidence; no row remains unreviewed or conditionally ignored.
- Added commands, features, authorities, schema objects, sidecars, and persisted semantics are represented in the authority inventory, semantic-delta contract, target implementation, migration import, characterization corpus, and acceptance evidence as applicable.
- Bug fixes that alter governed behavior or durable facts are characterized and reflected in target semantics.
- Changes deployed after a prior acceptance run invalidate the affected acceptance evidence until impact review and required re-execution complete.
- No production feature is silently dropped at cutover; it is preserved, explicitly retired by Marco, or explicitly isolated with migration evidence.
- The implementation and migration documents bind the exact source commit/release range they cover.

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

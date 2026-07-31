# Database-backed task store: Stage A architecture

Status: Stage A architecture ready for implementation-design handoff after a code-grounded
consistency pass on 31 July 2026. This document is not implementation authorization and does not
authorize a production cutover. The human-approved decisions below are binding design constraints
until Marco explicitly changes them. Agents may identify conflicts or risks, but must not silently
weaken, reinterpret, or overrule them.

Current behavior remains defined by [`architecture.md`](architecture.md),
[`runtime-contract.md`](runtime-contract.md), and [`rollout.md`](rollout.md). Until an explicitly
authorized cutover completes, the live Asana task remains authoritative and the current SQLite
store remains the legacy workflow/runtime database.

## Human-approved architectural decisions

When an implementation or review agent finds that one of these decisions creates a material
problem, it must:

1. state the exact conflict and cite the relevant code or design evidence;
2. present the smallest viable alternatives and their consequences;
3. stop and ask Marco before changing the approved direction.

A later agent review is not authority to reopen these decisions by itself.

1. **Target database — PostgreSQL.** The new authoritative task and workflow store uses PostgreSQL,
   not SQLite. SQLite remains only the legacy store until the authority cutover. All task state and
   workflow evidence that must commit atomically move into the same PostgreSQL transaction domain.
2. **Staged delivery.** Stage A moves the existing title/body document authority to PostgreSQL.
   After Stage A is live, there is an explicit real-world battle-hardening period before Stage B is
   authorized. Stage A then remains the live production system while Stage B is developed and
   tested. Stage A is not disposable scaffolding, and its task/version foundation must remain
   representation-neutral.
3. **Canonical commit boundaries and execution journaling.** Incomplete Planning, Research, and
   Verification-round work does not advance canonical task content. Dish durably records the
   service-visible commands, workflow transitions, ownership changes, attempted mutations,
   outcomes, and compensations for each active attempt. It does not checkpoint an agent's private
   reasoning, notes, or unfinished draft. If an agent disappears before the complete `prepare` or
   Verification decision boundary, that intellectual work is discarded and recovery starts from
   the last committed canonical version.
4. **One-way authority direction.** Before cutover, Asana is authoritative and confirmed Asana state
   is mirrored into non-authoritative PostgreSQL shadow state. At cutover, authority flips once.
   After cutover, PostgreSQL is authoritative and projects committed state one-way to Asana. There
   is no bidirectional synchronization and never peer authority.
5. **Asana remains Marco's human interface after cutover.** The post-cutover Asana copy is read-only
   from the human and agent perspective. Direct edits in Asana never flow back into PostgreSQL.
   A new private frontend is not a Stage A prerequisite and may be considered separately later.
6. **Universal Dish task identity.** Every authoritative task has a Dish UUID. Imported Asana task
   GIDs are immutable external aliases in a separate alias relation; they are never the internal
   primary key. Compatibility APIs may resolve an Asana alias temporarily, but authoritative
   responses and storage use the Dish UUID.
7. **Archive, do not routinely delete.** Ordinary lifecycle commands never hard-delete a task or its
   history. Unapproved, redundant, or retired dishes use the governed archive direction already
   described in [`future.md`](future.md). Exceptional data purging, if ever required, is a separate
   administrative and policy design.
8. **Preferred PostgreSQL application stack.** Use SQLAlchemy 2.0.50 for ORM/database access,
   Alembic 1.18.4 for every schema migration, `psycopg[binary]` 3.3.4 as the PostgreSQL driver, and
   Pydantic alongside SQLAlchemy for command, API, and domain validation. Pydantic is not the ORM.
   An implementation agent may propose a change only for a concrete compatibility, security, or
   operational reason and must not substitute a different stack merely by preference.
9. **The non-authoritative side never blocks the authoritative side.** During shadow operation, a
   PostgreSQL mirror failure is logged, retried, and reconciled asynchronously but never changes a
   successful Asana result. After cutover, an Asana projection failure is logged, retried, and
   reconciled asynchronously but never rolls back or reclassifies a successful PostgreSQL result.
10. **Cooked and Archived are distinct outcomes.** Cooked records that a dish was actually made and
    may later carry cook-log history. Archived removes an unapproved, redundant, obsolete, or
    retired task from active work without claiming it was cooked. Neither state implies the other,
    and both preserve full history.
11. **Mutation coverage is progressive application work.** Stage A does not require Marco to define
    every future human mutation in advance or require a generic editor. Implement the existing
    governed actions and the smallest additional actions discovered during shadow use. Adding later
    commands, constraints, indexes, or Alembic migrations is normal application evolution, not a
    database-architecture redesign.
12. **Battle-hardening and cutover are evidence-based.** There is no fixed duration or arbitrary
    pass count. Marco decides near cutover using observed failures, recoverability, diagnosis and
    repair burden, projection correctness, backup/restore confidence, and actual usage.
13. **Historical exceptions are never silently discarded.** Problematic imported tasks are
    reconciled or quarantined case by case from exact source evidence. The architecture does not
    require one universal exception policy before implementation design begins.

## Approved database implementation conventions

These conventions are approved defaults for Stage A unless Dish-specific evidence justifies a
narrow exception:

- Use SQLAlchemy 2 declarative models with `DeclarativeBase`, `Mapped`, and `mapped_column`.
- Alembic owns every schema change. Production and normal test setup must not use
  `Base.metadata.create_all()` as a migration substitute.
- Keep migrations ordered, clearly named, reversible where safely possible, and exercised from an
  empty database through `alembic upgrade head` in CI.
- Read `DATABASE_URL` from environment configuration. Avoid opening PostgreSQL connections at module
  import time; initialize engines and session factories lazily.
- Inject an explicitly owned SQLAlchemy `Session` or unit of work at the application-command
  boundary. HTTP handlers, CLI commands, workers, scripts, and tests must be able to supply that
  boundary without hidden connection or commit ownership.
- The application/service operation owns commit and rollback. Lower-level repository and helper
  functions may `flush()` but must not perform surprising commits.
- Enforce durable invariants with PostgreSQL foreign keys, unique constraints, partial indexes,
  checks, exclusion constraints, or triggers where appropriate—not only Python validation.
- Use explicit PostgreSQL concurrency behavior, including `ON CONFLICT`, row locks, stable lock
  ordering, and handling of `IntegrityError`, serialization failures, and deadlocks through exact
  request replay.
- Store instants as offset-aware PostgreSQL `TIMESTAMPTZ`, normalized to UTC. Domain wall-clock
  values, wherever required, must be modeled separately and explicitly.
- Define foreign-key deletion behavior deliberately (`RESTRICT`, `CASCADE`, `SET NULL`, or governed
  archival behavior). Do not rely on implicit defaults.
- Maintain a separate test PostgreSQL database and isolated container/project environment, with a
  hard guard that refuses destructive test setup against development or production databases.
- Tests must run the real Alembic migration chain. Tests may commit normally; isolation may be
  restored by truncating governed test tables between tests rather than masking transaction behavior
  behind a permanent rollback fixture.
- Future mutations may require ordinary Alembic migrations for new tables, columns, constraints, or
  indexes. That is expected application evolution, not an architectural redesign.

## Decision summary

Replace Asana's remaining live authority with a domain-native, versioned task store in PostgreSQL.
Stage A stores immutable title/body documents and preserves the current content contract while
moving task state, workflow evidence, replay, execution journaling, and mutation atomicity into
PostgreSQL. Stage B later introduces versioned structured Planning and dish authority after Stage A
has been battle-tested.

Keep `dish-service` as the only live mutation authority and keep the current workflow,
Verification, lease, replay, audit, and Part I abandonment boundaries unless this design explicitly
changes them. The authoritative task revision, workflow transition, governed audit evidence,
execution evidence, replay result, and any projection-outbox item commit in one PostgreSQL
transaction when they belong to one command boundary.

The replacement is not an Asana clone. It owns only:

- immutable document versions in Stage A and structured versions in Stage B, with
  representation-appropriate exact identities;
- exact imported source documents and generated human-readable renderings;
- one current Dish location, distinct cooked state, governed archive disposition, and immutable
  transition history;
- the existing workflow and Verification evidence;
- task creation, browsing, search, and Marco's narrow governed interventions;
- lifecycle-authorized editing without a generic content-save bypass;
- an extensible bounded command layer whose initial mutation set is proven by actual Stage A use;
- future cook-log records only when separately designed and approved.

Authority is singular throughout the migration. Before cutover, production writes go to Asana and
confirmed state is mirrored one-way into PostgreSQL shadow storage. A shadow-write failure is
reported and repaired asynchronously but never changes the Asana result. After cutover, production
writes commit to PostgreSQL and a transactional outbox updates Asana as Marco's read-only interface.
A projection failure is reported and repaired asynchronously but never changes the PostgreSQL
result. Shadow or projection state never decides workflow legality.

## Why consider this after activation

The current external-effect protocol is intentionally conservative. Dish records intent, calls
Asana, rereads the task, and classifies the effect as `confirmed`, `not_applied`, or `uncertain`.
That protects production work but creates recovery states that exist only because the document and
workflow evidence commit in different systems.

A PostgreSQL-native task mutation can commit the new task revision, workflow transition, governed
audit evidence, command-execution journal, replay result, and projection-outbox item together. A
process failure before commit rolls the whole unit back; a response loss after commit is answered by
exact request replay. This removes ordinary content writes, moves, cooked-state changes, archive
changes, and task creation from the ambiguous external-effect model.

The canonical-boundary rule makes recovery simpler without inventing agent-work checkpointing.
Planning and Research `start` commands establish durable operations but do not change canonical
content. Verification `start` and `inspect` establish review evidence but do not change canonical
content. The complete Planning or Research `prepare`, and the complete Verification-round decision,
are the content commit boundaries. If a run disappears earlier, Dish retains its service-visible
command history, abandons or compensates its workflow attempt under the existing recovery contract,
and starts from the last committed version. The abandoned agent's unpublished work is intentionally
lost.

Structured dish storage later removes repeated parse-and-reconstruct validation from the steady
state. Dish validates typed fields and relationships directly, hashes one canonical JSON
representation for exact version identity, and renders documents for humans or compatibility
surfaces. Parsing remains an import and Stage B migration concern, not a Stage A authority boundary.

These are different benefits with different risks. Moving task and workflow authority into
PostgreSQL removes cross-system uncertainty while preserving the existing document contract.
Moving from documents to structured dishes changes content identity, schema semantics, API
payloads, editing, rendering, and the object to which Verification binds. The staged decision keeps
those proof obligations separate.

## Independent project priority

The sequence is fixed:

1. **Stage A — authority migration.** Mirror confirmed Asana state into PostgreSQL, prove the
   document-compatible repository and workflow path, perform a separately authorized authority
   flip, and run PostgreSQL with immutable title/body documents as the production authority.
2. **Battle-hardening pause.** Operate Stage A in production long enough to expose real workflow,
   recovery, projection, backup, and operator issues before authorizing Stage B.
3. **Stage B — representation migration.** Keep Stage A live while defining, validating, storing,
   editing, rendering, and verifying structured Planning and dish versions inside the already
   authoritative PostgreSQL service.

Stage A must not embed title/body assumptions into task identity, workflow bindings, replay,
location, archive, or transition APIs in a way that forces a second backend redesign. Stage B is a
separately approved content-representation migration, not another authority cutover.

## Goals

1. Make one PostgreSQL transaction authoritative for a task mutation and its durable workflow
   result.
2. Support immutable title/body document versions as Stage A authority and immutable structured
   versions as the separately gated Stage B representation.
3. Record every service-visible stage command, durable workflow mutation, outcome, and compensation
   without checkpointing private agent work or advancing canonical content before the governed boundary.
4. Preserve exact imported source documents, independent Verification, run lineage, action
   authority, request replay, leases, Part I abandonment/successor semantics, audits, backup, and restore.
5. Remove out-of-band live task edits and Asana network uncertainty from normal workflow after
   cutover.
6. Preserve stable Dish command lifecycle and response meaning wherever the backend change does not
   require a deliberate identifier or recovery-contract revision.
7. Keep Asana available as Marco's post-cutover read-only human interface through a transactional
   projection outbox.
8. Import the live corpus deterministically, quarantine exceptions, and retain exact source
   snapshots for acceptance.
9. Permit a long-running Asana-authoritative shadow period without introducing dual authority.
10. Delete the executable Asana authority path after acceptance while retaining the isolated
    read-only projector.
11. Replace Planning's read-only lookup of completed cooking history with a Dish query.
12. Let a dish be found by its destination category independent of its current
    Research/Verification Queue placement.

## Non-goals

- generic projects, memberships, teams, assignees, comments, notifications, or permissions;
- a permanent title/body blob as the final canonical dish model after Stage B;
- dish-field editing that bypasses a lifecycle-authorized, revision-checked Dish command;
- browser or CLI access to raw SQL or generic row CRUD;
- multi-user or hostile-tenant authorization;
- multi-region, multi-primary, or automatic-failover architecture as a Stage A prerequisite;
- a new private frontend as a Stage A prerequisite;
- automatic semantic recipe judgment;
- recursive dependency discovery;
- simultaneous Asana and PostgreSQL authorities;
- bidirectional synchronization or a writable Asana fallback after PostgreSQL cutover;
- a writable compatibility engine for historical Asana workflow states;
- routine hard deletion of tasks or history.

## Target authority model

After cutover, the three authorities become:

1. **Current Honest assets** define the supported protocol release and canonical task schema.
2. **Dish task storage in PostgreSQL** owns the current authoritative version—document-compatible in
   Stage A and structured after Stage B—plus location, distinct cooked/archive disposition, immutable
   task revisions, and operation/cycle command journals.
3. **Dish workflow storage in PostgreSQL** owns operation intent, Verification evidence, actor/run
   lineage, leases, request replay, recovery facts, and audit history.

The second and third authorities share one PostgreSQL transaction manager, but remain separate
domain concepts. The current task row is not a substitute for append-only workflow evidence, and
workflow phase is not a substitute for the current task revision.

```text
private dish CLI / dish-admin ──────────────────────────┐
GPT Action ─────────────> bounded Action routes ─────────┼─> DishService
future private UI ──────> private query/command routes ─┘       |
                                                                  v
                                                   CurrentWorkflowService
                                                     |          |
                                                     v          v
                                        versioned task store  workflow evidence
                                                     \          /
                                                  PostgreSQL transaction
                                                           |
                                                           v
                                                projection outbox
                                                           |
                                                           v
                                              read-only Asana interface
```

The Action listener remains bounded. Asana is the initial post-cutover human view; a future private
UI is optional and must use bounded service APIs rather than database access. Neither surface
receives database credentials or reconstructs legal actions independently.

During shadow operation, live authority remains Asana-backed. Only confirmed Asana rereads and
periodic reconciliation observations feed one-way into structurally isolated PostgreSQL shadow
state. During PostgreSQL authority, the Asana projector is downstream of the committed outbox.
Shadow records never influence live decisions before cutover, and Asana projection state never
influences workflow legality afterward.

## Stable interface and identifiers

The agent-facing command lifecycle remains `create`, `read`, `start`, `prepare`, `inspect`,
`approve` or `reject`, and `submit`. Existing administrative commands remain narrow. Asana-specific
recovery commands or fields are removed only after their historical and current callers have been
accounted for.

Every authoritative task receives a canonical non-nil Dish UUID at import or creation. Imported
Asana GIDs are preserved only as immutable aliases:

```text
task_external_aliases
  alias_id
  task_id
  source_system            asana | future source
  external_id
  imported_at
  source_batch_id          nullable
```

`(source_system, external_id)` is unique and resolves to exactly one Dish task. Aliases are
provenance and compatibility lookup keys, never task authority. New tasks do not receive an Asana
GID until the downstream projector creates a mirror mapping.

The public `task_gid` field may remain temporarily as a compatibility field, but internally it must
resolve either a Dish UUID or an immutable Asana alias and then operate only on the Dish UUID.
Authoritative responses should expose the Dish UUID, and a later API version should rename the
field to `task_id` without changing identity. Compatibility parsing, OpenAPI, request hashing, and
response identity must make the accepted identifier form explicit rather than treating an arbitrary
string as interchangeable identity.

Dish locations use stable Dish identifiers internally. Imported Asana project and section GIDs are
immutable aliases and provenance, never routing authority. Stage A has one deliberate compatibility
exception: the existing title/body document grammar continues to contain the historical/current
Asana destination section `name — numeric_gid` pair because current validators and clients require
it. Import and every Stage A write resolve that pair through an immutable location alias to the
Dish `location_id`; workflow legality and routing use the Dish location. Stage B structured content
stores only the Dish location identifier and removes the external GID from canonical content.

## Stage B structured command and query contract

The lifecycle command names may remain stable, but content-bearing commands move deliberately to a
versioned structured payload. The exact schema belongs to Honest and the implementation plan. Its
shape should resemble:

```json
{
  "schema_version": "…",
  "title": "…",
  "portions": 4,
  "destination_location_id": "…",
  "ingredients": [
    {
      "name": "…",
      "quantity": {
        "kind": "exact",
        "value": "200",
        "unit_id": "gram"
      },
      "preparation": "…",
      "purpose": "…"
    }
  ],
  "steps": [
    {
      "position": 1,
      "instruction": "…",
      "timing": "…",
      "temperature": "…"
    }
  ]
}
```

This example is illustrative, not approval of the final field set. Nutrition, shopping,
quantities, equipment, storage, source, and other current canonical facts should become typed
objects or child collections only after their exact Honest grammar is approved.

Planning briefs also receive a versioned structured payload matching their distinct Honest schema.
They are not encoded as incomplete dishes. Bare creation remains title-only.

Dish canonicalizes the structured value deterministically and hashes that representation for
version identity. Object-key order, presentation whitespace, generated Markdown, and the current
name of a referenced location do not change identity. Array order is significant only for fields
whose domain semantics require order.

Before a structured schema is approved, it must define exact domain representations for decimals,
fractions, ranges, approximate and optional quantities, counted items, “to taste,” units,
temperatures, times, and sensory stop conditions. Identity-bearing quantities must not use binary
floating point. Use explicit typed objects containing exact decimal strings, rational components,
ranges, or other approved lossless forms.

The schema and canonicalizer must also define Unicode normalization, line endings, prose whitespace,
null versus omitted fields, empty versus absent collections, enum casing, stable unit identifiers,
ordered versus unordered collections, and any stable child identifiers. Every structured version
stores its canonicalization version. A later canonicalizer may create a new version deliberately,
but must never reinterpret or silently change an old identity.

Simple invariants use PostgreSQL data types, foreign keys, uniqueness, checks, and where useful
deferrable or exclusion constraints. Cross-field and release-specific rules remain centralized in
Dish domain validation. Database constraints are not a second independently evolving recipe schema.

During migration, compatibility adapters may translate current title/body commands to or from the
candidate structured model. They are explicit API versions and temporary versioned boundaries, not
a permanent alternate content API or a changed interpretation of existing content fields. New
frontend editing should target the structured contract rather than a full-document Markdown save.

## Storage model

The exact SQL belongs to an approved implementation plan. The conceptual model is:

### `tasks`

One current row per task:

```text
task_id                      canonical non-nil Dish UUID
current_version_id          exact immutable version currently authoritative
current_location_id         controlled Dish location
cooked                      current fact that the dish has actually been cooked
revision                    monotonically increasing optimistic revision
created_at
modified_at
```

`current_version_id`, `current_location_id`, `cooked`, and `revision` change only in the same
transaction as their workflow evidence and audit. Archive is represented by a controlled archive
location/disposition, not by overloading `cooked` and not by a second independently writable task
body flag. Cooked and Archived remain distinct: an archive action does not claim a cook occurred,
and a cooked transition does not silently archive the task. The exact future cook-log model may add
append-only cook records without changing task/version authority.

Tasks are never hard-deleted through an ordinary command. Cooked history, exclusion, or governed
archive preserves their identifiers, versions, execution journals, workflow evidence, and audit
relationships. The archive route follows the direction recorded in `future.md`: it must not move an
active task out from under an open operation.

The current version pointer replaces `task_content_state` as the authoritative current projection.
There must not be two independently writable current-content tables. During database migration,
`task_content_state` may be converted into a compatibility view or retired after every caller uses
the task pointer and its version-specific schema and document-authority provenance has been
migrated.

### Operations, Verification cycles, command journals, and canonical commit boundaries

The current domain already has the right durable attempt identities: a Planning or Research
`operation`, and an exact `verification_cycle` inside a Verification operation. Stage A must evolve
those records rather than create a second independently writable stage-attempt state machine.
Implementation design may add one-to-one extension tables or read views, but `operations` and
`verification_cycles` remain the lifecycle authority.

Each operation records or references the exact canonical version and revision for its current
workflow phase. Each Verification round is identified by its exact cycle and reviewed version.
Existing owner/agent/run lineage remains immutable. The new or evolved command journal records
the service-visible progress through those attempts:

```text
operation_command_journal
  entry_id
  task_id
  operation_id
  verification_cycle_id      nullable; present for cycle-specific commands
  sequence
  request_id
  execution_id
  command_kind
  control_state_before
  control_state_after
  canonical_version_before
  canonical_version_after
  intended_mutation
  result_kind
  compensation_state          none | required | applied | blocked
  recorded_at
```

The exact columns and whether this evolves `operation_executions`/`operation_steps` or adds a narrow
new relation belong to implementation design. The semantics are fixed:

- entries are append-only and service-generated;
- they record every command Dish receives for the operation/cycle and every durable workflow,
  ownership, or external-effect mutation caused by that command;
- they retain enough exact before-state, intended effect, result, and provenance to replay,
  compensate, or reconcile a committed intermediate command after failure;
- there is no Stage A `checkpoint` or draft-journal command, and no requirement to store an agent's
  unpublished notes or partial candidate;
- the final complete candidate first enters Dish through the existing complete `prepare` or
  Verification-decision payload.

Planning and Research `start` create/bind the operation but do not change canonical task content. A
pre-construction Research hold records durable workflow control state but still does not change
canonical content. Verification `start` and `inspect` bind the exact review subject and append review
evidence but do not change canonical content.

The governed content boundaries are:

- completed Planning `prepare` commits the final Planning document and handoff;
- completed Research `prepare` commits the final candidate and handoff or governed non-material
  completion;
- one complete Verification decision commits that round's exact outcome, including a corrected
  candidate, signoff, successor cycle, or hold route as applicable.

Submission is a later governed location/terminal transition after an approved round; it does not
reopen that round's content boundary.

A command boundary that changes canonical content commits the new version, workflow transition,
request result, execution evidence, audit, and projection event atomically. A command before that
boundary may commit control/evidence state, but `tasks.current_version_id` remains the exact
canonical version for that phase or Verification cycle.

Rollback of an abandoned attempt means restoring actionable workflow control to the last committed
canonical boundary while preserving history. Dish does not delete command-journal or audit evidence.
It terminalizes or compensates the abandoned operation/cycle, releases or fences ownership, and
uses Part I's exact fresh-successor operation/cycle rules where current recovery requires them.
Stage A does not introduce same-operation takeover or unfinished-authority transfer.

Complete immutable intermediate versions may be stored inside a completed command only when exact
workflow lineage requires them—for example reviewed → corrected → signed evidence. They are not
partial agent checkpoints and do not become current merely because they exist.

### Structured dish versions

Use a common immutable `task_versions` envelope so historical source-backed or explicitly approved
intermediate document versions can be preserved without pretending they are structured dishes:

```text
version_id
task_id
representation_kind     bare | title_body_document | structured_planning_brief | structured_dish
identity_scheme
canonical_identity
title
source_kind             creation | workflow | import | migration
recorded_at
became_current_at
```

A version's identity scheme is immutable and domain-separates the representation, framing,
normalization, and digest algorithm used to produce `canonical_identity`. Initial schemes should
be explicit values such as `dish-bare-v1`, `dish-title-body-v1`,
`dish-structured-planning-json-v1`, and `dish-structured-dish-json-v1`. A digest is meaningful only
with its scheme; canonicalization version remains additional structured-JSON provenance and does
not replace the cross-representation identity scheme.

A bare version has a title and no body graph. A structured Planning version has one
`planning_brief_versions` row and typed version-owned planning fields defined by Honest. Every
structured representation also has one `structured_versions` row:

```text
version_id
canonical_json
canonicalization_version
schema_version
```

A structured dish version has one corresponding `dish_versions` row:

```text
dish_version_id
version_id
destination_location_id
protocol_release        nullable; present only when a fact of this version
```

The remaining canonical fields also live in typed version-owned tables such as ingredients, steps,
quantities, nutrition, equipment, storage, shopping items, and source references. The final tables
follow the approved Honest schema rather than this document inventing a generic recipe ontology.
Every child row is keyed to one version, has deterministic ordering where order matters, and is
immutable after insertion. Candidate editing creates a complete replacement rather than mutating a
recorded version.

The canonical JSON and typed graph are one consistency-checked representation pair, not two
authorities. Domain validation runs against one in-memory structured value before either is
inserted. The stored canonical JSON is the immutable identity witness and API representation; the
typed rows are query and constraint material belonging to that exact version. Semantic validation
must reconstruct the value from the typed graph and prove byte equality with `canonical_json`,
prove that hashing it produces `task_versions.canonical_identity`, and prove that its title equals
`task_versions.title`. The envelope owns identity and title; the structured row owns the JSON,
canonicalizer version, and structured schema version. None is independently writable.
Disagreement is corruption and blocks readiness.

A transaction may create and validate a complete structured candidate without making it current.
Only the governed commit transaction for the complete Planning stage, Research stage, or Verification
round advances `tasks.current_version_id` and records the required workflow lineage atomically.
Partial, inconsistent, or incomplete-attempt version graphs never become current.

An immutable version may become current at most once. `became_current_at` is set in the same
transaction as its one pointer advancement and never changes. Revert, restoration of old content,
clone, or canonicalizer migration creates a new version with explicit source/predecessor lineage,
even when its canonical content equals an older version. It never reactivates the old row or
inherits that row's Verification merely because the identity matches. Whole-system database restore
remains an operational rollback to a compatible historical state, not a normal version
reactivation.

Content becoming current proves version authority only. It does not imply Verification signoff,
which remains separate evidence bound to the exact version occurrence and identity.

Existing `content_versions` and `task_content_state` remain historical migration evidence. Their
title, notes, identity, schema, and confirmation timestamps are preserved and mapped into the new
version/import provenance model before either table is retired or projected as a compatibility
view. They do not remain a second writable current-content authority.

### Asana observations, import origins, source documents, and destination evidence

Shadow observations, reconciliation evidence, and authoritative cutover origins have distinct
meanings. Before cutover these observation tables live only in the separate shadow database. The
cutover importer copies the two frozen, closed cutover batches and their witnesses into isolated
import staging, then preserves the approved batch as provenance while creating fresh authoritative
rows. Observation and source-document tables have no foreign-key path into workflow authority;
`task_import_origins` is the only audited bridge from an approved cutover observation to a new
authoritative task. Record each coordinated Asana enumeration as a batch:

```text
asana_observation_batches
  batch_id
  batch_sequence
  purpose                    shadow | reconciliation | cutover
  started_at
  completed_at
  corpus_manifest_identity
  complete

asana_task_observations
  observation_id
  batch_id
  source_task_gid
  source_project_gid
  content_identity_scheme
  content_identity
  current_section_gid
  current_section_name
  source_completed
  source_modified_at
  observed_at

asana_section_observations
  batch_id
  source_project_gid
  source_section_gid
  source_section_name
  display_order
```

A batch has a durable monotonic sequence assigned at creation; UUID equality or ordering is never
used to interpret historical validity. A batch is complete only when its task set, exact content
identity pairs, placements, source completion states, source-document witnesses, and section
registry are captured and its corpus manifest is deterministically hashed.

The manifest hashes the complete canonical corpus relation, not a multiset of document digests. Each
task row includes at least source project GID, source task GID, qualified content identity, current
section GID and name, source completion state, and the matching source-document witness identity.
Each section row includes source project GID, section GID, section name, and display order. Canonical
ordering is explicit. Every digest is qualified by its identity scheme. Batch-local identifiers and
observation timestamps are excluded so two independent frozen enumerations of the same corpus
produce the same manifest identity; swapping documents between tasks or changing placement cannot
produce the same manifest.
Batch completion additionally requires exactly one source-document witness for every in-scope task
observation, matching observation/document linkage, scheme, and identity, every expected section
observation, and no duplicate task or section GIDs. A source document linked into the batch
manifest for an observation outside that batch also invalidates completeness. Database constraints
enforce the local cardinality and linkage rules, while the semantic validator computes corpus
closure before the monotonic `complete` state may be recorded.

Batch completion is irreversible lifecycle evidence. Before the one permitted transition from
incomplete to complete, the validator proves closure and fixes `completed_at` and the manifest
identity. A completed batch cannot be reopened, have observations or witnesses added or changed,
or have its manifest replaced. Approval is separate append-only evidence:

```text
cutover_authority_approvals
  approval_id
  asana_batch_id              unique
  matching_asana_batch_id
  legacy_snapshot_id          unique
  asana_manifest_identity
  approved_by
  approved_at
```

Only a complete `cutover` batch may be approved. Its matching batch must be an earlier complete
`cutover` batch with the same manifest identity and exact closed corpus facts. The authority
approval repeats the immutable Asana manifest identity, names the complete frozen legacy Dish
snapshot, cannot be changed or cleared, and is rejected if either batch, manifest, or legacy
snapshot does not match. Repeated shadow and reconciliation rows remain comparison evidence. They cannot become task origin authority
merely because they are newest or individually complete.

Cutover also binds one exact frozen legacy Dish workflow snapshot. The snapshot manifest records at
least the SQLite database digest, schema migration level, Dish/Honest release identities, semantic
validation result, authoritative table counts, open/pending/uncertain state summary, and creation
time:

```text
legacy_dish_snapshot_manifests
  snapshot_id
  sqlite_sha256
  schema_migration_level
  dish_release_identity
  honest_release_identity
  semantic_validation_status
  table_count_manifest
  open_state_manifest
  created_at
  complete
```

A cutover authority approval is valid only when it names both matching complete Asana cutover
batches and one complete frozen legacy Dish snapshot. The importer must prove exact links between
imported workflow facts and the approved Asana observations/source documents. The Asana corpus
alone cannot establish Verification, replay, lease, abandonment, or operation authority.

Only a cutover authority approval that binds the two matching complete Asana batches and the
complete legacy Dish snapshot may establish imported task origins:

```text
task_import_origins
  task_id
  cutover_approval_id
  source_observation_id
  resolved_location_id
  placement_alias_id
  selected_destination_resolution_id   nullable
  imported_at
```

The origin links the authoritative imported task to exactly one observation in the authority
approval's selected Asana batch and records the location resolution used for its initial projection.
Imported placement and source completion state are origin facts mapped explicitly to Dish cooked
state; they do not fabricate
Dish transitions for state that predates DB authority. Quarantine records may cite observations,
but only separately audited promotion from an approved cutover authority may create a task origin.

Every imported legacy workflow row is also traceable to the approved frozen snapshot and exact
source row identity through one migration run:

```text
legacy_workflow_import_runs
  migration_run_id
  cutover_approval_id
  legacy_snapshot_id
  importer_release_identity
  started_at
  completed_at

legacy_workflow_row_origins
  migration_run_id
  target_relation
  target_row_id
  source_relation
  source_row_identity
```

The importer proves that operation, cycle, request, lease, effect, abandonment, and audit references
resolve to the same imported Dish task/version occurrences established from the approved Asana
batch. Missing or contradictory links are reconciled or quarantined; they are never inferred.

Preserve every observed source document exactly and link it to its observation:

```text
source_document_id
source_observation_id          unique
source_task_gid
source_title
source_body
source_identity_scheme
source_identity
recorded_at
```

The source task GID must equal its observation's task GID, and the source-document identity must
equal that observation's identity under the same scheme. `source_title` and `source_body` are the
exact logical Unicode-string witness returned by Asana. The named identity scheme defines their
UTF-8 encoding, field framing, normalization policy, and digest algorithm; implementations may
additionally retain the exact framed preimage as a BLOB, but must not call two unspecified database
text values a byte witness. The observation carries the qualified identity into its corpus manifest.

Parsing an embedded destination produces append-only resolution evidence:

```text
source_document_destination_resolutions
  resolution_id
  source_document_id
  embedded_identifier
  embedded_name
  resolved_location_id       nullable
  matched_alias_id           nullable
  parser_version
  resolution_status
  recorded_at
```

Failed parses and unresolved or conflicting matches remain evidence alongside later parser results;
reparsing never updates an earlier row. The import origin or migration candidate explicitly names
the selected resolution used for migration. That selection must be successful and belong to the
source document linked through the same import evidence. A selected successful embedded pair
resolves against an appropriate immutable location alias. It is never compared to the task's
current placement: a task may correctly be in Verification Queue while its document names a
destination such as Main Dishes.

A structured version derived from a source document records that relationship plus orthogonal
classification facts:

```text
document_kind              bare | planning_brief | canonical | unknown
validation_status          unvalidated | valid | invalid | unsupported_schema
declared_schema_version    nullable version claimed by the source document
validated_schema_version   nullable schema against which validation actually succeeded
```

The source document remains immutable evidence even when parsing succeeds. Recognizably canonical
but unsupported, malformed, partially structured, or unknown history can remain an exact snapshot
without inventing structured fields or schema validity. Parsing and validation results are
repeatable derived evidence; reparsing never silently replaces the imported source or a current
structured version.

### Document-compatible DB authority

Stage A makes an immutable title/body document version authoritative in PostgreSQL. This is the
approved independently deployable authority migration and a legitimate production state. Structured
data remains the Stage B target representation after Stage A has been battle-tested.

Such a version uses a one-to-one `title_body_document_versions` row containing the exact body and
applicable schema provenance. Stage A does not store unfinished agent drafts. A complete
`prepare` or Verification-decision command may create the immutable version or versions required by
its exact workflow lineage, but only the complete governed stage or Verification-round boundary
advances the canonical pointer. Imported source documents are never overwritten.

The document-compatible store uses the same task pointer, transaction, replay, workflow, location,
cooked/archive, audit, command-journal, and rollback contracts. It preserves source documents and
may attach non-authoritative Stage B structured conversion candidates. Its command API must not
force a future frontend to depend on raw database rows or prevent later versioned structured
payloads.

During Stage A, the existing embedded destination section `name — numeric_gid` remains part of the
title/body document contract. It is resolved through an immutable Asana location alias to the
internal Dish location ID. That external identifier remains compatibility content only; it does not
become PostgreSQL routing authority.

Once structured parity is proven, a governed database migration creates complete structured
versions, verifies their deterministic identities and renderings, and advances eligible task
pointers. This is not a backend-authority cutover because both representations are inside the same
service, but it is an authoritative content-representation migration with explicit identity,
lineage, and Verification consequences.

### Verification across representation migration

Verification follows the workflow-wide version-occurrence binding below. Every inspection, review,
correction, and signoff subject records the exact `task_id`, `version_id`, `identity_scheme`, and
`canonical_identity`. Fields such as `inspection_subject_version_id`, `reviewed_version_id`,
`corrected_version_id`, and `signed_version_id` reference same-task `task_versions` rows. Semantic
validation proves that each evidence record's stored scheme and identity equal the referenced
version's scheme and identity. Two versions with the same scheme and identity are still different
authority occurrences and never share Verification implicitly.

Existing Verification signs the exact imported title/body document version occurrence and identity.
Successful parsing or byte-equal compatibility rendering does not automatically sign a structured
version. Renderer equality is useful evidence, but may omit authoritative distinctions unless the
approved migration contract proves otherwise.

The default gradual route is:

- import the signed title/body document version as current;
- attach any structured conversion only as a non-authoritative candidate;
- bind imported signoff to that exact imported version and identity;
- let the next governed workflow create a structured version and obtain whatever new Verification
  that workflow requires.

During Stage B, a corpus-wide or progressive representation migration may use re-Verification or a
separately approved privileged migration-equivalence attestation. Any attestation is append-only and records at least:

```text
source_version_id
structured_version_id
parser_version
renderer_version
source_identity_scheme
source_identity
rendered_identity_scheme
rendered_identity
semantic_validation_result
migration_run_id
approved_by
approved_at
```

The attestation contract must name exactly which workflow and signoff facts transfer. It may not
rewrite an old Verification cycle to point at a new identity. Every active signed or correction-
lineage version must have an explicit disposition—remain current as a title/body document, be
reverified, or use an approved attestation—before a structured pointer can become authoritative.

A canonicalizer upgrade follows the same discipline even when human meaning is intended to remain
unchanged. It preserves the old canonical JSON and signed identity, creates a new single-use
version with explicit lineage, and requires re-Verification or an approved equivalence attestation
before signoff-dependent workflow facts transfer. Startup and schema migration never silently
recanonicalize historical rows. Stored canonical JSON remains readable without the current
canonicalizer; supported current representations retain versioned decoding and reconstruction
rules, while unsupported historical representations remain inspectable without being asserted
ready for current mutation.

### `task_locations`

Replace the Asana section registry with controlled Dish locations:

```text
location_id                 stable Dish identifier
current_name                unique current display name
role                        research_queue | verification_queue | destination | cooked_history | archive | excluded
active                      whether new routing may target it
display_order
```

External identifiers and historical names live in a separate immutable alias relation:

```text
task_location_aliases
  alias_id
  location_id
  source_system
  external_project_id
  external_section_id
  external_name
  valid_from_batch_id

task_location_alias_retirements
  alias_id
  final_batch_id
  reason
  retired_at
```

A location may therefore have multiple historical Asana aliases, while each alias resolves to
exactly one Dish location for its declared batch interval. Alias rows remain immutable; optional
retirement is separate append-only evidence, and interval interpretation uses durable batch
sequence rather than UUID ordering. Retirement must reference a batch at or after the alias's
starting batch and may be recorded at most once. Aliases are provenance and compatibility
evidence, not routing authority. `source_document_destination_resolutions.matched_alias_id` records
the exact alias used for an embedded destination; `task_import_origins.placement_alias_id` records
the independently resolved current placement.

Exactly one active Research Queue and Verification Queue are required. Sourcing and Reference
import as excluded locations. The governed Archived section imports as an archive location. A Cooked
section or Cooking History project/section imports as a `cooked_history` location when included.
Other approved Cooking sections import as destinations. Cooked state is still an explicit Dish fact
and is not inferred solely from placement. Removing or repurposing a referenced location is
prohibited; retire it instead.

The destination resolver is version-aware:

- a structured dish version stores the authoritative Dish `destination_location_id`;
- the renderer may show that location's `current_name`, but the display name is not part of
  structured identity;
- an imported pre-cutover source document may contain the exact immutable
  Asana section GID mapped by a version-appropriate location alias;
- for that source document, the embedded name and identifier are historical evidence resolved
  through an explicitly selected `source_document_destination_resolutions` row, independently of
  the task's imported placement;
- the matched alias resolves parsing and migration to the Dish `location_id` but is never itself
  authority and is never emitted into structured JSON;
- the next governed structured rewrite records only the Dish identifier.

Location names may therefore change without invalidating structured signoff or immutable source
history. An exact historical rendering uses the preserved source document or a stored rendering
artifact; a current human-readable rendering may show the current name. This preserves imported
content identity and signoff without retaining a writable Asana namespace.

### Location history

Add a dedicated append-only `task_location_transitions` table. Each committed transition records
task, operation when applicable, old and new locations, purpose, request/execution provenance, and
timestamp.

Historical Asana `movement_attempts` remain immutable evidence and are never reused for local
transitions. For database-native transitions, there is no `started`, `not_applied`, or `uncertain`
network outcome: a committed transaction contains one transition and a rolled-back transaction
contains none. Local transitions must not manufacture terminal “confirmed attempts” for work that
never crossed an external-effect boundary.

### Cooked history

Cooked and Archived are separate domain outcomes. Archive is expressed through a governed archive
location/disposition and its location transition history. Cooking is expressed through a distinct
current projection and append-only history:

```text
task_cooked_transitions
  transition_id
  task_id
  old_cooked
  new_cooked
  purpose
  actor
  request_id
  reason
  occurred_at
```

The mutable `tasks.cooked` value is the current projection, not historical proof. Marking a dish
cooked, clearing that projection through an approved reopen route, or applying another governed
cooked-state change appends
one transition and commits it with the projection, governed audit, lifecycle evidence, and canonical
request result. A governed archive transition never sets `cooked`; a cooked transition never
silently archives. A future `log-cook` command may append richer cook records without allowing a
cooking agent to mutate the signed task body.

A clone is a new task with explicit source lineage; it does not rewrite the original task's cooked
or archive history.

### Content transition evidence

Historical Asana `write_attempts` also remain immutable. Database-native content changes append a
new immutable version graph and all required lineage in one transaction. They do not insert
database-backed `write_attempts`.

Every version ancestry edge uses one common append-only relation:

```text
task_version_lineage
  predecessor_version_id
  successor_version_id
  relationship
  operation_id             nullable
  migration_run_id         nullable
  recorded_at
```

Allowed relationships include `workflow_revision`, `small_correction`,
`non_material_checkin`, `revert`, `clone`, `representation_migration`, and
`canonicalizer_migration`. Both versions must belong to the same task except for an explicit
`clone` edge, whose source and new task ownership are recorded and validated. A successor version
has the lineage required by its creation purpose before it can become current. This relation proves
ancestry only; specialized Verification, non-material approval, or migration-attestation evidence
still grants the applicable authority.

Do not discard the intent, purpose, and reviewed-to-corrected-to-signed relationships currently
carried by historical write records. Give those facts domain-native columns or explicit lineage
relationships on task/dish versions and workflow evidence before migrating every semantic validator
and historical query that consumes them. Keep the external-effect attempt tables readable for
pre-cutover history, but do not carry their recovery ontology into the local authority.

### Workflow-wide version-occurrence binding

Every durable workflow fact that names task content binds one exact same-task `task_versions`
occurrence. The version ID identifies the authority occurrence; its repeated identity scheme and
canonical identity prove the exact bytes. Scheme and digest never substitute for the occurrence,
and equality with another version's identity never permits an operation to continue against that
other row.

The exact schema plan must apply this rule to every content-bearing relation, including:

- an operation's expected starting version, scheme, and identity;
- operation steps and actor facts that name a subject or candidate;
- holds, resumes, pending content intent, and recovery baselines;
- material-classification subjects and candidate lineage;
- Small-correction reviewed, corrected, and signed occurrences;
- non-material check-in predecessors, candidates, and inherited cycle;
- submission baselines and destination-ready versions;
- migration, reopen, revert, restoration, and clone evidence.

Representative fields include:

```text
operations.expected_version_id
operations.expected_identity_scheme
operations.expected_identity
operation_steps.subject_version_id
operation_actor_facts.candidate_version_id
holds.held_version_id
```

Each content-bearing record repeats the referenced version's identity scheme and identity where
the evidence must remain independently explainable. Composite foreign keys or semantic validation
enforce same-task ownership and exact agreement. A missing version occurrence is not treated as a
wildcard, including for migrated historical work; it is reconciled, preserved as explicitly
limited history, or quarantined.

An accepted non-material change does not independently sign its candidate version. It preserves
the original approved cycle only through explicit append-only occurrence lineage:

```text
non_material_signoff_lineage
  operation_id
  predecessor_version_id
  candidate_version_id
  source_cycle_id
  recorded_at
```

The predecessor and candidate are distinct successive same-task occurrences, the operation names
the exact approved cycle it inherited, and the candidate's general ancestry also contains the
matching `non_material_checkin` edge. Each later check-in links its exact predecessor occurrence to
its exact candidate occurrence. Transitive resolution follows version IDs through these rows and
never searches for another version with the same candidate identity.

### Rendered views and projection outbox

For a structured version, Markdown, plain text, and Asana notes are deterministic renderings. They
are not parsed back into authority after cutover. A document-compatible current version is read
directly as its exact authoritative title/body rather than pretending that it is a structured
rendering. Where exact historical reproduction matters, store the renderer version, rendering
identity, and generated artifact or preserve the exact source document.

Each PostgreSQL mutation that affects the Asana view appends an outbox item in the same authoritative
transaction:

```text
projection_outbox
  event_id
  task_id
  task_revision
  event_kind
  payload_identity
  status                     pending | applied | failed
  attempt_count
  next_attempt_at
  created_at
  applied_at                 nullable

asana_projection_mappings
  task_id
  asana_task_gid
  last_applied_revision
  last_attempted_revision
  projection_status
  last_error                 nullable
```

A separate worker renders and applies committed revisions. Required behavior:

- processing is ordered per task revision;
- workers lock or otherwise serialize one task mapping while applying an event;
- an event whose revision is at or below `last_applied_revision` is an idempotent stale no-op;
- projection payloads describe complete authoritative state for their revision, so a newer revision
  may supersede an unapplied older update after the task mapping exists; mapping creation and any
  non-supersedable effect remain explicitly ordered;
- mirror creation uses a stable correlation marker and reconciliation lookup so a lost response
  cannot create an unbounded sequence of duplicate Asana tasks;
- failures remain retryable and visible without changing PostgreSQL authority.

Projection failure never blocks, rolls back, or reclassifies the PostgreSQL mutation. Command and
read results expose the committed task revision and projection state (`pending`, `current`, or
`failed`) so a stale Asana view is not mistaken for authority. Out-of-band Asana edits are detected
and overwritten or flagged; they are never ingested as new authority. Any duplicate is explicitly a
mirror artifact and must not appear as a second Dish task.

### Audit and read projections

The existing `audit_events` table remains the append-only audit authority. Do not add a generic
`task_events` stream unless a concrete query cannot be served from task/dish versions, location
transitions, operations, Verification records, and audit events.

Search indexes, denormalized list views, or full-text indexes are disposable read projections. They
may be rebuilt from authoritative rows and must never decide workflow legality.

### Pointer, representation, and quarantine integrity

The final schema and semantic validator must enforce:

- `tasks.current_version_id` references a version for the same task;
- every version has exactly one complete representation matching `representation_kind`;
- every version has an allowed immutable identity scheme matching its representation, and its
  canonical identity validates under that scheme;
- every structured representation has exactly one root and only same-version child rows;
- every workflow content subject, including Verification, references an exact version occurrence
  owned by the same task and repeats that version's exact scheme and canonical identity where
  required;
- every successor version has purpose-appropriate append-only ancestry, and non-material signoff
  inheritance resolves only through exact predecessor/candidate occurrences;
- current versions are complete, valid for their claimed authority, and not shadow candidates;
- completed observation batches satisfy source-document and section closure with no duplicate
  external task or section identifiers, matching qualified identities, and monotonic completion;
- cutover approval is append-only and names one matching earlier complete Asana batch plus the exact
  complete frozen legacy Dish snapshot;
- quarantined imports cannot be promoted or resolved through ordinary task commands;
- location, cooked, and archive projections match the import origin plus latest post-import transitions;
- one committed current-state mutation advances the task revision exactly once.

Use composite foreign keys, uniqueness constraints, checks, and declarative PostgreSQL
constraints wherever possible. Use triggers only for invariants that cannot be expressed safely
otherwise. Semantic startup validation covers release-specific or cross-table rules that the
database cannot prove alone.

Quarantine remains outside authoritative `tasks` and ordinary service reads. Promotion is a
separately audited import action that inserts a proven task and its origin state; it is not a
status flip on an otherwise authoritative task.

### Request execution ownership

Separate the immutable replay envelope from its expiring executor claim. Every mutation request,
including an operation-scoped command, permanently records:

```text
service_requests
  request_id
  owner_id
  client_run_id
  command
  request_contract_version
  payload_identity
  task_id                        nullable
  operation_id                   nullable
  adapter_version                nullable
  structured_schema_version      nullable
  canonicalization_version       nullable
  reserved_task_id               nullable deterministic output identity
  canonical_candidate            nullable immutable derived payload
  status
  result
  created_at
  completed_at                   nullable
```

The authenticated principal/owner, run provenance, command, canonical argument identity, contract
pins, and reserved outputs are immutable after reservation. Task/operation bindings append only when
proved. Status and result advance monotonically. All survive completion and explain exact replay
permanently.

Request-scoped and operation-scoped work use PostgreSQL executor claims with the same core fencing
shape rather than host/PID liveness:

```text
execution_claims
  claim_id
  request_id
  operation_id                  nullable
  owner_token
  claim_generation
  claimed_at
  expires_at
  completed_at                  nullable
```

Only an atomic compare-and-swap may acquire an unowned or expired claim. A live foreign claim
returns one stable non-terminal `REQUEST_IN_PROGRESS` response rather than executing. That response
is not the canonical result, requires the same `request_id` for replay, never exposes the foreign
owner token, and states whether retry must wait for claim completion, expiry, or named recovery.

Recovery increments the generation and issues a new owner token. Every effect transaction rechecks
the exact token and generation under lock; a displaced executor cannot commit. Completion of the
effect and storage of the canonical result retire the claim atomically without changing the
request's immutable identity.

Stage A may initially deploy one active Dish mutation-service instance, but correctness must not
depend on hostname/PID liveness. If single-instance operation is required operationally, enforce it
with a PostgreSQL advisory/application lock and health reporting. Per-request and per-operation
claims remain database-fenced so later worker or service topology changes do not require a replay
redesign.

Request replay must never reinterpret a compatibility payload under newly deployed parsing or
canonicalization code. Reservation persists the exact request contract and version pins. For an
adapter-based request, prefer persisting the already-derived canonical candidate or its immutable
identity-bearing representation before execution ownership; otherwise recovery must retain the
exact adapter/parser implementation named by the request. Deployment normally requires no pending
requests, but quiescence is an operational gate rather than a substitute for a correct durable
recovery contract.

## Transaction contract

Request reservation and execution ownership remain durable admission steps because they must
survive a dead executor. They may commit before the task mutation, but they grant no task effect. A
pending `service_requests` row does not by itself authorize an executor to run the request.

Operation-scoped and task/request-scoped mutations both use the PostgreSQL execution-claim contract
above. Operation-scoped commands additionally validate the exact operation, Verification cycle when
applicable, command-journal context, and service lease. Task/request-scoped commands such as
`create`, `start`, Marco's cooked/completion command, permitted bare-task title changes, and
comparable lifecycle interventions lock and reread the task or reserved task identity inside the
PostgreSQL transaction.

Where useful, reservation stores deterministic output identity before execution. In particular,
`create` reserves its new task UUID on the replay-bound request. Exact concurrent replays can observe
or recover the same request, but cannot both create the task.

After admission, every database-native command has one effect transaction:

1. authenticate and validate the request envelope;
2. reserve or match the replay-bound service request and any deterministic output identifiers;
3. acquire the exact execution claim and required operation/lease ownership;
4. begin the PostgreSQL transaction;
5. lock and reread the pending request, claim token/generation, task, operation, and
   Verification-cycle rows, exact version occurrence, location, and other command preconditions;
6. assert the action through `CurrentWorkflowService` and domain policy;
7. append the command-journal entry and every required workflow, Verification, lineage, ownership,
   audit, and transition fact;
8. when this command is a governed content boundary, append the complete version graph and advance
   the canonical pointer exactly once; otherwise prove the pointer remains unchanged;
9. append any ordered projection-outbox item for the new task revision;
10. finalize the operation/cycle state, execution claim, service lease, and canonical request result;
11. build a fresh authoritative post-finalization snapshot and derive principal-filtered
    `allowed_actions` and ownership guidance;
12. commit once.

A crash before commit leaves none of that command's effect facts committed. A crash or response loss
afterward returns the stored result on exact replay. Commands from earlier points in the same stage
may already be committed; the operation/cycle command journal identifies them and drives idempotent
compensation or Part I abandonment recovery without deleting history.

An interruption after admission but before the effect transaction may leave a pending request or
expired claim but no task change. Recovery reacquires the exact claim generation, rereads the request
and task, and never infers a task effect from the admission record.

Expected current version occurrence, identity scheme, canonical identity, and location remain the
semantic concurrency check. Every workflow continuation also revalidates the exact occurrences
recorded by its operation, steps, actors, holds, classification, signoff lineage, and submission
baseline. The monotonic task `revision` is an additional compare-and-swap guard and query aid, not a
replacement for exact content, placement, signoff, or actor evidence.

### Audit and read boundaries

Governed audit facts and transition evidence required to prove a mutation are written inside its
effect transaction. The canonical request result is atomic with that mutation.

Invocation and transport audit remains a success-preserving, repairable boundary after the canonical
result. Its failure must not roll back or turn a committed workflow success into a retry signal.
Moving that audit into the effect transaction would be a separate contract change.

An authoritative read that uses multiple SQL statements runs in one read-only `REPEATABLE READ`
transaction or uses a single composed query that proves the same logical task revision. Reads never
update leases or disposable projections as a side effect.

PostgreSQL backup, point-in-time recovery, snapshot retention, and restore are operational
boundaries rather than ordinary task transactions. The existing SQLite file-backup implementation
does not carry over unchanged. Future notifications or exports also require their own classified
effect protocol; moving task storage into PostgreSQL does not justify weakening non-database effect
handling.

## Workflow and recovery changes

The guarded state machine and independent Verification do not change. In particular:

- one active operation per task remains enforced;
- actor and verifier run lineage remains durable;
- every content-bearing workflow fact remains bound to an exact same-task version occurrence and
  its identity, including operation baselines, steps, actors, holds, classification, correction,
  non-material check-in, migration, reopen, submission, inspection, review, and signoff;
- Small-correction lineage remains reviewed → corrected → signed;
- non-material signoff inheritance follows explicit predecessor/candidate version occurrences back
  to the source approved cycle and never follows identity equality;
- allowed actions remain derived once from the authoritative snapshot;
- Marco-only holds and interventions remain private and narrow.

Stage A preserves Part I abandonment semantics. A permanently lost Planning or Research attempt is
terminalized and, at an eligible clean frontier, recovered through the exact fresh successor
operation with its immutable baseline. A lost Verification run uses the exact fresh successor
operation/cycle rules. The task-level abandonment fence, old-run exclusion, crash convergence, and
manual reconciliation behavior remain until a separate post-Stage-A design explicitly replaces
them. Operation/cycle command journals supplement these records; they do not authorize
same-operation takeover, session replacement, or transfer of unpublished work.

Normal PostgreSQL-native content, placement, cooked-state, archive, and creation mutations no longer
return `BACKEND_UNCERTAIN`. A database availability or lock failure before commit is safe to retry
under the exact request identity rules. Semantic constraint failures remain fail-closed.

If a connection failure makes commit acknowledgement indeterminate, the service stops mutation
readiness for the affected path, reconnects, and inspects the replay record and task revision before
advising retry. It must not report rollback merely because PostgreSQL normally gives atomic
transactions.

Recovery distinguishes:

- historical unresolved Asana effects preserved from before cutover;
- PostgreSQL command transactions that committed or rolled back;
- previously committed intermediate stage-control commands that require idempotent compensation or
  Part I abandonment;
- PostgreSQL backup, point-in-time recovery, and restore operations;
- workflow holds and expired leases, which remain real regardless of backend.

Do not keep generic write/movement recovery executable for new PostgreSQL-native transitions merely
because historical rows use it. Historical unresolved effects must be resolved or quarantined
before cutover; historical terminal evidence stays readable.

Planning reopen becomes an ordinary transactional cooked-state and workflow change. It
remains Marco-only because that is a lifecycle authority decision, not because the update is
technically uncertain.

## Human interface

### Stage A interface

Asana remains Marco's human-facing interface throughout Stage A:

- before cutover it is authoritative and writable under the existing governed model;
- after cutover it is a read-only asynchronous projection of PostgreSQL authority;
- direct Asana edits after cutover are drift, never commands or imported authority;
- projection revision and state must be available through Dish reads/status so stale display is not
  mistaken for current authority.

The Stage A mutation surface is intentionally progressive. Engineering implements the current Dish
commands plus the smallest additional governed actions discovered during shadow use, with archive a
known real need. The authority flip occurs only when Marco judges that the actions he actually needs
are available. Missing later mutations are added as ordinary application commands and migrations;
this is not permission to expose generic row CRUD or a full-document save bypass.

### Future private interface

A separate private list/search/read/history/action interface may be built later, including during
Stage B development. It is not a Stage A implementation or cutover prerequisite. When built, it
must read through bounded query APIs and mutate only through named Dish commands. It must not access
raw PostgreSQL, impersonate an agent, invent run lineage, or expose private admin routes through the
Action listener.

A future structured editor submits complete versioned candidates with exact task revision and
expected-version preconditions. A cooking planner, scaling, prioritization, and rich cook-log editing
remain separately designed features rather than database-cutover requirements.

## Import and cutover

### Phase 1: Asana-authoritative PostgreSQL shadow

Keep all live reads, writes, workflow decisions, and human actions Asana-authoritative. After each
confirmed Asana reread, mirror the observed state one-way into structurally isolated PostgreSQL
shadow storage. Periodic reconciliation also captures direct human Asana changes and missed mirror
delivery.

The shadow may use the eventual PostgreSQL platform, but its schema and credentials must not provide
a path into authoritative task, operation, or Verification rows. It contains periodic and
command-triggered `asana_observation_batches`, `asana_task_observations`, source witnesses, and any
`shadow_*` candidate graph. Store observations with purpose `shadow` or `reconciliation`, including:

- the exact title/body, qualified content identity, section, source completion state, archive
  placement where applicable, and source timestamps;
- the corresponding operation/request when the observation followed a Dish command;
- when Stage B development begins, an attempted structured parse and its validation/classification
  evidence, normalized candidate rows, deterministic candidate JSON identity, and compatibility
  rendering comparison.

Shadow persistence failure is logged with enough request/task identity for asynchronous retry and
reconciliation. It never blocks, rolls back, delays, or reinterprets a confirmed Asana mutation.
Shadow rows are never read to authorize live work, written back to Asana, or treated as authoritative
merely because they exist. Incomplete batches remain diagnostic evidence but cannot claim corpus
completeness.

The title/body shadow battle-tests import, identity, reconciliation, query behavior, and sustained
parallel persistence. It does not prove PostgreSQL-native execution ownership, transaction crash
atomicity, or recovery; those require direct fault injection and rehearsal.

At cutover, the system must reconcile the final frozen Asana state with the PostgreSQL candidate
state and create or activate authoritative records only from approved complete evidence. It must not
silently relabel an incomplete or contradictory shadow row as authority. The expected operational
flip is small because the service and schema have already run in shadow, but the final authority
proof remains explicit.

### Phase 2: shadow execution

For each governed production command, apply the command intent to the separate shadow candidate
database or reducer and compare its predicted version, workflow state, allowed actions, and
location with the eventual confirmed Asana result. When testing structured representation, also
compare structured identity and rendering. Candidate output remains non-authoritative and cannot
affect the production response.

Human out-of-band Asana changes are imported observations, not fabricated Dish commands. Repeated
observations identify the narrow human commands that must exist before cutover.

Exercise concurrency, request claims, transaction interruption, restart, and recovery directly
against copied candidate databases. Long runtime supplies representative inputs, but elapsed shadow
time alone is not proof of transactional safety.

### Phase 3: Stage A battle-hardening readiness

Before the authority flip, exercise PostgreSQL queries, transaction ownership, exact replay,
operation/cycle command journals, Part I abandonment, complete-candidate handling, projection
outbox behavior, backup/restore, and all current workflow routes against copied or shadow-derived data. Asana remains
the only production authority during this phase, and a PostgreSQL shadow failure never blocks an
Asana workflow.

There is no fixed duration or numerical pass gate. Readiness evidence includes observed mismatch and
failure rates, successful asynchronous repair, diagnosis and recovery burden, direct crash and
concurrency fault tests, replay convergence, projection ordering, backup/PITR/restore rehearsal, and
coverage of the real human mutations Marco has needed during the shadow period. Marco authorizes the
flip using that evidence near cutover.

A new private frontend is not part of this phase. Equivalent narrow Dish CLI/admin commands cover
the human mutations actually required before Asana becomes read-only; additional mutations remain
ordinary application work afterward.

### Fixed cutover target

The first production cutover target is **document-compatible PostgreSQL authority**:

- imported and DB-native canonical content remains exact title/body document versions;
- PostgreSQL owns the canonical task pointer, locations, distinct cooked/archive state, command
  journals, workflow evidence, replay, and mutation transactions;
- Asana becomes the downstream read-only interface;
- structured candidates may be shadowed, but cannot delay or silently alter Stage A authority.

After a separate battle-hardening period and explicit approval, Stage B performs the governed
representation migration inside PostgreSQL. It must resolve structured schema, rendering, editing,
identity, lineage, and Verification treatment before any structured version becomes canonical.

### Import classes

Import classes are:

1. **Active or incomplete governed tasks:** reconcile exact current identity and location against
   `task_content_state`, content versions, operation history, and applicable signoff. Stage A
   preserves the authoritative title/body document without inventing structured validity. Open or
   unresolved mutation authority is completed, abandoned under the current contract, or
   quarantined before import; it is not migrated by inference.
2. **Tasks connected to unresolved or open evidence:** resolve the evidence or quarantine the task
   before cutover. No unresolved external effect becomes a local committed fact by inference.
3. **Cooked/completed historical tasks:** import the exact source document and selected cutover
   observation as read-only history with explicit provenance. Do not assert current-schema
   conformance, complete workflow evidence, signoff, document kind, or validation success that the
   source does not prove. Preserve the qualified source identity, source modification time, and
   import time.
   Apply migration and current validation only if a later named command reopens or clones the task.
4. **Excluded Sourcing and Reference records:** import only when an approved reading, search, or
   provenance requirement includes them; otherwise retain them in the source snapshot without
   making them governed Dish tasks.

The importer is one-purpose migration tooling, not a permanent alternate backend. It reads an exact
snapshot and writes only the staged database.

### Rehearsal

> **Deferred human decision:** the exact writer freeze, open-operation policy, acceptance window, and
> rollback boundary below remain a proposed mechanism, not an approved cutover contract. Engineering
> must return with a concrete evidence-based recommendation before production authorization.

1. Freeze the exact Dish and Honest revisions for the document-compatible Stage A target.
2. Snapshot the complete Asana corpus, legacy Dish SQLite database, shadow evidence, and
   configuration. Produce and validate the complete legacy Dish snapshot manifest bound to the
   exact schema and release identities.
3. Require no executing claims, unresolved effects, or uncompleted service requests.
4. Rehearse the safest resolved-only route—finish, abandon under the current contract, or
   quarantine every open operation—and separately characterize whether exact journaled open
   operations could ever migrate safely. The production policy remains deferred to Marco.
5. Import every in-scope task and the exact frozen legacy workflow snapshot under its class into a
   copied database, recording row-level migration origins.
6. Prove observation-batch closure, including one exact source-document witness per task,
   complete section coverage, matching linkage and qualified identities, and no duplicate external
   IDs;
   then reconcile current pointers, location/cooked/archive state, operation history, signoff,
   and provenance. Structured conversions are Stage B evidence and do not affect Stage A import.
7. Quarantine mismatches that affect live authority; do not infer content, readiness, destination,
   validation, or signoff.
8. Validate PostgreSQL semantics, queries, backup/PITR/restore, request ownership,
   operation/cycle command journals,
   canonical commit boundaries, Asana projection, and the full document-compatible workflow suite.
9. Exercise every required CLI/admin human mutation command and the read-only Asana projection
   against the imported copy.
10. Rehearse both pre-mutation rollback and DB-backed rollback after a simulated first mutation.

### Production cutover

After separate explicit authorization:

1. stop mutation admission and drain admitted requests;
2. prove the same request, operation, claim, lease, and external-effect quiescence conditions used
   in rehearsal;
3. declare an Asana authority freeze: Marco and every agent stop manual Asana task, section, and
   project mutations, including edits, moves, cooked-state changes, creation, and section changes;
4. revoke or temporarily disable every credential capable of writing the authoritative Asana
   project where practical, retaining only the minimum read access needed for observation;
5. enumerate the complete frozen corpus into a first `cutover` observation batch, including the
   task set and count, section registry, exact title/body logical-string witnesses and qualified
   identities, placements, and cooked states; reject duplicate task or section GIDs and compute
   its corpus-manifest identity only after source-document and section closure passes;
6. repeat the complete enumeration under the same freeze into a second `cutover` batch and require
   its independent closure plus exact agreement of task set, count, section registry, source
   document identity schemes and identities, placements, and cooked states; `modified_at`
   agreement alone is never closure proof;
7. freeze and validate the final legacy Dish SQLite snapshot manifest, then append one immutable
   approval binding the second matching Asana manifest, its earlier matching batch, and that exact
   complete legacy workflow snapshot; take final configuration, code, and source-export snapshots
   bound to the same approval;
8. import only the approved Asana observations/source documents and the exact approved legacy Dish
   workflow snapshot into production PostgreSQL, recording row-level migration origins; quarantine
   any unapproved or contradictory mismatch;
9. activate the matching Dish code, schema, Honest revision, query/command surface, and human
   command coverage as one compatible set;
10. remove Asana from live task reads, workflow decisions, and ordinary mutation credentials;
11. grant only the read-only projector's dedicated worker credential and enqueue projection from
   committed PostgreSQL state;
12. keep the approved manifest, its two observation batches, and the exact source export immutable
    during acceptance;
13. admit DB-backed mutations only after identity, location, cooked/archive, request ownership,
    backup/restore, workflow, and human-command gates pass.

The Asana authority freeze begins before the first final observation and remains in force until DB
authority is active or the pre-mutation rollback restores Asana authority deliberately. Normal work
is not released between import and activation. Because Marco is the sole human operator, this is a
short operational freeze rather than a synchronization product, but it is the closure proof for
the authority transfer.

A long parallel-persistence period is allowed and expected, but it is never dual authority. Before
cutover, PostgreSQL writes are non-authoritative shadow observations or shadow execution derived from
confirmed Asana state. After cutover, Asana writes are downstream projection effects derived from
committed PostgreSQL state. Only one store is production authority at a time.

### DB-authoritative read-only Asana projection

The projector is required for Stage A because Asana remains Marco's human-facing interface after
PostgreSQL becomes authoritative. Every committed task mutation that affects the Asana view appends
an outbox item in the same PostgreSQL transaction. A separate worker renders and applies it.

Required rules:

- humans and agents do not edit projected tasks as an input to Dish;
- projection freshness and last applied PostgreSQL revision are visible;
- projection events are ordered per task revision, stale retries are no-ops, and mapping creation is
  idempotently reconciled;
- projection failure never blocks, rolls back, or reclassifies a PostgreSQL mutation;
- out-of-band Asana drift is flagged and overwritten from PostgreSQL, never imported;
- repair acts only on projection mappings and exact committed versions;
- ambiguous mirror creation cannot create a second Dish task;
- the projector uses a dedicated credential and code path that cannot execute historical Asana
  authority operations.

The exact project topology—reusing the current project or projecting into a separately labeled
mirror—must be proposed during cutover planning based on safety and Marco's usability. Regardless of
topology, Asana is never writable authority after the flip.

### Rollback boundary

Before the first DB-native production mutation, rollback may restore the complete prior Asana-based
code, database, configuration, and corpus authority.

After the first DB-native mutation, Asana is stale. Ordinary rollback must restore a compatible
PostgreSQL-backed code, database, command surface, and required Asana projector from managed backup
or point-in-time recovery.
An apparently current Asana projection is not rollback authority. Returning authority to Asana
would require a separately designed, rehearsed reverse migration that preserves every intervening
task version, transition, request result, and audit fact; it is not part of this design.

This boundary must be explicit in the cutover approval. Acceptance gates should complete before
opening mutations so rollback to Asana remains simple while it is still valid.

## Implementation sequence

1. Inventory every Asana-owned fact, canonical field, gateway call, identifier, health dependency,
   recovery branch, validator, test fixture, and required human action.
2. Establish PostgreSQL deployment, SQLAlchemy unit-of-work boundaries, Alembic migrations,
   connection ownership, managed backup, point-in-time recovery, and rehearsed restore without
   changing production authority. Define a strangler plan for the current SQLite-specific SQL and
   transaction helpers rather than maintaining two live workflow engines indefinitely.
3. Build the representation-neutral Stage A foundation: universal Dish UUIDs and external aliases,
   task/version envelope, immutable title/body documents, evolved operations/cycles and command
   journals, controlled locations and distinct cooked/archive state, exact workflow-version
   bindings, cooked/location history, immutable request envelopes, execution claims, transactional
   repository path, quarantine, and projection outbox.
4. Build one-way Asana-to-PostgreSQL observation mirroring and periodic reconciliation. Run it for a
   sustained period with no production reads or authority from PostgreSQL.
5. Add shadow execution and direct crash/concurrency fault testing against representative copied
   data. Rehearse PostgreSQL backup, PITR, restore, and projection recovery.
6. Implement every narrow human mutation command required once Asana becomes read-only. A private
   frontend is not required.
7. Rehearse the document-compatible production import, authority flip, pre-mutation rollback, and
   PostgreSQL-backed rollback after a simulated first mutation.
8. Perform the separately authorized Stage A cutover. PostgreSQL becomes authoritative; the isolated
   Asana authority credential/path is removed; the read-only projector remains.
9. Pause for battle-hardened production operation. Resolve Stage A defects without beginning a
   representation migration merely to preserve schedule momentum.
10. Separately approve and implement Stage B: structured Honest schema, canonicalization, parser,
    renderer, structured editing, exact Verification/signoff migration, and governed pointer
    advancement.
11. Retire document-only compatibility adapters only after every real producer and preserved
    historical requirement has been accounted for.

At no point may production route different tasks to different authorities or accept peer writes
from both Asana and PostgreSQL.

## Required proof

Each implemented project must test the applicable items below. Structured schema, canonical JSON,
typed-graph, parser, renderer, structured-editor, and representation-migration items are additional
gates for Stage B; they do not gate the document-compatible Stage A authority cutover.

- fresh task creation with universal Dish UUIDs and exact resolution of immutable imported Asana aliases;
- audited human cooked-state changes and cooked-history lookup;
- exact reads and consistent list/search snapshots;
- incomplete Planning, Research, and Verification-round attempts that append only service-visible
  command/control evidence without advancing the canonical task pointer or Asana projection;
- atomic complete-stage and complete-round commits, with crash injection before and after the single
  canonical pointer advancement;
- Part I abandonment and fresh-successor restart from the last committed version without preserving
  or transferring unpublished agent work;
- deterministic structured JSON reconstruction, canonicalization, hashing, and round trip;
- byte equality between stored canonical JSON and typed-graph reconstruction, with readiness
  blocked on disagreement, plus equality of the envelope title and identity;
- representation-specific, domain-separated identity-scheme fixtures for bare, title/body,
  structured Planning, and structured dish versions, including rejection under the wrong scheme;
- exact decimal, fraction, range, approximate, optional, unit, Unicode, whitespace, null/omission,
  collection-order, and canonicalizer-version fixtures;
- complete immutable version graphs, ordered child collections, foreign keys, and rollback of
  partial graphs;
- structured domain validation for every approved Honest field and cross-field invariant;
- title-only bare creation and rejection of a bare body;
- lifecycle-authorized structured editing, stale-revision rejection, and preservation of both
  versions after an edit conflict;
- rejection of ordinary edits to Planning briefs, governed canonical tasks, signed/destination
  tasks, cooked tasks where the lifecycle forbids editing, and tasks owned by an operation;
- request-scoped ownership for concurrent exact replays of `create`, `start`, cooked-state change,
  bare-title change, and other non-operation admission paths;
- permanent immutable request contract/payload/version evidence for both request- and
  operation-scoped mutations after their expiring execution claims are retired;
- deterministic `create` identity across crash, recovery, and replay;
- concurrent mutations against the same and different tasks;
- request replay before, during, and after transaction commit;
- recovery of old and adapter-based requests without reinterpretation across deployment;
- replayed results whose leases, ownership guidance, and principal-filtered `allowed_actions`
  reflect the committed post-finalization snapshot;
- content, location, cooked/archive, signoff, and actor drift;
- operation baselines, steps, actor candidates, holds, material classifications, non-material
  check-ins, submissions, migrations, reopens, inspection, review, correction, and signoff
  references to exact same-task version occurrences, including two same-task versions with the same
  identity where only one is authorized;
- transitive non-material signoff inheritance through exact predecessor/candidate version
  occurrences, including rejection of a same-identity occurrence outside that lineage;
- imported signoff bound only to the exact imported title/body version occurrence, never a future
  same-identity version;
- every Planning, Research, Verification, correction, hold, reopen, and submit route;
- structured-version schema, source, timestamp, renderer, and applicable release provenance;
- unsupported, malformed, partially structured, and unknown historical snapshots without inferred
  validity, plus exact source snapshot/modification/import provenance;
- imported signed destination pairs and imported current placements independently resolved through
  exact location-alias rows without rewriting source content, including multiple historical Asana
  aliases that map to one Dish location;
- imported current placement independent of embedded destination, and imported source-completion
  and location origin without fabricated local transitions;
- append-only destination parse and resolution attempts, including failed and superseded parser
  results, with the exact selected resolution retained by import evidence;
- signed title/body versions remaining current by default, plus separately tested re-Verification
  and approved-attestation routes if either direct migration route is implemented;
- canonicalizer upgrades that create new single-use versions, preserve old JSON and signoff, and
  cannot inherit Verification without re-Verification or approved attestation;
- rejection of attempts to make a recorded version current twice; revert and restoration commands
  must create new versions with explicit lineage;
- location rename behavior that preserves source/rendering snapshots without changing structured
  identity;
- source-to-structured parsing and structured-to-compatibility-rendering reconciliation across the
  active corpus;
- one-way shadow gaps, replay, periodic reconciliation, and proof that the separate shadow database
  cannot authorize or alter Asana-backed production or resolve candidate IDs in the live repository;
- separate shadow/reconciliation observations and approved cutover origins, with no path that
  promotes the newest ordinary observation or shadow candidate implicitly;
- observation-batch closure requiring one exact source-document witness per task, full section
  coverage, matching qualified identities and linkage, and rejection of duplicate task or section
  GIDs;
- irreversible batch completion and append-only approval of only a matching later cutover batch;
- immutable legacy Dish snapshot manifest, row-level workflow import origins, and cross-link
  validation against the approved Asana task/version occurrences;
- immutable many-to-one location aliases, append-only retirement evidence, and interval resolution
  by durable batch sequence rather than batch UUID ordering;
- shadow execution divergence reporting without production response influence;
- document-compatible Stage A cutover rehearsal and a separately gated Stage B
  representation-migration rehearsal;
- historical terminal write/movement evidence, dedicated local transitions, and absence of
  fabricated database-backed attempt records;
- atomic cooked-state and reopen transitions, current cooked projection, governed audit, and
  request replay;
- uniform non-terminal `REQUEST_IN_PROGRESS` responses that preserve the pending request and never
  expose execution tokens;
- governed audit rollback on failure and success-preserving invocation-audit repair;
- class-specific import validation, including read-only historical imports and rejected unresolved
  live evidence;
- database migration from every preserved schema version;
- semantic validation of current pointers, common version ancestry, workflow-wide exact-occurrence
  bindings, and specialized non-material signoff lineage;
- composite task/version ownership, exactly-one representation, same-version child ownership,
  single revision advancement, and quarantine promotion constraints;
- service restart, PostgreSQL lock contention/deadlock handling, managed backup, point-in-time
  recovery, restore, and restore rollback;
- isolation of any future private interface from the Action listener and command-only mutation;
- stale shadow reads and stale Asana projection views that cannot authorize production mutations;
- DB-backed production with no Asana authority calls or credentials;
- projection outbox replay, lag, out-of-band drift, update failure, and ambiguous mirror creation
  without changing DB workflow results;
- exact corpus import counts, identities, locations, cooked states, and quarantine reports;
- a frozen-authority cutover with two complete enumerations agreeing on task set/count, section
  registry, exact source-document witnesses and qualified identities, placements, and source
  completion states before import from the named manifest, plus exact agreement with the approved
  legacy Dish workflow snapshot.

The complete automated suite, an imported-corpus rehearsal, live test-project workflow, backup and
restore rehearsal, and cutover/rollback rehearsal are handoff gates. Testing must exercise real
repository transactions rather than mocking the task repository at the workflow boundary.

## Risks and controls

| Risk | Control |
| --- | --- |
| Two current-content authorities inside PostgreSQL | One task pointer; retire or project `task_content_state` |
| Structured schema merely copies Markdown headings | Derive typed fields and relationships from approved Honest domain semantics |
| Canonical JSON identity varies by serializer or domain ambiguity | Versioned canonicalization, exact quantity semantics, round-trip fixtures, and stored identity verification |
| A digest is interpreted under the wrong representation rules | Immutable domain-separated identity scheme on every version |
| Canonical JSON and normalized rows drift | One validated in-memory value, atomic insertion, byte-for-byte reconstruction checks, and readiness failure |
| Partial normalized graph becomes current | Insert, validate, hash, point, and evidence the complete representation pair in one transaction |
| Envelope and structured metadata drift | Envelope owns title/identity; structured row owns JSON/canonicalizer/schema; validate equality atomically |
| Generated rendering becomes a second authority | Structured version is canonical; rendering is versioned output or preserved source evidence |
| Shadow state influences production | Structurally isolated shadow schema/database, one-way post-reread feed, and no live authorization or write-back |
| Ordinary shadow row becomes import authority | Batch observations by purpose; only an approval binding two matching Asana manifests and one exact legacy workflow snapshot may establish origins |
| Parallel persistence recreates dual authority | One-way Asana-authoritative shadow before cutover; required one-way PostgreSQL-authoritative outbox projection afterward; never ingest the downstream copy |
| Non-authoritative persistence failure blocks production | Log and repair shadow/projection failure asynchronously; never change the authoritative command result |
| Backend abstraction becomes a permanent second engine | Test-only selection before cutover; delete live Asana mutation after acceptance |
| A parallel stage-attempt table disagrees with operations/cycles | Evolve existing operation and Verification-cycle authority; command journal is subordinate evidence only |
| The journal grows into agent draft storage | No checkpoint command; record only service-visible commands, mutations, results, and compensations |
| Frontend bypasses workflow legality | Query APIs for reads; existing command applications for every mutation |
| Editor overwrites newer or governed content | State-specific lifecycle command plus exact version/revision; no generic save |
| Frontend couples to intermediate blobs | Stable service views/actions; structured forms wait for the structured payload |
| Drag-and-drop disguises an arbitrary state change | Approved planning model and named commands; never equate board columns with workflow sections |
| Stage A document authority silently becomes the final representation | Keep Stage B as a separately approved target, but do not start it until the battle-hardening gate passes |
| Concurrent replay executes a non-operation mutation twice | Durable request execution ownership; deterministic reserved IDs; transactional ownership recheck |
| Stored replay result describes pre-finalization state | Finalize claims and leases, reread, filter actions, then persist the result |
| Retiring `task_content_state` loses provenance | Move version-specific kind, schema, source, time, and release facts onto immutable versions |
| Legacy destination rewrite invalidates signoff | Preserve exact source and immutable location aliases; structured versions use Dish IDs |
| Imported queue placement is mistaken for embedded destination | Separate task observation/origin, source document, and destination-resolution evidence |
| Repeated parsing overwrites migration evidence | Append-only resolution attempts and an explicit selected resolution |
| An incomplete manifest lacks importable source content | Batch closure requires one matching qualified source-document witness per task and complete section coverage |
| Alias history is mutated or ordered by opaque IDs | Immutable aliases, append-only retirement, and durable batch sequence |
| Parsing silently transfers Verification | Keep the signed document current by default; require re-Verification or an approved append-only equivalence attestation |
| Same-content version inherits an earlier signoff | Bind every Verification subject to task, version occurrence, identity scheme, and identity |
| Same-content version satisfies another workflow binding | Bind every content-bearing workflow fact to its exact same-task version occurrence |
| Non-material approval is lost or inherited by hash | Append exact predecessor/candidate occurrence lineage back to the source approved cycle |
| Canonicalizer upgrade silently transfers Verification | Create a new single-use version and apply the same re-Verification or attestation rule |
| Old version is reactivated with stale workflow authority | Versions become current once; revert or restoration creates a new explicitly linked version |
| Location rename invalidates identity | Stable ID is structured authority; names are rendered or historical display facts |
| Historical import invents schema validity | Orthogonal kind/validation facts and immutable source snapshot provenance |
| Local facts inherit Asana uncertainty semantics | Dedicated local transition evidence; historical attempt tables remain immutable and external-only |
| Cooked history is reduced to a mutable flag | Append-only cooked transitions commit with the projection, audit, and result |
| Cooked and Archived become the same outcome | Store cooked history separately from governed archive placement/disposition; neither implies the other |
| Asana completion is imported ambiguously | Preserve `source_completed` and map it explicitly to Dish cooked state through import provenance |
| Stale Asana projection is mistaken for authority | Read-only labeling, revision freshness, no ingestion, and DB-only legality |
| Out-of-order projection overwrites newer state | Revision-bound full-state events, per-task serialization, and stale-event no-ops |
| Ambiguous projection creation looks like duplicate work | Reconcile mirror mapping; never create another Dish task or authority record |
| Live request claims produce inconsistent client behavior | One non-terminal code and replay contract across every route |
| Expiring claim erases replay interpretation | Keep immutable request envelope separate; retire only the executor claim |
| Pending request is reinterpreted after deployment | Persist contract, payload, adapter, schema, and canonicalization identity with the request |
| Incidental audit failure reverses success | Governed evidence is transactional; invocation/transport audit remains success-preserving and repairable |
| Identifier migration breaks agents | Use Dish UUIDs universally; retain `task_gid` only as a compatibility resolver over immutable Asana aliases |
| Historical evidence becomes unreadable | Preserve terminal attempts and provenance; migrate consumers before cleanup |
| PostgreSQL loss or corruption | Managed backups, continuous point-in-time recovery, rehearsed restore, source snapshots, and sensible off-account/off-device copies |
| Cutover rollback loses DB-native work | Complete acceptance before mutation; use DB backup rollback after first DB write |
| PostgreSQL lock contention or deadlocks | Keep transactions bounded, lock rows in a stable order, retry serialization/deadlock failures through exact request replay, and measure production load |
| Import silently blesses drift | Exact snapshot reconciliation and quarantine; never infer missing facts |
| Final Asana edit is omitted during cutover | Freeze every writer, compare two complete manifests, and import only the named matching batch |
| Asana corpus is imported without its workflow authority | Bind cutover approval to the exact complete legacy Dish snapshot and preserve row-level migration origins |
| Shadow candidate IDs leak into production | Structurally isolated shadow schema/database; authoritative records are created or activated only from approved complete cutover evidence |
| Source digest becomes ambiguous | Store and manifest the identity scheme with every observation and source witness |
| Cutover batch is reopened or reapproved | Irreversible completion plus one append-only approval tied to an earlier matching complete batch |
| Quarantine leaks into ordinary authority | Keep quarantine outside tasks; separately audited promotion only |

## Deferred decisions and later gates

No unresolved human decision blocks Stage A implementation-design work. The items below are
intentionally decided when direct evidence exists; surrounding text must not be treated as an
implicit answer.

### Before Stage A production cutover

1. **Final mutation coverage.** Shadow use identifies the narrow actions Marco actually needs after
   Asana becomes read-only. Engineering implements those actions and presents any remaining gap as a
   concrete workflow, not a request for a complete future UI design.
2. **Historical corpus scope and exceptions.** Whether every cooked-history, Sourcing, and Reference
   record enters authoritative PostgreSQL or remains only in immutable source snapshots. No task is
   silently discarded; problematic records are reconciled or quarantined case by case.
3. **Cutover and operational-confidence gate.** The final open-operation rule, authority-flip point,
   acceptance window, and rollback boundary are selected near cutover using observed system
   behavior and Marco's infrastructure judgment.
4. **Asana projection topology.** Whether the read-only projection safely reuses the existing Asana
   project or uses a separately labeled mirror project.

These are production-authorization decisions, not missing Stage A architecture.

### Before Stage B

5. **Structured content boundary and schema.** The exact structured Planning and dish grammar,
   including quantities, units, sensory stop conditions, shopping, equipment, storage, provenance,
   and which facts remain workflow or lifecycle state.
6. **Verification across representation migration.** Whether an existing signed title/body version
   remains current until ordinary governed work replaces it, or whether a narrowly defined
   human-approved equivalence attestation may transfer specified facts to a structured occurrence.
   No automatic digest- or rendering-based transfer is allowed.
7. **Stage B activation scope.** Whether structured authority is migrated corpus-wide, only for
   active tasks, or progressively when a task next undergoes governed work.

### Deferred product choices

8. A future private frontend, cooking planner, scaling, priority, and rich cook-log editing remain
   separate product decisions. They do not block Stage A. Cooked and Archived semantics are already
   distinct; only the richer cook-log command and presentation remain deferred.

## Approved implementation direction

The implementation plan must conform to these settled defaults:

1. PostgreSQL is the sole target authoritative database; SQLite is legacy-only until cutover.
2. Stage A is a document-compatible PostgreSQL authority migration followed by a battle-hardening
   pause; Stage B is a later structured representation migration.
3. Every task uses a Dish UUID; Asana task and section identifiers are external aliases. Stage A
   document compatibility may retain the embedded destination section alias while internal routing
   uses a Dish location ID.
4. Dish journals service-visible commands, durable mutations, outcomes, and compensations against the
   authoritative operation and Verification-cycle identities. It does not checkpoint private agent
   work. Canonical content advances only at completed Planning,
   Research, or Verification-round boundaries.
5. Stage A preserves Part I fresh-successor abandonment and task-fence semantics. It does not add
   same-operation replacement or unfinished-authority transfer.
6. Every content-bearing workflow fact binds to its exact task/version occurrence, identity scheme,
   and identity; same digest does not transfer authority or Verification.
7. Use controlled Dish locations, separate cooked history, and governed archive rather than project
   emulation, conflated lifecycle meaning, or hard deletion.
8. Before cutover, Asana-to-PostgreSQL mirroring is one-way and non-authoritative; mirror failures
   are asynchronous and never block Asana. After cutover, PostgreSQL-to-Asana projection is one-way
   and read-only; projection failures are asynchronous and never block PostgreSQL.
9. The Stage A Asana projection uses an ordered, revision-bound, idempotent outbox worker and an
   isolated credential with no authority path.
10. PostgreSQL task state, workflow evidence, request result, governed audit, command-journal facts,
    and projection outbox share the required atomic command transaction boundary.
11. Cutover authority binds two matching complete Asana corpus manifests and one exact complete
    legacy Dish workflow snapshot manifest. No open or unresolved state is inferred.
12. Managed backup, continuous point-in-time recovery, and rehearsed restore are Stage A operational
    requirements. Multi-region or automatic failover is not.
13. A private frontend is optional and later. The Stage A mutation surface is progressive, bounded,
    and extended through ordinary commands and Alembic migrations as real needs appear.
14. Ordinary commands archive rather than hard-delete; Cooked and Archived remain distinct;
    exceptional purge is outside this design.
15. Use SQLAlchemy 2.0.50, Alembic 1.18.4, `psycopg[binary]` 3.3.4, and Pydantic as the default
    PostgreSQL application stack, following the approved transaction, migration, constraint,
    timestamp, connection-lifecycle, and test-isolation conventions above.
16. PostgreSQL execution ownership uses opaque database-fenced tokens/generations rather than
    SQLite-era hostname/PID liveness. Multi-statement authoritative reads use one consistent
    revision/snapshot.
17. Historical import exceptions are never silently dropped and do not require one universal policy
    now; they are quarantined or reconciled case by case from exact source evidence.
18. Battle-hardening and cutover are evidence-based decisions made near the relevant phase, not
    fixed-duration gates inferred by implementation agents.


Table names, PostgreSQL constraint forms, lock primitives, outbox worker implementation, migration
tooling, and API-internal naming are engineering decisions. The implementation may replace
`REPEATABLE READ` with an equivalent single-query consistency proof, but may not weaken the required
one-revision read contract. Engineering decisions return to Marco only if evidence exposes a
material product, safety, operational, or cost tradeoff not already settled above.

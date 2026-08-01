# Database-backed task store: Stage A architecture

Status: Stage A architecture approved for implementation-design handoff. No unresolved human
architecture decision remains. The 1 August 2026 review sequence now includes the grounded
authority, failure-recovery, scope-control, audit-repair, backup-retirement, cutover-activation,
legacy-generation, projection-corpus, create-feasibility, and proof-oracle corrections.
This document is not code-implementation authorization and
does not authorize a production cutover. The human-approved decisions below are binding design
constraints until Marco explicitly changes them. Agents may identify conflicts or risks, but must
not silently weaken, reinterpret, or overrule them.

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
3. **Canonical commit boundaries and intermediate-operation journal.** Incomplete or private
   agent work does not advance canonical task content. Dish durably records service-visible
   commands, workflow transitions, ownership changes, attempted mutations, outcomes, and named
   compensation/reconciliation facts. It does not checkpoint an agent's private reasoning, notes,
   or unfinished draft. Every complete named governed command that intentionally changes the
   authoritative title/body atomically creates all complete immutable versions required by its exact
   lineage and activates exactly one of them as current with its workflow evidence. Additional
   complete versions in that same command are permitted only when the governed lineage requires
   them; they are never private draft checkpoints. If an agent disappears before such a command
   commits, its unpublished work is discarded and recovery starts from the last committed canonical
   version.
4. **One-way authority direction.** Before cutover, Asana is authoritative and confirmed Asana state
   is mirrored into non-authoritative PostgreSQL shadow state. At cutover, authority flips once.
   After cutover, PostgreSQL is authoritative and projects committed state one-way to Asana. There
   is no bidirectional synchronization and never peer authority.
5. **Reuse the existing in-scope Asana project set as Marco's downstream interface after
   cutover.** PostgreSQL is the sole authority. The existing Asana projects, task GIDs, links, and
   history remain the downstream projection because Marco is the sole user and values continuity
   over the extra isolation of a second mirror project set. Direct Asana edits are unsupported
   drift: they never flow into PostgreSQL or become Dish commands, are logged, and are overwritten
   asynchronously from PostgreSQL. The surface is behaviorally read-only even where Asana cannot
   technically prevent Marco from editing it. A new private frontend is not a Stage A prerequisite.
6. **Universal Dish task identity.** Every authoritative task has a Dish UUID. Imported Asana task
   GIDs are immutable external aliases in a separate alias relation; they are never the internal
   primary key. Compatibility APIs may resolve an Asana alias temporarily, but authoritative
   responses and storage use the Dish UUID.
7. **Archive semantics, when introduced.** Archive is a future authoritative Dish disposition
   orthogonal to workflow/catalog location; any Asana Archived-section placement is only its
   downstream rendering. This approved semantic direction supersedes the older placement-as-
   authority proposal in [`future.md`](future.md), but Archive is not a prerequisite for the
   database-first migration. Ordinary lifecycle commands never hard-delete a task or its history.
   Exceptional data purging, if ever required, is a separate administrative and policy design.
8. **Preferred PostgreSQL application stack.** Use SQLAlchemy 2.x for ORM/database access, Alembic
   for every schema migration, psycopg 3 as the PostgreSQL driver, and Pydantic alongside SQLAlchemy
   for command, API, and domain validation. Pydantic is not the ORM. The preferred stack families are binding defaults; exact package versions belong in the dependency
   lock and implementation evidence rather than in permanent architecture semantics. The current
   implementation baseline may begin with SQLAlchemy 2.0.50, Alembic 1.18.4, and
   `psycopg[binary]` 3.3.4. An implementation agent may propose a change only for a
   concrete compatibility, security, or operational reason and must not substitute a different
   stack merely by preference.
9. **The non-authoritative side never blocks the authoritative side.** During shadow operation, a
   PostgreSQL delivery failure never changes a successful Asana result. Exact command-level shadow
   retry is allowed only from a durably captured pre-effect shadow envelope; otherwise the command is
   recorded as an explicit unshadowed proof gap and later current-state reconciliation must not
   pretend to reconstruct its missing pre-command evidence. After cutover, an Asana projection
   failure is logged, retried, and reconciled asynchronously but never rolls back or reclassifies a
   successful PostgreSQL result.
10. **Completion, Cooked, and Archived are distinct outcomes.** The current task-completion flag
    remains a separate Planning-eligibility gate: a completed bare task requires Marco's audited
    `reopen-planning` command, which clears completion only. When later introduced, Cooked records
    that a dish was actually made and Archived removes an unapproved, redundant, obsolete, or
    retired task from active work. Neither future feature is required for the initial database
    migration, neither is inferred from completion, and all enabled outcomes preserve full history.
11. **Mutation coverage is progressive application work.** Stage A does not require Marco to define
    every future human mutation in advance or require a generic editor. Implement the retained
    governed actions and only the smallest additional actions discovered during shadow use that are
    individually named in the semantic-delta matrix and accepted before cutover. Direct Asana habits
    are evidence of a possible need, not automatic command requirements. Adding later commands,
    constraints, indexes, or Alembic migrations is normal application evolution, not a database-
    architecture redesign.
12. **Battle-hardening and cutover are evidence-based.** There is no fixed duration or arbitrary
    pass count. Marco decides near cutover using observed failures, recoverability, diagnosis and
    repair burden, projection correctness, backup/restore confidence, and actual usage.
13. **Historical exceptions are never silently discarded.** Problematic imported tasks are
    reconciled or quarantined case by case from exact source evidence. The architecture does not
    require one universal exception policy before implementation design begins.
14. **Initial PostgreSQL deployment class.** Stage A initially runs self-managed PostgreSQL in
    Docker Compose on Marco's laptop, consistent with his other systems and cost constraints. The
    same authority and storage contracts must permit later relocation to a self-managed AWS host
    without redesign. Managed PostgreSQL, multi-node failover, and high availability are not Stage A
    requirements. PostgreSQL may fail independently of Dish; authoritative mutations fail closed
    when it is unavailable, and Marco owns backup, restore rehearsal, upgrades, credentials, and
    monitoring.
15. **Resolved-only one-way cutover.** Production cutover migrates no live or uncertain workflow
    authority. Every open operation, cycle, request, lease, or external-effect uncertainty is
    completed, recovered, abandoned under Part I, reconciled, or quarantined first. Before the
    rollback-burn fact and first PostgreSQL mutation-request admission, the cutover may be rolled
    back to Asana authority. Admission itself creates durable intent and burns ordinary rollback,
    even if the later task-domain mutation does not commit. After rollback burn, recovery uses
    PostgreSQL restore/recovery; returning authority to Asana would require a separately designed
    reverse migration and is intentionally out of scope.
16. **Database-first scope and live-domain re-baseline.** PostgreSQL authority work comes before
    introducing Cooked, Archive, or Cooking History as new governed Dish concepts. Stage A migrates
    the authoritative Dish domain that actually exists when implementation and cutover begin. Today
    that is the Cooking project and the current workflow/runtime database. Before implementation and
    again before cutover, the scope is re-baselined against the live code and architecture so any
    separately landed governed feature is preserved. The migration must not invent a fallback
    meaning for Cooking History, Cooked, or Archive merely because a future design mentions them.
    Their distinct semantics remain approved for when those features are introduced, but they are
    not prerequisites for the initial shadow baseline or authority flip unless they already exist.
17. **Deliberate API redesign is allowed.** Backward compatibility with the present command or
    response schema is not an architecture requirement. Stage A may replace identifiers, commands,
    request envelopes, response shapes, and the Custom GPT Action schema where that produces a
    cleaner correct contract. Rollout must coordinate the service, OpenAPI/Action schema, agent
    instructions, examples, and an explicit protocol/release identity so an agent cannot silently
    use the wrong contract. Before cutover, no live request may use a PostgreSQL shadow alias or
    candidate UUID as routing authority. The public switch from Asana GID identity to Dish UUID
    therefore occurs with the authority flip unless an equivalent mapping is first placed inside
    and validated by the current Asana/SQLite authority domain. Compatibility adapters are optional
    rollout tools, not permanent design constraints.
18. **Complete, gap-free baseline before shadow execution.** PostgreSQL shadow execution starts
    only after a complete imported baseline of the then-current authoritative Asana corpus and exact
    legacy Dish runtime authority bundle has been created, brought current through durable delta
    capture and reconciliation, and validated as gap-free. Every subsequent governed command is
    evaluated against that equivalent baseline. While Asana remains authoritative, any command or
    state that PostgreSQL shadows must remain behaviorally representable and executable by the live
    Asana-backed system; PostgreSQL-only authoritative semantics remain inactive until the authority
    flip. This is one-way compatibility with the current authority, not peer authority or reverse
    synchronization.
19. **Destructive PostgreSQL restore policy.** PostgreSQL restore or point-in-time recovery
    that can discard committed history is an offline, exclusive operator action, not an ordinary
    replay-bound Dish command. Every such restore establishes a new database-authority generation
    before mutation admission resumes. Requests, executor claims, mutation fences, and agent runs
    from an earlier generation are rejected and never silently reinterpreted as new work; anything
    genuinely required is deliberately reissued under the new generation. Restore control evidence
    lives outside the database timeline being replaced, and the restored database must be reconciled
    to that evidence before normal readiness. Asana remains non-authoritative and is fully
    reprojected from the restored PostgreSQL state.
20. **PostgreSQL backup creation is operator-managed.** Stage A retires the current
    replay-bound `backup-create` command together with the connected `backup-restore` surface at
    authority cutover. PostgreSQL base backup, WAL archiving, retention, verification, and restore
    are exclusive operator procedures, not ordinary Dish command API mutations. Historical SQLite
    backup requests, `backup_creations` rows, reserved/completed artifact identities, and retained
    artifacts remain immutable migration witnesses. Cutover requires every open backup reservation
    to be completed, explicitly terminalized, or quarantined; no unresolved reservation is silently
    dropped. Operator-created PostgreSQL backup evidence is recorded through the operational
    backup/restore control plane and is never presented as replay of a retired Dish request.

## Architecture-lock status

All current Stage A architecture questions requiring Marco's decision are resolved. The current
Planning-intent gate, legacy restore sidecars, destructive-restore authority generation, retired
`backup-create` surface, and quarantined-source projection disposition are explicitly covered below. Later
production-cutover authorizations and Stage B product decisions remain explicitly deferred below;
they are not permission for implementation agents to reopen the locked Stage A direction.

## Approved database implementation conventions

These conventions are approved defaults for Stage A unless Dish-specific evidence justifies a
narrow exception:

- Use SQLAlchemy 2 declarative models with `DeclarativeBase`, `Mapped`, and `mapped_column`.
- Alembic owns every schema change. Production and normal test setup must not use
  `Base.metadata.create_all()` as a migration substitute. Alembic executes and projects the current
  schema revision, but it is not the sole immutable history authority: PostgreSQL also preserves an
  append-only, database-generation- and release-bound migration provenance ledger, including exact
  disposition of every imported legacy `schema_migrations` row.
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
moving task state, workflow evidence, replay, execution causality and recovery evidence, and mutation atomicity into
PostgreSQL. Stage B later introduces versioned structured Planning and dish authority after Stage A
has been battle-tested.

Keep `dish-service` as the only live mutation authority and keep the current workflow,
Verification, lease, replay, audit, and Part I abandonment boundaries unless this design explicitly
changes them. The authoritative task revision, workflow transition, governed audit evidence,
execution evidence, replay result, and any immutable projection event commit in one PostgreSQL
transaction when they belong to one command boundary.

The replacement is not an Asana clone. It owns only:

- immutable document versions in Stage A and structured versions in Stage B, with
  representation-appropriate exact identities;
- exact imported source documents and generated human-readable renderings;
- orthogonal current-state axes for the domain actually enabled at that release: current
  workflow/catalog location, version-owned intended destination, and task completion/Planning
  eligibility now; Cooked and Archive remain separate governed axes when later introduced, each
  with immutable transition evidence and never inferred from unrelated Asana flags;
- the existing workflow and Verification evidence;
- task creation, current governed reads, and Marco's narrow retained interventions; any broader
  browsing/search surface is a separately classified interface addition, not an implicit Stage A
  cutover prerequisite;
- lifecycle-authorized editing without a generic content-save bypass;
- an extensible bounded command layer whose initial mutation set is proven by actual Stage A use;
- Cooked, Archive, Cooking History ingestion, and future cook-log records only when separately
  implemented or already present in the live domain being re-baselined; they are not prerequisites
  for the database-first authority migration.

Authority is singular throughout the migration. Before cutover, production writes go to Asana and
confirmed state is mirrored one-way into PostgreSQL shadow storage. A shadow-write failure is
reported and repaired asynchronously but never changes the Asana result. After cutover, production
writes commit to PostgreSQL and transactional projection events drive an asynchronous Asana worker as Marco's non-authoritative downstream interface.
A projection failure is reported and repaired asynchronously but never changes the PostgreSQL
result. Shadow or projection state never decides workflow legality.

## Why consider this after activation

The current external-effect protocol is intentionally conservative. Dish records intent, calls
Asana, rereads the task, and classifies the effect as `confirmed`, `not_applied`, or `uncertain`.
That protects production work but creates recovery states that exist only because the document and
workflow evidence commit in different systems.

A PostgreSQL-native task mutation can commit the new task revision, workflow transition, governed
audit evidence, command/execution causality evidence, replay result, and immutable projection event together. A
process failure before commit rolls the whole unit back; a response loss after commit is answered by
exact request replay. This removes ordinary content writes, moves, and task creation from the ambiguous
external-effect model. Later PostgreSQL-native lifecycle features such as Cooked or Archive obtain
that same benefit when they are introduced.

The canonical-boundary rule makes recovery simpler without inventing agent-work checkpointing.
Planning and Research `start` commands establish durable operations but do not change canonical
content. Verification `start` and `inspect` establish review evidence but do not change canonical
content. Complete Planning or Research `prepare` and complete Verification decisions are the normal
agent content boundaries. Existing complete governed administrative commands that deliberately
rewrite authoritative content—such as schema migration, two-pass reopen, material hold resolution,
and destination repair—are also content boundaries and activate immutable versions with their exact
workflow evidence. If a run disappears before one of these commands commits, Dish retains its
service-visible command history, abandons or compensates its workflow attempt under the existing
recovery contract, and starts from the last committed version. The abandoned agent's unpublished
work is intentionally lost.

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
location, lifecycle, or transition APIs in a way that forces a second backend redesign. Stage B is a
separately approved content-representation migration, not another authority cutover.


Stage A is one production programme but must be designed and accepted through separately provable
slices so one broad shadow period cannot conceal where a failure originates:

1. **Identity transition:** universal Dish UUIDs, immutable Asana aliases, and an explicitly
   versioned command/API rollout. Shadow candidate identity is never live routing authority. The
   public identifier switch occurs with the authority flip unless the current authority domain has
   first adopted and validates the mapping. Legacy field compatibility may be used temporarily but
   is not a target invariant.
2. **PostgreSQL persistence parity:** imported task/workflow evidence, representation-neutral
   versions, consistent reads, and migration validation.
3. **Execution and replay fencing:** immutable request outcomes, database-fenced executor claims,
   exact recovery, and fresh current-action views.
4. **Authority and projection:** PostgreSQL-native canonical mutation, transactional projection events,
   removal of Asana authority calls, and the one-way cutover.

These slices use one architecture and one eventual authority flip; they are not separate production
authorities or permission to run a partial dual-authority system.

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
6. Permit a deliberate command/API redesign where it improves authority, replay, identity, or
   recovery semantics, with one coordinated service/OpenAPI/instructions rollout and explicit
   protocol identity rather than indefinite backward compatibility.
7. Keep Asana available as Marco's post-cutover non-authoritative human interface through transactional
   immutable projection events and an asynchronous delivery worker.
8. Import the live corpus deterministically, quarantine exceptions, and retain exact source
   snapshots for acceptance.
9. Permit a long-running Asana-authoritative shadow period without introducing dual authority.
10. Delete the executable Asana authority path after acceptance while retaining the isolated
    downstream projector.
11. Keep the persistence and query model capable of later efficient Cooking History ingestion
    and search without making Cooking History, Cooked, or Archive prerequisites for the initial
    authority migration.
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
2. **Dish task storage in PostgreSQL** owns the current authoritative version—document-compatible
   in Stage A and structured after Stage B—plus current location, the existing separate completion/
   Planning gate, immutable task revisions, and any Cooked or Archive axes only after those features
   are separately introduced or already exist in the re-baselined live domain.
3. **Dish workflow storage in PostgreSQL** owns operation intent, Verification evidence, actor/run
   lineage, leases, request replay, execution claims, committed-fact causality links, recovery facts, and
   audit history.

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
                                           projection events/attempts
                                                           |
                                                           v
                                              non-authoritative Asana downstream interface
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

The current workflow stages and governed meanings of `create`, `read`, `start`, `prepare`,
`inspect`, `approve` or `reject`, and `submit` remain unless this architecture explicitly changes
one. Their public command names, grouping, arguments, and response shape may change in the deliberate
Stage A API redesign. The existing read-only `sections`/destination-registry capability is retained
semantically as a Dish-location query: after cutover it reads active PostgreSQL locations and,
during Stage A, returns the exact approved numeric alias needed to author the retained
title/body destination pair. It never reads projected Asana placement as routing authority.
Existing administrative commands remain narrow. Asana-specific recovery commands or fields are
removed only after their historical and current callers have been accounted for.

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
GID until the downstream projector creates a mirror mapping. Across imported aliases and projection
mapping history, one Asana task GID may resolve to at most one Dish task; if the same GID appears in
both relations under an existing-project topology, both must name the same task.

The PostgreSQL-authoritative public API uses `task_id` for the Dish UUID. Before cutover, the live
Asana-authoritative API continues to route by Asana GID unless an equivalent Dish-UUID mapping has
been adopted inside the current SQLite authority and is revalidated against live Asana; the
PostgreSQL shadow mapping is never consulted for live authorization or routing. At the authority
flip, the Action/API schema may switch directly to `task_id`. A temporary `task_gid` resolver may be
retained only if it materially reduces rollout friction; it is not required. Any accepted Asana
alias or confirmed post-cutover projection GID resolves explicitly to one Dish UUID and remains a
lookup key only, never task or workflow authority. OpenAPI, request hashing, response identity, and
protocol/release evidence must make every accepted identifier domain explicit rather than treating
an arbitrary string as interchangeable identity.

Dish locations use stable Dish identifiers internally. Imported Asana project and section GIDs are
immutable aliases and provenance, never routing authority. Stage A has one deliberate compatibility
exception: the existing title/body document grammar continues to contain the historical/current
Asana destination section `name — numeric_gid` pair because current validators and clients require
it. Import and every Stage A write resolve that pair through an immutable location alias to the
Dish `location_id`; workflow legality and routing use the Dish location. During Stage A, a
destination used in canonical content must already have an immutable numeric compatibility alias
from the approved cutover corpus. Adding a brand-new destination with no such alias is not a
cutover prerequisite and requires a separately governed compatibility extension or waits for Stage
B; asynchronous Asana projection must never mint routing authority. Stage B structured content
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

## Shared deterministic domain decision boundary

Stage A shadow execution requires an explicit domain boundary; it cannot emerge merely by replacing
one repository object in the current command code. The architecture introduces backend-neutral,
deterministic command planning and evidence adjudication rather than one adapter-specific handler.

`AuthoritativeSnapshot` contains every exact task, version, location, operation, Verification-cycle,
lease, actor, hold, signoff, and recovery fact needed to decide the command. `CommandIntent` is the
canonical replay-bound request. `PinnedContractInputs` contains nondeterministic inputs whose values
must be identical for live and shadow evaluation: current time, generated identifiers, resolved
location/section identities, release and schema identities, principal/run facts, adapter versions,
and equivalent environment-derived values.

The shared domain boundary has two deterministic stages before cutover:

```text
AuthoritativeSnapshot + CommandIntent + PinnedContractInputs
    -> CommandPlan

PersistedIntent + BeforeEvidence + AdapterResult + AfterEvidence
    -> CommandAdjudication
```

`CommandPlan` names exact preconditions and authority bindings, intended domain facts, proposed
canonical version/transition facts, and the next exact external effect or locally atomic fact plan
to attempt. It does **not** claim that an Asana effect succeeded. A command with several ordered
external effects—such as a content write followed by a movement—uses repeated plan/adjudication
rounds: each adjudication consumes the persisted intent plus exact before/after observations and
adapter outcome, classifies that effect as `confirmed`, `not_applied`, or `uncertain`, and either
produces the next exact plan or the terminal immutable command outcome and named recovery
classification. The adapter never chooses the next workflow step independently.

After PostgreSQL becomes authoritative, the local state mutation and its evidence share one
transaction domain. The plan and adjudication may then collapse into one deterministic
`CommandDecision` whose facts either commit together or do not commit.

Snapshot builders, transactional committers, the pre-cutover Asana effect executor, the PostgreSQL
shadow recorder, and the post-cutover projection worker are adapters around these decisions. They do
not independently reproduce workflow transition or adjudication rules. The shared unit is the
domain plan/fact model and post-effect evidence classifier, not a fictional common external
transaction.

Shadow comparison evaluates the same captured **pre-command** snapshot and pinned inputs used by the
live plan. For effect-bearing commands it also compares the shadow adjudication using the exact live
persisted intent and before/after evidence; it must never substitute a post-mutation reread as the
pre-command input. Extracting these boundaries from the current Asana/SQLite-coupled command
handlers is an explicit prerequisite for Phase 2 shadow execution.

Before that refactor changes the live semantic implementation, Dish freezes an independent,
versioned **current-behavior characterization corpus** from the existing Asana/SQLite system. For
every retained route and representative recovery state it records the canonical principal/request,
exact pre-command authority snapshot, external-effect intent and observations, canonical result,
current-view/legal-action result, normalized durable facts, and terminal or recovery
classification. This corpus is not generated by the new shared kernel. Phase 2 proves two separate
claims: the shared kernel matches the frozen current-behavior oracle, and PostgreSQL shadow execution
matches the new live path from the same captured envelope. Agreement between two adapters that use
the same new decision code is never sufficient by itself to prove preservation of current behavior.

Pre-cutover authoritative `create` has an additional exact-effect requirement. Before calling Asana,
the current authority domain reserves the Dish UUID and create intent, writes a stable unique
correlation marker into a discoverable non-canonical Asana surface, and records the effect-attempt
intent. A lost response is reconciled by that marker before any new create attempt is permitted; the
confirmed Asana GID binds to the reserved Dish UUID exactly once. If the deployed Asana contract
cannot provide a supported discoverable correlation surface, `create` is explicitly excluded from
Phase 2 command-parity claims and readiness evidence rather than being presented as exactly
adjudicable.

Post-cutover PostgreSQL-native creation has a stricter feasibility gate because the Asana projector
is the required Stage A human-facing surface. Before architecture activation, the deployed Asana
contract must prove that one create effect can atomically carry a stable non-canonical marker, that
an exact lookup can classify zero, one, or multiple matches after response loss, and that one match
can bind the GID exactly once while multiple matches block automatic progress. If that capability is
not available, PostgreSQL-native `create` remains disabled and cutover cannot silently claim the
fixed topology is complete; Marco must choose a separately approved fallback such as another
projection topology or governed creation interface.

Phase 2 also requires a rollout-only durable shadow input envelope in the current Asana/SQLite
authority domain. Because a destructive legacy `backup-restore` can replace ordinary SQLite request
and rollout history while its external restore journal survives, shadow evidence is generation-
bound before cutover as well as after it:

```text
legacy_authority_generations
  legacy_authority_generation_id
  predecessor_generation_id     nullable
  reason                         shadow_bootstrap | destructive_sqlite_restore
  external_restore_control_id    nullable
  established_at

shadow_rollout_registrations
  legacy_authority_generation_id
  rollout_sequence              monotonic and unique within the generation
  request_id
  command_execution_or_effect_identity
  task_or_reserved_task_id
  registered_at

shadow_command_envelopes
  envelope_id
  legacy_authority_generation_id
  rollout_sequence              unique within the generation
  authoritative_snapshot
  canonical_command_intent
  pinned_contract_inputs
  persisted_external_effect_intent
  live_before_evidence
  live_adapter_result             nullable
  live_after_evidence             nullable
  delivery_state                  pending | delivered | adjudicated
  created_at

shadow_command_gaps
  gap_id
  legacy_authority_generation_id
  rollout_sequence              unique within the generation
  gap_reason
  created_at
```

Every governed rollout command first commits one small registration with its ordinary current-
authority request reservation. It then produces exactly one evidence path: a complete immutable
envelope committed before the live external effect, or an explicit durable gap marker. PostgreSQL
delivery may fail without blocking Asana because the current SQLite authority retains the exact
retryable envelope. If complete capture fails, the live command may still succeed under the current
authority and the registration is settled as a gap. A registered sequence with neither envelope nor
gap is itself an unresolved rollout fact that blocks parity claims and cutover readiness until
recovery classifies it; it is never silently skipped. Periodic reconciliation may repair current
shadow state but may not count a gap as exact plan/adjudication parity evidence.

Every pre-cutover service request, client run, rollout registration, envelope, gap, PostgreSQL
shadow delivery/adjudication, baseline, and parity result is bound to the current
`legacy_authority_generation_id`. Before any destructive SQLite restore, the next generation is
reserved in restore-control evidence outside replaceable SQLite. After restore, prior-generation
requests and runs are rejected, prior-generation command-parity evidence is permanently
disqualified, surviving Asana state is reconciled only as current-state evidence, and a new complete
gap-free baseline is required before shadow command execution resumes. The final legacy runtime
authority bundle and cutover approval name the exact current legacy generation.

## Storage model

The exact SQL belongs to an approved implementation plan. The conceptual model is:

### `tasks`

One current row per task:

```text
task_id                      canonical non-nil Dish UUID
current_version_id           exact immutable version currently authoritative
current_location_id          current workflow/catalog location; not archive or cooking history
completed                    current task-completion / Planning-eligibility flag
operability_state            governed | historical_read_only
# optional feature extensions, absent or nullable until their feature is enabled:
archive_state                active | archived
cooked                       current projection that the dish has actually been cooked
task_revision                monotonic revision of canonical/projected task state
created_at
modified_at
```

The task's current workflow/catalog location, completion flag, operability state, and canonical
content pointer are the Stage A baseline axes. Archive and Cooked remain orthogonal axes when those
features are introduced; their columns may be added later or remain nullable behind an explicit
feature capability, but initial shadowing and cutover do not fabricate values or expose default
`active`/`false` values as facts for product concepts that do not yet exist.
Completion preserves the current bare-task Planning gate and is never evidence that a dish was
cooked. Once enabled, a dish may be cooked while still in a workflow location; a cooked dish may
later be archived; archiving does not erase or replace location, destination, completion history, or
cook history. The intended destination belongs to the exact content version and is not inferred
from current workflow placement.

`task_revision` advances only when an enabled canonical or projected task axis changes: current
version, workflow/catalog location, completion flag, operability state, and—only after their feature
activation—Archive or Cooked. Each enabled projection and `task_revision` change only in the same
transaction as its named transition evidence and request
outcome, and audit. Opening or completing an
operation, binding or inspecting a Verification cycle, changing a lease, resolving a hold, or
settling an execution does not advance `task_revision` unless that command also changes one of those
canonical/projected task axes.

Operations, Verification cycles, service leases, requests, and executions carry their own monotonic
versions, generations, or exact immutable status tokens. A fresh current view includes the exact
versions/tokens of every authority source used to derive legal actions and exposes one opaque,
principal/run-scoped `current_view_token` for conditional reads or mutations. The token is only a
staleness precondition: every command still authenticates the caller and revalidates exact domain
authority. `task_revision` therefore orders authority-changing projected states, while the separate
per-task projection sequence orders actual delivery and projection-only refreshes. Task revision is
not misrepresented as an ETag for the whole workflow view. Archive is a
governed disposition, not a special location and not a task-body flag. Ordinary archive leaves the
last meaningful workflow/catalog location intact; a restore therefore does not need to guess the
prior location. A command may deliberately change location and archive disposition together only
when its domain contract names both transitions.

A `historical_read_only` task remains searchable, readable, and projectable from its exact imported
version and provenance, but ordinary workflow and content mutations expose no legal actions. It may
become `governed` only through a named migration, clone, or promotion command that creates or proves
a supported current version and commits the mode transition with exact import/lineage evidence.
Quarantine remains separate for identity, closure, or authority contradictions that cannot safely
be represented even as read-only task history.

Tasks are never hard-deleted through an ordinary command. Governed exclusion/quarantine—and,
when introduced, Cooked history and Archive—preserve identifiers, versions, command causality,
workflow evidence, and audit relationships. When Archive is implemented, the approved orthogonal
archive disposition supersedes `future.md`'s older proposal to make Archived-section placement the
authority. Its exact command timing and active-operation restrictions belong to that feature's
implementation design and must not block the database-first migration.

The current version pointer replaces `task_content_state` as the authoritative current projection.
There must not be two independently writable current-content tables. During database migration,
`task_content_state` may be converted into a compatibility view or retired after every caller uses
the task pointer and its version-specific schema and document-authority provenance has been
migrated.

### Operations, Verification cycles, and command causality

The current domain already has the right durable attempt identities: a Planning or Research
`operation`, and an exact `verification_cycle` inside a Verification operation. Stage A evolves
those records rather than creating a second independently writable stage-attempt state machine.
`operations` and `verification_cycles` remain lifecycle authority. Each operation retains its
immutable kind, actor/run lineage, exact expected version occurrence and identity, and the governed
Dish/Honest protocol, schema, and release pins under which it began. A later deployment never
silently reinterprets an operation; the only retained exception is Part I's exact unclaimed prepared
Planning/Research successor, which may adopt the current governed schema atomically with its first
eligible claim after current-release live validation. `service_requests` remains request identity/result
authority. Stage A introduces one durable `command_execution` authority for every task/workflow mutation
request, including commands that occur before or outside an operation. Admission-only Planning-
intent challenge issuance is the explicit exception: its durable authority is the service request
plus challenge relation, not a task/workflow command execution. Existing legacy
`operation_executions` are preserved and migrate as operation-bound execution records or a strict
subtype/projection of that general authority. Named workflow/domain tables remain authority for
their specific facts.

The currently implemented two-request Planning-intent gate remains durable admission authority and
is migrated explicitly rather than folded into ordinary workflow execution:

```text
planning_intent_challenges
  challenge_id
  authority_generation_id
  owner_id
  client_run_id
  task_id
  agent
  target_identity
  intent_basis
  override_reason             nullable
  issued_request_id
  claimed_request_id          nullable
  consumed_request_id         nullable
  resulting_operation_id      nullable
  settlement_request_id       nullable
  settled_by                  nullable
  settlement_reason           nullable
  state                       issued | claimed | consumed | settled_unconsumed
  issued_at
  claimed_at                  nullable
  consumed_at                 nullable
  settled_at                  nullable
```

Challenge identity, owner/run/task/agent/target bindings, intent basis, override reason, and request
links are immutable. State is monotonic and single-use. The first request creates the challenge and
its completed replay result without reading or mutating the task, opening an operation, acquiring a
lease, or taking a task or operation mutation fence. A fresh request claims the exact challenge.
Successful Planning start atomically consumes it with operation creation and the canonical request
result.

Marco may use one audited administrative settlement action to move an `issued` or
claimed-but-unconsumed challenge to `settled_unconsumed`. The action requires a reason, proves that no
Planning operation was created from the challenge, and is permanently non-reusable. A later attempt
to start Planning requires a new challenge and fresh request identities. Settlement appends terminal
evidence; it does not rewrite the original issuance, claim, or request outcomes. Shadow state,
migration provenance, semantic validation, exact replay, and cutover closure all include issuance,
claim, consumption, and settlement facts.

Current governed Planning-field changes also retain their distinct persisted Marco-authorization
authority. An authorization is not interchangeable with an audit row or an operation attribute:

```text
marco_authorizations
  authorization_id
  authority_generation_id
  task_id
  operation_id                 nullable
  governed_field
  exact_before_value
  exact_after_value
  reason
  granted_by
  granted_run_id
  granted_at

marco_authorization_reservations
  reservation_id
  authorization_id
  reservation_request_id
  reservation_execution_id
  reserved_at
  released_by_fact_id          nullable
  released_at                  nullable

marco_authorization_consumptions
  consumption_id
  authorization_id             unique; one consumption at most
  reservation_id               unique
  consumed_request_id
  consumed_execution_id
  consumed_version_id
  consumed_at
```

Grant evidence and exact task/optional-operation, field, before, after, reason, actor, and run
bindings are immutable. A governed mutation atomically resolves and reserves the complete exact set,
fails closed on missing or ambiguous authority, and consumes each capability at most once with the
committed candidate/version occurrence. Reservations are append-only attempts. Abandonment or a
named terminal failure may append release evidence for an unconsumed reservation without erasing it;
the unchanged grant then returns to availability only where the preserved current contract permits a
new exact reservation. Unused grants migrate exactly. Production cutover permits no active,
ambiguous, or orphaned reservation: every reservation is consumed, released by named evidence, or
quarantined with the operation that owns it. Retiring or weakening this authority requires a separate
Marco decision.

Implementation design must include a row-by-row **current-authority coverage matrix**, not only a
conceptual-table mapping. At minimum it accounts for `service_requests`, `audit_events`,
`task_content_state`, `content_versions`, `operations`, `verification_cycles`, write/movement and
operation-execution attempts, `operation_steps`, `operation_actor_facts`, `marco_authorizations`,
`command_audit_repairs`, `two_pass_resets`, `dish_inspect_facts`, `planning_intent_challenges`,
`submission_terminal_intent`, `planning_reopen_attempts`, service leases, `backup_creations`,
abandonment/succession facts, schema migrations, and every durable external sidecar. For each source
it names the target authority relation or exact preserved witness, identity remapping, open/terminal
cutover rule, semantic validation, and retirement rule. A generic audit event or causality link is
never a substitute for a named current authority source.

Alembic's current revision is a readiness projection, not complete historical authority. Preserve an
append-only schema-migration provenance relation that records the Alembic revision and predecessor,
immutable migration/code identity, Dish release, database-authority generation, initiating operator
or deployment authority, start and terminal outcome, and application time. Any governed downgrade,
repair, or stamp appends new evidence instead of rewriting earlier history. Imported legacy
`schema_migrations` rows are linked to exact preserved provenance or an explicit recorded retirement
rule.

A narrow append-only command-causality index may be added or evolved from existing execution/step
relations. It is task- or operation-bound as facts become known, rather than requiring every command
to have an operation:

```text
command_fact_links
  entry_id
  task_id                     nullable for pre-task command facts
  operation_id                nullable
  verification_cycle_id       nullable; present for cycle-specific commands
  request_id
  execution_id                required for committed mutation facts
  command_kind
  created_fact_references    ordered typed references to exact committed domain facts/effects
  committed_at
```

Exactly one immutable link is emitted only when a command's authoritative domain facts commit.
Every such link references the durable command execution that committed those facts. Admission-only
request results that create no authoritative domain fact require no command-causality link. The link
answers: “which committed command produced these exact facts?” It does not represent command start,
pending, failure, uncertainty, abandonment, or recovery as an independent lifecycle. Those remain
authoritative in `service_requests`, `command_executions`, executor claims, operation/cycle rows,
and named recovery facts. A command that commits no authoritative domain fact needs no causality
link merely to prove that execution was attempted.

The index must not copy `control_state_before`, `control_state_after`, canonical before/after state,
a terminal command classification, or a generic `compensation_state` as independently interpretable
authority. Current state is read from the task, operation, cycle, request, execution, and named
domain facts. Compensation or rollback authority lives in a named domain relation—such as
abandonment, lease release, hold resolution, movement/write recovery, or a future explicit reversal
transition—and the causality index references that committed relation.

The index and domain facts provide the journal Marco requires for intermediate system operations:
Dish can identify exactly which commands committed and process any named reversal or reconciliation
in order. Ordinary PostgreSQL-local command effects do not require a general undo log because one
command's task/workflow changes commit atomically or not at all. Earlier command boundaries such as
`start` or `inspect` remain durable history; abandoning their attempt terminalizes or supersedes the
operation/cycle rather than deleting those facts. Before cutover, external Asana effects continue to
use the existing domain-specific write/movement attempt and reconciliation records. After cutover,
Asana is only an outbox projection and is repaired through projection evidence, not workflow
rollback.

There is no Stage A `checkpoint` or draft-journal command and no requirement to store an agent's
unpublished notes or partial candidate. The final complete candidate first enters Dish through the
existing complete `prepare` or Verification-decision payload.

Planning and Research `start` create/bind the operation but do not change canonical task content. A
pre-construction Research hold records durable workflow control state but still does not change
canonical content. Verification `start` and `inspect` bind the exact review subject and append review
evidence but do not change canonical content. In the target contract, `inspect` is explicitly a
replay-bound evidence mutation: it has a service request, command execution, exact idempotency and
causality evidence, but it neither advances `tasks.current_version_id` nor `task_revision` unless a
separate named command changes a canonical/projected axis. The pre-cutover adapter may preserve the
legacy public shape, but Phase 2 and post-cutover implementation must not route `inspect` through a
pure read path.

A **content-boundary command** is any complete named governed command that intentionally changes
the authoritative title/body. The retained Stage A boundaries include at least:

- completed Planning `prepare`;
- completed Research `prepare`, including governed non-material Change completion;
- one complete Verification decision, including corrected candidate, signoff, successor-cycle, or
  hold-route content as applicable;
- governed schema migration;
- Marco-authorized two-pass reopen;
- Evidence/Human hold resolution that writes the resumed document, whether or not it installs a material candidate;
- post-signoff destination repair;
- any other retained current command proven by code characterization to write canonical content.

Submission is normally a governed location/terminal transition after an approved round; it creates a
new version only when its exact retained command contract also changes authoritative title/body.

A content-boundary command atomically writes every complete immutable version required by its exact
lineage, activates exactly one as current, and commits the workflow transition, immutable command
outcome, execution evidence, audit, and projection event. Commands
such as `start`, `inspect`, lease changes, and control-only holds may commit control/evidence state,
but `tasks.current_version_id` remains unchanged.

Recovery of an abandoned attempt restores actionable workflow control to the last committed
canonical boundary while preserving history. Dish terminalizes or compensates the abandoned
operation/cycle, releases or fences ownership, and uses Part I's exact fresh-successor
operation/cycle rules where current recovery requires them. Stage A does not introduce
same-operation takeover or unfinished-authority transfer.

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
```

A version's identity scheme is immutable and domain-separates the representation, framing,
normalization, and digest algorithm used to produce `canonical_identity`. Initial schemes should
be explicit values such as `dish-bare-v1`, `dish-title-body-v1`,
`dish-structured-planning-json-v1`, and `dish-structured-dish-json-v1`. A digest is meaningful only
with its scheme; canonicalization version remains additional structured-JSON provenance and does
not replace the cross-representation identity scheme.

A version's intended destination, when that document kind carries one, is also exact immutable
version-owned authority rather than a value re-resolved from current location names at read time:

```text
task_version_destinations
  version_id                  unique
  destination_location_id
  matched_location_alias_id  nullable; exact Stage A compatibility alias
  embedded_name              nullable; exact title/body witness
  embedded_identifier        nullable; exact title/body witness
```

Bare versions have no destination row. Stage A Planning/canonical title-body versions store the
exact internal location plus the exact alias/name/identifier pair validated from their body.
Structured versions store the internal location and must agree with the destination in canonical
JSON; they do not require an Asana alias. Imported versions name the exact selected source-document
resolution. Representation-specific validation proves these fields agree with the immutable content
rather than becoming a second editable destination authority.

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
Only a complete named content-boundary command advances `tasks.current_version_id` and records the
required workflow lineage atomically. Partial, inconsistent, or incomplete-attempt version graphs
never become current.

Version rows are fully immutable. Current-version activation is separate append-only evidence:

```text
task_version_activations
  activation_id
  task_id
  version_id                  unique; one activation at most
  prior_version_id            nullable for creation/import
  task_revision
  provenance_kind             command | cutover_import
  request_id                  nullable; required only for command provenance
  execution_id                nullable; required only for command provenance
  task_import_origin_id       nullable; required only for cutover_import provenance
  legacy_workflow_import_run_id nullable; required only for cutover_import provenance
  cutover_approval_id          nullable; required only for cutover_import provenance
  reason
  activated_at
```

Activation provenance is a constrained tagged union: exactly one complete authority path is present.
Ordinary creation or mutation uses the exact request and command execution. Initial cutover import
uses the exact task import origin, legacy workflow import run, and cutover approval. Import atomically
creates the task, imported version, initial activation, initial task revision, and all enabled initial
state axes without fabricating a Dish request, command outcome, or workflow transition. After initial
creation/import, only a complete governed content-boundary command may advance the current pointer.

For command provenance, the activation row, task pointer, `task_revision` advancement, workflow
transition, immutable command outcome, and audit commit together. Revert, restoration of old content,
clone, or canonicalizer migration creates a new version with explicit source/predecessor lineage,
even when its canonical content equals an older version. It never reactivates the old row or inherits
that row's Verification merely because the identity matches. Whole-system database restore remains
an operational rollback to a compatible historical state, not a normal version reactivation.

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
  content_identity_scheme
  content_identity
  source_completed
  source_modified_at
  observed_at

asana_membership_observations
  membership_observation_id
  batch_id
  source_task_gid
  source_project_gid
  source_section_gid          nullable only when Asana reports project membership without a section
  source_section_name        nullable
  observed_at

asana_section_observations
  batch_id
  source_project_gid
  source_section_gid
  source_section_name
  display_order
```

Every batch also names an immutable, versioned `corpus_scope_contract`. That contract declares the
exact projects, sections, Cooking History sources, archive sources, completed-bare-task treatment,
source pagination/closure rules, project and section membership interpretation, and handling classes
for inaccessible, deleted, duplicate, or malformed tasks. Completeness is proven against that named
contract, not against whatever the importer implementation happens to enumerate on that run.

A batch has a durable monotonic sequence assigned at creation; UUID equality or ordering is never
used to interpret historical validity. A batch is complete only when its task set, exact content identities, **all** in-scope project and
section memberships, source completion states, source-document witnesses, and section registry are
captured and its corpus manifest is deterministically hashed.

The manifest hashes the complete canonical corpus relation, not a multiset of document digests. Each
task row includes source task GID, qualified content identity, source completion state, and the
matching source-document witness identity. Each membership row includes source task GID, source
project GID, section GID/name when present, and the membership interpretation required by the corpus
contract. Each section row includes source project GID, section GID, section name, and display order. Canonical
ordering is explicit. Every digest is qualified by its identity scheme. Batch-local identifiers and
observation timestamps are excluded so two independent frozen enumerations of the same corpus
produce the same manifest identity; swapping documents between tasks or changing placement cannot
produce the same manifest.
Batch completion additionally requires exactly one source-document witness for every in-scope task
observation, matching observation/document linkage, scheme, and identity, every expected membership
and section observation, no duplicate task GIDs within a batch, and no duplicate membership tuple
for one task/project/section. A task may legitimately have several project memberships. A source
document linked into the batch manifest for an observation outside that batch also invalidates
completeness. Database constraints
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
  legacy_authority_bundle_id   unique
  asana_manifest_identity
  approved_by
  approved_at
```

Only a complete `cutover` batch may be approved. Its matching batch must be an earlier complete
`cutover` batch with the same manifest identity and exact closed corpus facts. The authority
approval repeats the immutable Asana manifest identity, names the complete frozen legacy Dish
runtime authority bundle, cannot be changed or cleared, and is rejected if either batch, manifest,
or authority bundle does not match. Repeated shadow and reconciliation rows remain comparison evidence. They cannot become task origin authority
merely because they are newest or individually complete.

Cutover also binds one exact frozen **legacy Dish runtime authority bundle**. SQLite is not the
entire current authority: restore control and database ownership deliberately include durable
sidecars outside the replaceable database. The bundle manifest records at least:

```text
legacy_runtime_authority_bundle_manifests
  legacy_authority_bundle_id
  legacy_authority_generation_id
  sqlite_snapshot_identity
  sqlite_snapshot_method       online_backup_api | proven_checkpointed_bundle
  sqlite_wal_closure_proof
  schema_migration_level
  dish_release_identity
  honest_release_identity
  semantic_validation_status
  table_count_manifest
  open_state_manifest
  database_ownership_marker_identity
  database_ownership_marker_status
  restore_request_journal_manifest
  restore_fault_marker_identity       nullable
  restore_fault_status
  associated_restore_artifact_manifest
  audit_repair_main_manifest          nullable
  audit_repair_importing_manifest     nullable
  audit_repair_capture_status
  created_at
  complete
```

The SQLite identity names a transactionally complete validated snapshot produced by the SQLite
online-backup API, or an equivalently proven checkpointed database bundle with no committed pages
left outside the captured image. Hashing or copying only the live main database file is insufficient
because committed WAL state may not yet be incorporated.

Completion fails closed unless the SQLite snapshot is the canonical service-owned target, WAL
closure is proved, no restore is active or ambiguous, no restore-fault marker is active or
unreadable, and every restore journal entry is terminal and consistent with the exact legacy
authority generation being imported. Capture is coordinated with the invocation-audit repair lock and accounts for both
the normal emergency JSONL sidecar and any atomically claimed `.importing` file. Every valid repair
record is replayed to a proven zero-pending state before the snapshot or imported into PostgreSQL
with exact row-level provenance; malformed records are preserved in explicit quarantine. The lock
file is capture protocol, not historical authority. Any unreadable, concurrently changing, or
unaccounted sidecar makes the bundle incomplete. Terminal historical sidecar evidence is preserved
in immutable migration provenance or retired only through an explicit recorded migration rule; it
is never omitted because it is not inside SQLite.

A cutover authority approval is valid only when it names both matching complete Asana cutover
batches and one complete frozen legacy runtime authority bundle. The importer must prove exact links
between imported workflow facts and the approved Asana observations/source documents. The Asana
corpus alone cannot establish Verification, replay, Planning-intent, lease, abandonment, operation,
or restore authority.

Only a cutover authority approval that binds the two matching complete Asana batches and the
complete legacy runtime authority bundle may establish imported task origins:

```text
task_import_origins
  task_id
  cutover_approval_id
  source_observation_id
  resolved_location_id
  selected_placement_membership_id
  selected_destination_resolution_id   nullable
  imported_at
```

The origin links the authoritative imported task to exactly one task observation in the authority
approval's selected Asana batch, while preserving all membership observations for that source task.
It records the exact selected membership and location resolution used for the initial workflow/catalog
projection. `source_completed` is immutable provenance **and** initializes the separate authoritative
`tasks.completed` Planning-gate projection; it is never interpreted as evidence that the dish was
cooked. The initial database-first migration does not require a Cooked axis or Cooking History
import. If Cooked has been separately introduced before a later import or cutover re-baseline, its
initial value requires exact approved-batch Cooking History membership, a separately audited import
decision based on explicit human evidence, or a governed Cooked transition. Import facts never
fabricate ordinary Dish transitions for product concepts that did not yet exist. Quarantine records may cite observations,
but only separately audited promotion from an approved cutover authority may create a task origin.

Every imported legacy workflow row is also traceable to the approved frozen runtime authority
bundle and exact source row identity through one migration run:

```text
legacy_workflow_import_runs
  migration_run_id
  cutover_approval_id
  legacy_authority_bundle_id
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

The importer proves that Planning-intent challenge, operation, cycle, request, lease, effect,
abandonment, restore-sidecar provenance, and audit references resolve to the same imported Dish
task/version occurrences established from the approved Asana batch. Missing or contradictory links are reconciled or quarantined; they are never inferred.

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

Parsing and classification evidence derived from a source document records orthogonal facts before
any authoritative version is created:

```text
document_kind              bare | planning_brief | canonical | unknown
validation_status          unvalidated | valid | invalid | unsupported_schema
declared_schema_version    nullable version claimed by the source document
validated_schema_version   nullable schema against which validation actually succeeded
parser_or_validator_version
recorded_at
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

Such a version uses a one-to-one `title_body_document_versions` row:

```text
version_id
document_kind              planning_brief | canonical | unknown
body
validation_status          valid | invalid | unsupported_schema
declared_schema_version    nullable
validated_schema_version   nullable
protocol_release_identity  nullable
```

The row contains the exact body, kind, and schema/release provenance, plus the exact version-owned
destination relation when that document kind carries a destination. A current **governed** Stage A
title/body version must be a supported `planning_brief` or `canonical` document valid under its
recorded contract. A `historical_read_only` task may point to an exact imported invalid,
unsupported, or unknown version for browse/search/projection only; that occurrence grants no
workflow authority and cannot be reinterpreted or promoted without a named governed command.
Otherwise such material remains quarantine. Bare content uses the separate `bare`
representation kind rather than a title/body row. Stage A does not store unfinished agent drafts. A complete named content-boundary command may create the immutable version or versions required
by its exact workflow lineage, and only such a complete governed command advances the canonical
pointer. Imported source documents are never overwritten.

The document-compatible store uses the same task pointer, transaction, replay, workflow, location,
orthogonal location/completion/cooked/archive, audit, command-causality, and recovery contracts. It preserves source documents and
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

Replace the Asana section registry with controlled Dish workflow/catalog locations:

```text
location_id                 stable Dish identifier
current_name                unique current display name
role                        research_queue | verification_queue | destination | unrouted
active                      whether new routing may target it
display_order
```

Archive and cooked history are not location roles. Archive is an orthogonal task disposition;
cooking is an orthogonal projection plus append-only history. Sourcing, Reference, malformed
imports, and other non-live corpus classes remain source snapshots, reconciliation records, or
quarantine unless a later approved import policy promotes them into authoritative tasks.

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

task_location_stage_a_compatibility_selections
  selection_id
  location_id
  alias_id
  predecessor_selection_id    nullable
  selected_by_request_id      nullable for cutover import selection
  selected_at
  retired_at                  nullable
```

A location may therefore have multiple historical Asana aliases, while each alias resolves to
exactly one Dish location for its declared batch interval. Alias rows remain immutable; optional
retirement is separate append-only evidence, and interval interpretation uses durable batch
sequence rather than UUID ordering. Retirement must reference a batch at or after the alias's
starting batch and may be recorded at most once. Aliases are provenance and compatibility evidence, not routing authority. For every Stage A active
destination that may appear in newly authored title/body content, exactly one current compatibility
selection names the alias returned by `sections` and accepted for new version creation. Replacing
that selection is a governed append-only transition and never reinterprets an older version, which
retains its own exact matched alias. `source_document_destination_resolutions.matched_alias_id`
records the exact alias used for an embedded destination;
`task_import_origins.selected_placement_membership_id` records the independently selected source
placement while all other memberships remain preserved evidence.

Exactly one active Research Queue and Verification Queue are required. Other approved Cooking
sections import as destinations. The initial database-first baseline does not require Cooking
History or Archived membership. If either feature has separately become governed before a later
re-baseline, its source membership is preserved as provenance and initializes the corresponding
feature only under that feature's approved import contract; neither becomes a routing location. If
a safe current workflow/catalog location cannot be proven, use the controlled `unrouted` location
or quarantine rather than inventing one. Removing or repurposing a referenced location is
prohibited; retire it instead.

The destination resolver is version-aware:

- a structured dish version stores the authoritative Dish `destination_location_id`;
- the renderer may show that location's `current_name`, but the display name is not part of
  structured identity;
- an imported pre-cutover source document may contain the exact immutable Asana section GID mapped
  by a version-appropriate location alias;
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

### Operability disposition

`tasks.operability_state` distinguishes current workflow authority from exact read-only history.
Import origin initializes it. Any later change is append-only governed evidence:

```text
task_operability_transitions
  transition_id
  task_id
  old_state                    governed | historical_read_only
  new_state                    governed | historical_read_only
  source_version_id
  target_version_id            nullable when moving to read-only without content change
  request_id
  execution_id
  reason
  occurred_at
```

Promotion to `governed` requires a supported exact target version and all workflow/schema evidence
needed by the named route. An ordinary failure or unsupported parser result never flips the mode
implicitly. Demotion to historical read-only is not a substitute for archive, abandonment, or
quarantine and requires its own explicit administrative reason.

### Feature-gated Archive disposition

This section fixes Archive's semantics for a later governed feature; it is not required for the
initial database-first shadow or authority cutover. When introduced, Archive is an orthogonal
lifecycle disposition with append-only evidence:

```text
task_archive_transitions
  transition_id
  task_id
  old_state                    active | archived
  new_state                    active | archived
  request_id
  execution_id
  operation_id                 nullable
  reason
  occurred_at
```

`tasks.archive_state` is the current projection. Archive does not rewrite canonical content,
intended destination, cooked history, or current workflow/catalog location. An archive command is
rejected while an operation owns the task unless a separately approved route first resolves that
operation. Restoring an archived task changes only the archive disposition unless the same governed
command explicitly records a separate location transition.

### Completion and Planning eligibility

The current Asana completion flag remains a separate authoritative lifecycle axis because it gates
Planning admission for completed bare tasks. It is not cooked evidence and is not archive state.

```text
task_completion_transitions
  transition_id
  task_id
  old_completed
  new_completed
  purpose
  request_id
  execution_id
  actor
  occurred_at
```

`tasks.completed` is the current projection. Imported `source_completed` initializes it exactly.
The retained Marco-only `reopen-planning` transition is legal only for the same completed bare-task
predicate as today and clears completion while preserving exact content and location. It does not
clear Cooked, archive, cook history, or another workflow fact. Any later command that sets completion
must be separately named and governed rather than inferred from cooking or archival placement.

### Feature-gated Cooked history

This section fixes Cooked's separation from Completion and Archive for a later governed feature; it
is not required for the initial database-first shadow or authority cutover. When introduced,
Cooking is expressed through a distinct current projection and append-only history:

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
cooked, clearing that projection through a separately approved cooked-state route, or applying
another governed cooked-state change appends
one transition and commits it with the projection, governed audit, lifecycle evidence, and canonical
request result. A governed archive transition never sets `cooked`; a cooked transition never
silently archives. Cooking or `log-cook` remains legal while a workflow operation is open when it
does not change the exact content, location, or other facts on which that operation depends; the
command still serializes through the task mutation lane. A future `log-cook` command may append
richer cook records without allowing a cooking agent to mutate the signed task body.

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

Allowed relationships include at least `planning_handoff`, `research_handoff`,
`verification_approval`, `small_correction`, `large_correction`, `non_material_checkin`,
`schema_migration`, `two_pass_reopen`, `hold_resolution`, `destination_repair`, `revert`, `clone`,
`representation_migration`, and `canonicalizer_migration`. Both versions must belong to the same
task except for an explicit
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

Post-signoff destination repair has its own narrow authority and does not pretend that the repaired
version was independently reviewed. Approval remains bound to the exact approved cycle and approved
version. A separate append-only repair fact links that approved version, the exact failed
destination evidence, the repaired successor version, the one-field governed diff, Marco's reason,
and request/execution provenance. Submission is legal for the repaired version only through that
exact repair fact. Any unrelated content change requires the ordinary revision and Verification
route.

### Rendered views and projection evidence

For a structured version, Markdown, plain text, and Asana notes are deterministic renderings. They
are not parsed back into authority after cutover. A document-compatible current version is read
directly as its exact authoritative title/body rather than pretending that it is a structured
rendering. Where exact historical reproduction matters, store the renderer version, rendering
identity, and generated artifact or preserve the exact source document.

The fixed existing-project topology uses a versioned **projection routing contract** that maps
Dish workflow/catalog locations and every enabled completion/Cooked/Archive presentation to the
in-scope external projects, sections, completion flags, and memberships. Those external mappings are projector
configuration/evidence, never Dish routing or lifecycle authority. Every projection event pins the exact routing/renderer contract used to construct its complete
payload, so a later project or section change cannot reinterpret an older event. The baseline
routing contract covers the currently governed Cooking project; later projects or lifecycle
presentations join only through their separately governed feature rollout.

Database authority generations, projection epochs, and the one-time authority activation are
separate append-only evidence:

```text
authority_activations
  authority_activation_id
  cutover_approval_id
  legacy_workflow_import_run_id
  database_authority_generation_id
  projection_epoch_id
  alembic_head
  dish_release_identity
  honest_release_identity
  protocol_release_identity
  openapi_action_release_identity
  governed_action_coverage_proof_id
  prepared_at

authority_activation_events
  activation_event_id
  authority_activation_id
  event_kind                    activated | aborted | rollback_burned
  event_reason                  nullable
  created_at

database_authority_generations
  authority_generation_id
  predecessor_generation_id   nullable
  reason                       initial_cutover | destructive_restore
  external_restore_control_id  nullable
  restore_source_identity      nullable
  established_at

projection_authority_epochs
  epoch_id
  database_authority_generation_id
  predecessor_epoch_id         nullable
  reason                       initial_cutover | database_restore
  restore_source_identity      nullable
  established_at
```

Activation lifecycle is derived only from the append-only events. A prepared activation may append
exactly one `activated` or `aborted` event. An activated activation may append `aborted` only during
the still-authorized pre-admission rollback window. `rollback_burned` may occur only after
`activated`, at most once, and permanently forbids an abort or return to Asana authority. A crash at
any point resolves from these facts; process state or routing configuration never decides which
store is authoritative.

Mutation admission also uses a service-issued run capability:

```text
client_run_capabilities
  capability_id
  authority_generation_id
  owner_id
  client_run_id
  protocol_release_identity
  secret_verifier_or_key_id
  issued_at
  expires_at                   nullable
  revoked_at                   nullable
  revocation_reason            nullable
```

The capability is unguessable or cryptographically verifiable and is never supplied by the client as
self-asserted authority. Registration after destructive restore requires a fresh run identity and a
new capability; a pre-restore run ID or capability cannot be refreshed as a continuation. The
capability verifier key/epoch is rotated or freshly established from restore-control/service-secret
evidence outside the restored database timeline before readiness; a verifier restored from the old
timeline is never accepted as current.

A long-lived owner bearer credential alone is insufficient to mint the first post-restore run
capability. The operator/restart orchestration establishes a generation-specific bootstrap authority
or launch credential unavailable to processes that survived from the superseded generation. Queued
requests and automatic retry buffers do not cross the restore boundary. When erased logical work is
intentionally submitted again, an explicit `reissue_authorization` or equivalent operator-visible
evidence binds the deliberate reissue to the new generation. A stale process cannot become current
merely by choosing a fresh UUID and repeating normal registration.

Exactly one database authority generation and one projection epoch are current. Initial cutover
establishes both through one prepared authority activation plus exactly one append-only `activated`
event that binds the exact approved import, release/schema/API set, and governed-action proof.
PostgreSQL mutation admission remains closed before activation and after an `aborted` event. Every accepted mutation request and client run is bound
to the current database authority generation, the activated authority record, and a valid service-
issued run capability. An exclusive destructive restore
establishes a new generation before normal mutation admission resumes; requests, runs, and
capabilities from earlier generations fail closed and must be reissued deliberately under a fresh run
identity. A corresponding fresh projection epoch fences all pre-restore projector work and triggers
full downstream reconciliation. Projection epochs affect only the non-authoritative Asana copy;
database authority generations govern request admission, execution, replay, and current views. A
caller-provided generation value is routing context, never mutation authority, and an old process
cannot become current by merely reading and echoing the new generation. Neither generation nor
capability manufactures task revisions or workflow facts.

Each PostgreSQL mutation that affects the Asana view—including content, placement, completion,
and any enabled Cooked or Archive presentation—appends one immutable projection event in the same
authoritative transaction. Projection history, delivery attempts, and operational summary are
separate concepts:

```text
projection_events
  event_id
  projection_epoch_id         current PostgreSQL projection authority epoch
  task_id
  projection_sequence         monotonic per task within the epoch
  task_revision               exact authoritative task revision being rendered
  source_execution_id         exact command or system-maintenance execution
  event_kind
  projected_state_reference   immutable complete payload or references sufficient to reconstruct it
  payload_identity
  renderer_contract_identity
  projection_routing_contract_identity
  created_at

projection_attempt_intents
  attempt_id
  event_id
  mapping_id                  nullable until a create attempt confirms mapping
  worker_token
  worker_generation
  external_correlation        nullable
  attempt_kind                deliver | reconcile | stale_check
  prepared_at

projection_attempt_outcomes
  attempt_id                  unique; one terminal adjudication at most
  completed_at
  effect_outcome              nullable; confirmed | not_applied | uncertain
                              null only when no external effect was attempted
  delivery_disposition        applied | superseded | stale_noop | retryable_failure | blocked | dead_lettered
  replacing_event_id          nullable; required when superseded
  before_observation          nullable immutable evidence
  after_observation           nullable immutable evidence
  error_class                 nullable
  error_detail                nullable

asana_projection_mappings
  mapping_id
  task_id
  asana_task_gid              globally unique external mapping
  mapping_generation          monotonic per task
  predecessor_mapping_id      nullable; exact retired predecessor when replacing
  origin_kind                 imported_existing | projected_create
  source_import_origin_id     nullable
  created_by_event_id         nullable
  created_at

asana_projection_mapping_retirements
  mapping_id                  unique; one retirement at most
  reason
  retired_at

asana_projection_state
  task_id
  projection_epoch_id
  active_mapping_id           nullable while creation is unresolved
  last_applied_projection_sequence
  last_applied_task_revision
  last_attempted_projection_sequence
  next_retry_at               nullable
  projection_health           pending | current | retrying | uncertain | blocked
  last_error                  nullable
```

A database-authority epoch is established at initial cutover and replaced after any PostgreSQL
restore that may move authority backward. Every event and projected Asana marker carries the exact
epoch. Sequence ordering applies only inside one epoch; an external marker from another epoch is
stale downstream evidence and can never outrank the current PostgreSQL epoch.

`projection_events`, `projection_attempt_intents`, and `projection_attempt_outcomes` are append-only
evidence. Before any external call, the worker commits one exact attempt intent bound to its current
worker claim/generation and stable correlation marker. After the call and required reread, it appends
at most one outcome for that intent. A crash may therefore leave an intent with no outcome; exact
reconciliation settles that same intent when the external result can be proved, while a genuine new
retry appends a new intent. Every task mutation that changes the Asana-rendered view appends exactly
one event in its authoritative transaction, uniquely bound to the originating execution and task
revision so exact replay cannot duplicate it. A
projection-only refresh—such as a renderer-contract rollout, mapping replacement, or drift repair—may
append another event for the same current task revision without fabricating a task mutation; it has
a new per-task `projection_sequence`, an explicit maintenance reason, exact current-state
references, and a durable command or system-maintenance execution. `effect_outcome` records what the exact external call did when that is knowable, while
`delivery_disposition` records what the worker does next; a retry/dead-letter decision never
overwrites or masquerades as effect evidence.

Each confirmed `task_id` ↔ `asana_task_gid` mapping row is immutable and may serve only as a
compatibility lookup. Its origin is exact: a task imported at cutover binds the approved import
origin, while a task created after cutover binds its confirmed non-supersedable projection-create
event. Exactly one origin path is present. At most one unretired mapping is active for a task. A deleted or deliberately
replaced mirror task appends mapping-retirement evidence and a new mapping generation; an old GID is
never reassigned to another Dish task. Revision, retry, active-mapping, and health fields live only
in the mutable `asana_projection_state` summary, which never replaces event, attempt, or
mapping-lifecycle history. The exact projected state must remain durably reconstructible even after
newer task revisions exist; a payload identity that can only be recomputed from the latest task
pointer is insufficient.

A separate worker renders and applies committed events. Required behavior:

- processing is ordered by current `projection_epoch_id` and then `projection_sequence` per task;
- workers lock or otherwise serialize one task mapping while applying an event;
- an event whose projection sequence is at or below `last_applied_projection_sequence` is an
  idempotent stale no-op, recorded through an attempt intent and outcome rather than rewriting event
  history;
- a projection-only refresh must reference the then-current task revision, and within one epoch no
  event may project an older task revision after a newer task revision has already been applied;
- after restore, a newly established epoch fences pre-restore workers and triggers a complete
  reconciliation/refresh of every current mapping; downstream tasks or markers that exist only in a
  superseded epoch are identified by durable origin/epoch markers, retired or clearly isolated by
  the projector's recovery policy, and never imported into PostgreSQL authority;
- mapping creation is non-supersedable and uses a stable correlation marker plus reconciliation
  lookup so a lost response cannot create duplicate Asana tasks;
- a continuous corpus reconciler enumerates the complete in-scope Asana project set and classifies
  every visible GID as an active imported/projected mapping, an unresolved projector-create
  correlation, an explicit non-authoritative isolation, or a blocking unknown; an unmapped object is
  never ingested as authority and must be isolated or conspicuously removed from the governed
  working surface before projector readiness is healthy;
- after mapping exists, complete-state update events may be coalesced only when skipped revisions
  have no distinct human-visible effect that must be preserved; each skipped event receives an
  append-only stale-check intent and `superseded` outcome referencing the replacing event;
- each attempt records the exact renderer/projection contract, worker fencing generation, external
  correlation, request outcome classification, and before/after observations needed to distinguish
  `confirmed`, `not_applied`, and `uncertain`;
- uncertain effect evidence, retryable delivery failure, blocked/manual state, and dead-lettered
  delivery disposition have distinct meanings; they are not collapsed into one ambiguous `failed`
  state;
- a worker may not mark revision N applied using an observation or payload for revision N+1;
- failures remain visible and recoverable without changing PostgreSQL authority.

Projection failure never blocks, rolls back, or reclassifies the PostgreSQL mutation. Command and
read results expose the committed task revision and projection health so a stale Asana view is not
mistaken for authority. Out-of-band Asana edits are detected and overwritten or flagged; they are
never ingested as new authority. Any duplicate is explicitly a mirror artifact and must not appear
as a second Dish task.

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
- current governed versions are complete and valid for current mutation; current historical-read-only
  versions are exact imported occurrences with no ordinary mutation authority; neither may be a
  shadow candidate;
- completed observation batches satisfy source-document, membership, and section closure with no
  duplicate task identifiers or duplicate task/project/section memberships, matching qualified
  identities, and monotonic completion;
- cutover approval is append-only and names one matching earlier complete Asana batch plus the exact
  complete frozen legacy Dish runtime authority bundle;
- quarantined imports cannot be promoted or resolved through ordinary task commands;
- location, completion, and operability projections—and Cooked or Archive projections only when
  enabled—match the import origin plus latest post-import transitions;
- imported Asana aliases and projection mapping history never resolve one Asana GID to different
  Dish tasks;
- one committed mutation that changes canonical/projected task axes advances `task_revision` exactly once; control-only mutations leave it unchanged.

Use composite foreign keys, uniqueness constraints, checks, and declarative PostgreSQL
constraints wherever possible. Use triggers only for invariants that cannot be expressed safely
otherwise. Semantic validation has explicit operating tiers:

- database constraints enforce local invariants continuously;
- command-time validation proves the facts created by that mutation;
- readiness checks are bounded to current pointers, open operations/cycles, active leases/claims,
  pending requests, and other facts that can affect present legality;
- complete historical validation runs during migration, import, cutover, schema upgrades, and
  explicit administrative audits;
- a historical diagnostic anomaly does not force an unbounded startup scan or block readiness
  unless it can affect current authority or mutation legality.

Quarantine remains outside authoritative `tasks` and ordinary service reads. Promotion is a
separately audited import action that inserts a proven task and its origin state; it is not a
status flip on an otherwise authoritative task.

The reused Asana project set may not contain an ambiguous authority surface when PostgreSQL mutation
admission opens. Every Asana task still visible in an in-scope downstream project must either have
exactly one authoritative Dish task and active/imported projection mapping, or have been explicitly
isolated from the projection surface under the cutover quarantine manifest. An unresolved or
unisolated quarantine blocks cutover. Isolation preserves the source task/GID and exact observation
but places or marks it outside ordinary projected workflow so direct edits cannot be mistaken for
Dish inputs. Later promotion is separately audited, may reuse the original Asana GID only after
establishing one exact mapping, and removes the isolation marker without creating a second Dish task.
The exact temporary Asana section/project used for isolation is a cutover-plan choice; the authority
invariant is not optional.

### Request execution ownership

Separate the immutable replay envelope from its expiring executor claim. Every task/workflow
mutation request, including an operation-scoped command, permanently records the following. The
admission-only first Planning-intent request uses `service_requests` plus
`planning_intent_challenges` and deliberately does not create a `command_execution`:

```text
service_requests
  request_id
  authority_generation_id
  owner_id
  client_run_id
  run_capability_id             nullable only for imported terminal pre-capability requests
  command
  request_contract_version
  payload_identity
  dish_release_identity
  honest_release_identity
  content_schema_version         nullable
  task_id                        nullable
  operation_id                   nullable
  adapter_version                nullable
  canonicalization_version       nullable
  reserved_task_id               nullable deterministic output identity
  canonical_candidate            nullable immutable derived payload
  status                          pending | completed | uncertain
  initial_result                  nullable; immutable first terminal outcome
  resolution_result               nullable; immutable append-once resolution of uncertainty
  created_at
  initially_completed_at          nullable
  resolved_at                     nullable
```

The database authority generation, authenticated principal/owner, fresh run identity,
service-issued run capability, command, canonical argument identity, Dish/Honest release and schema
pins, adapter/canonicalization pins, and reserved outputs are immutable after reservation. A mutation
request must present a valid capability bound to the current generation, owner, run, and protocol
release. Merely echoing the generation exposed by bootstrap or `current_view` is never sufficient.
A request, run, or capability from a superseded generation is rejected before replay lookup or task
authority evaluation and is never admitted as new work merely because PITR removed its former row.
After destructive restore, reconnecting requires a newly registered run identity and newly issued
capability; an old process may not reuse its previous run ID as a continuation. A missing, invalid,
unknown, revoked, or superseded capability fails closed and returns guidance to reconnect and
reissue rather than attempting compatibility inference. Imported terminal legacy requests may retain
no capability only as immutable replay/migration witnesses; they are never admitted for continued
execution under the new generation. Task/operation bindings append only when proved. Request settlement is first-writer-wins and monotonic: `pending` advances once to
`completed` or `uncertain`; only an `uncertain` request may later advance to `completed`, and only by
appending a separate immutable `resolution_result` without rewriting its `initial_result` or first
completion time. The effective canonical result returned by exact replay is the resolution result
when present, otherwise the initial result. Both outcomes remain permanently inspectable. They record
the committed task revision/version, operation/cycle identifiers, created facts, and recovery
classification applicable at that settlement point; neither claims to describe what the principal
may do now.

Legacy SQLite request results are migrated without turning their stored `allowed_actions` into
current authority. Preserve each exact original initial/resolution JSON as an immutable legacy
result witness, and separately derive the new action-free initial/resolution outcome used by this
contract. Compatibility responses combine that outcome with a freshly derived current view; they
never copy historical action fields back as legal now.

Every mutating request also has one durable command-execution record that survives worker expiry
and can own task-scoped recovery before an operation exists:

```text
command_executions
  execution_id
  authority_generation_id
  request_id                  unique within authority generation
  task_id                     nullable; set when known
  reserved_task_id            nullable for create before insertion
  operation_id                nullable; append-only when/if created or resolved
  command
  baseline_evidence
  status                      started | completed | uncertain
  initial_terminal_evidence   nullable; immutable first completion/uncertainty evidence
  resolution_evidence         nullable; immutable append-once uncertainty resolution
  created_at
  initially_completed_at      nullable
  resolved_at                 nullable
```

The execution baseline is resource-appropriate: request and reserved identity for `create`, exact
task state for task-scoped commands, and exact task/operation/cycle state for operation-bound
commands. Identity and baseline are immutable; bindings append only when proved. `started` advances
once to `completed` or `uncertain`; only `uncertain` may later advance to `completed`, by appending
separate resolution evidence while preserving the original uncertain evidence and timestamp.
Settlement is first-writer-wins and supplies the durable recovery identity referenced by mutation
fences. Legacy `operation_executions` import into or remain a strict operation-bound subtype of this
contract.

Executor ownership and domain mutation exclusion are separate contracts.

```text
request_execution_claims
  claim_id
  authority_generation_id
  request_id                  unique active claim per request/generation
  owner_token
  claim_generation
  claimed_at
  expires_at
  completed_at                nullable

operation_mutation_fences
  fence_id
  authority_generation_id
  operation_id
  execution_id
  request_id
  fence_generation
  state                       active | unresolved | released
  acquired_at
  released_at                 nullable

task_mutation_fences
  fence_id
  authority_generation_id
  task_id
  execution_id
  request_id
  fence_generation
  state                       active | unresolved | released
  acquired_at
  released_at                 nullable
```

Fence identity and acquisition history are durable. PostgreSQL enforces at most one `active` or
`unresolved` fence per operation and at most one per task mutation lane, while released fence rows
remain immutable historical evidence and do not prevent later commands from acquiring a new
generation.

The expiring request claim says which worker may execute or recover one request. The operation/task
fence says whether another mutation may begin against that domain resource. An unresolved or
uncertain execution remains a fence after its worker claim expires; only exact replay/recovery or a
named administrative reconciliation may release it. Thus two different request IDs cannot both
begin live mutation of the same operation and merely rely on later row locking to discover drift.

A live foreign request claim or mutation fence returns one stable non-terminal
`REQUEST_IN_PROGRESS` or named unresolved-state result rather than executing. The response never
exposes owner tokens. Recovery increments the request-claim generation and issues a new owner token within the same
current database authority generation. Every effect transaction rechecks the database authority
generation, exact worker token/claim generation, and matching task/operation fence under lock; a
displaced executor or pre-restore process cannot commit.

Commands acquire locks in one documented order:

1. immutable service request and request-execution claim;
2. task row and task mutation lane where applicable;
3. operation row and operation mutation fence where applicable;
4. Verification cycle;
5. governing service lease/actor authority;
6. exact version, transition, and other domain rows required by the decision.

Commands that start an operation acquire the task lane before creating and fencing the new operation.
`create` has no pre-existing task row: its replay-bound reserved UUID and uniqueness constraints are
the contention authority until the task is inserted, after which any continuing mutation uses the
ordinary task lane. Commands already bound to an operation acquire both the task row and the
operation fence in the same order. Completion of the authoritative facts and canonical result
releases the relevant mutation fence atomically. If the outcome remains unresolved, the fence
remains durable even when no worker currently owns the request.

Stage A may initially deploy one active Dish mutation-service instance, but correctness must not
depend on hostname/PID liveness. If single-instance operation is required operationally, enforce it
with a PostgreSQL advisory/application lock and health reporting; database-fenced claims and mutation
lanes remain the correctness authority.

Request replay must never reinterpret a compatibility payload under newly deployed parsing or
canonicalization code. Reservation persists the exact request contract and version pins. For an
adapter-based request, prefer persisting the already-derived canonical candidate or its immutable
identity-bearing representation before execution ownership; otherwise recovery must retain the
exact adapter/parser implementation named by the request. Deployment normally requires no pending
requests, but quiescence is an operational gate rather than a substitute for a correct durable
recovery contract.


### Planning-start admission scope

Stage A preserves the currently implemented durable two-request Planning-intent authority.

The first connected Planning-start request is admission-only. It reserves and completes its exact
service-request result while issuing one immutable, owner/run/task/agent/target-bound challenge. It
must stop before ordinary workflow construction: it does not read or mutate the task, create an
operation, acquire a service lease, create a command execution, or take a task/operation mutation
fence.

A fresh replay-bound request may continue only by claiming that exact challenge under the same
principal and run bindings. The claimed challenge is single-use. Successful Planning admission
atomically consumes it with operation creation, operation/request bindings, lease acquisition where
required, and the canonical result. Failure before successful operation creation leaves the exact
challenge lifecycle and request outcomes recoverable under the current contract; implementation
must not infer user intent merely from workflow legality.

PostgreSQL shadowing mirrors issuance, claim, consumption, and Marco-authorized terminal settlement
exactly. Historical challenge facts and request bindings migrate as provenance. Production cutover
requires no `issued` or claimed-but-unconsumed challenge to remain; each is consumed or moved through
the audited, reason-bearing, permanently non-reusable `settled_unconsumed` transition before the
final legacy authority bundle is approved. Stage A does not silently drop or weaken this gate.

## Transaction contract

Planning-intent challenge issuance is the deliberate exception to ordinary mutation admission. Its
first request reserves/completes the request and appends the challenge in one local transaction, then
returns without task snapshotting, command execution ownership, task/operation fencing, workflow
planning, or lease handling. The fresh claiming request enters the ordinary transaction contract
only after exact challenge claim has succeeded, and successful Planning start consumes the challenge
in the same authoritative transaction as operation creation and request completion.

Request reservation and execution ownership remain durable admission steps because they must
survive a dead executor. They may commit before the task mutation, but they grant no task effect. A
pending `service_requests` row, command execution, executor claim, or mutation fence does not by
itself authorize a task effect.

Admission authenticates and validates the request envelope; reserves or matches the immutable
service request and any deterministic output identifiers; creates or matches its durable command
execution; and acquires the exact request-execution claim plus task/operation mutation fence. It
also acquires or validates the governing service lease according to the command's current contract.
Operation-scoped commands bind the exact operation and Verification cycle when applicable.
Task/request-scoped commands such as `create`, `start`, Verification `inspect`, Marco's
completion/reopen command, permitted bare-task title changes, and comparable lifecycle interventions
bind the task or reserved
task identity. Feature-gated Cooked or Archive commands follow the same rule only after enabled. These admission facts may be recovered or released after a
crash, but cannot be interpreted as a committed workflow mutation.

Where useful, request reservation stores deterministic output identity before execution. In
particular, `create` reserves its new task UUID on the replay-bound request. Exact concurrent
replays can observe or recover the same request, but cannot both create the task.

After admission, every database-native command has one effect transaction:

1. lock and reread the service request, command execution, executor claim token/generation, exact
   active task/operation fence, task or reserved task identity, operation, Verification cycle,
   governing lease, exact version occurrence, location, and other command preconditions;
2. reauthenticate the caller context used by the request and prove that every locked authority fact
   still matches the admitted command;
3. build the captured `AuthoritativeSnapshot`, invoke the shared deterministic domain kernel with
   the replay-bound intent and pinned inputs, and validate the resulting PostgreSQL-native
   `CommandDecision`;
4. append every required workflow, Verification, lineage, ownership, audit, and named transition
   fact, then append the immutable command-causality link referencing exactly those committed facts;
5. when this command is a governed content boundary, append every complete version graph required by
   the command's exact lineage, append exactly one activation, and advance the canonical pointer
   exactly once; otherwise prove the pointer
   remains unchanged;
6. append exactly one immutable ordered projection event when the resulting task revision changes
   the Asana-rendered view; projection-only maintenance events use a separate governed transaction
   and projection sequence without changing task authority;
7. finalize the operation/cycle state and any route-specific lease transition;
8. settle the command execution and service request under the first-writer-wins initial/resolution
   contract, and atomically release or retain the task/operation mutation fence according to the
   terminal or unresolved outcome;
9. retire the exact executor claim and commit once.

A command refusal that creates no workflow/domain fact still settles its execution and request and
releases its fence atomically; it does not need a command-fact link or task revision. An uncertain
outcome preserves the original evidence and keeps the durable fence until exact recovery or named
reconciliation appends resolution evidence.

After commit—or after loading an exact replay—the response layer separately reads the latest
committed authoritative state and builds a `current_view`. Only
`current_view.allowed_actions` and its ownership guidance mean “legal now.” The immutable canonical
result never embeds that promise. If the canonical result is already known or loaded but the current view cannot be refreshed because
of a read-path failure, return the durable canonical result with actions suppressed and explicit
view-recovery guidance; do not reverse or reclassify committed success.

For a PostgreSQL-authoritative local command, a crash before the effect transaction commits leaves
none of that command's domain-effect facts committed. Before cutover, Asana effects remain governed
by the existing persisted-intent, reread, and post-effect adjudication contract and may be confirmed,
not applied, or uncertain independently of a later local crash. A crash or response loss after a
PostgreSQL-authoritative commit returns the stored canonical result on exact replay and generates a
new current view.
Commands from earlier points in the same stage may already be committed; the command-causality index
and named domain facts identify them for idempotent reconciliation or Part I abandonment recovery
without deleting history.

The response contract is therefore versioned as two semantic parts:

```json
{
  "canonical_result": { "...": "immutable outcome of this request" },
  "current_view": {
    "database_authority_generation_id": "…",
    "task_revision": 42,
    "current_view_token": "opaque exact-authority token",
    "authority_versions": { "operation": 7, "cycle": 3, "lease_generation": 11 },
    "allowed_actions": [],
    "ownership": { "...": "current guidance" },
    "status": "available | unavailable"
  }
}
```

The logical storage and authorization distinction is mandatory. Stage A may make an immediate
public wire-format break, which is preferred when it yields the clearer contract. A temporary flat
compatibility envelope is optional rollout tooling only; if used, it must be assembled from the
immutable canonical result plus freshly derived current state/actions with explicit replay metadata
and must never expose historically stored actions as current authority. The service, OpenAPI/Action
schema, instructions, examples, and protocol identity change as one coordinated rollout.

An interruption after admission but before the effect transaction may leave a pending request or
expired claim but no task change. Recovery reacquires the exact claim generation, rereads the request
and task, and never infers a task effect from the admission record.

Expected current version occurrence, identity scheme, canonical identity, and location remain the
semantic concurrency check. Every workflow continuation also revalidates the exact occurrences
recorded by its operation, steps, actors, holds, classification, signoff lineage, and submission
baseline. The monotonic `task_revision` is a coarse compare-and-swap guard for commands whose contract
depends on the complete projected task state and is carried by every projection event as the exact
state being rendered. Per-task `projection_sequence`, not task revision alone, orders delivery and
projection-only refreshes. Task revision is not a universal reason to reject a command when only an
explicitly permitted orthogonal axis changed;
commands validate the exact task axes and workflow facts they semantically depend on. The opaque
`current_view_token` additionally binds the exact
operation/cycle/lease/execution authority versions used for legal actions; neither replaces exact
content, placement, signoff, or actor evidence.

### Audit and read boundaries

Governed audit facts and transition evidence required to prove a mutation are written inside its
effect transaction. The canonical request result is atomic with that mutation.

Invocation and transport audit remains a success-preserving, repairable boundary after the canonical
result. Its failure must not roll back or turn a committed workflow success into a retry signal.
Moving that audit into the effect transaction would be a separate contract change.

When PostgreSQL cannot accept either the invocation audit or its normal repair row, Dish must durably
append and `fsync` one immutable emergency repair record outside the unavailable transaction domain
before returning the committed success. The repair record carries exact request/result/audit
identity, supports append-only claim/import, deduplicated settlement, repaired or quarantined
terminal disposition, and is included in backup, restore-generation reconciliation, and operational
readiness. A process-local log message is not repair authority.

An authoritative read that uses multiple SQL statements runs in one read-only `REPEATABLE READ`
transaction or uses a single composed query that proves one consistent authority snapshot across the
task revision and every operation/cycle/lease/execution version used by the current view. Reads never
update leases or disposable projections as a side effect.

PostgreSQL backup, point-in-time recovery, snapshot retention, and restore are operational
boundaries rather than ordinary task transactions. At cutover Stage A retires both the replay-bound
SQLite `backup-create` command and the connected `backup-restore` command. Historical requests,
`backup_creations` rows, artifact identities, and retained files remain immutable migration
witnesses; every open reservation is completed, explicitly terminalized, or quarantined before
cutover. New PostgreSQL backups are operator-created and recorded through the operational control
plane, never through ordinary Dish request replay. Future notifications or exports also require
their own classified effect protocol; moving task storage into PostgreSQL does not justify weakening
non-database effect handling.

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
- legal actions are derived from a fresh authoritative `current_view`; historical replay results never grant current authority;
- Marco-only holds and interventions remain private and narrow.

Stage A preserves Part I abandonment semantics. A permanently lost Planning or Research attempt is
terminalized and, at an eligible clean frontier, recovered through the exact fresh successor
operation with its immutable baseline. A lost Verification run uses the exact fresh successor
operation/cycle rules. The task-level abandonment fence, old-run exclusion, crash convergence, and
manual reconciliation behavior remain until a separate post-Stage-A design explicitly replaces
them. Command-to-fact causality links supplement these records; they do not authorize
same-operation takeover, session replacement, or transfer of unpublished work.

Normal PostgreSQL-native content, placement, and creation mutations—and feature-gated Cooked or
Archive mutations after they are introduced—no longer return `BACKEND_UNCERTAIN`. A database availability or lock failure before commit is safe to retry
under the exact request identity rules. Semantic constraint failures remain fail-closed.

If a connection failure makes commit acknowledgement indeterminate, the service stops mutation
readiness for the affected path, reconnects, and inspects the replay record, task revision, and current authority versions before
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

Planning reopen becomes an ordinary transactional completion-state change. It clears only the
completed bare-task Planning gate while preserving exact content and placement. It remains
Marco-only because that is a lifecycle authority decision, not because the update is technically
uncertain.

## Human interface

### Stage A interface

Asana remains Marco's human-facing interface throughout Stage A:

- before cutover it is authoritative and writable under the existing governed model;
- after cutover it is an asynchronous, non-authoritative projection of PostgreSQL authority in
  the existing in-scope project set; it is behaviorally read-only even where Asana permissions do
  not prevent Marco from editing it;
- direct Asana edits after cutover are drift, never commands or imported authority;
- projection revision and state must be available through Dish reads/status so stale display is not
  mistaken for current authority.

The Stage A mutation surface is intentionally progressive. Engineering implements the retained
current Dish commands plus the smallest additional governed actions discovered during shadow use
and explicitly accepted for cutover. The implementation design's semantic-delta matrix enumerates
every retained, retired, added, or reclassified command; its caller, legality, replay treatment,
result semantics, feature stage, and migration disposition. The matrix explicitly retires
`backup-create` and connected restore at cutover; they are not carried forward as PostgreSQL command
API mutations. New Archive or
Cooked commands are not database-cutover prerequisites unless those features have separately landed
before the live-domain re-baseline. The authority flip occurs only when Marco judges that the actions he actually needs
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

## PostgreSQL deployment and outage model

Stage A initially uses one self-managed PostgreSQL primary in Docker Compose on Marco's laptop.
Dish and PostgreSQL may share the laptop while remaining separate containers and failure processes.
The architecture and backup format must permit later relocation to a self-managed AWS host without
changing task identity, authority, transaction, replay, or recovery semantics. Managed PostgreSQL is
not required.

Required failure assumptions:

- PostgreSQL unavailability makes authoritative Dish reads/current views unavailable and fails
  authoritative mutations closed; Asana remains only a possibly stale downstream view and never
  becomes fallback read or mutation authority;
- request admission/result recovery distinguishes a confirmed committed outcome from an unavailable
  fresh `current_view`;
- connection URLs, credentials, pool sizing, health checks, backup locations, and restore tooling
  remain deployment-explicit rather than assuming local-file semantics;
- Marco owns upgrades, credential handling, monitoring, backup, and restore rehearsal;
- before production cutover, at least one encrypted backup/WAL destination must be outside the laptop failure domain; a backup stored only on the same laptop is not disaster recovery;
- periodic base backups plus WAL-based point-in-time recovery are the target where practical for the
  chosen self-managed deployment; exact initial RPO/RTO are an implementation/cutover gate informed
  by measured restore rehearsal;
- the laptop is a single failure domain and authoritative work may be unavailable while it is off,
  disconnected, or under maintenance; Stage A does not promise high availability;
- multi-region, multi-primary, automatic failover, and a managed cloud database are not required.

A later move to self-managed PostgreSQL on AWS is an operational relocation with rehearsed backup/
restore or replication, not an authority-model redesign.

Any restore that can discard committed PostgreSQL history is an offline, exclusive operator
procedure. Stage A does not retain an ordinary connected or replay-bound PostgreSQL restore command.
Before restore begins, the operator creates durable restore-control evidence outside the database
being replaced, containing the restore operation identity, selected backup/PITR source, newly
reserved database-authority generation, and fail-closed in-progress status. Dish mutation services,
projector workers, and maintenance writers remain stopped or connection-fenced while that marker is
active or unreadable.

After the database timeline is restored, Dish starts only in restore-reconciliation mode. It verifies
the restored database, appends/activates the externally reserved new database-authority generation,
rotates or establishes the generation-bound run-capability verifier, establishes a corresponding
fresh projection epoch, validates request/execution/fence invariants, and only then terminalizes the
external restore-control marker. Normal mutation readiness requires that marker, generation, and
capability verifier to agree. All requests, agent runs, run capabilities, executor claims, and
mutation fences from an earlier generation are rejected; they are not replayed, refreshed as a
continuation, reconstructed from Asana, or silently admitted as new work. Fresh run registration
requires the generation-specific bootstrap authority established outside the restored timeline; the
ordinary long-lived bearer credential alone cannot authorize a surviving process to resume. Needed
work is deliberately reissued under explicit new-generation evidence, and automatic retry queues are
discarded or quarantined at the boundary.

The projector then rebuilds/reconciles the downstream Asana view from restored PostgreSQL state.
Asana content from the superseded generation or epoch is never used to reconstruct lost PostgreSQL
work, even when it visibly contains later data. Restore readiness and projector readiness are
reported separately; authoritative mutations may resume before asynchronous projection repair
finishes only after the new database generation is valid and current.

## Import and cutover

### Phase 1: complete baseline, then Asana-authoritative PostgreSQL shadow

Before command shadowing begins, establish the current legacy authority generation and import and
validate a complete baseline of the then-current in-scope Asana corpus and exact legacy Dish runtime
authority bundle bound to that generation. The baseline must be
behaviorally equivalent to the live authority: task identities, title/body content, completion and
placement, Planning-intent challenges, Marco authorizations, operations, Verification, leases,
replay, abandonment, restore sidecars, invocation-audit repair state, effects, and every other
current authority fact resolve consistently. Today the governed Asana scope is the Cooking project; the
implementation re-baselines this against the live code and architecture rather than assuming that
Cooking History, Cooked, or Archive has landed.

Bootstrap must be gap-free. Non-authoritative command/observation capture is enabled before or at a
recorded bootstrap high-water mark; the bulk baseline records its exact start/end evidence; all
captured deltas after that mark are applied; direct Asana changes are reconciled through complete
observations; and baseline completion is recorded only after closure proves that no authoritative
change was lost between the bulk snapshot and ongoing mirroring. A brief writer pause, durable delta
capture plus matching scans, or another implementation mechanism is acceptable only if it proves
this invariant. Observation capture may begin before the baseline is complete, but command shadow
execution may not.

After that complete gap-free baseline exists, keep all live reads, writes, workflow decisions, and
human actions Asana-authoritative. After each confirmed Asana reread, mirror the observed state one-way
into structurally isolated PostgreSQL shadow storage. Periodic reconciliation also captures direct
human Asana changes and missed mirror delivery.

The shadow may use the eventual PostgreSQL platform, but its schema and credentials must not provide
a path into authoritative task, operation, or Verification rows. It contains periodic and
command-triggered `asana_observation_batches`, `asana_task_observations`, source witnesses, and any
`shadow_*` candidate graph. Store observations with purpose `shadow` or `reconciliation`, including:

- the exact title/body, qualified content identity, all memberships in the re-baselined in-scope
  project set, source completion state, and source timestamps; Archive, Cooked, or Cooking History
  evidence is included only if the corresponding concept or project is actually in scope at that
  baseline;
- the corresponding operation/request when the observation followed a Dish command;
- when Stage B development begins, an attempted structured parse and its validation/classification
  evidence, normalized candidate rows, deterministic candidate JSON identity, and compatibility
  rendering comparison.

Before a governed live effect, the current authority domain durably registers the rollout sequence
with the ordinary request reservation. A command is eligible for exact Phase 2 comparison only when
the complete immutable shadow input envelope is also committed before the effect. PostgreSQL
delivery failure then remains non-blocking and exactly retryable from that envelope. If complete
envelope capture fails, the Asana command may proceed under the existing live-authority contract and
the registered sequence is permanently settled as an unshadowed command gap; request/task identity
or a later post-state observation is not enough to reconstruct exact parity. Shadow rows are never
read to authorize live work, written back to Asana, or treated as authoritative merely because they
exist. Incomplete batches, unclassified registrations, and unshadowed gaps remain diagnostic evidence
but cannot claim corpus or command-parity completeness.

The title/body shadow battle-tests import, identity, reconciliation, query behavior, and sustained
parallel persistence. It does not prove PostgreSQL-native execution ownership, transaction crash
atomicity, or recovery; those require direct fault injection and rehearsal.

At cutover, the system must reconcile the final frozen Asana state with the PostgreSQL candidate
state and create or activate authoritative records only from approved complete evidence. It must not
silently relabel an incomplete or contradictory shadow row as authority. The expected operational
flip is small because the service and schema have already run in shadow, but the final authority
proof remains explicit.

### Phase 2: shadow execution

For each governed production command claimed as Phase 2 evidence, atomically capture the complete
shadow input envelope in the current authority domain before the live effect, then evaluate one
shared `CommandPlan` for both the live Asana path and PostgreSQL shadow. Pre-cutover `create` also
uses the reserved Dish UUID and discoverable stable Asana correlation marker required above; a create
without that supported correlation is excluded from exact command-parity claims. During this phase,
every shadowed command and state must remain behaviorally representable and executable by the
Asana-backed authority; PostgreSQL-only authority semantics may be modeled but remain inactive. Do
not implement a second reducer or duplicate transition engine. After the live effect, feed the same
persisted intent and exact before/after evidence into the shared `CommandAdjudication`; mirror only
the evidence-backed terminal classification. Shadow evaluation must not reread post-mutation Asana
state as though it were the pre-command input. Compare the
planned and adjudicated workflow facts, canonical/projected task revision, current-view authority
versions, and location with the confirmed live result. When testing structured representation,
also compare structured identity and rendering. Candidate output remains non-authoritative and
cannot affect the production response.

Human out-of-band Asana changes are imported observations, not fabricated Dish commands. Repeated
observations identify the narrow human commands that must exist before cutover.

Exercise concurrency, request claims, transaction interruption, restart, and recovery directly
against copied candidate databases. Long runtime supplies representative inputs, but elapsed shadow
time alone is not proof of transactional safety.

### Phase 3: Stage A battle-hardening readiness

Before the authority flip, exercise PostgreSQL queries, transaction ownership, exact replay,
command-to-fact causality links, Part I abandonment, complete-candidate handling, projection
event/attempt behavior, backup/restore, and all current workflow routes against copied or shadow-derived data. Asana remains
the only production authority during this phase, and a PostgreSQL shadow failure never blocks an
Asana workflow.

There is no fixed duration or numerical pass gate. Readiness evidence includes observed mismatch and
failure rates, successful asynchronous repair, diagnosis and recovery burden, direct crash and
concurrency fault tests, replay convergence, projection ordering, backup/PITR/restore rehearsal, and
coverage of the real human mutations Marco has needed during the shadow period. Before cutover, one
explicit governed-action inventory must show that every routine mutation observed in actual Asana
use has a supported Dish command or an intentionally approved operational alternative. At minimum
it covers every routine action actually present in the re-baselined live system—such as title/body
correction through lifecycle routes, completed-task Planning reopen, destination/location repair,
quarantine promotion, human holds and decisions—and any recurring direct Asana action discovered
during shadow use. Archive/restore or Cooked/log-cook handling enters this gate only if that feature
has been introduced before cutover. Marco authorizes the
flip using that evidence near cutover.

A new private frontend is not part of this phase. Equivalent narrow Dish CLI/admin commands cover
the human mutations actually required before Asana becomes non-authoritative; additional mutations
remain ordinary application work afterward.

### Fixed cutover target

The first production cutover target is **document-compatible PostgreSQL authority**:

- imported and DB-native canonical content remains exact title/body document versions;
- PostgreSQL owns the canonical task pointer, location, completion/Planning eligibility, command
  causality, workflow evidence, replay, and mutation transactions; Cooked or Archive state is owned
  there only if the corresponding feature has separately been introduced;
- Asana becomes the downstream non-authoritative interface;
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
3. **Additional historical projects or lifecycle features, when in scope:** import their exact
   source documents, all in-scope memberships, and selected observations with explicit provenance
   only when the live-domain re-baseline or a separate authorization includes them. A supported exact
   task may be `governed` where a current lifecycle route genuinely applies; an unsupported or legacy
   document that is still safe to identify becomes `historical_read_only`, not silently
   workflow-mutable. `source_completed` initializes only the separate completion/Planning-gate axis.
   Cooking History membership may initialize Cooked only if the Cooked feature and its import
   contract already exist. Do not assert current-schema conformance, complete workflow evidence,
   signoff, document kind, or validation success that the source does not prove. Preserve the
   qualified source identity, source modification time, and import time. A later named migration,
   clone, or promotion validates and creates the supported occurrence before changing a historical-
   read-only task to governed operation.
4. **Excluded Sourcing and Reference records:** import only when an approved reading, search, or
   provenance requirement includes them; otherwise retain them in the source snapshot without
   making them governed Dish tasks.

The importer is one-purpose migration tooling, not a permanent alternate backend. It reads an exact
snapshot and writes only the staged database.

### Open-operation scope

The non-authoritative shadow baseline and its snapshot builder must represent every current open
authority fact needed to evaluate equivalent commands, including operations, Verification cycles,
leases, pending requests, and unresolved external effects. That representation is shadow evidence,
not a production migration of live authority.

The production cutover importer does not create live PostgreSQL authority from any unresolved open
operation, cycle, lease, request, authorization reservation, backup reservation, or external effect.
Before the final manifest, each such item is completed, settled through current recovery, abandoned
under Part I, explicitly terminalized, or quarantined. Any future ability to migrate open authority
as live production state is a separately approved architecture extension and is not inferred from
shadow rows.

This does not pre-commit Marco to a cutover date or confidence gate. It fixes only the supported
migration surface so implementation is not forced to reconstruct live authority implicitly.

### Rehearsal

> **Approved architecture, later operational authorization:** the migration surface is resolved-only
> and ordinary rollback to Asana authority ends immediately before the first PostgreSQL mutation
> request is admitted under an authority activation with an appended `activated` event. The exact writer-freeze timing, acceptance window, evidence threshold, and go/no-go
> authorization remain later operational decisions.

1. Freeze the exact Dish and Honest revisions for the document-compatible Stage A target.
2. Snapshot the complete Asana corpus, shadow evidence, and configuration. Produce the SQLite
   component through the online-backup API or an equivalently proved checkpointed bundle, prove WAL
   closure, capture the ownership/restore sidecars, and lock-coordinate the audit-repair main and
   `.importing` files. Validate the complete legacy Dish runtime authority-bundle manifest against
   the exact schema and release identities.
3. Require no executing claims, unresolved effects, uncompleted service requests, nonterminal
   Planning-intent challenge, ambiguous Marco-authorization reservation, unresolved backup
   reservation, active/ambiguous restore journal or fault marker, unaccounted invocation-audit
   repair record, unclassified or pending
   shadow envelope, active abandonment, or prepared successor/continuation awaiting claim.
4. Rehearse the resolved-only route—finish, abandon under the current contract, or quarantine
   every open operation. Open-authority migration is outside Stage A unless Marco later approves a
   separate architecture extension.
5. Import every in-scope task and the exact frozen legacy runtime authority bundle under its class into a
   copied database, recording row-level migration origins.
6. Prove observation-batch closure, including one exact source-document witness per task, complete
   membership and section-registry coverage for the re-baselined in-scope project set, matching
   linkage and qualified identities, no duplicate task GIDs, and no duplicate task/project/section
   membership tuples; then reconcile current pointers, location, completion, operation history,
   signoff, and provenance, plus Cooked or Archive only if the corresponding feature is active.
   Structured conversions are Stage B evidence and do not affect Stage A import.
7. Quarantine mismatches that affect live authority; do not infer content, readiness, destination,
   validation, or signoff.
8. Validate PostgreSQL semantics, queries, backup/PITR/restore, non-forgeable run-capability
   admission, pre-cutover Asana-create correlation/reconciliation, request ownership,
   command-to-fact causality links,
   canonical commit boundaries, Asana projection, and the full document-compatible workflow suite.
9. Exercise every required CLI/admin human mutation command and the non-authoritative Asana projection
   against the imported copy.
10. Rehearse rollback before admission, rollback burn immediately before an admitted PostgreSQL
    mutation request, failure after admission but before task-domain commit, and PostgreSQL-backed
    recovery after rollback burn.

### Production cutover

After separate explicit authorization:

1. stop mutation admission and drain admitted requests;
2. prove the same request, Planning-intent challenge, Marco-authorization reservation, operation,
   claim, lease, backup reservation, restore-sidecar, invocation-audit repair, abandonment/
   successor, shadow-envelope/
   gap-classification, and external-effect quiescence conditions used in rehearsal, and prove that
   the final WAL-closed capture can now be taken exclusively;
3. declare an Asana authority freeze: Marco and every agent stop manual mutation of every
   re-baselined in-scope Asana task, section, and project, including edits, moves, creation, and any
   enabled lifecycle action;
4. establish a fail-closed legacy-writer fence before final capture: stop and isolate every old
   service endpoint/process, revoke or disable every legacy Asana-writing credential, and require a
   shared external authority marker checked before every remaining legacy mutation path; retaining
   read-only observation access is permitted, but an old process must be mechanically incapable of
   writing after activation;
5. enumerate the complete frozen corpus into a first `cutover` observation batch, including the
   task set and count, full in-scope project/section membership relation, section registry, exact
   title/body logical-string witnesses and qualified identities, source completion provenance, and
   evidence for any separately enabled lifecycle feature; reject duplicate task GIDs, duplicate
   membership tuples, or duplicate section
   GIDs and compute
   its corpus-manifest identity only after source-document and section closure passes;
6. repeat the complete enumeration under the same freeze into a second `cutover` batch and require
   its independent closure plus exact agreement of task set, count, section registry, source
   document identity schemes and identities, full membership relation, source completion provenance,
   and evidence for every enabled lifecycle feature; `modified_at`
   agreement alone is never closure proof;
7. freeze and validate the final legacy Dish runtime authority-bundle manifest—including a
   transactionally complete online-backup snapshot with proved WAL closure, the service-ownership
   marker, restore request journal, restore-fault marker, required restore artifacts, and the
   lock-coordinated invocation-audit repair main/`.importing` sidecars—then append one immutable
   approval binding the second matching Asana manifest, its earlier matching batch, and that exact
   complete legacy runtime authority bundle; take final configuration, code, and source-export snapshots
   bound to the same approval;
8. import only the approved Asana observations/source documents and the exact approved legacy Dish
   runtime authority bundle into production PostgreSQL, recording row-level migration origins;
   resolve or explicitly isolate every unapproved or contradictory mismatch under the cutover
   quarantine manifest, and do not proceed while any visible in-scope Asana task remains unmapped;
9. validate the approved governed-action coverage matrix against the frozen corpus and observed
   shadow-period human actions, then append one `prepared` authority activation binding the exact
   cutover approval, workflow import run, initial database generation, projection epoch, Alembic
   head, Dish/Honest/protocol/OpenAPI releases, and proof identities;
10. prove the hard legacy-writer fence, remove Asana from live task reads and workflow decisions,
    and verify that no directly reachable legacy endpoint or retained process can perform an Asana
    authority write;
11. atomically or through a crash-safe activation protocol append the exact activation's
    `activated` event; PostgreSQL mutation admission checks that event and no other imported
    generation may open;
12. grant only the downstream projector's dedicated worker credential, establish continuous
    mapped/correlated/isolated Asana corpus classification, and enqueue projection from committed
    PostgreSQL state;
13. keep the approved manifest, its two observation batches, exact source export, and activation
    evidence immutable during acceptance;
14. immediately before admitting the first PostgreSQL mutation request, append the activation's
    `rollback_burned` event. From that point, rollback to Asana authority is illegal even if the admitted
    request later fails before committing a task-domain mutation;
15. admit DB-backed mutations only after identity, location, completion, request ownership,
    retired-backup-command, backup/restore, workflow, create-correlation feasibility, continuous
    corpus-closure, and human-command gates pass, plus any separately enabled feature gates.

The Asana authority freeze begins before the first final observation and remains in force until DB
authority is active or the pre-mutation rollback restores Asana authority deliberately. Normal work
is not released between import and activation. Because Marco is the sole human operator, this is a
short operational freeze rather than a synchronization product, but it is the closure proof for
the authority transfer.

A long parallel-persistence period is allowed and expected, but it is never dual authority. Before
cutover, PostgreSQL writes are non-authoritative shadow observations or shadow execution derived from
confirmed Asana state. After cutover, Asana writes are downstream projection effects derived from
committed PostgreSQL state. Only one store is production authority at a time.

### DB-authoritative Asana downstream projection

The projector is required for Stage A because Asana remains Marco's human-facing downstream view
after PostgreSQL becomes authoritative. Every committed task mutation that affects the Asana view
appends an immutable projection event in the same PostgreSQL transaction. A separate worker renders
and applies it, recording a durable pre-call attempt intent and an append-only adjudicated outcome,
while updating only derived retry/mapping summaries.
The existing in-scope Asana project set is reused as a non-authoritative downstream projection.
Because Marco is the sole user, continuity of links, GIDs, history, and working habits outweighs the
extra isolation of a second mirror set. The projection is behaviorally read-only: direct edits may
appear to succeed in Asana, but they are unsupported drift, never imported, logged, and overwritten
from PostgreSQL.

Required rules:

- humans and agents do not edit projected tasks as an input to Dish;
- projection renders completion and current workflow/catalog placement from their separate
  PostgreSQL axes, plus Cooked history membership or Archive presentation only when those features
  are enabled; it must not flatten one into another or treat an Asana move as authority;
- projection freshness and last applied PostgreSQL revision are visible;
- projection events are ordered per task revision, stale retries are no-ops, and mapping creation is
  idempotently reconciled;
- projection failure never blocks, rolls back, or reclassifies a PostgreSQL mutation;
- out-of-band Asana drift is flagged and overwritten from PostgreSQL, never imported;
- continuous full-project reconciliation maintains the invariant that every visible in-scope GID is
  mapped, currently correlated to one unresolved projector create, or explicitly isolated; an
  unknown object makes projector readiness unhealthy and is never treated as Dish work;
- repair acts only on projection mappings and exact committed versions; unmapped unknown objects are
  handled only by the non-authoritative isolation policy;
- ambiguous mirror creation cannot create a second Dish task;
- PostgreSQL-native `create` is enabled only after the deployed Asana marker/reconciliation
  feasibility proof passes; if it fails, `create` stays disabled until Marco approves a different
  projection or governed creation topology;
- the projector uses a dedicated credential and code path that cannot execute historical Asana
  authority operations.

The topology is fixed: reuse the existing project set that is in scope at cutover. The projector
preserves the approved task aliases and links, renders conspicuous projection freshness, and repairs
or overwrites direct drift asynchronously. If a later feature adds Cooking History, Archive
presentation, or another project membership, that project joins the same downstream projection only
through its separately governed feature design. Asana is never writable authority after the flip.

### Rollback boundary

Before the rollback-burn fact is appended and the first PostgreSQL mutation request is admitted,
rollback may restore the complete prior Asana-based code, database, configuration, and corpus
authority. Admission itself creates durable intent and therefore burns rollback even if no later
task-domain mutation commits.

After rollback burn, Asana is stale authority and ordinary rollback to it is illegal. Ordinary rollback must restore a compatible
PostgreSQL-backed code, database, command surface, and required Asana projector from the self-managed
PostgreSQL backup/PITR system, establish a fresh database-authority generation and projection epoch
through the offline restore-control procedure, reject all pre-restore runs/requests, and reconcile
the entire non-authoritative Asana view from restored PostgreSQL state.
An apparently current Asana projection is not rollback authority. Returning authority to Asana
would require a separately designed, rehearsed reverse migration that preserves every intervening
task version, transition, request result, and audit fact; it is not part of this design.

This boundary must be explicit in the cutover approval. Acceptance gates should complete before
opening mutations so rollback to Asana remains simple while it is still valid.

## Implementation sequence

1. Inventory every current authority relation, durable sidecar, Asana-owned fact, canonical field,
   gateway call, identifier, health dependency, recovery branch, validator, test fixture, and
   required human action. Produce both the row-by-row authority-disposition matrix and a semantic-
   delta matrix tagging each command/proof item as mandatory Stage A, authority-driven Stage A
   change, conditional feature, or Stage B/later.
2. Before refactoring current command semantics, freeze the independent current-behavior
   characterization corpus for every retained route and representative failure/recovery state.
3. Establish PostgreSQL deployment, SQLAlchemy unit-of-work boundaries, Alembic execution plus
   immutable migration provenance, connection ownership, operator-managed backup, PITR, external
   restore/bootstrap authority, and rehearsed restore without changing production authority. Define
   a strangler plan for SQLite-specific SQL and transaction helpers rather than maintaining two live
   workflow engines indefinitely.
4. Build the representation-neutral Stage A foundation: universal Dish UUIDs and external aliases,
   task/version envelope, immutable title/body documents, evolved operations/cycles and command
   causality links, controlled locations and completion/operability state, exact workflow-version
   bindings, location/completion history, immutable request envelopes, execution claims,
   transactional repository path, audit-repair fallback, quarantine, authority activation, and
   projection event/attempt handling. Cooked and Archive storage/commands remain conditional until
   separately enabled by live-domain re-baseline.
5. Build one-way Asana-to-PostgreSQL observation mirroring, legacy-generation-bound rollout evidence,
   and periodic reconciliation. Run it for a sustained period with no production reads or authority
   from PostgreSQL; a destructive SQLite restore must invalidate the old shadow generation and
   require a new baseline.
6. Add shared-engine shadow execution and direct crash/concurrency fault testing against
   representative copied data. Prove both shared-kernel equivalence to the frozen current-behavior
   oracle and PostgreSQL shadow equivalence to the new live path. Rehearse PostgreSQL backup, PITR,
   restore/bootstrap fencing, and projection recovery.
7. Prove the deployed Asana correlation surface for both pre-cutover and projector creation, plus
   continuous mapped/correlated/isolated corpus closure. If the post-cutover feasibility gate fails,
   stop before cutover and return the topology choice to Marco.
8. Implement the retained narrow human mutation commands and explicit authority-driven Stage A API
   changes in the semantic-delta matrix. A private frontend, broad search product, Cooked, Archive,
   Cooking History ingestion, and generic promotion/editor commands are not required unless
   separately activated.
9. Rehearse the document-compatible production import, crash-safe authority activation, hard old-
   writer fence, pre-admission rollback, rollback-burn boundary, and PostgreSQL-backed recovery after
   an admitted mutation request.
10. Perform the separately authorized Stage A cutover. PostgreSQL becomes authoritative only through
    the activation's appended `activated` event; the legacy Asana authority credential/path is mechanically fenced;
    the downstream projector and continuous corpus reconciler remain.
11. Pause for battle-hardened production operation. Resolve Stage A defects without beginning a
   representation migration merely to preserve schedule momentum.
12. Separately approve and implement Stage B: structured Honest schema, canonicalization, parser,
    renderer, structured editing, exact Verification/signoff migration, and governed pointer
    advancement.
13. Retire document-only compatibility adapters only after every real producer and preserved
    historical requirement has been accounted for.

At no point may production route different tasks to different authorities or accept peer writes
from both Asana and PostgreSQL.

## Required proof

Each implemented project must test the applicable items below according to the semantic-delta and
feature-stage matrix. Structured schema, canonical JSON, typed-graph, parser, renderer, structured-
editor, Cooked, Archive, Cooking History, cook-log, and representation-migration items are
conditional feature or Stage B gates unless the live-domain re-baseline proves that the feature is
already governed; they do not otherwise gate the document-compatible Stage A authority cutover.

- fresh task creation with universal Dish UUIDs and exact resolution of immutable imported Asana aliases;
- the post-cutover `sections`/destination query reading controlled Dish locations and returning only
  valid Stage A compatibility aliases, with no Asana routing authority;
- when separately feature-enabled, audited human cooked-state changes and cooked-history lookup;
- exact reads and consistent list/search snapshots;
- incomplete Planning, Research, and Verification-round attempts that append only service-visible
  command/control evidence without advancing the canonical task pointer or Asana projection;
- atomic complete named content-boundary commands—including stage/round decisions and retained
  migration, reopen, hold-resolution, and destination-repair writes—with crash injection before and
  after the single canonical pointer advancement;
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
- request-scoped ownership and durable command execution for concurrent exact replays of
  `create`, `start`, completion/reopen, and every retained or separately feature-enabled
  non-operation admission path named by the semantic-delta matrix;
- permanent immutable request contract/payload/version evidence for both request- and
  operation-scoped mutations after their expiring execution claims are retired;
- deterministic `create` identity across crash, recovery, and replay, including pre-cutover
  lost-response correlation/reconciliation and exactly-once Dish UUID ↔ Asana GID binding;
- non-forgeable generation-bound run-capability tests, including post-restore rejection of every old
  run, capability, and delayed request even when PITR erased its former database row;
- post-restore bootstrap tests proving that a surviving old process with the ordinary bearer token
  cannot mint a current run capability, transfer retry buffers, or resubmit erased logical work
  without explicit new-generation reissue evidence;
- pre-cutover destructive-restore tests proving legacy-generation rollover, prior shadow-evidence
  disqualification, stale request/run rejection, rollout-sequence separation, and mandatory fresh
  baseline before command parity resumes;
- SQLite cutover capture tests proving online-backup/WAL completeness and lock-coordinated accounting
  for audit-repair main and `.importing` sidecars, including malformed-record quarantine;
- shadow-envelope fault tests proving exact retry after PostgreSQL outage and permanent exclusion of
  commands whose pre-effect envelope was not durably captured;
- independent characterization tests proving the new shared kernel against the frozen current-system
  oracle before shared live/shadow parity is accepted;
- pre-cutover separation of `CommandPlan` from post-effect `CommandAdjudication`, including
  ordered multi-effect commands where each evidence-backed adjudication either yields the next
  exact effect plan or the terminal `confirmed`, `not_applied`, or `uncertain` command outcome;
- concurrent mutations against the same and different tasks;
- request replay before, during, and after transaction commit;
- first-writer-wins request/execution settlement, preservation of the initial uncertain outcome,
  append-once resolution evidence, and exact replay returning the resolved outcome without deleting
  the earlier uncertainty;
- durable historical mutation-fence generations with at most one active/unresolved task or
  operation fence and no reuse of released fence identity;
- recovery of old and adapter-based requests without reinterpretation across deployment, including
  exact legacy result witnesses whose historical action fields never become current authority;
- exact replay returning the immutable canonical command outcome plus a newly derived current view;
  only the fresh view may expose principal-filtered `allowed_actions`, and view failure suppresses
  actions without reclassifying committed success;
- content, location/completion/operability state, signoff, and actor drift, plus Cooked/Archive only
  when separately feature-enabled;
- operation baselines, steps, actor candidates, holds, material classifications, non-material
  check-ins, submissions, migrations, reopens, destination repairs, inspection, review, correction, and signoff
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
- imported current placement independent of embedded destination, all memberships preserved,
  and imported source-completion initializing only the separate completion axis without fabricated
  local transitions or Cooked inference;
- append-only destination parse and resolution attempts, including failed and superseded parser
  results, with the exact selected resolution retained by import evidence;
- signed title/body versions remaining current by default, plus separately tested re-Verification
  and approved-attestation routes if either direct migration route is implemented;
- canonicalizer upgrades that create new single-use versions, preserve old JSON and signoff, and
  cannot inherit Verification without re-Verification or approved attestation;
- immutable version rows plus append-only single-use activation records; rejection of a second
  activation for the same version, while revert and restoration create new versions with explicit
  lineage;
- location rename behavior that preserves source/rendering snapshots without changing structured
  identity;
- exactly one current Stage A compatibility-alias selection per authorable destination, preservation
  of older version-owned alias choices, and rejection or explicit deferral of a brand-new destination
  lacking an approved numeric compatibility alias; asynchronous Asana projection never creates
  routing authority;
- source-to-structured parsing and structured-to-compatibility-rendering reconciliation across the
  active corpus;
- one-way shadow gaps, replay, periodic reconciliation, and proof that the separate shadow database
  cannot authorize or alter Asana-backed production or resolve candidate IDs in the live repository;
- separate shadow/reconciliation observations and approved cutover origins, with no path that
  promotes the newest ordinary observation or shadow candidate implicitly;
- observation-batch closure requiring one exact source-document witness per task, full
  project/section membership and section-registry coverage, matching qualified identities and
  linkage, and rejection of duplicate task GIDs, duplicate membership tuples, or duplicate section
  GIDs;
- irreversible batch completion and append-only approval of only a matching later cutover batch;
- immutable legacy Dish runtime authority-bundle manifest—including a WAL-closed online-backup
  SQLite snapshot, ownership marker, restore journal/fault state, required restore artifacts, and
  audit-repair main/`.importing` sidecars—plus row-level workflow import origins
  and cross-link validation against the approved Asana task/version occurrences;
- immutable many-to-one location aliases, append-only retirement evidence, and interval resolution
  by durable batch sequence rather than batch UUID ordering;
- shadow execution through the exact shared domain handlers and policy with alternate
  unit-of-work/effect adapters, plus divergence reporting without production response influence;
- document-compatible Stage A cutover rehearsal and a separately gated Stage B
  representation-migration rehearsal;
- historical terminal write/movement evidence, dedicated local transitions, and absence of
  fabricated database-backed attempt records;
- when separately feature-enabled, separate atomic Cooked transitions and completion/reopen
  transitions, including any approved cook-log behavior;
- uniform non-terminal `REQUEST_IN_PROGRESS` responses that preserve the pending request and never
  expose execution tokens;
- exact Planning-intent challenge issuance, replay, claim, single-use consumption, Marco-only
  reason-bearing permanent settlement, concurrency, shadow parity, migration provenance, and cutover
  closure with no nonterminal challenge;
- governed audit rollback on failure and success-preserving invocation-audit repair, including the
  PostgreSQL-era external emergency journal when PostgreSQL cannot store the audit or normal repair,
  append-before-response durability, claim/import crashes, deduplication, malformed-record
  quarantine, backup/restore reconciliation, and cutover accounting;
- retirement of `backup-create`/connected restore at cutover, preservation of historical requests,
  `backup_creations` rows and artifact witnesses, and closure of every open reservation before
  activation;
- append-only schema-migration provenance across clean upgrades, failure, repair/stamp/downgrade,
  destructive restore generations, and imported legacy `schema_migrations` evidence;
- class-specific import validation, including historical-read-only tasks with no ordinary workflow
  authority, exact governed promotion/demotion evidence, and rejected unresolved live evidence;
- row-by-row current-authority coverage proving every named source relation and external sidecar has
  an exact target authority/witness, identity remapping, semantic validator, cutover disposition, and
  retirement rule;
- database migration from every preserved schema version;
- tiered semantic validation: bounded readiness over current/open authority, command-time proof for
  inserted facts, and explicit full-history audit at migration/cutover without unbounded startup
  scans;
- composite task/version ownership, exactly-one representation, same-version child ownership,
  single revision advancement, and quarantine promotion constraints;
- service restart, PostgreSQL lock contention/deadlock handling, encrypted off-host self-managed
  backup, point-in-time recovery, offline restore-control evidence, fresh database-authority
  generation, run-capability verifier and projection epoch, rejection of pre-restore
  runs/requests/capabilities, and reconciliation of an
  Asana view that may be ahead of restored authority;
- isolation of any future private interface from the Action listener and command-only mutation;
- stale shadow reads and stale Asana projection views that cannot authorize production mutations;
- a DB-backed authoritative mutation path with no Asana authority calls or credentials; only the
  separately fenced downstream projector retains its dedicated Asana projection credential;
- authority-activation crash tests across prepared, active, and aborted states; hard legacy-writer
  fencing of direct endpoints/processes/credentials; and rollback burn immediately before the first
  PostgreSQL mutation request is admitted;
- continuous Asana corpus closure that classifies every visible in-scope GID as mapped, unresolved
  correlated create, explicitly isolated, or blocking unknown, without importing external drift;
- deployed Asana projector-create feasibility tests for atomic marker installation, delayed lookup,
  zero/one/multiple-match adjudication, duplicate-marker corruption, worker takeover, and the
  fail-closed disabling of PostgreSQL-native create when the capability is unavailable;
- projection event replay, exactly one mutation-origin event per rendered task revision plus ordered
  projection-only refresh events, restore-epoch fencing and full downstream reconciliation, separate
  external-effect outcome from worker delivery disposition, durable pre-call attempt intents before
  every external call, crash recovery for an intent with no outcome, append-only single-use
  adjudicated outcomes, immutable mapping identity
  and compatibility lookup, lag, out-of-band drift, update failure, uncertainty, dead-lettering,
  mapping replacement, and ambiguous mirror creation without changing DB workflow results;
- exact corpus import counts, identities, full memberships, locations, completion and operability
  dispositions, quarantine reports, and Cooked/Archive evidence only when separately enabled;
- a frozen-authority cutover with two complete enumerations agreeing on task set/count, section
  registry, exact source-document witnesses and qualified identities, full memberships, and source
  completion states before import from the named manifest, plus exact agreement with the approved
  legacy Dish runtime authority bundle.

The complete automated suite, imported-corpus rehearsal, live test-project workflow, backup and
restore rehearsal, and cutover/rollback rehearsal are implementation-acceptance and production-
cutover gates, not prerequisites for beginning implementation design. Testing must exercise real
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
| Ordinary shadow row becomes import authority | Batch observations by purpose; only an approval binding two matching Asana manifests and one exact legacy runtime authority bundle may establish origins |
| Parallel persistence recreates dual authority | One-way Asana-authoritative shadow before cutover; required one-way PostgreSQL-authoritative outbox projection afterward; never ingest the downstream copy |
| Non-authoritative persistence failure blocks production or fabricates parity | Persist the complete shadow input envelope before the live effect; retry delivery exactly, or classify an immutable unshadowed gap without changing the authoritative result |
| Backend abstraction becomes a permanent second engine | Shared deterministic plan/adjudication kernel; adapters never choose transitions; delete live Asana mutation after acceptance |
| A multi-effect Asana command is reduced to one predicted outcome | Iterate exact plan/adjudication rounds; only evidence from one effect may authorize the next |
| A generic journal disagrees with workflow authority | Keep operations/cycles, requests, executions, and named domain facts authoritative; journal only ordered causality links to those facts |
| The journal grows into agent draft storage | No checkpoint command; record only service-visible commands, mutations, results, and compensations |
| Frontend bypasses workflow legality | Query APIs for reads; existing command applications for every mutation |
| Editor overwrites newer or governed content | State-specific lifecycle command plus exact version/revision; no generic save |
| Frontend couples to intermediate blobs | Stable service views/actions; structured forms wait for the structured payload |
| Drag-and-drop disguises an arbitrary state change | Approved planning model and named commands; never equate board columns with workflow sections |
| Stage A document authority silently becomes the final representation | Keep Stage B as a separately approved target, but do not start it until the battle-hardening gate passes |
| Concurrent replay executes a non-operation mutation twice | Durable request execution ownership; deterministic reserved IDs; transactional ownership recheck |
| Historical replay result is mistaken for current authority | Preserve immutable initial and resolution outcomes; derive a fresh current view for every response and suppress actions if that view is unavailable |
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
| Historical read-only content becomes workflow-mutable by accident | Explicit operability state; no ordinary workflow actions; named promotion with supported exact version and append-only evidence |
| Local facts inherit Asana uncertainty semantics | Dedicated local transition evidence; historical attempt tables remain immutable and external-only |
| Cooked history is reduced to a mutable flag | Append-only cooked transitions commit with the projection, audit, and result |
| Cooked and Archived become the same outcome | Keep workflow/catalog location, cooked projection/history, and archive disposition orthogonal; neither implies the other |
| Asana completion is imported ambiguously | Preserve it as a separate completion/Planning-gate axis; never infer Cooked from it. Establish Cooked only from exact Cooking History evidence, separately audited human evidence, or a governed cooked transition |
| Stale Asana projection is mistaken for authority | Conspicuous non-authoritative labeling, revision freshness, no ingestion, DB-only legality, and asynchronous overwrite of drift in the reused existing project set |
| Out-of-order projection overwrites newer state | Per-task projection sequence, exact task-revision binding, serialized application, and stale-event no-ops |
| Projection retry history or effect evidence is lost in a mutable queue row | Keep immutable sequence-ordered events, durable pre-call attempt intents, and append-only adjudicated outcomes with effect outcome separate from delivery disposition; mapping/retry rows are derived summaries only |
| Two request IDs mutate one operation concurrently | Separate request-executor claims from generation-identified task/operation mutation fences; at most one active/unresolved fence per lane and unresolved execution remains fenced |
| Ambiguous pre-cutover Asana creation duplicates a task or loses alias binding | Reserve Dish UUID and create intent, write a stable discoverable Asana correlation marker, reconcile before retry, and bind the GID exactly once |
| Ambiguous projection creation looks like duplicate work | Reconcile mirror mapping; never create another Dish task or authority record |
| Live request claims produce inconsistent client behavior | One non-terminal code and replay contract across every route |
| Expiring claim erases replay interpretation | Keep immutable request envelope separate; retire only the executor claim |
| Pending request is reinterpreted after deployment | Persist contract, payload, Dish/Honest release, adapter, schema, and canonicalization identity with the request |
| Planning intent is inferred from workflow legality | Preserve the replay-bound two-request challenge relation; first call performs no task read/effect/lease/fence, and successful start consumes the exact challenge atomically |
| Governed Planning change proceeds without exact Marco authority or reuses a grant | Preserve immutable exact grants, append-only reservations/releases, single-use consumption bound to the committed version occurrence, and no active reservation at cutover |
| PITR erases request history and an old retry executes as new work | Offline exclusive restore, external restore-control evidence, new database-authority generation, fresh non-forgeable service-issued run capabilities, and fail-closed rejection of every pre-restore run/request/capability |
| Incidental audit failure reverses success or loses repair intent while PostgreSQL is unavailable | Governed evidence is transactional; invocation/transport audit remains success-preserving and uses a crash-safe external emergency repair journal when PostgreSQL cannot store the audit or repair |
| Identifier/API migration breaks agents | Use Dish UUIDs universally and coordinate service, OpenAPI/Action schema, instructions, examples, and protocol identity; retain `task_gid` compatibility only if it materially reduces rollout risk |
| Historical evidence becomes unreadable | Preserve terminal attempts and provenance; migrate consumers before cleanup |
| PostgreSQL loss or corruption | Operator-managed encrypted off-host backups, WAL/PITR where practical, rehearsed restore, generation-specific bootstrap authority, fresh projection epoch, full downstream reconciliation, source snapshots, and sensible off-account/off-device copies; retired Dish backup commands are not fallback control authority |
| A destructive legacy SQLite restore leaves stale shadow evidence qualified | Bind requests, runs, rollout sequences, envelopes, gaps, baselines, and parity to a legacy authority generation; invalidate prior evidence and rebuild a complete baseline after restore |
| Authority flip leaves an old Asana writer alive | Use one crash-safe authority activation plus a hard shared writer fence; PostgreSQL admission requires the active fact and rollback burns before first request admission |
| A surviving pre-restore client self-registers as new | Require generation-specific bootstrap authority unavailable to old processes and explicit reissue evidence; ordinary bearer credentials and fresh UUIDs are insufficient |
| Unmapped tasks appear in the reused Asana project | Continuously enumerate and classify every visible GID as mapped, correlated, isolated, or blocking unknown; never ingest it as authority |
| Projector create cannot be reconciled after a lost response | Prove an atomic uniquely searchable Asana marker before enabling DB-native create; otherwise keep create disabled and return topology choice to Marco |
| Cutover rollback loses admitted DB-native intent | Complete acceptance before admission; append rollback burn immediately before the first PostgreSQL mutation request, then use PostgreSQL backup/restore recovery |
| PostgreSQL lock contention or deadlocks | Keep transactions bounded, lock rows in a stable order, retry serialization/deadlock failures through exact request replay, and measure production load |
| Import silently blesses drift | Exact snapshot reconciliation and quarantine; never infer missing facts |
| Final Asana edit is omitted during cutover | Freeze every writer, compare two complete manifests, and import only the named matching batch |
| Asana corpus is imported without its workflow authority | Bind cutover approval to the exact complete WAL-closed legacy Dish runtime authority bundle, including Marco authorizations and audit-repair sidecars, and preserve row-level migration origins |
| Shadow candidate IDs leak into production | Structurally isolated shadow schema/database; authoritative records are created or activated only from approved complete cutover evidence |
| Source digest becomes ambiguous | Store and manifest the identity scheme with every observation and source witness |
| Cutover batch is reopened or reapproved | Irreversible completion plus one append-only approval tied to an earlier matching complete batch |
| Quarantine leaks into ordinary authority | Keep quarantine outside tasks; separately audited promotion only |
| Unmapped quarantined Asana task remains in the reused projection surface | Resolve or explicitly isolate it under the cutover quarantine manifest; any visible unmapped in-scope task blocks mutation admission |

## Deferred decisions and later gates

No human decision remains before Stage A implementation-design handoff. The items below are later
production-authorization or Stage B gates; surrounding text must not be treated as an implicit
answer or as permission to reopen the approved Stage A architecture.

### Before Stage A production cutover

If the deployed Asana contract cannot satisfy the mandatory post-cutover create-correlation
feasibility gate, Marco must choose a different projection/creation topology or accept that
PostgreSQL-native task creation remains disabled. This is a conditional topology decision, not an
implementation agent's discretion.

1. **Final mutation coverage.** Shadow use identifies the narrow actions Marco actually needs after
   Asana becomes non-authoritative. Engineering implements those actions and presents any remaining
   gap as a concrete workflow, not a request for a complete future UI design.
2. **Final live corpus re-baseline and exceptions.** Immediately before cutover, implementation
   derives the exact governed project and workflow scope from the live Dish code and architecture.
   Today only the Cooking project is governed. Cooking History, Cooked, Archive, Sourcing, Reference,
   or other classes are included only if they have separately become part of the live governed
   domain or are explicitly authorized for import. No observed source item is silently discarded;
   out-of-scope data remains in immutable source evidence, while problematic in-scope records are
   reconciled or quarantined case by case and explicitly isolated from the reused Asana projection
   surface before authority cutover.
3. **Cutover and operational-confidence gate.** The exact authority-flip point, acceptance window,
   and evidence threshold are selected near cutover using observed system behavior and Marco's
   infrastructure judgment. The migration surface is resolved-only. Ordinary rollback to Asana is
   available only before the rollback-burn fact and first PostgreSQL mutation-request admission;
   after that point recovery uses PostgreSQL backup/PITR unless a separately approved reverse-
   migration architecture is added.

### Before Stage B

4. **Structured content boundary and schema.** The exact structured Planning and dish grammar,
   including quantities, units, sensory stop conditions, shopping, equipment, storage, provenance,
   and which facts remain workflow or lifecycle state.
5. **Verification across representation migration.** Whether an existing signed title/body version
   remains current until ordinary governed work replaces it, or whether a narrowly defined
   human-approved equivalence attestation may transfer specified facts to a structured occurrence.
   No automatic digest- or rendering-based transfer is allowed.
6. **Stage B activation scope.** Whether structured authority is migrated corpus-wide, only for
   active tasks, or progressively when a task next undergoes governed work.

### Deferred product choices

7. A future private frontend, Cooked and Archive feature activation, Cooking History ingestion,
   cooking planner, scaling, priority, and rich cook-log editing remain separate product decisions.
   They do not block the database-first Stage A migration. The already approved constraint is only
   that Completion, Cooked, and Archived remain semantically distinct when those features exist.

## Approved implementation direction

The implementation plan must conform to these settled defaults:

1. PostgreSQL is the sole target authoritative database; SQLite is legacy-only until cutover.
2. Stage A is a document-compatible PostgreSQL authority migration followed by a battle-hardening
   pause; Stage B is a later structured representation migration.
3. Every task uses a Dish UUID; Asana task and section identifiers are external aliases. Stage A
   document compatibility may retain the embedded destination section alias while internal routing
   uses a Dish location ID.
4. Dish journals intermediate system commands through an ordered causality index linked to exact
   authoritative request, execution, operation/cycle, transition, and recovery facts. The journal
   does not duplicate current control state or checkpoint private agent work. Canonical content
   advances only through complete named governed commands that intentionally write authoritative
   title/body content.
5. Stage A preserves Part I fresh-successor abandonment and task-fence semantics. It does not add
   same-operation replacement or unfinished-authority transfer.
6. Every content-bearing workflow fact binds to its exact task/version occurrence, identity scheme,
   and identity; same digest does not transfer authority or Verification.
7. Use orthogonal state axes. The database-first baseline includes controlled workflow/catalog
   location, version-owned intended destination, separate completion/Planning eligibility, and
   governed-versus-historical-read-only operability. Cooked projection/history and governed Archive
   disposition retain their approved distinct semantics but are feature-gated until separately
   introduced; they are not migration prerequisites and are never inferred from completion or
   placement. Ordinary commands do not hard-delete.
8. A complete validated baseline of the then-current Asana corpus and exact legacy Dish workflow
   database is mandatory before shadow execution. Before cutover, Asana-to-PostgreSQL mirroring is
   one-way and non-authoritative; every shadowed command remains behaviorally executable by the
   Asana-backed authority and mirror failures never block Asana. After cutover,
   PostgreSQL-to-Asana projection into the existing in-scope project set is one-way and
   non-authoritative; projection failures never block PostgreSQL.
9. The Stage A Asana projection uses one immutable mutation-origin event per rendered task revision
   plus a monotonic per-task projection sequence for explicit renderer, mapping, or repair refreshes,
   durable pre-call attempt intents and append-only adjudicated outcomes with external-effect outcome
   separate from worker disposition, and a derived mutable mapping/retry summary. Creation is stably correlated and
   non-supersedable; update coalescing is permitted only after mapping and only when skipped
   revisions have no distinct human-visible effect.
10. PostgreSQL task state, workflow evidence, immutable canonical request outcome, version
    activation, governed audit, committed-fact causality links, and immutable projection events share the
    required atomic command transaction boundary. Fresh current-action views are derived after commit and on
    replay rather than stored as historical authority.
11. Cutover authority binds two matching complete Asana corpus manifests and one exact complete
    WAL-closed legacy Dish runtime authority-bundle manifest, including Planning-intent, Marco-
    authorization, restore-sidecar, and invocation-audit repair authority. Stage A migration is
    resolved-only: open or unresolved
    authority is completed, recovered, abandoned, or quarantined rather than inferred or migrated.
12. Stage A initially runs self-managed PostgreSQL in Docker Compose on Marco's laptop, with
    encrypted off-host backup, WAL/PITR where practical, monitoring, and rehearsed restore as operational requirements.
    The design permits later relocation to a self-managed AWS host without authority changes.
    Multi-region, managed service, or automatic failover is not required.
13. Preserve the currently implemented durable two-request Planning-intent authority. Challenge
    issuance is admission-only and does not read/mutate the task, create an operation, take a fence,
    or acquire a lease; a fresh exact request claims it, and successful Planning start consumes it
    atomically with operation creation. Marco may permanently settle an issued or claimed-but-
    unconsumed challenge through an audited, reason-bearing, non-reusable administrative action.
    Shadowing and migration preserve the full lifecycle, and cutover requires no nonterminal
    challenge to remain.
14. Before cutover, shared command logic separates pre-effect planning from post-effect evidence
    adjudication. After PostgreSQL authority, local commands may collapse them into one transaction.
15. Every task/workflow mutation request has durable command-execution authority, including task-
    scoped commands before or outside an operation; expiring worker claims and durable task/operation
    fences remain separate. Admission-only Planning challenge issuance remains the one explicit
    request-plus-challenge exception and takes no task/operation fence.
16. Cutover observations preserve every in-scope Asana project/section membership rather than
    flattening a task to one project.
17. Production migration is resolved-only, and ordinary rollback to Asana authority ends when the
    rollback-burn fact is appended immediately before first PostgreSQL mutation-request admission.
18. A private frontend is optional and later. The Stage A mutation surface is progressive, bounded,
    and extended through ordinary commands and Alembic migrations as real needs appear.
19. The database migration precedes activation of new Cooked or Archive product concepts. When those
    features are separately introduced, ordinary commands archive rather than hard-delete, Cooked
    and Archived remain distinct, and exceptional purge remains outside this design.
20. Destructive PostgreSQL restore is an offline exclusive operator boundary. It establishes a new
    database-authority generation from durable control evidence outside the restored timeline and
    issues fresh non-forgeable capabilities only to newly registered run identities; pre-restore
    requests, runs, and capabilities are rejected and required work is deliberately reissued. The
    downstream Asana projection is rebuilt under a fresh projection epoch.
21. Cutover authority binds the complete legacy runtime authority bundle, including a validated
    online-backup snapshot with WAL closure, the database-ownership marker, restore journal/fault
    state, required restore artifacts, and all invocation-audit repair sidecars. No
    visible in-scope Asana task may remain unmapped: contradictory source tasks are resolved or
    explicitly isolated from the projection surface before mutation admission opens.
22. Use SQLAlchemy 2.x, Alembic, psycopg 3, and Pydantic as the default PostgreSQL application
    stack, following the approved transaction, migration, constraint, timestamp,
    connection-lifecycle, and test-isolation conventions above. Exact versions belong in the
    implementation lockfile and release evidence; the currently preferred
    starting baseline is SQLAlchemy 2.0.50, Alembic 1.18.4, and `psycopg[binary]` 3.3.4.
23. PostgreSQL separates expiring request-executor claims from durable, generation-identified
    task/operation mutation fences. At most one active or unresolved fence exists per contention
    lane; released fence rows remain historical evidence and are never reused. Claims and fences use
    database-fenced tokens/generations rather than SQLite-era hostname/PID liveness, unresolved
    executions remain fences, and all commands follow one documented lock order. Multi-statement
    authoritative reads use one consistent snapshot.
24. Historical import exceptions are never silently dropped and do not require one universal policy
    now; they are quarantined or reconciled case by case from exact source evidence.
25. Battle-hardening and cutover are evidence-based decisions made near the relevant phase, not
    fixed-duration gates inferred by implementation agents.
26. Shadow and authoritative execution use one shared deterministic `CommandPlan` over the
    same captured pre-command snapshot and pinned inputs, plus shared post-effect adjudication over
    the same persisted intent and evidence. Phase 2 reserves a rollout sequence and durably records
    either the complete pre-effect shadow envelope or an explicit permanent gap; reconciliation never
    fabricates command parity. Pre-cutover `create` is parity-eligible only with a stable discoverable
    Asana correlation marker and exactly-once UUID/GID binding. Multi-effect commands iterate through
    exact plan/adjudication rounds; adapters never choose the next workflow step. Snapshot, commit,
    and effect adapters differ, but transition and outcome-classification rules do not. A separate
    shadow reducer is prohibited.
27. Version rows are fully immutable. Becoming current is represented by a separate append-only,
    single-use activation record committed with the task pointer and revision. Activation provenance
    is a constrained union: request/execution authority for commands, or exact cutover approval,
    import origin, and workflow import run for initial import—never a fabricated command.
28. Every response separates the request's immutable first terminal outcome and any append-once
    uncertainty resolution from a fresh current view. Exact replay returns the resolution when one
    exists while preserving the original uncertain outcome permanently. Only the current view may
    expose `allowed_actions` as legal now. The public API may break deliberately; the deployed
    service, OpenAPI/Action schema, agent instructions, and protocol identity change as one rollout.
    Compatibility assembly is optional and must not weaken the new authority contract.
29. `task_revision` advances only for canonical/projected task axes. Operations, cycles, leases,
    requests, and executions carry separate versions/generations, and an opaque principal/run-scoped
    current-view token binds the complete authority snapshot used for legal actions as a staleness
    precondition, never as standalone mutation authority.
30. `source_completed` is immutable import provenance and initializes the separate completion/
    Planning-gate axis; it never implies Cooked. Cooked is feature-gated and, when introduced, may
    be established only by exact governed Cooking History evidence, separately audited human
    evidence, or a governed transition.
31. The final cutover proof includes a versioned live-domain corpus-scope contract and an explicit
    governed-action coverage matrix; importer behavior and informal UI habits are not completeness
    proofs.
32. PostgreSQL authority migration is database-first. Before shadowing and cutover, implementation
    re-baselines the current live domain and migrates exactly that authority. New Cooked, Archive,
    or Cooking History semantics are neither inferred nor made prerequisites unless separately
    implemented before the re-baseline.
33. Backward-compatible commands and responses are not required. API changes are coordinated with
    the Action schema, instructions, examples, and explicit protocol/release identity so old agents
    fail clearly rather than operating under a mismatched contract. Shadow UUIDs never route live
    Asana mutations; the authoritative identifier switch occurs with the authority flip unless the
    mapping has first become part of the current authority domain.
34. Implementation design must prove a row-by-row disposition for every current named authority
    relation and durable external artifact. Verification `inspect` is a replay-bound evidence
    mutation in the target contract, not a pure read; Marco authorizations remain exact reservable,
    releasable, single-use capabilities rather than generic audit evidence.
35. Stage A retires the replay-bound SQLite `backup-create` and connected restore commands at
    cutover. PostgreSQL backup and restore are operator-managed; all historical backup request/artifact
    evidence is preserved and no open reservation may cross activation.
36. Pre-cutover shadow authority is bound to a legacy authority generation. Destructive SQLite
    restore invalidates prior-generation requests/runs and command-parity evidence and requires a new
    complete baseline.
37. The one-time authority flip is controlled by an immutable activation preparation plus append-
    only activation events binding the exact import, initial database generation/epoch, releases,
    schema head, and proof set. A hard legacy-writer fence is mandatory, and rollback to Asana burns
    immediately before first PostgreSQL mutation-request admission.
38. Post-restore run registration requires generation-specific bootstrap authority unavailable to
    surviving old processes; ordinary bearer identity and a newly chosen UUID are insufficient.
39. The reused Asana project set maintains continuous mapped/correlated/isolated corpus closure.
    PostgreSQL-native create is enabled only after the deployed marker/reconciliation feasibility
    gate passes; otherwise it remains disabled pending Marco's topology decision.
40. Invocation-audit repair retains a crash-safe PostgreSQL-era external fallback when PostgreSQL
    cannot store the audit or normal repair. Alembic execution is accompanied by immutable,
    generation- and release-bound schema-migration provenance.
41. Shared live/shadow logic is not its own correctness oracle. Freeze current-system behavior before
    refactoring and prove both kernel-to-oracle preservation and live-to-shadow parity. Every proof
    and command item is explicitly tagged Stage A, conditional feature, or Stage B/later.


Table names, PostgreSQL constraint forms, lock primitives, outbox worker implementation, migration
tooling, and API-internal naming are engineering decisions. The implementation may replace
`REPEATABLE READ` with an equivalent single-query consistency proof, but may not weaken the required
one-consistent-authority-snapshot read contract. Engineering decisions return to Marco only if
evidence exposes a
material product, safety, operational, or cost tradeoff not already settled above.

Before code implementation begins, this architecture must be accompanied by separate controlled
artifacts for the Stage A implementation design and the proof/cutover plan. Those documents may
contain exact SQL, Alembic ordering, repository boundaries, worker topology, deployment commands,
and test matrices; they must not silently amend the architecture decisions or later human gates
recorded here.

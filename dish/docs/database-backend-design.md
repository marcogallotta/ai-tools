# Database-backed task store: paused design snapshot

Status: paused target-architecture snapshot after Marco's review on 31 July 2026. This document is
not implementation or cutover authorization. The human-approved decisions below are binding design
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
3. **Canonical commit boundaries.** Incomplete Planning, Research, and Verification-round work does
   not advance canonical task content. Intermediate work is append-only journal evidence and/or
   non-current candidate versions. Canonical task content advances only when the complete Planning
   stage, Research stage, or one complete Verification round reaches its governed commit boundary.
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

## Decision summary

Replace Asana's remaining live authority with a domain-native, versioned task store in PostgreSQL.
Stage A stores immutable title/body documents and preserves the current content contract while
moving task state, workflow evidence, replay, and mutation atomicity into PostgreSQL. Stage B later
introduces versioned structured Planning and dish authority after Stage A has been battle-tested.

Keep `dish-service` as the only live mutation authority and keep the current workflow,
Verification, lease, replay, audit, and recovery boundaries unless this design explicitly changes
them. The authoritative task revision, workflow transition, governed audit evidence, execution
evidence, replay result, and any projection-outbox item commit in one PostgreSQL transaction.

The replacement is not an Asana clone. It owns only:

- immutable document versions in Stage A and structured versions in Stage B, with
  representation-appropriate exact identities;
- exact imported source documents and generated human-readable renderings;
- one current Dish location, completion state, archive disposition, and immutable transition
  history;
- the existing workflow and Verification evidence;
- task creation, browsing, search, and Marco's narrow governed interventions;
- lifecycle-authorized editing without a generic content-save bypass;
- a bounded command for every current human Asana mutation that remains necessary after Asana
  becomes read-only;
- future cook-log records only when separately designed and approved.

Authority is singular throughout the migration. Before cutover, production writes go to Asana and
confirmed state is mirrored one-way into PostgreSQL shadow storage. After cutover, production writes
commit to PostgreSQL and a transactional outbox updates Asana as Marco's read-only interface.
Shadow or projection state never decides workflow legality.

## Why consider this after activation

The current external-effect protocol is intentionally conservative. Dish records intent, calls
Asana, rereads the task, and classifies the effect as `confirmed`, `not_applied`, or `uncertain`.
That protects production work but creates recovery states that exist only because the document and
workflow evidence commit in different systems.

A PostgreSQL-native task mutation can commit the new task revision, workflow transition, governed
audit evidence, execution evidence, replay result, and projection-outbox item together. A process
failure before commit rolls the whole unit back; a response loss after commit is answered by exact
request replay. This removes ordinary content writes, moves, completion changes, archive changes,
and task creation from the ambiguous external-effect model.

The human-approved canonical-boundary rule provides an additional recovery simplification:
incomplete stage work remains journaled and non-current. A replacement execution starts from the
last committed task version and may reuse journal material only as independently validated evidence;
it does not need unfinished canonical content to be repaired or transferred.

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
3. Journal incomplete Planning, Research, and Verification-round work without advancing canonical
   task content.
4. Preserve exact imported source documents, independent Verification, run lineage, action
   authority, request replay, leases, audits, backup, and restore.
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
   Stage A and structured after Stage B—plus location, completion, archive disposition, immutable
   task revisions, and stage-attempt journals.
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
field to `task_id` without changing identity.

Section GIDs likewise survive only as immutable external aliases and provenance. Structured
responses and new versions use stable Dish location identifiers. An imported Asana section GID
never becomes current location authority.

## Structured command and query contract

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
completed                   current cooked/completion flag
revision                    monotonically increasing optimistic revision
created_at
modified_at
```

`current_version_id`, `current_location_id`, `completed`, and `revision` change only in the same
transaction as their workflow evidence and audit. Archive is represented by the controlled current
location whose role is `archive`, not by a second independently writable task flag. `modified_at`
is generated by Dish and is not mutation authority by itself.

Tasks are never hard-deleted through an ordinary command. Completion, exclusion, or governed
archive preserves their identifiers, versions, journals, workflow evidence, and audit relationships.
The archive route follows the direction recorded in `future.md`: it must not move an active task out
from under an open operation.

The current version pointer replaces `task_content_state` as the authoritative current projection.
There must not be two independently writable current-content tables. During database migration,
`task_content_state` may be converted into a compatibility view or retired after every caller uses
the task pointer and its version-specific schema and document-authority provenance has been
migrated.

### Stage attempts, journals, and canonical commit boundaries

Each Planning stage, Research stage, and Verification round runs inside an explicit durable attempt.
The exact schema belongs to implementation design, but it must preserve at least:

```text
stage_attempts
  attempt_id
  task_id
  operation_id
  stage_kind                 planning | research | verification_round
  starting_version_id
  starting_revision
  status                     active | committed | abandoned | blocked
  committed_version_id       nullable
  started_at
  finished_at                nullable

stage_journal_entries
  entry_id
  attempt_id
  sequence
  entry_kind
  payload_or_evidence
  request_id                 nullable
  execution_id               nullable
  recorded_at
```

Journal entries are append-only. They may record candidate content, observations, validation,
decisions, declared external effects, confirmed external-effect results, errors, and recovery
evidence. They are not canonical task content merely because they are durable.

Candidate versions may be complete immutable `task_versions` rows with `became_current_at IS NULL`,
or an equivalently strict attempt-owned candidate representation. Creating or validating a
candidate never changes `tasks.current_version_id`, task location, completion, or the Asana
projection.

The complete governed boundary commits once:

- completed Planning commits its final Planning version and transition;
- completed Research commits its final candidate/handoff version and transition;
- one complete Verification round commits the exact round outcome, including any governed corrected
  version and route transition.

That transaction advances the canonical pointer only after all required stage/round evidence is
present. A crash before commit leaves the previous canonical task unchanged. An abandoned attempt
remains readable history but carries no unfinished canonical authority. A later execution starts
from the last committed version and may reuse journal material only after independently validating
it under the current workflow and release contract.

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
  completed
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
identity pairs, placements, completion states, and section registry are captured and its corpus
manifest is deterministically hashed. The manifest hashes each
`(content_identity_scheme, content_identity)` pair, never an unqualified digest. It hashes corpus
facts only; batch-local identifiers and observation timestamps do not prevent two
independent frozen enumerations of the same corpus from producing the same manifest identity.
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
cutover_batch_approvals
  batch_id                    unique
  matching_batch_id
  manifest_identity
  approved_by
  approved_at
```

Only a complete `cutover` batch may be approved. Its matching batch must be an earlier complete
`cutover` batch with the same manifest identity and exact closed corpus facts. The approval repeats
the immutable manifest identity, cannot be changed or cleared, and is rejected if either batch or
manifest does not match. Repeated shadow and reconciliation rows remain comparison evidence. They
cannot become task origin authority merely because they are newest or individually complete.

Only a complete `cutover` batch that records exact agreement with an earlier complete cutover batch
through this approval record may establish imported task origins:

```text
task_import_origins
  task_id
  cutover_batch_id
  source_observation_id
  resolved_location_id
  placement_alias_id
  selected_destination_resolution_id   nullable
  imported_at
```

The origin links the authoritative imported task to exactly one observation in that cutover batch
and records the location resolution used for its initial projection. Imported placement and
completion are origin facts; they do not fabricate Dish transitions for state that predates DB
authority. Quarantine records may cite observations, but only separately audited promotion from an
approved cutover batch may create a task origin.

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
additionally retain the exact framed preimage as a BLOB, but must not call two unspecified database text values a byte witness. The observation carries the qualified identity into its corpus
manifest.

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
applicable schema provenance. Stage attempts may create new immutable non-current candidate document
versions, but only the complete governed stage or Verification-round commit advances the canonical
pointer. Imported source documents are never overwritten.

The document-compatible store must still use the same task pointer, transaction, replay, workflow,
location, completion, audit, and rollback contracts. It preserves source documents and may attach
non-authoritative structured candidates. Its command API must not force the future frontend to
depend on raw database rows or prevent later versioned structured payloads.

A lightweight categorical metadata layer — destination category, region, protein type, tier, and an
explicit availability blocker — may be added directly at this document-compatible stage as simple
indexed columns or a side table, independent of structured dish versions. These are low-identity-risk
tags, not lossless quantity or nutrition data, so they do not need to wait for the separately gated
representation migration below.

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
role                        research_queue | verification_queue | destination | archive | excluded
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
import as excluded locations. The governed Archived section imports as an archive location. Other
approved Cooking sections import as destinations. Removing or repurposing a referenced location is
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

### Completion history

Add a dedicated append-only `task_completion_transitions` table:

```text
transition_id
task_id
old_completed
new_completed
purpose
actor
request_id
reason
occurred_at
```

The mutable `tasks.completed` flag is the current projection, not historical proof. Marking a task
cooked, reopening it, or applying another approved completion-state change appends one transition
and commits it with the flag, governed audit, lifecycle evidence, and canonical request result.
Completed-history lookup reads the current flag for membership and the transition history for
provenance. A clone is a new task with explicit source lineage; it does not rewrite the original
task's completion history.

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

If Asana remains as a read-only human view, each DB mutation that affects its display appends a
projection-outbox item in the authoritative transaction. A separate worker renders the committed
version and applies the projection. Projection state records pending, applied, failed, and
reconciliation evidence plus the Asana task mapping.

Projection failure never rolls back or reclassifies the DB mutation. It produces stale-view
health and repair guidance, not `BACKEND_UNCERTAIN` for production work. Out-of-band Asana edits are
detected and overwritten or flagged; they are never ingested as new authority. Asana task creation
ambiguity may require projection repair, but any duplicate is explicitly a mirror artifact and
must not appear as a second Dish task.

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
- cutover approval is append-only and names one matching earlier complete cutover batch;
- quarantined imports cannot be promoted or resolved through ordinary task commands;
- location and completion projections match the import origin plus latest post-import transitions;
- one committed current-state mutation advances the task revision exactly once.

Use composite foreign keys, uniqueness constraints, checks, and declarative PostgreSQL
constraints wherever possible. Use triggers only for invariants that cannot be expressed safely
otherwise. Semantic startup validation covers release-specific or cross-table rules that the
database cannot prove alone.

Quarantine remains outside authoritative `tasks` and ordinary service reads. Promotion is a
separately audited import action that inserts a proven task and its origin state; it is not a
status flip on an otherwise authoritative task.

### Request execution ownership

Separate the immutable replay envelope from its expiring executor lease. Every mutation request,
including an operation-scoped command, permanently records:

```text
service_requests
  request_id
  command
  request_contract_version
  payload_identity
  adapter_version                 nullable
  structured_schema_version       nullable
  canonicalization_version        nullable
  reserved_task_id                nullable deterministic output identity
  canonical_candidate             nullable immutable derived payload
  status
  result
```

The request's identity and interpretation fields are immutable after reservation; status and result
advance monotonically. All survive request completion and explain exact replay permanently.
Operation execution claims may serialize an operation-scoped command, but never replace this
request envelope or its payload-version evidence.

Commands without an existing operation additionally use a one-to-one expiring executor lease:

```text
request_execution_claims
  request_id
  owner_token
  claim_generation
  claimed_at
  expires_at
```

Only an atomic compare-and-swap may acquire an unowned or expired claim. A live foreign claim
returns one stable non-terminal `REQUEST_IN_PROGRESS` response rather than executing. That response
is not the canonical request result, does not complete or consume the request, requires the same
`request_id` for replay, and reports whether retry may occur now or must wait for expiry or named
recovery. It never exposes the foreign owner token. Its `retryable` meaning and retry guidance are
identical across command routes: `retryable` is true only for replay of the same request identity,
`safe_to_retry_now` is false while the foreign claim is live, and `retry_condition` names claim
completion, expiry, or named recovery. The exact envelope and timing field must be added to
`runtime-contract.md` with implementation.

Recovery increments the generation and issues a new owner token; the displaced executor can no
longer pass the ownership check inside its effect transaction. Completion of the effect and storage
of the canonical result retire or delete the expiring claim atomically. They never delete or mutate
the request's identity, payload interpretation, reserved output, or stored result. These claim
semantics are required for request-scoped mutations and do not replace the existing operation claim
for work that already has an operation.

Request replay must never reinterpret a compatibility payload under newly deployed parsing or
canonicalization code. Reservation persists the exact request contract and version pins. For an
adapter-based request, the preferred contract also persists the already-derived canonical candidate
or its immutable identity-bearing representation before execution ownership; otherwise recovery
must retain the exact adapter and parser implementation named by the request. Deployment normally
requires no pending requests, but quiescence is an operational gate rather than a substitute for a
correct durable recovery contract.

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
interventions. They use `request_execution_claims` with an owner token and expiry/recovery rules.
Task-scoped mutations additionally lock and reread the task inside the PostgreSQL transaction,
normally through row-level locking on the exact task and request/operation ownership rows.

Where useful, reservation stores deterministic output identity before execution. In particular,
`create` reserves its new task UUID on the replay-bound request. After acquiring request ownership,
its effect transaction proves that the request is still pending, the executor still owns the
request claim, and no task already has that reserved identifier. Exact concurrent replays can
observe or recover the same request, but cannot both execute it.

After admission, every database-native task mutation has one effect transaction:

1. authenticate and validate the request envelope;
2. reserve or match the replay-bound service request and any deterministic output identifiers;
3. acquire the applicable operation-scoped or request-scoped execution ownership;
4. begin the PostgreSQL transaction;
5. reread the pending request, ownership token, current task when one exists, and exact
   content/location/version expectations;
6. assert the action through `CurrentWorkflowService`, including lifecycle-specific content
   authority;
7. append the complete new version graph, location transition, or completion transition;
8. update the current task pointer/state and append workflow, Verification, lineage, and governed
   transition-audit evidence plus any projection-outbox item;
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

Expected current version occurrence, identity scheme, canonical identity, and location remain the
semantic concurrency check. Every workflow continuation also revalidates the exact version
occurrences recorded by its operation, steps, actors, holds, classification, signoff lineage, and
submission baseline. The monotonic `revision` is an additional compare-and-swap guard and query
aid, not a replacement for exact content, placement, signoff, or actor evidence.

### Audit boundaries

Governed audit facts and transition evidence required to prove the mutation are written inside the
effect transaction. The canonical request result is also atomic with that mutation.

Invocation and transport audit remains a success-preserving, repairable boundary after the
canonical result. Its failure must not roll back or turn a committed workflow success into a retry
signal. Moving that audit into the effect transaction would be a separate contract change and is
not part of this design.

Read commands use one consistent PostgreSQL transaction snapshot to build the authoritative task and workflow view.
They never update leases or read projections as a side effect.

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

Normal DB-native content, placement, completion, and creation mutations no longer return
`BACKEND_UNCERTAIN`. A database availability or writer-lock failure before commit is safe to retry
under the existing request identity rules. Semantic constraint failures remain fail-closed.

If a connection failure makes commit acknowledgement indeterminate, the service must stop mutation
readiness for the affected path, reconnect, and inspect the replay record and task revision before
advising retry. It must not report rollback merely because PostgreSQL normally gives atomic
transactions.

Recovery must distinguish:

- historical unresolved Asana effects preserved from before cutover;
- database transactions that either committed or rolled back;
- PostgreSQL backup, point-in-time recovery, and restore operations;
- workflow holds and expired leases, which remain real regardless of backend.

Do not keep generic write/movement recovery executable for new DB-native transitions merely because
historical rows use it. Historical unresolved effects must be resolved or quarantined before
cutover; historical terminal evidence stays readable.

Planning reopen becomes an ordinary transactional completion-state change. It remains Marco-only
because that is a lifecycle authority decision, not because the update is technically uncertain.

## Human interface

### Stage A interface

Asana remains Marco's human-facing interface throughout Stage A:

- before cutover it is authoritative and writable under the existing governed model;
- after cutover it is a read-only projection of PostgreSQL authority;
- direct Asana edits after cutover are drift, never commands or imported authority;
- projection lag and last applied PostgreSQL revision must be visible enough that stale display is
  not mistaken for current authority.

Because Asana becomes read-only after cutover, every human mutation Marco still needs must have a
narrow Dish command or admin action before the authority flip. The exact list is a deferred human
review item; it is not permission to build a generic editor.

### Future private interface

A separate private list/search/read/history/action interface may be built later, including during
Stage B development. It is not a Stage A implementation or cutover prerequisite. When built, it
must read through bounded query APIs and mutate only through named Dish commands. It must not access
raw PostgreSQL, impersonate an agent, invent run lineage, or expose private admin routes through the
Action listener.

A future structured editor submits complete versioned candidates with exact task revision and
expected-version preconditions. A cooking planner, scaling, prioritization, and other product ideas
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

- the exact title/body, qualified content identity, section, completion/archive state, and source
  timestamps;
- the corresponding operation/request when the observation followed a Dish command;
- when Stage B development begins, an attempted structured parse and its validation/classification
  evidence, normalized candidate rows, deterministic candidate JSON identity, and compatibility
  rendering comparison.

Shadow persistence failure is visible but does not reinterpret a confirmed Asana mutation. Shadow
rows are never read to authorize live work, written back to Asana, or treated as authoritative
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
journals, candidate handling, projection outbox behavior, backup/restore, and all current workflow
routes against copied or shadow-derived data. Asana remains the only production authority during
this phase.

A new private frontend is not part of this phase. Equivalent narrow Dish CLI/admin commands must
cover every human mutation that will no longer be possible directly in Asana after cutover.

### Fixed cutover target

The first production cutover target is **document-compatible PostgreSQL authority**:

- imported and DB-native canonical content remains exact title/body document versions;
- PostgreSQL owns the canonical task pointer, locations, completion/archive state, journals,
  workflow evidence, replay, and mutation transactions;
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
3. **Completed historical tasks:** import the exact source document and selected cutover
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
2. Snapshot the complete Asana corpus, Dish database, shadow evidence, and configuration.
3. Require no executing claims, unresolved effects, or uncompleted service requests.
4. Rehearse the safest resolved-only route—finish, abandon under the current contract, or
   quarantine every open operation—and separately characterize whether exact journaled open
   operations could ever migrate safely. The production policy remains deferred to Marco.
5. Import every in-scope task under its class into a copied database.
6. Prove observation-batch closure, including one exact source-document witness per task,
   complete section coverage, matching linkage and qualified identities, and no duplicate external
   IDs;
   then reconcile current pointers, location/completion/archive state, operation history, signoff,
   and provenance. Structured conversions are Stage B evidence and do not affect Stage A import.
7. Quarantine mismatches that affect live authority; do not infer content, readiness, destination,
   validation, or signoff.
8. Validate PostgreSQL semantics, queries, backup/PITR/restore, request ownership, stage journals,
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
   project mutations, including edits, moves, completion changes, creation, and section changes;
4. revoke or temporarily disable every credential capable of writing the authoritative Asana
   project where practical, retaining only the minimum read access needed for observation;
5. enumerate the complete frozen corpus into a first `cutover` observation batch, including the
   task set and count, section registry, exact title/body logical-string witnesses and qualified
   identities, placements, and completion states; reject duplicate task or section GIDs and compute
   its corpus-manifest identity only after source-document and section closure passes;
6. repeat the complete enumeration under the same freeze into a second `cutover` batch and require
   its independent closure plus exact agreement of task set, count, section registry, source
   document identity schemes and identities, placements, and completion states; `modified_at`
   agreement alone is never closure proof;
7. append one immutable approval for the second matching complete manifest as the sole cutover
   import batch, and take final database, configuration, code, and source-export snapshots bound to
   it;
8. import only observations from that approved batch into the production PostgreSQL database under
   the document-compatible Stage A contract and quarantine any unapproved mismatch;
9. activate the matching Dish code, schema, Honest revision, query/command surface, and human
   command coverage as one compatible set;
10. remove Asana from live task reads, workflow decisions, and ordinary mutation credentials;
11. grant only the read-only projector's dedicated worker credential and enqueue projection from
   committed PostgreSQL state;
12. keep the approved manifest, its two observation batches, and the exact source export immutable
    during acceptance;
13. admit DB-backed mutations only after identity, location, completion, request ownership,
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
2. Establish PostgreSQL deployment, migrations, connection ownership, managed backup, point-in-time
   recovery, and rehearsed restore without changing production authority.
3. Build the representation-neutral Stage A foundation: universal Dish UUIDs and external aliases,
   task/version envelope, immutable title/body documents, stage attempts and journals, controlled
   locations and archive state, exact workflow-version bindings, completion/location history,
   immutable request envelopes, execution claims, transactional repository path, quarantine, and
   projection outbox.
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
- audited human task completion and completed-history lookup;
- exact reads and consistent list/search snapshots;
- incomplete Planning, Research, and Verification-round attempts that append journal evidence and
  candidate versions without advancing the canonical task pointer or Asana projection;
- atomic complete-stage and complete-round commits, with crash injection before and after the single
  canonical pointer advancement;
- abandonment or restart from the last committed version without treating journal material as
  canonical or inherited authority;
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
  tasks, completed tasks, and tasks owned by an operation;
- request-scoped ownership for concurrent exact replays of `create`, `start`, completion, bare-title
  change, and other non-operation admission paths;
- permanent immutable request contract/payload/version evidence for both request- and
  operation-scoped mutations after their expiring execution claims are retired;
- deterministic `create` identity across crash, recovery, and replay;
- concurrent mutations against the same and different tasks;
- request replay before, during, and after transaction commit;
- recovery of old and adapter-based requests without reinterpretation across deployment;
- replayed results whose leases, ownership guidance, and principal-filtered `allowed_actions`
  reflect the committed post-finalization snapshot;
- content, location, completion, signoff, and actor drift;
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
- imported current placement independent of embedded destination, and imported completion/location
  origin without fabricated local transitions;
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
- immutable many-to-one location aliases, append-only retirement evidence, and interval resolution
  by durable batch sequence rather than batch UUID ordering;
- shadow execution divergence reporting without production response influence;
- document-compatible Stage A cutover rehearsal and a separately gated Stage B representation-migration rehearsal;
- historical terminal write/movement evidence, dedicated local transitions, and absence of
  fabricated database-backed attempt records;
- atomic completion and reopen transitions, current completion projection, governed audit, and
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
- service restart, PostgreSQL lock contention/deadlock handling, managed backup, point-in-time recovery, restore, and restore rollback;
- isolation of any future private interface from the Action listener and command-only mutation;
- stale shadow reads and stale Asana projection views that cannot authorize production mutations;
- DB-backed production with no Asana authority calls or credentials;
- projection outbox replay, lag, out-of-band drift, update failure, and ambiguous mirror creation
  without changing DB workflow results;
- exact corpus import counts, identities, locations, completion states, and quarantine reports;
- a frozen-authority cutover with two complete enumerations agreeing on task set/count, section
  registry, exact source-document witnesses and qualified identities, placements, and completion
  states before import from the named manifest.

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
| Ordinary shadow row becomes import authority | Batch observations by purpose; only one approved complete cutover manifest may establish origins |
| Parallel persistence recreates dual authority | One-way Asana-authoritative shadow before cutover; required one-way PostgreSQL-authoritative outbox projection afterward; never ingest the downstream copy |
| Backend abstraction becomes a permanent second engine | Test-only selection before cutover; delete live Asana mutation after acceptance |
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
| Completion history is reduced to a mutable flag | Append-only completion transitions commit with the flag, audit, and result |
| Stale Asana projection is mistaken for authority | Read-only labeling, revision freshness, no ingestion, and DB-only legality |
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
| Shadow candidate IDs leak into production | Structurally isolated shadow schema/database; authoritative records are created or activated only from approved complete cutover evidence |
| Source digest becomes ambiguous | Store and manifest the identity scheme with every observation and source witness |
| Cutover batch is reopened or reapproved | Irreversible completion plus one append-only approval tied to an earlier matching complete batch |
| Quarantine leaks into ordinary authority | Keep quarantine outside tasks; separately audited promotion only |

## Outstanding human decisions

No decision in this section is required now. Agents must not infer an answer from surrounding text.
They should return to Marco only when the relevant implementation phase is close enough to present a
small, concrete choice.

### Before Stage A production cutover

1. **Human mutation coverage.** Which actions Marco must still perform once Asana is read-only, and
   which narrow Dish command or temporary interface supplies each action. Engineering must first
   inventory actual current Asana mutations and present only gaps.
2. **Historical corpus scope.** Whether Stage A imports all completed Cooking history and whether
   Sourcing/Reference records enter PostgreSQL or remain only in immutable source snapshots.
3. **Cutover, open-operation, rollback, and battle-hardening gate.** Whether every operation must
   be closed before the flip or an exact journaled operation may migrate; the concrete acceptance
   evidence, observation duration, rollback point, and operating period required before the
   authority flip and before Stage B may begin. Engineering must propose measurable criteria rather
   than ask Marco to design the test programme.
4. **Asana projection topology.** Whether the read-only projection safely reuses the existing Asana
   project or uses a separately labeled mirror project. The proposal must prioritize Marco's normal
   usability while proving that no projection credential can act as authority.

### Before Stage B

5. **Structured content boundary and schema.** The exact structured Planning and dish grammar,
   including quantities, units, sensory stop conditions, shopping, equipment, storage, provenance,
   and which facts remain workflow or lifecycle state.
6. **Verification across representation migration.** Whether an existing signed title/body version
   must remain current until ordinary governed work replaces it, or whether a narrowly defined
   human-approved equivalence attestation may transfer specified facts to a structured occurrence.
   No automatic digest- or rendering-based transfer is allowed.
7. **Stage B activation scope.** Whether structured authority is migrated corpus-wide, only for
   active tasks, or progressively when a task next undergoes governed work.

### Deferred product choices

8. A future private frontend, cooking planner, scaling, priority, and cook-log editing remain
   separate product decisions. They do not block Stage A.

## Approved implementation direction

The implementation plan must conform to these settled defaults:

1. PostgreSQL is the sole target authoritative database; SQLite is legacy-only until cutover.
2. Stage A is a document-compatible PostgreSQL authority migration followed by a battle-hardening
   pause; Stage B is a later structured representation migration.
3. Every task uses a Dish UUID; Asana task and section identifiers are external aliases only.
4. Incomplete stage and Verification-round work remains journaled/non-current; canonical content
   advances only at the complete governed boundary.
5. Every content-bearing workflow fact binds to its exact task/version occurrence, identity scheme,
   and identity; same digest does not transfer authority or Verification.
6. Use controlled Dish locations, completion history, and governed archive rather than project
   emulation or hard deletion.
7. Before cutover, Asana-to-PostgreSQL mirroring is one-way and non-authoritative. After cutover,
   PostgreSQL-to-Asana projection is one-way and read-only.
8. The Asana projection is required for Stage A as Marco's human interface and uses an isolated
   outbox worker and credential with no authority path.
9. PostgreSQL task state, workflow evidence, request result, governed audit, journal commit, and
   projection outbox share the required atomic transaction boundary.
10. No open or unresolved mutation state may be silently inferred into the imported authority;
    mismatches are reconciled or quarantined.
11. The final cutover, open-operation, acceptance, and rollback boundaries remain explicitly
    deferred; implementation must not treat the proposed mechanics as human-approved.
12. Managed backup, continuous point-in-time recovery, and rehearsed restore are Stage A operational
    requirements. Multi-region or automatic failover is not.
13. A private frontend is optional and later. Stage A must instead provide narrow commands for every
    required human mutation after Asana becomes read-only.
14. Ordinary commands archive rather than hard-delete; exceptional purge is outside this design.

Table names, PostgreSQL constraint forms, lock primitives, transaction isolation, outbox worker
implementation, migration tooling, and API-internal naming are engineering decisions. They return to
Marco only if evidence exposes a material product, safety, operational, or cost tradeoff not already
settled above.

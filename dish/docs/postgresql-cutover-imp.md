# Dish PostgreSQL Implementation Readiness Plan

## Status

**Implementation-planning baseline — 5 August 2026**

This document converts the approved recovery design and Marco product decisions into an actionable implementation package.

It does **not** authorize repository changes by itself. It defines what must be specified, built, tested, and approved before PostgreSQL cutover.

## Source authority

This plan is derived from:

- `dish-postgresql-recovery-simplification-design-final.md`
- `marco-dish-postgresql-product-decisions-final.md`

When interpretation differs:

1. Marco’s explicit product decisions control.
2. The recovery and simplification design controls architecture and sequencing.
3. This document controls implementation planning and work decomposition.
4. AI-authored severity labels, earlier handoffs, and inferred requirements do not override those sources.

---

# 1. Implementation Objective

Deliver the smallest coherent PostgreSQL Stage A system that can:

- keep GPT Actions supported while performing the approved, explicit `create` response migration;
- return the canonical Dish UUID immediately, with a frontend URL when configured;
- support asynchronous Human Review across agent runs;
- allow safe reclaim after lease expiry or explicit lease termination;
- preserve exact authorization, request replay, audit, and rollback;
- project PostgreSQL state to Asana reliably;
- run a truthful dark launch;
- cut over during a controlled maintenance window;
- perform one verified first PostgreSQL mutation before normal writes open;
- keep Asana read-only after cutover until Marco decides otherwise.

The implementation must reduce existing release, evidence, and migration complexity rather than add another control layer.

---

# 2. Product Decisions That Are Closed

The following are **not open implementation questions** and must not be relitigated by implementation agents.

## 2.1 Authority and workflow

- Marco is the final product authority.
- Agents may warn and record concerns, but may not impose substantive product or safety blocks on Marco.
- Only database mechanical-integrity and recovery conditions may block execution.
- Discussion, clarification, urgency, silence, disagreement, and continued conversation are not authorization.
- Findings, evidence, proposals, authorization, and applied mutations are separate records or states.
- Approval is separate from application.
- Semantic proposals are retained as asynchronous Human Review.
- A later eligible agent must be able to apply the exact approved change without depending on the original agent run.

## 2.2 Reclaim and recovery

- Lease expiry or explicit lease termination normally makes work reclaimable.
- A second abandonment operation is not required merely because a lease ended.
- Safe reclaim requires mechanically proven absence of pending execution, unresolved external effects, uncertain outcome, incomplete settlement, and partial mutation.
- Formal abandonment and succession are reserved for genuine recovery risk.
- Reclaim must atomically fence the previous owner and reject late writes.

## 2.3 Override and verification

- Dish has no agent-controlled hard blocks.
- One explicit Marco override ends repeated challenge on that specific concern.
- An override remains recorded and may be reopened only for materially new evidence or Marco’s explicit request.
- Marco may move a Dish out of `needs verification` through `dish-admin`.
- Marco may also override through an agent after one concise warning.
- Light verification is a post-cutover requirement, not a cutover blocker.
- “Proceed now, reconcile later” is a post-cutover requirement, not a cutover blocker.

## 2.4 Rollback and history

- Whole-version rollback is required before cutover.
- Rollback is admin-only and requires Marco’s explicit confirmation.
- Restoring an older version creates a new canonical version.
- History must never be deleted or rewritten.
- The prior version, exact applied diff, approving authority, brief rationale, and resulting version must be retained.

## 2.5 External and cutover behavior

- GPT Actions remain supported through cutover.
- Existing non-`create` command semantics and the general result envelope remain stable unless separately approved.
- `create` intentionally migrates to required `dish_id`, optional configured `url`, and optional `asana_task_gid`.
- The legacy `task_gid` field must never contain a Dish UUID.
- Every deployed consumer of the old `create` response must be updated before general PostgreSQL admission opens.
- Request IDs remain reserved permanently.
- PostgreSQL backup, clean restore, verification, and an off-device copy are required.
- Full point-in-time recovery is not required initially.
- Cutover uses a planned write-free maintenance window.
- One controlled first PostgreSQL request must pass outcome, replay, audit, projection, and Asana reread checks before general admission opens.
- If the first request fails after authority activation, remain in maintenance mode, determine commit state, repair PostgreSQL, and retry the same request ID.
- Asana remains a read-only projection/interface until Marco decides PostgreSQL is trusted sufficiently.
- A shared backend authority/action contract is required before cutover.
- A frontend redesign is not required before cutover.

---

# 3. Definition of “Ready to Implement”

Structural PostgreSQL work is ready to begin only when the following implementation artifacts exist and agree with one another:

1. complete issue disposition matrix;
2. minimum Stage A behavioral contract;
3. approved target PostgreSQL schema;
4. shared command and authority/action contract;
5. runtime integration design;
6. migration, deletion, and import plan;
7. test and acceptance plan;
8. sequenced implementation backlog.

The artifacts may be separate files, but each must have an owner, status, dependencies, and acceptance criteria.

---

# 4. Required Implementation Artifacts

## 4.1 Issue disposition matrix

Create one row for every known issue from:

- current design findings;
- previous reviews;
- handoff documents;
- `ops-issues.md`;
- confirmed test failures;
- confirmed inert or incomplete schema;
- production runtime gaps;
- command-contract duplication;
- release and evidence subsystem findings.

### Required columns

| Column | Meaning |
|---|---|
| Issue ID | Stable identifier |
| Source | Review, handoff, file, test, or investigation |
| Description | Factual problem statement |
| Classification | Live defect, cutover gap, excess, inert schema, verification item, or deferred feature |
| Disposition | Fix, retain, merge, delete, defer, or verify |
| Cutover blocker | Yes or no |
| Affected components | Files, commands, tables, services, or surfaces |
| Required guarantee | Product or integrity behavior that must survive |
| Acceptance test | Objective evidence that disposition is complete |
| Phase | 0 through 6 |
| Status | Not started, investigating, ready, active, blocked, or complete |
| Notes | Dependencies and unresolved technical details |

### Required first entries

- `inspect` incorrectly classified as read-only.
- `apply-proposal` client omits required request ID.
- PostgreSQL semantic proposal/Human Review gap.
- Legacy GPT Actions compatibility gap.
- canonical Dish UUID creation result and optional frontend URL.
- Production baseline capture host execution and final import/reconciliation verification.
- Missing deployable PostgreSQL authority composition.
- Missing production Asana projection adapter.
- Missing production reconciliation adapter.
- Evidence fields with no production writer.
- Hidden consumers of deletion candidates.
- Full `ops-issues.md` disposition.
- Planning challenge/override authorization leakage verification.

### Completion rule

No table deletion, migration squash, or control-plane collapse begins until every known issue has a recorded disposition.

---

## 4.2 Minimum Stage A behavioral contract

Write implementation-level transition tables for the behaviors below. General prose is insufficient.

### A. Authority and admission

Define:

- current authority;
- dark-launch authority;
- cutover activation;
- admission modes `closed`, `exact_request`, and `open`;
- legal transitions;
- actor allowed to perform each transition;
- required preconditions;
- database constraints preventing illegal ordering;
- irreversible PostgreSQL authority boundary.

### B. Request identity and replay

Define for every consequential command:

- request-ID requirement;
- principal, run, command, and payload binding;
- first-authoritative-outcome storage;
- exact replay response;
- pending and uncertain behavior;
- permanent request-ID reservation;
- archival rules for bulky request details;
- failure behavior after transport loss.

`inspect` must be consequential and replay-bound.

### C. Human Review

Define the lifecycle for:

- finding creation;
- evidence and confidence;
- proposed correction;
- open question;
- Marco answer;
- revision requested;
- approval;
- rejection;
- override;
- application;
- rollback;
- closure.

The contract must prove that:

- Marco’s exact words are stored separately from agent interpretation;
- approval binds to one exact proposal and candidate version;
- approval cannot authorize a later modified proposal;
- application may be performed by a later eligible agent;
- application records the exact approved diff;
- a rejected correction does not invalidate the underlying finding automatically.

### D. Lease and safe reclaim

Define:

- lease acquisition;
- lease expiry;
- explicit release or termination;
- reclaim eligibility;
- atomic fencing;
- late-write rejection;
- audit lineage;
- formal abandonment;
- formal succession.

“No pending execution” must be a database-computable condition.

### E. Marco override

Define both paths:

- direct admin override;
- override communicated through an agent.

The agent path must require explicit Marco words. It must not infer override from urgency, clarification, frustration, or continued discussion.

The result must:

- preserve the concern;
- record Marco’s words;
- identify the version accepted for use;
- move the Dish out of `needs verification`;
- stop repeated challenge on that concern unless materially new evidence appears.

### F. Whole-version rollback

Define:

- versions eligible for rollback;
- admin preview;
- explicit confirmation;
- creation of a new canonical version;
- audit record;
- projection behavior;
- handling of newer unrelated changes;
- replay and failure recovery.

### G. Projection and reconciliation

Define:

- ordered projection intent;
- attempt identity;
- idempotent delivery;
- authoritative Asana reread;
- applied, not-applied, and uncertain settlement;
- retry rules;
- drift detection;
- reconciliation;
- behavior when PostgreSQL commit succeeds but projection or observation fails.

Committed PostgreSQL success must remain success. Projection failure is a separate follow-up state, not advice to repeat the mutation.

---

## 4.3 Target PostgreSQL schema specification

Produce a schema specification before writing the squashed migration.

### Every retained table must document

- runtime purpose;
- production writer;
- production reader or consumer;
- primary key;
- foreign keys;
- uniqueness rules;
- check constraints;
- mutable and immutable fields;
- retention policy;
- deletion or archival behavior;
- associated commands;
- associated acceptance tests.

### Required schema capabilities

- canonical Dish versions;
- exact before/after mutation representation;
- Human Review findings and proposals;
- Marco approval and override evidence;
- permanent request identity reservation;
- first-authoritative command outcomes;
- lease ownership and fencing;
- safe-reclaim audit;
- uncertain external-effect recovery;
- ordered Asana projection intent and attempts;
- canonical audit trail;
- admission gate;
- immutable cutover record;
- backup/restore and final reconciliation references.

### Candidate deletions requiring dependency proof

- `causality_edges`;
- `request_uncertainty_resolutions`;
- `applied_migration_events`;
- `source_import_native_links`;
- incomplete readiness/evidence structures;
- release, rehearsal, certification, and manifest layers collapsed by the approved minimal model.

A table may not remain solely because tests construct it.

---

## 4.4 Shared command and authority/action contract

Create one typed source for retained commands.

### Required command fields

- command name;
- query or consequential classification;
- principal and authority requirements;
- run requirement;
- request-ID requirement;
- input schema;
- legal source states;
- exact state transition;
- external effects;
- replay behavior;
- failure states;
- result envelope;
- exposure surfaces;
- dark-launch treatment;
- next legal actions;
- Stage A status.

### Required derived consumers

- service validation;
- client request-ID behavior;
- CLI metadata;
- GPT Actions/OpenAPI;
- `dish-admin`;
- agent action descriptions;
- dark-launch command coverage;
- test inventories.

### Constraint

This contract describes legal actions and effects. It must not become a generic workflow language, and mutation implementation must not be moved into metadata.

---

## 4.5 Runtime integration design

Specify the production runtime composition.

### Required components

- deployable PostgreSQL-backed `dish-service`;
- authentication and principal mapping;
- configuration and secrets;
- health and readiness;
- legacy GPT Actions compatibility adapter;
- production Asana projection adapter;
- production reconciliation fetcher/comparator;
- projection worker;
- dark-launch shadow worker with no Asana mutation credentials;
- import command;
- backup and restore commands;
- `dish-admin` actions for override, lease termination, reclaim inspection, and rollback;
- operational launch or supervision mechanism.

### Required runtime proof

Each required table, state, or evidence field must have a production writer and consumer. Test-only creation does not count.

---

## 4.6 Migration, deletion, and import plan

Because no PostgreSQL data requires preservation:

- archive the old migration chain for provenance;
- create one clean initial Stage A migration;
- include only approved tables, functions, triggers, and constraints;
- remove historical compatibility logic for migrations `0001` through `0029`;
- rebuild the disposable PostgreSQL database;
- import from an actual SQLite/WAL/sidecar bundle;
- compare imported PostgreSQL state against the complete Asana corpus;
- produce one immutable import/reconciliation report.

### Deletion safety rule

Before deleting a table, command, trigger, function, or evidence type:

1. search production code, tests, scripts, docs, and operations tooling;
2. identify every writer and consumer;
3. record the result in the issue disposition matrix;
4. either migrate the consumer or prove it is obsolete;
5. add a regression test for the retained guarantee.

---

## 4.7 Test and acceptance plan

Organize tests around guarantees rather than historical implementation layout.

### Mandatory tests

#### Live defect fixes

- `inspect` requires a request ID.
- `inspect` stores and replays the first authoritative outcome.
- `apply-proposal` generates or accepts a request ID.
- transport-loss resubmission returns the same result.

#### Canonical Dish creation contract

- `create` returns required `dish_id`.
- `create` returns configured `url` when available.
- `asana_task_gid` is optional projection metadata.
- `task_gid` is never populated with a Dish UUID.
- retry of the same request returns the same Dish UUID.
- canonical Dish creation remains successful when Asana projection is delayed or fails.
- configured Dish URLs resolve to the same Dish UUID.
- all known clients, GPT Action schemas, instructions, and scripts are migrated from the old response.

#### Human Review

- finding and proposal are separate.
- clarification does not approve a proposal.
- approval binds exact Marco words, proposal, and candidate version.
- modified proposal invalidates prior approval.
- later agent applies exact approved proposal.
- override through an agent requires explicit instruction.
- one override prevents repeated challenge on the same concern.

#### Reclaim

- expired lease with safe state is reclaimable.
- explicitly terminated lease with safe state is reclaimable.
- reclaim atomically fences the old owner.
- old owner’s late write fails.
- pending or uncertain execution prevents safe reclaim.
- formal abandonment remains available for uncertain effects.

#### Rollback

- admin preview shows exact restoration and loss.
- confirmation is required.
- rollback creates a new canonical version.
- history remains unchanged.
- projection of rollback is idempotent.
- rollback replay does not create duplicate versions.

#### Mechanical integrity

- stale ownership blocks writes.
- duplicate request identity with different payload fails.
- unresolved external effect prevents unsafe retry.
- incomplete settlement prevents unsafe reclaim.
- shadow path cannot mutate Asana.
- admission ordering is database-enforced.

#### Cutover

- backup restores into a clean database.
- restored corpus and workflow state verify.
- final import detects missing, duplicate, unknown, and mismatched entities.
- all legacy writers are mechanically fenced.
- only the reserved first request is admitted.
- first request replay, audit, projection, and Asana reread succeed.
- failure after activation remains in maintenance mode.
- general admission cannot open before first-request verification.

#### Authorization leakage

- planning challenge/override can permit planning only.
- it cannot satisfy or imply mutation authorization.
- it cannot be reused as Human Review approval.

### Testing simplifications

- derive command inventories from the canonical command contract;
- derive Alembic head automatically;
- stop making line-count and duplicate-body checks blocking;
- stop hashing all test files as a product baseline;
- use native PostgreSQL for locks, triggers, concurrency, and server behavior;
- avoid adding SQLite emulation for PostgreSQL-specific semantics.

---

# 5. Sequenced Implementation Backlog

## Phase 0 — Correct current live defects

1. Fix `inspect` classification and replay.
2. Fix `apply-proposal` request identity.
3. Add end-to-end tests.
4. Confirm the current Asana/SQLite production path remains green.

**Exit gate:** both defects are merged and verified.

## Phase 1 — Freeze scope and complete implementation specifications

1. Freeze new release/evidence concepts.
2. Build the complete issue disposition matrix.
3. Complete the minimum Stage A behavioral contract.
4. Complete the target schema specification.
5. Complete the shared command and authority/action contract.
6. Specify and approve the canonical `create` response (`dish_id`, optional `url`, optional `asana_task_gid`).
7. Inventory every consumer of the old Asana-oriented `create` response and approve its migration treatment.
8. Verify planning challenge/override cannot authorize mutation.
9. Approve table and subsystem dispositions.
10. Confirm no PostgreSQL data requires preservation.

**Exit gate:** specifications and dispositions are approved. No destructive schema work before this gate.

## Phase 2 — Reduce and rebuild the control plane

1. Implement the minimal admission gate.
2. Implement the immutable cutover record.
3. Port Human Review, safe reclaim, override, and whole-version rollback.
4. Consolidate command metadata.
5. Remove or collapse approved release/evidence structures.
6. Remove confirmed inert schema.
7. Squash migrations.
8. Rebuild disposable PostgreSQL.

**Exit gate:** clean migration, schema agreement, and core contract tests pass.

## Phase 3 — Complete production runtime

1. Build deployable PostgreSQL service composition.
2. Build legacy GPT Actions compatibility.
3. Implement canonical Dish creation returning `dish_id` and optional configured `url`.
4. Build production Asana projection and reread.
5. Build reconciliation.
6. Build production import and baseline capture.
7. Build required `dish-admin` actions.
8. Prove every retained table and evidence field has a production path.

**Exit gate:** full rehearsal succeeds against real disposable PostgreSQL and an isolated Asana project.

## Phase 4 — Dark launch

1. Import a production baseline.
2. Enable fail-open live capture.
3. Run shadow replay without Asana mutation credentials.
4. Exercise all retained commands.
5. Battle-test workflows manually through agents.
6. Reconcile gaps, lag, mismatches, and backlog.
7. Resolve every unexplained discrepancy.

**Exit gate:** known errors are gone, scripted manual tests pass, reconciliation is clean, and Marco is satisfied.

## Phase 5 — Cutover

1. Create backup.
2. Restore into a clean database.
3. Verify restored state.
4. Retain off-device copy.
5. Enter maintenance mode.
6. Resolve in-flight operations.
7. Fence all legacy writers.
8. Capture final source bundle and Asana corpus.
9. Import into a clean target generation.
10. Run final reconciliation.
11. Approve the cutover record.
12. Activate PostgreSQL under `exact_request`.
13. Revalidate fence, generation, backup, snapshot, and reconciliation.
14. Execute and verify the reserved first request.
15. Open general admission.
16. Keep Asana read-only.

**Exit gate:** PostgreSQL is authoritative and normal writes are open.

## Phase 6 — Stabilize and later remove transition machinery

After Marco decides PostgreSQL is sufficiently trusted:

- remove shadow capture and spool;
- remove import bootstrap code;
- remove obsolete cutover tools;
- remove legacy mutation paths;
- archive final evidence;
- consider light verification, proceed-now/reconcile-later, field-level rollback, and Stage B redesign.

There is no fixed deletion date.

---

# 6. Resolved Implementation Decisions

These decisions close OPEN-1 through OPEN-10. They are implementation constraints, not new product questions.

## OPEN-1 — Safe reclaim representation

Safe reclaim creates a **new mechanically linked operation**.

- The inactive operation remains immutable for audit.
- The new agent restarts the research, verification, or planning step.
- Unapproved partial agent work may be discarded.
- Existing Marco discussion, exact words, and still-valid approvals remain available where applicable.
- The successor link, previous owner, new owner, reason, and timestamps must be recorded.
- Atomic fencing must reject late writes from the previous operation owner.

## OPEN-2 — Mechanical definition of safe reclaim

Reclaim is allowed only when committed database state proves:

- no consequential command is `running`, `pending`, or `uncertain`;
- no external effect lacks terminal `applied` or `not_applied` settlement;
- no proposal, application, or settlement step is incomplete;
- no projection attempt has an unresolved outcome;
- no live lease or claim is held by another owner;
- atomic fencing prevents any later commit from the previous owner.

This must be implemented as one mechanically checkable database predicate used by service code, `dish-admin`, and tests.

Safe reclaim must rely on committed lifecycle state plus atomic fencing, not inspection of the PostgreSQL server’s current transaction list.

## OPEN-3 — Approval evidence representation

Store:

- Marco’s exact words;
- normalized decision: approve, reject, or request revision;
- exact proposal ID and semantic digest;
- exact candidate-version ID and semantic digest;
- actor identity;
- approving surface;
- request ID;
- timestamp.

Store the agent’s interpretation and rationale separately.

Any semantic change to the proposal or candidate invalidates approval and requires a new approval record. Formatting-only or unrelated metadata changes do not.

Invalidated approvals remain immutable history.

## OPEN-4 — Whole-version rollback storage

Use immutable full canonical version snapshots.

Each canonical version must:

- link to its parent version;
- retain the exact diff;
- retain rationale and approving authority;
- remain immutable.

Rollback creates a new canonical version based on the selected prior snapshot. It never rewrites or deletes history.

## OPEN-5 — Canonical Dish creation result

In the PostgreSQL-authoritative system, the canonical object is a **Dish**, not an Asana task.

`create` must return immediately with:

- the canonical Dish UUID;
- a frontend URL when one is configured.

The UUID is authoritative. The URL is a convenient representation that agents should be able to accept and resolve.

Asana ID becomes optional projection metadata populated later. Failure to project to Asana must not make canonical Dish creation fail.

This is Marco’s explicit superseding product decision and must also appear in the design and product-decision record.

## OPEN-6 — Request-detail archival

Deferred implementation detail.

Required now:

- request IDs remain reserved permanently;
- command identity, actor, semantic payload digest, authoritative outcome, and essential audit fields remain durable.

Bulky payloads, response bodies, and diagnostics may remain in the primary database initially. Archival timing and storage may be decided later if volume becomes material.

This does not block Phase 0, Phase 1, schema implementation, or cutover.

## OPEN-7 — Hidden-consumer discovery

Before deleting or merging any table, trigger, function, command, evidence structure, or control-plane subsystem, perform a repository-wide dependency check across:

- production code;
- tests;
- scripts;
- operations tooling;
- documentation;
- migrations.

Every discovered consumer must appear in the issue disposition matrix and must either be migrated or proven obsolete before deletion.

This is a Phase 1 exit gate.

## OPEN-8 — Complete issue inventory

Before schema reduction or migration squashing, create one master issue disposition matrix covering every known issue from:

- `ops-issues.md`;
- handoffs;
- independent reviews;
- failing tests;
- current investigations;
- confirmed runtime gaps;
- inert or excess schema;
- command-contract duplication.

Each row must state whether the item is fixed, retained, merged, deleted, deferred, or still requires verification, plus its cutover impact and acceptance evidence.

This is a Phase 1 exit gate.

## OPEN-9 — Executable dark-launch rehearsal

Before dark launch, implement a literal executable procedural test script in code.

The script must:

- create controlled test data;
- exercise retained workflows through real service interfaces;
- cover create, planning, verification, Human Review, later-run approval/application, reclaim, override, rollback, projection failure, retry, and reconciliation;
- simulate relevant failures;
- assert database, audit, projection, and reconciliation outcomes;
- produce a machine-readable result report;
- fail clearly on any mismatch.

This is a Phase 4 entry gate. It does not block Phase 0 or Phase 1.

## OPEN-10 — Existing `dish-admin` scope

The existing `dish-admin` remains in place. Existing functionality stays unless the disposition review explicitly proves a function obsolete or duplicated.

Before cutover, add or complete:

- direct override out of `needs verification`;
- preview and explicit confirmation of whole-version rollback;
- safe-reclaim eligibility inspection with reasons;
- display of Marco’s exact approval or override evidence.

Existing lease termination remains, but safe reclaim must not require a second abandonment step when state is mechanically safe.

This blocks only the relevant Phase 3 `dish-admin` implementation work.

## Gate summary

| Decision | Gate |
|---|---|
| OPEN-1 | Phase 1 exit |
| OPEN-2 | Phase 1 exit |
| OPEN-3 | Phase 1 exit |
| OPEN-4 | Phase 1 exit |
| OPEN-5 | Phase 1 exit for specification and consumer inventory; Phase 3 for implementation |
| OPEN-6 | Deferred; non-blocking |
| OPEN-7 | Phase 1 exit |
| OPEN-8 | Phase 1 exit |
| OPEN-9 | Phase 4 entry |
| OPEN-10 | Before Phase 3 `dish-admin` implementation |



# 7. Explicitly Deferred Product Work

These items are important but do not block Stage A cutover:

- light verification mode;
- “proceed now, reconcile later”;
- field-level rollback;
- more structured rationale capture;
- broader workflow simplification;
- full frontend presentation redesign;
- Stage B data-model redesign.

Implementations must preserve enough history and authority information to support these later without another destructive migration.

---

# 8. First Implementation Deliverables

The first assignable package should contain:

1. issue disposition matrix;
2. Stage A transition and authority tables;
3. target schema table inventory;
4. canonical command inventory;
5. canonical `create` response specification and old-consumer inventory;
6. Phase 0 patches for `inspect` and `apply-proposal`;
7. Phase 1 authorization-leakage test results.

After those are reviewed, schema reduction and migration squashing can begin.

---

# 9. Approval Checklist

Before treating this plan as implementation-ready, confirm:

- [ ] Every closed product decision is represented correctly.
- [ ] No implementation question silently reopens a closed product decision.
- [ ] Every known issue has a disposition row.
- [ ] OPEN-1 through OPEN-5 and OPEN-7 through OPEN-10 are specified, approved, implemented, and tested according to their assigned phase gates; OPEN-6 remains explicitly deferred.
- [ ] Every retained table has a production writer and consumer.
- [ ] Human Review, safe reclaim, override, and rollback have transition tables.
- [ ] Approval binds Marco’s exact words to an exact proposal and candidate version.
- [ ] Mechanical blocks are distinct from agent warnings.
- [ ] Planning override cannot authorize mutation.
- [ ] `inspect` and `apply-proposal` fixes are merged.
- [ ] Target schema is approved before migration squash.
- [ ] Runtime adapters exist before certification expands.
- [ ] Dark-launch script and exit evidence are defined.

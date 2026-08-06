# Dish PostgreSQL Recovery and Simplification Design

## Status

**Approved product and recovery design baseline — 5 August 2026.**

This document defines the agreed Stage A product and architecture contract. It does not, by itself, authorize an unreviewed repository change.

## Source authority

Marco’s explicit product decisions control this design. The synchronized implementation plan controls execution sequencing and gates but may not override this design.

## Purpose

Recover the Dish PostgreSQL migration from scope growth without weakening the safety properties that matter:

- one explicit mutation authority;
- durable authorization and attribution;
- exact request replay;
- safe parallel-agent operation;
- no blind retry after uncertain external effects;
- faithful Stage A dark-launch comparison;
- controlled cutover;
- recoverable PostgreSQL operation.

The goal is not to redesign the permanent Stage B system yet. The goal is to produce the smallest coherent Stage A system that can reach dark launch and cutover safely.

---

# 1. Current System Model

## 1.1 Current authority

- **Asana** is canonical for task content, placement, and completion.
- **SQLite** is canonical for workflow intent, operations, leases, locks, requests, recovery state, verification evidence, authorization evidence, and audit.
- **dish-service** is the live mutation authority.
- CLI, admin CLI, GPT Actions, and agents are transports into that authority.

## 1.2 Dark launch

- Asana and SQLite remain authoritative.
- PostgreSQL receives imported and mirrored state.
- Shadow replay must not mutate Asana.
- PostgreSQL results are compared with the live path.
- Dark launch is evidence-gathering, not authority transfer.

## 1.3 Cutover

- PostgreSQL becomes canonical.
- Legacy Asana/SQLite mutation paths are mechanically fenced.
- Asana becomes a projection/interface, not an independent editing authority.
- After the first accepted PostgreSQL mutation, rollback means PostgreSQL recovery or forward repair, not reverting authority to SQLite.

## 1.4 Scale

- One human operator: Marco.
- Approximately 100 Dishes currently represented through Asana tasks.
- Low throughput.
- Multiple agents may operate concurrently.
- Strong attribution, replay safety, and concurrency controls are required.
- Enterprise-scale release governance is not automatically required.

---

# 2. Findings That Drive This Design


### Phase 1 authorization verification gate

Before Phase 1 completes:

- inspect planning challenge/override behavior across service, CLI, GPT Actions, agent instructions, and tests;
- prove that it only permits planning to begin;
- prove that it cannot satisfy, imply, or be reused as mutation authorization;
- add regression tests binding mutation approval to Marco’s explicit words, exact proposal, and candidate version.

Phase 2 must not begin until this verification passes.

## 2.1 Sound foundations to preserve

The following are broadly sound and should not be rewritten as part of recovery:

- explicit live mutation authority;
- durable intent before Asana effects;
- authoritative reread after effects;
- applied / demonstrably-not-applied / uncertain settlement;
- exact request replay;
- run, lease, candidate, and authorization binding;
- independent verification evidence;
- separation of proposal approval from proposal application;
- PostgreSQL transaction ownership for command mutation, audit, outcome, and projection intent;
- ordered projection with generation and epoch fencing;
- absolute separation between shadow and live projection;
- shared legal-action policy rather than a second PostgreSQL state matrix.

## 2.2 Confirmed live defects

### Inspect classification

`inspect` appends durable verification evidence and changes available legal actions, but the legacy service classifies it as read-only and does not give it request-replay protection.

### Apply-proposal request identity

`apply-proposal` is replay-required, but the bundled client does not generate the required request ID.

These should be fixed before broader migration work.

## 2.3 Confirmed Stage A gaps

- PostgreSQL lacks semantic-proposal/review/application authority.
- PostgreSQL's Action contract is not legacy-compatible.
- PostgreSQL and existing clients do not yet implement the approved canonical Dish `create` response (`dish_id`, optional `url`, optional `asana_task_gid`).
- Production baseline capture now has an explicit read-only manifest path; host execution and final import/reconciliation evidence remain outstanding.
- No deployable production PostgreSQL authority service composition is present.
- No concrete production Asana projection adapter is present.
- No concrete production reconciliation fetcher/comparator is present.
- The shipped reconciliation path cannot populate all fields required by release validation.
- Some required readiness and import evidence has no production writer.
- Invocation-audit obligations required by cutover validation are not fulfilled by a production path.

## 2.4 Confirmed structural excess

- 102 application tables.
- 29 Alembic revisions.
- Large release/cutover/evidence subsystem.
- Multiple evidence layers certifying other evidence.
- Command identity and mutation classification repeated across several registries.
- Stage A baseline hashes 189 test files, causing structural test changes to appear as product-baseline drift.
- PostgreSQL-target SQLite compatibility duplicates PostgreSQL triggers, constraints, and migration behavior.
- Test-governance metadata creates blocking failures for file movement and helper extraction.
- Several schema concepts are inert, aspirational, or incomplete.

## 2.5 No preservation constraint

There is no PostgreSQL database or Stage A data outside the disposable local fixture instance that must survive. Therefore:

- the PostgreSQL schema may be rebuilt;
- migration history may be squashed;
- disposable rehearsal rows may be discarded after reports are archived;
- no production data migration compatibility must be preserved.

---

# 3. Design Principles

## 3.1 Fix current live defects first

The current Asana/SQLite system remains production authority. Confirmed live defects must not wait behind architecture work.

## 3.2 Freeze scope, not safety work

Freeze:

- new release/evidence concepts;
- new Stage 6–8 tables;
- new certification scripts;
- new readiness taxonomies;
- work whose only purpose is satisfying an existing validator.

Continue:

- live bug fixes;
- factual investigation;
- emergency production fixes;
- work explicitly required by the agreed minimum Stage A contract.

## 3.3 Preserve guarantees, not accidental implementations

Stage A must preserve user-visible behavior and safety guarantees. It does not need to preserve every SQLite mechanism or every AI-authored internal distinction.

## 3.4 Do not begin schema reduction against an undefined target

No PostgreSQL schema squash or subsystem deletion begins until the minimum Stage A contract is written and approved.

## 3.5 Build runtime before expanding certification

The target service, adapters, reconciliation, and command parity must exist before more certification machinery is added.

## 3.6 Every retained concept must have a production writer and consumer

A required table or evidence type must be reachable through a production path. Test-only construction is not sufficient.

## 3.7 Prefer one authoritative record over chains of attestations

Where possible:

- one canonical cutover snapshot;
- one approval;
- one admission state;
- one immutable cutover record;
- external reports referenced by digest.

Avoid records whose primary purpose is certifying that another certification record was checked.

---

# 4. Required Minimum Stage A Contract

This contract must be approved before PostgreSQL structural changes.

## 4.1 External command contract

GPT Actions remain a supported surface at cutover.

The approved contract treatment is:

- preserve existing command names, input semantics, authorization behavior, and general result envelope unless this design explicitly changes them;
- intentionally migrate the `create` result to the canonical Dish identity;
- return required `dish_id` containing the canonical Dish UUID;
- return optional `url` when a frontend base URL is configured;
- return optional `asana_task_gid` only after Asana projection identity is known;
- never place a Dish UUID into the legacy `task_gid` field;
- update the deployed GPT Action schema, agent instructions, client code, and any scripts that consume `create` before general PostgreSQL admission opens;
- treat Asana projection failure as a follow-up projection problem, not failure of canonical Dish creation.

This is an approved, explicit `create` response-contract migration. It is not an accidental compatibility break.

## 4.2 Authorization contract

Stage A must preserve:

- discussion is not authorization;
- findings and proposals do not mutate canonical state;
- proposal approval and application are separate;
- Marco authorization is durable and scoped;
- verification decisions are bound to exact reviewed content;
- mutations remain attributable and reviewable.

## 4.3 Request and retry contract

Every consequential command must have:

- explicit request identity;
- binding to principal, run, command, and payload;
- first-authoritative-outcome replay;
- fail-closed handling of pending or uncertain execution;
- request identity accessible through the CLI and agent surface.

`inspect` must be classified as consequential.

## 4.4 Concurrency contract

Stage A must preserve:

- one writer per governed authority boundary;
- exact lease/claim ownership;
- row-level serialization where required;
- stale worker and stale lease rejection;
- generation and epoch fencing;
- no shadow-origin external effects.

## 4.5 Projection contract

PostgreSQL mutation must atomically create ordered projection intent.

Projection must provide:

- idempotent delivery;
- durable attempt identity;
- observation after external effect;
- applied / not-applied / uncertain settlement;
- drift detection;
- reconciliation.

## 4.6 Dark-launch contract

Dark launch requires:

- truthful production baseline capture;
- exact source and target generation identity;
- complete import;
- fail-open live capture;
- bounded local spool;
- shadow worker without Asana credentials;
- effect kill switch;
- comparison status for gaps, mismatches, lag, and backlog;
- explicit treatment for unsupported commands.

Dark launch does not require cutover approval, rollback burn, writer fencing, first-request reservation, or production authority activation.

---

# 5. Target Minimal Cutover Control Model

Replace the current release platform with a smaller control plane.

## 5.1 Durable admission gate

One database-enforced gate with explicit modes:

- `closed`
- `exact_request`
- `open`

Properties:

- defaults to `closed`;
- shadow replay is a separate non-live path;
- `exact_request` permits only one predefined request identity and payload digest;
- `open` permits general live mutation;
- changes are audited and generation-bound;
- runtime request admission depends only on this gate, not on the full release-candidate subsystem.

## 5.2 Immutable cutover record

One append-only record, or one record with append-only revisions, containing:

- target generation;
- schema version;
- application/protocol artifact digest;
- final source bundle digest;
- final Asana corpus identity;
- import/reconciliation report digest;
- writer-fence report digest;
- backup/restore report digest;
- approval identity and timestamp;
- activation timestamp;
- rollback-burn timestamp;
- first-request identity and result;
- general-admission-open timestamp.

This record is the durable provenance summary. It should reference external reports rather than reproduce every checkpoint as separate tables.

## 5.3 Exact final import and reconciliation

A one-shot production command must:

- fetch the complete final Asana corpus;
- bind the exact SQLite/WAL/sidecar bundle;
- import into a clean PostgreSQL generation;
- independently compare expected and actual membership and content;
- produce one immutable report;
- fail closed on unknown, missing, duplicated, or mismatched entities.

## 5.4 Mechanical writer fence

All confirmed legacy writers must enforce the same fence:

- dish-service;
- `dish`;
- `dish-admin`.

The fence must be testable mechanically. Stopping services alone is insufficient.

## 5.5 Verified backup and restore

Before activation:

- create a PostgreSQL backup;
- restore it into a clean database;
- verify corpus, workflow state, audit, requests, schema, and projection state;
- retain an off-device copy;
- record the result in a referenced report.

Full PITR is optional unless Marco chooses an RPO that requires it.

## 5.6 Controlled first live request

While admission is `exact_request`:

- submit one fixed idempotent request;
- verify command outcome;
- verify replay;
- verify audit;
- verify projection;
- reread Asana;
- then explicitly transition admission to `open`.

## 5.7 Rollback boundary

Before the first PostgreSQL mutation, aborting cutover remains possible.

After the first accepted PostgreSQL mutation:

- SQLite does not regain canonical authority;
- recovery uses PostgreSQL restore, forward repair, or controlled projection repair;
- the irreversible boundary is written to the cutover record.

---

# 6. Current Stage 6–8 Disposition

## 6.1 Keep as core safety

Retain the underlying guarantees, though implementation may be simplified:

- authority generation;
- durable mutation admission;
- mechanical writer fence;
- final source capture;
- exact import and reconciliation;
- backup/restore;
- rollback-burn boundary;
- one controlled first request;
- projection epoch and external-effect switch;
- generation fencing.

## 6.2 Collapse into the minimal model

| Current concept | Proposed destination |
|---|---|
| Release candidates | Cutover-record draft/revision |
| Release evidence items | External reports referenced by digest |
| Rehearsal runs/checkpoints | External rehearsal reports |
| Release evidence bundles | Cutover record attachment set |
| Cutover approvals | Approval fields on cutover record |
| Cutover runs/checkpoints | Cutover record state transitions |
| Final Asana closures | Final import/reconciliation report |
| Closure invalidations | New report revision invalidating prior revision |
| Recertifications | New approval/revision |
| Runtime release attestations | Artifact digest on cutover record |
| Worker readiness evidence | Runtime rehearsal report |
| Candidate manifests | One canonical cutover snapshot digest |
| Approval-manifest bindings | Snapshot digest stored directly on approval |
| Manifest revalidations | New snapshot revision |
| First-admission plans | `exact_request` gate configuration |
| First-request reservations | `exact_request` gate state |
| Authority activation | Cutover record plus admission transition |
| Backup evidence | Backup/restore report |
| Writer-fence observations | Writer-fence report |
| Import native links | Import evidence, only where needed for integrity |

## 6.3 Delete unless a concrete requirement is established

- evidence certifying other evidence;
- typed worker-probe inventory / requirement / evidence / completion layers;
- database-backed rehearsal checkpoint bureaucracy;
- separate recertification nouns where a new signed revision is sufficient;
- separate manifest-of-manifest chains;
- permanent first-admission planning subsystem;
- production-change ledger machinery beyond exact final source identity;
- source-import linkage duplicated by canonical import evidence;
- cutover controls with no production writer or consumer.

## 6.4 Inert or incomplete schema candidates

Subject to final dependency confirmation:

- `causality_edges`;
- `request_uncertainty_resolutions`;
- `applied_migration_events`;
- `source_import_native_links`;
- incomplete generation bootstrap authority;
- unimplemented readiness and audit-fulfillment structures.

A table should not remain solely because tests can construct it.

---

# 7. Command and Contract Consolidation

## 7.1 Canonical command definition

Create one typed command-definition source containing:

- command name;
- principal;
- query versus consequential;
- request-replay requirement;
- run requirement;
- argument schema;
- exposure surfaces;
- Stage A disposition;
- dark-launch treatment;
- expected effects.

Derived artifacts:

- service validation;
- client request-ID behavior;
- CLI metadata;
- OpenAPI;
- target command membership;
- dark-launch coverage checks;
- test inventories.

Not every policy value must be identical. Exposure, migration disposition, and dark-launch treatment remain separate fields in one canonical definition.

## 7.2 Immediate bug fixes

### Inspect

- classify as consequential;
- require request ID;
- store/replay first authoritative outcome;
- update client, OpenAPI, docs, and tests from canonical metadata.

### Apply-proposal

- generate request identity in ordinary client path;
- expose request ID to CLI;
- add end-to-end client/service test.

## 7.3 CLI request identity

For all replay-bound commands:

- accept `--request-id`;
- generate one when omitted;
- display it before dispatch;
- include it in result and failure output;
- allow exact resubmission after transport loss.

---

# 8. PostgreSQL Runtime Completion

## 8.1 Service composition

Use the existing transport where practical. Provide a PostgreSQL implementation of the application interface rather than a second unrelated server stack.

Required:

- HTTP/process entry point;
- authentication and principal mapping;
- configuration;
- health and readiness;
- request validation;
- canonical external result envelope;
- deployment unit or explicit supervised launch mechanism.

## 8.2 GPT Actions contract migration and compatibility

Use the existing transport where practical.

At cutover:

- GPT Actions remain supported;
- non-`create` command names, inputs, authority semantics, and envelope behavior remain stable unless separately approved;
- `create` adopts the explicit canonical response contract:
  - `dish_id`: required canonical Dish UUID;
  - `url`: optional configured frontend URL;
  - `asana_task_gid`: optional Asana projection identity;
- the legacy `task_gid` field must not be repurposed to contain a Dish UUID;
- deployed GPT Action schema, custom instructions, service clients, and scripts must be updated together;
- any temporary compatibility field may contain only its original semantic value.

The shared backend authority/action contract remains the source of legal actions and effects.

## 8.3 Asana projection adapter

Implement a production adapter for:

- task creation where retained;
- content update;
- placement;
- completion;
- authoritative reread;
- external identity binding;
- uncertain-result adjudication.

## 8.4 Reconciliation adapter

Implement a production fetcher/comparator that can:

- read the complete Asana corpus;
- identify the external snapshot/high-water boundary;
- compare expected PostgreSQL projection;
- detect missing, unknown, stale, or mismatched objects;
- produce both dark-launch and final-cutover reports.

## 8.5 Semantic proposals

Retain the semantic-proposal workflow in the form required by the asynchronous Human Review contract.

Required lifecycle:

- create a finding and exact proposed correction;
- queue it for Marco without blocking unrelated work;
- preserve candidate identity, evidence, rationale, questions, answers, and authorization state;
- allow Marco to approve, reject, or request revision later;
- keep approval separate from application;
- allow a later eligible agent to apply the exact approved proposal;
- preserve audit lineage and rollback data.

This is no longer an open product decision. Implementation may simplify names or table structure, but it must preserve these semantics.

## 8.6 Create behavior

PostgreSQL creates the canonical **Dish** first.

The authoritative `create` result is:

```json
{
  "dish_id": "<canonical Dish UUID>",
  "url": "<configured frontend URL or null>",
  "asana_task_gid": "<projected Asana task GID or null>"
}
```

Rules:

- `dish_id` is required and authoritative;
- `url` is convenience metadata and must resolve to the same Dish UUID;
- agents and clients should accept either a Dish UUID or configured Dish URL as an identifier;
- Asana projection occurs independently;
- `asana_task_gid` may be absent or null until projection succeeds;
- Asana projection failure must not roll back or invalidate canonical Dish creation;
- the request remains replay-safe and returns the same canonical Dish UUID;
- no field named `task_gid` may contain the Dish UUID.

The exact response schema, consumer inventory, and migration tests are a Phase 1 exit requirement. Runtime implementation is Phase 3 work.

# 9. Schema and Migration Reset

## 9.1 Preconditions

Do not squash until:

- live `inspect` and `apply-proposal` fixes are merged;
- minimum Stage A contract is approved;
- table disposition is approved;
- disposable rehearsal artifacts are archived;
- no PostgreSQL data requiring preservation is reconfirmed.

## 9.2 Squash strategy

- create one clean initial Stage A migration;
- include only approved retained tables, constraints, triggers, and functions;
- remove historical compatibility logic for revisions 0001–0029;
- remove revision-specific upgrade fixtures that no longer represent a supported deployed database;
- keep a static archive of the old migration history outside the active migration chain if useful for provenance;
- rebuild the disposable database from scratch.

## 9.3 SQLite-target compatibility

Legacy SQLite remains fully supported.

For `dish_pg` tests:

- pure planner/service logic may use SQLite;
- PostgreSQL DDL and migration semantics use PGlite;
- locking, triggers, concurrency, and server behavior use native PostgreSQL;
- stop adding SQLite emulation of PostgreSQL-specific behavior;
- remove duplicated SQLite trigger/migration branches where native/PGlite coverage exists.

## 9.4 Validation after squash

Required:

- ORM/schema agreement;
- clean upgrade to head;
- import from an actual legacy bundle;
- dark-launch capture/replay tests;
- PGlite migration suite;
- native concurrency/trigger suite;
- service and adapter integration;
- full command contract tests;
- complete test suite;
- comparison of retained Stage A behavioral invariants.

---

# 10. Test and Governance Simplification

## 10.1 Preserve

- authorization tests;
- request replay/idempotency;
- lease and concurrency;
- migration integrity;
- external-effect recovery;
- dark-launch separation;
- native PostgreSQL certification;
- backup/restore;
- writer fencing;
- producer-equivalence checks for fabricated test states;
- flake expiry and quarantine discipline.

## 10.2 Simplify now

- derive `PORTED_MUTATION_COMMANDS`;
- derive migration head from Alembic;
- derive command-name universes;
- co-locate workflow-builder metadata with helpers;
- make line-count ceilings advisory;
- make exact duplicate-body checks advisory;
- stop hashing every test file in the Stage A product baseline;
- derive path-ownership defaults and self-ownership;
- retain only manual exceptions and high-risk traits.

## 10.3 Baseline redesign

The Stage A baseline should record:

- governing production artifact digests;
- approved command disposition;
- approved schema identity;
- behavioral invariant categories;
- executed test/report digests.

It should not freeze:

- test filenames;
- comments;
- helper layout;
- arbitrary source organization.

---

# 11. Execution Sequence and Gates

## Phase 0 — Immediate live fixes

1. Fix `inspect`.
2. Fix `apply-proposal`.
3. Add end-to-end tests.
4. Confirm current live system remains green.

**Gate:** no further structural work until these fixes are merged.

## Phase 1 — Scope freeze and contract approval

1. Freeze new certification/evidence growth.
2. Write and approve the minimum Stage A behavioral contract.
3. Approve the target schema and subsystem disposition.
4. Specify the canonical Dish `create` response and identify every consumer of the old Asana-oriented response.
5. Specify Human Review, safe reclaim, approval evidence, rollback, request replay, projection, and cutover transitions.
6. Complete hidden-consumer discovery and the master issue disposition matrix.
7. Verify planning challenge/override cannot authorize mutation.

**Gate:** no schema reduction until the contract, target schema, consumer migration plan, and issue dispositions are approved.

## Phase 2 — Reduce control plane and schema

1. Implement minimal admission gate.
2. Implement immutable cutover record model.
3. Remove/collapse approved Stage 6–8 concepts.
4. Remove inert schema.
5. Consolidate command contract.
6. Squash migrations.
7. Rebuild disposable PostgreSQL.

**Gate:** clean schema, contract, and core tests pass.

## Phase 3 — Complete real runtime

1. Build PostgreSQL service composition.
2. Implement the approved GPT Actions contract migration and update all known `create` consumers.
3. Build production Asana projection adapter.
4. Build reconciliation adapter.
5. Port semantic proposals and asynchronous Human Review semantics.
6. Implement canonical Dish creation returning `dish_id` and optional configured `url`.
7. Implement truthful production baseline capture.

**Gate:** complete end-to-end rehearsal against disposable real PostgreSQL and isolated Asana targets.

## Phase 4 — Dark launch

1. Import production baseline.
2. Enable fail-open capture.
3. Run shadow worker.
4. Monitor backlog, gaps, lag, and mismatches.
5. Exercise all retained commands.
6. resolve discrepancies.
7. collect sufficient representative evidence.

**Gate:** approved dark-launch exit criteria met.

## Phase 5 — Cutover

1. Verify backup and independent restore.
2. Enter maintenance window.
3. resolve in-flight operations.
4. fence all legacy writers.
5. capture final source bundle and Asana corpus.
6. import into clean target generation.
7. run exact final reconciliation.
8. approve cutover record.
9. activate PostgreSQL with admission `exact_request`.
10. execute and verify first request.
11. open general admission.
12. monitor closely.

## Phase 6 — Stabilization and deletion

After an agreed stability and retention period:

- remove shadow capture and spool;
- remove import bootstrap machinery;
- remove obsolete cutover tooling;
- remove legacy mutation paths;
- archive final cutover evidence;
- begin Stage B data-model and usability redesign.

---

# 12. Existing Ops and Handoff Issue Disposition

Every item from the original handoff and `ops-issues.md` must be entered into a disposition matrix with:

- exact claim;
- current evidence;
- status:
  - confirmed blocker;
  - confirmed but later;
  - stale/fixed;
  - valid concern, excessive implementation;
  - unsupported;
- destination in this design;
- owner;
- validation test or report.

No issue is silently dropped.

Initial disposition examples:

| Issue | Disposition |
|---|---|
| Correct migration-head helper | Confirm and derive from one source |
| Clean migration rehearsal | Rerun after squash |
| Native PostgreSQL certification | Retain, rerun after squash |
| Backup/restore rehearsal | Required before cutover |
| Full PITR matrix | Product decision based on RPO |
| First-live-request rehearsal | Retain concept; repoint to minimal gate |
| Post-request reconciliation | Retain concept; implement real adapter |
| Production-shaped rehearsal | Required after real runtime exists |
| Stage A baseline evidence | Redesign to avoid test-layout hashing |
| Legacy-writer inventory | Retain as part of fence report |
| Stage 6 runbook command checks | Supersede after control-plane replacement |
| Typed readiness writers missing | Remove typed subsystem or implement one report |
| Import evidence not produced | Fix canonical import path |
| Invocation audit not fulfilled | Integrate or remove obligation |
| Stale lock/kill-switch/final-gate claims | Mark fixed/disproved |

---

# 13. Expected Reduction

Exact numbers require the final disposition pass, but the likely reduction sources are:

- most of the 27 release/cutover/support tables;
- much of the Stage 6 release/certification Python;
- large rehearsal scripts;
- 29-revision migration history;
- revision-specific fixtures;
- PostgreSQL SQLite-compatibility DDL;
- duplicated command registries;
- test-governance bookkeeping;
- inert schema and repositories.

The objective is not a cosmetic line target. The objective is to remove entire concepts and ownership surfaces. A permanent post-cutover system near 20,000–25,000 lines remains plausible because legacy, import, shadow, and cutover scaffolding can eventually disappear.

---

# 14. Risks and Controls

## Risk: removing a real safety guarantee

Control:

- every deletion maps to a retained invariant;
- native tests remain for concurrency and database semantics;
- no removal based only on line count.

## Risk: migration squash hides behavior regressions

Control:

- no data to preserve;
- archive old history;
- rebuild clean;
- rerun import, PGlite, native, contract, and full suites;
- compare behavioral invariants.

## Risk: command consolidation becomes another giant abstraction

Control:

- canonicalize metadata only;
- keep command handlers explicit;
- derive artifacts mechanically;
- do not create a generic workflow DSL.

## Risk: cutover model becomes too manual

Control:

- database-enforced admission and writer fence remain;
- external reports are immutable and digest-bound;
- manual approval is explicit;
- first request remains mechanically bounded.

## Risk: unfinished old ops work is wasted

Control:

- preserve reports and useful scripts;
- reuse low-level checks;
- repoint them at the minimal model;
- rerun only evidence invalidated by structural changes.

---

# 15. Decisions Required From Marco

The product decisions formerly listed here are resolved in Addendum B.

Semantic proposals, asynchronous Human Review, durable review state, and safe reclaim are required at initial cutover. Remaining items in this document are implementation verification tasks, not open product decisions.

# 16. Acceptance Criteria

This recovery plan is successful when:

- the two live bugs are fixed;
- one canonical command contract prevents registry drift;
- the Stage A contract is explicit and approved;
- the schema contains no required evidence without a production writer;
- PostgreSQL migrations are reset to a clean supported baseline;
- the PostgreSQL authority service is deployable;
- real Asana projection and reconciliation work;
- retained commands have approved parity or retirement treatment;
- production dark launch runs against the simplified system;
- cutover can be completed through a bounded, comprehensible procedure;
- transition machinery has a documented deletion point;
- future changes no longer require 1,500–2,000-line diffs to add one safety rule.

---

# 17. Review Questions for Claude

Claude should challenge this proposal on:

1. Which retained safety guarantees are missing?
2. Which proposed deletions have hidden production consumers?
3. Whether the minimal admission gate preserves all irreversible-boundary protections.
4. Whether one cutover record can replace the current evidence graph without weakening auditability.
5. Whether migration squashing invalidates any required deployed-state rehearsal.
6. Whether the approved GPT Actions `create` migration identifies and updates every existing consumer.
7. Whether canonical Dish creation remains independent from asynchronous Asana projection.
8. Whether semantic proposals and asynchronous Human Review are fully ported without hidden SQLite authority.
9. Which original handoff or `ops-issues.md` item has no disposition here.
10. Which proposed sequence would cause avoidable rework.


---

# Addendum A — Required Contract Additions

## Committed success remains success

Once a mutation commits successfully, later refresh, audit, projection-status, presentation, or secondary-observation failures must not turn that committed success into retry advice. Follow-up failures must be reported separately, and retries must remain bound to the original request identity.

## Recovery remains specific

There is no generic “unblock.” Lease recovery, safe reclaim, ambiguous-effect resolution, destination repair, discard, Evidence or Human Review resolution, abandonment/succession, and backup/restore each require narrow preconditions.

A changed chat session alone is not evidence of recovery risk.

## Human administration remains a product boundary

`dish-admin` remains Marco’s distinct operator interface. It must present human-readable actions and consequences, preserve explicit operator authority, and consume the same backend authority/action contract without becoming merely another generic API client.

## Asynchronous review and cross-run continuation

Initial cutover must support:

- queueing items that require Marco’s decision;
- agents continuing to other work;
- durable findings, evidence, questions, answers, proposals, authorization state, unresolved items, and candidate identity;
- later approval or rejection by Marco;
- later application by any eligible agent;
- no dependency on the original agent run;
- safe reclaim where no unresolved effect exists;
- formal abandonment/succession only where recovery risk exists.

## Findings, proposals, authorization, and mutation remain distinct

The system must represent separately:

1. finding;
2. evidence and confidence;
3. proposed correction;
4. authorization state;
5. applied mutation.

Clarification, urgency, silence, disagreement, or continued discussion are not authorization.

## Advisory concerns and execution blocks remain distinct

Agents may classify a concern as severe and warn clearly, but only database mechanical-integrity conditions may block execution. Substantive product or safety concerns remain warnings under Marco’s authority.

## Mutations remain reviewable and reversible

Before mutation, retain the prior canonical version, exact approved diff, approving authority, and applied result. Provide a bounded rollback path with audit.

## Safe reclaim and abandonment

Safe reclaim is allowed only when:

- the prior owner is objectively inactive;
- no execution is pending;
- no external effect is unresolved;
- no outcome is uncertain;
- no partially executed action exists;
- replay safety is established.

Reclaim must atomically fence the old owner, reject late writes, and record old owner, new owner, reason, and timestamp.

Formal abandonment and succession remain for genuine recovery risk.

## Cutover ordering

The database must mechanically prevent:

- approval before a complete cutover snapshot exists;
- activation before final source closure, writer fence, reconciliation, and backup verification;
- first-request admission before activation;
- general admission before first-request verification;
- rollback to legacy authority after the irreversible PostgreSQL mutation boundary.

Immediately before the controlled first PostgreSQL write, revalidate the final source closure, writer fence, target generation, reconciliation result, backup state, and approved cutover snapshot.

If that first write fails after authority switches, remain in maintenance mode, determine whether it committed, repair PostgreSQL, and retry the same request ID.

---

# Addendum B — Final Marco Product Decisions

1. **Asynchronous human review is required at initial cutover.**
   Agents must be able to queue work needing Marco’s decision, continue elsewhere, and allow a later eligible agent to resume and apply the exact approved change.

2. **GPT Actions remain supported through initial cutover.**
   Existing non-`create` command semantics and general envelope remain stable unless separately approved. The deployed contract must be updated atomically for the approved `create` response migration.

3. **`create` returns the canonical Dish identity.**
   It must return required `dish_id`, optional configured `url`, and optional `asana_task_gid`. Asana projection is secondary. The legacy `task_gid` field must never be repurposed to contain a Dish UUID.

4. **Verified backup and restore are required before cutover.**
   A PostgreSQL backup must be restored into a clean database and verified. An off-device copy is required. Full PITR is not required initially.

5. **Cutover may use a planned write-free maintenance window.**

6. **One controlled first PostgreSQL write is required inside the maintenance window.**
   Normal writes remain blocked until the request, replay, audit, projection, and reread all succeed.

7. **Keep one concise immutable cutover record, normal Git history, and backup artifacts.**
   Do not build a permanent evidence bureaucracy.

8. **Asana remains a read-only projection/interface after cutover until Marco decides PostgreSQL is sufficiently trusted.**

9. **Full cross-run redesign is not a cutover blocker.**
   The asynchronous review path, durable review state, and safe reclaim are required.

10. **Request IDs remain reserved permanently.**
    Detailed payloads or bulky diagnostic data may be archived separately, but a used request ID must never become reusable.

11. **Transition records have no fixed deletion date.**
    Keep them until Marco decides they are no longer needed and track post-cutover cleanup in a dedicated document.

12. **If the controlled first PostgreSQL write fails after authority switches, remain in maintenance mode.**
    Determine whether it committed, repair PostgreSQL, and retry the same request ID. Do not casually restore legacy authority.

13. **Dark launch ends based on successful evidence, not elapsed time.**
    Known errors must be resolved; retained workflows must pass scripted manual battle-testing against a test PostgreSQL database and test Asana project; reconciliation must succeed; no unexplained gaps or mismatches may remain; Marco must be satisfied.

14. **Safe reclaim is the normal path after lease expiry or explicit lease termination.**
    A second abandonment action is not required unless the database shows real recovery risk.

15. **Safe reclaim requires objective inactivity plus safe recorded state.**
    Reclaim is allowed when the lease has expired or was explicitly released/terminated and there is no pending execution, unresolved external effect, uncertain outcome, incomplete settlement, or partially applied mutation.

16. **Formal abandonment and succession are reserved for genuine recovery risk.**

17. **Late writes from the previous owner must fail after reclaim.**
    Reclaim atomically fences the old owner and records old owner, new owner, reason, and time.

18. **Discussion is not authorization.**
    Clarification, urgency, silence, disagreement, continued conversation, and acceptance of a finding are not approval of a proposed correction.

19. **Findings, proposed corrections, approval, and applied mutation remain distinct.**

20. **A brief free-text rationale is required for an approved agent-made change at cutover.**
    The format remains intentionally experimental and should be refined through real use.

21. **Whole-version rollback is required before initial cutover.**
    PostgreSQL must retain the prior canonical version, exact applied diff, approving authority, brief rationale, and resulting version.

22. **Rollback is admin-only and requires Marco’s explicit confirmation.**
    `dish-admin` must show the exact version being restored and what current changes will be undone. The rollback itself creates a new audit entry.

23. **“Proceed now, reconcile later” is not an initial-cutover requirement.**
    Record it as a post-cutover workflow requirement.

24. **Light verification is a post-cutover requirement, not a cutover blocker.**
    It should catch obvious safety issues, major contradictions, missing critical information, and execution blockers without exhaustive challenge, repeated source hunting, minor formatting disputes, or optional optimization.

25. **Dish has no hard blocks controlled by agents.**
    Agents may warn, explain, and record concerns, but they may not prevent Marco from proceeding.

26. **One explicit Marco override ends further challenge on that specific concern.**
    The concern remains recorded. Agents may reopen it only when materially new evidence appears or Marco explicitly requests renewed verification.

27. **Marco may move a Dish out of `needs verification`.**
    This must be easy in `dish-admin`.

28. **Marco may also override through an agent.**
    When Marco says he wants to cook with the current version now, the agent gives one concise warning, records the override, and proceeds.

29. **Generic safety guidance does not outweigh Marco’s explicit judgment about his own equipment, environment, or process.**

30. **Marco is the final authority by default.**
    Because he is the sole user, administrator, and developer, agents must treat his explicit override as authoritative, record it, and continue. They must not repeatedly argue, refuse, or abandon the workflow over generic guidance.

31. **One shared backend authority/action contract is required before cutover.**
    CLI, agents, GPT Actions, and `dish-admin` must consume the same source for legal actions, required authority, rationale, exact effect, warnings, continuation behavior, and next action.

32. **A frontend redesign is not required before cutover.**
    The shared backend contract is required; presentation improvements may continue separately.

## Deferred post-cutover requirements

- “Proceed now, reconcile later.”
- Light verification mode.
- More structured rationale capture after observing real use.
- Broader workflow simplification beyond the required asynchronous review and reclaim behavior.
- Field-level rollback.
- Full cross-surface presentation redesign.
- Planning challenge/override redesign unless verification shows it can grant mutation authority.

## Final verification status

The planning challenge/override verification is a mandatory Phase 1 gate. Phase 2 cannot begin until it passes.

## Implementation clarifications

- Safe reclaim creates a new mechanically linked operation. The inactive operation remains immutable; the new agent restarts the step; unapproved partial agent work may be discarded; applicable Marco discussion and still-valid approvals remain available; fencing and audit lineage must be unambiguous.
- “No pending execution” must be defined by mechanically checkable database state, not by an agent assertion.
- A durable Marco approval record must distinguish Marco’s exact words from an agent’s interpretation and bind the approval to an exact proposal and candidate version.
- An agent may record an override only after an explicit instruction from Marco. Urgency, clarification, silence, or continued discussion are insufficient.
- Restoring an older version must create a new canonical version. History must never be deleted or rewritten.
- The shared authority/action contract centralizes legal actions, required authority, effects, warnings, and next actions. It must not become a generic workflow DSL or move mutation logic into metadata.
- Planning-start permission must never be accepted as mutation authorization.

**Agents cannot impose substantive product or safety blocks on Marco. The database may and must still impose mechanical integrity and recovery fences for stale ownership, duplicate request identities, uncertain execution, incomplete settlement, unresolved external effects, replay safety, and concurrency control.**


## Execution-time verification still outstanding

The following are not assumed complete by this proposal:

- discovery of hidden consumers of tables proposed for deletion;
- the full disposition matrix for every `ops-issues.md` and handoff item.

These must be completed during Phase 0/1 before destructive schema work or migration squashing proceeds.
